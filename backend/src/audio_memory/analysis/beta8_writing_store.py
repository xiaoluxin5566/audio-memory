from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from uuid import uuid4


def digest(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WritingStopped(ValueError):
    pass


@dataclass(frozen=True)
class WritingLimits:
    allow_paid: bool = False
    max_requests: int = 0
    max_input_tokens: int = 160_000
    max_total_input_tokens: int = 2_000_000
    max_output_tokens: int = 24_000
    max_search_output_tokens: int = 24_000
    max_total_output_tokens: int = 600_000
    core_budget_bytes: int = 80_000
    max_research_tasks: int = 6
    source_limit: int = 5

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "allow_paid":
                if not isinstance(value, bool):
                    raise ValueError("allow_paid must be boolean")
            elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"Invalid budget: {name}")
        if self.core_budget_bytes < 1 or self.max_output_tokens < 1 or self.max_search_output_tokens < 1:
            raise ValueError("Input and output capacity must be positive")

    def output_tokens(self, stage: str) -> int:
        return self.max_search_output_tokens if stage == "P3" else self.max_output_tokens


class WritingStore:
    def __init__(self, root: Path, identity: dict, *, write_boundary=None):
        self.root = Path(root)
        self.boundary = write_boundary
        if self.boundary:
            self._ensure_directory(self.root)
        else:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("stages", "requests", "cards", "sources"):
            if self.boundary:
                self._ensure_directory(self.root / name)
            else:
                (self.root / name).mkdir(exist_ok=True, mode=0o700)
        existing = self.root / "identity.json"
        if self.exists("identity.json") and self.read("identity.json") != identity:
            raise WritingStopped("run identity changed; use a separate output directory")
        if not self.exists("identity.json"):
            self.write("identity.json", identity)
        self.identity = identity

    def _ensure_directory(self, path: Path) -> None:
        directory_fd = self.boundary.open_directory(path, create=True)
        if directory_fd is None:
            raise WritingStopped(f"Cannot create artifact directory: {path}")
        os.close(directory_fd)

    def exists(self, name: str) -> bool:
        path = self.root / name
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise WritingStopped("Artifact check escaped output directory")
        if self.boundary:
            return self.boundary.regular_file_exists(path)
        return path.is_file()

    def read(self, name: str):
        path = self.root / name
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise WritingStopped("Artifact read escaped output directory")
        return json.loads(self.boundary.read_text(path) if self.boundary else path.read_text())

    def write(self, name: str, value: object) -> None:
        self.write_text(name, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")

    def write_text(self, name: str, value: str) -> None:
        target = self.root / name
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Artifact path escaped run directory")
        if self.boundary:
            self._ensure_directory(target.parent)
            self.boundary.write_text_atomic(target, value)
            directory_fd = self.boundary.open_directory(target.parent, create=False)
            if directory_fd is None:
                raise WritingStopped("Artifact directory disappeared during write")
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = target.with_name(target.name + "." + uuid4().hex + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @contextmanager
    def locked(self):
        fd = self.boundary.open_regular_file(self.root / "run.lock", os.O_RDWR | os.O_CREAT) if self.boundary else os.open(self.root / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WritingStopped("run already active") from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _safe_key(key: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError("Invalid artifact key")
        return key

    def cached_stage(self, key: str, *, revalidate=None) -> dict | None:
        name = "stages/" + self._safe_key(key) + ".json"
        if not self.exists(name):
            return None
        record = self.read(name)
        if record["status"] == "failed_before_dispatch":
            self._validate_finished_stage(record)
            return None
        if record["status"] == "invalid" and revalidate is not None:
            # Only a fully received response reaches `invalid`. Retain the original
            # failure and rerun current deterministic validation, never the model.
            self._validate_finished_stage(record)
            # Revalidation always starts from the original provider response.
            # Historical local corrections are archival evidence, not model output.
            value = json.loads(record["raw"])
            if not isinstance(value, dict):
                raise WritingStopped("saved stage output is not an object")
            value = revalidate(value)
            receipt = {"original_stage_sha256": digest(record), "result": value,
                       "result_sha256": digest(value), "model_requests": 0}
            receipt_name = "raw-revalidations/" + self._safe_key(key) + ".json"
            if self.exists(receipt_name) and self.read(receipt_name) != receipt:
                raise WritingStopped("raw revalidation differs from saved receipt")
            if not self.exists(receipt_name):
                self.write(receipt_name, receipt)
            return value
        if record["status"] != "complete":
            raise WritingStopped("unresolved prior stage; automatic resend disabled")
        self._validate_finished_stage(record)
        return record["result"]

    def begin_stage(self, key: str, inputs: dict) -> None:
        name = "stages/" + self._safe_key(key) + ".json"
        if self.exists(name):
            previous = self.read(name)
            if previous.get("status") != "failed_before_dispatch":
                raise WritingStopped("unresolved or existing stage cannot be overwritten")
            self._validate_finished_stage(previous)
            archive = (
                "stages/" + self._safe_key(key)
                + "-failed-before-dispatch-" + uuid4().hex + ".json"
            )
            self.write(archive, previous)
        self.write(name, {"status": "started", "started_at": utc_now(), "inputs": inputs, "input_sha256": digest(inputs)})

    @staticmethod
    def _validate_finished_stage(record: dict) -> None:
        try:
            valid = (
                digest(record["result"]) == record["result_sha256"]
                and digest(record["raw"]) == record["raw_sha256"]
                and digest(record["inputs"]) == record["input_sha256"]
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise WritingStopped("corrupt cached stage")

    def finish_stage(self, key: str, raw: str, result: dict, *, status: str = "complete") -> None:
        name = "stages/" + self._safe_key(key) + ".json"
        record = self.read(name)
        if record["status"] != "started":
            raise WritingStopped("stage result is immutable")
        record.update(status=status, raw=raw, raw_sha256=digest(raw), result=result, result_sha256=digest(result), finished_at=utc_now())
        self.write(name, record)

    def requests(self) -> list[dict]:
        paths = self.boundary.list_regular_files(self.root / "requests", suffix=".json") if self.boundary else sorted((self.root / "requests").glob("*.json"))
        return [self.read("requests/" + path.name) for path in paths]

    def _request_entries(self) -> list[tuple[str, dict]]:
        paths = self.boundary.list_regular_files(self.root / "requests", suffix=".json") if self.boundary else sorted((self.root / "requests").glob("*.json"))
        return [(path.stem, self.read("requests/" + path.name)) for path in paths]

    def _has_request_resolution(self, token: str, row: dict) -> bool:
        name = f"request-resolutions/{self._safe_key(token)}.json"
        if not self.exists(name):
            return False
        receipt = self.read(name)
        if isinstance(receipt, dict) and receipt.get("disposition") == "explicit_retry_unresolved_report":
            required = {
                "disposition", "stage", "request_sha256",
                "retry_generation", "created_at",
            }
            if (
                set(receipt) != required
                or receipt["stage"] not in {"P1", "P2", "P4", "P5"}
                or row.get("stage") != receipt["stage"]
                or digest(row) != receipt["request_sha256"]
                or isinstance(receipt["retry_generation"], bool)
                or not isinstance(receipt["retry_generation"], int)
                or receipt["retry_generation"] < 1
            ):
                raise WritingStopped("corrupt explicit retry receipt")
            return True
        required = {
            "disposition", "stage", "task_key", "request_sha256",
            "failure_artifact", "failure_sha256", "created_at",
        }
        if (
            not isinstance(receipt, dict)
            or set(receipt) != required
            or receipt["disposition"] != "no_retry_failed_research"
            or receipt["stage"] != "P3"
            or row.get("stage") != "P3"
            or digest(row) != receipt["request_sha256"]
            or not isinstance(receipt["failure_artifact"], str)
            or not self.exists(receipt["failure_artifact"])
            or digest(self.read(receipt["failure_artifact"])) != receipt["failure_sha256"]
        ):
            raise WritingStopped("corrupt failed-search continuation receipt")
        return True

    def prepare_explicit_unresolved_retry(
        self, *, key: str, stage: str, retry_generation: int,
    ) -> str:
        """Authorize one resend after an explicit retry-analysis action."""
        if (
            stage not in {"P1", "P2", "P4", "P5"}
            or isinstance(retry_generation, bool)
            or not isinstance(retry_generation, int)
            or retry_generation < 1
        ):
            raise WritingStopped("invalid explicit retry authorization")
        previous_key = key if retry_generation == 1 else f"{key}-retry-{retry_generation - 1}"
        previous_name = f"stages/{self._safe_key(previous_key)}.json"
        if not self.exists(previous_name):
            return key
        previous = self.read(previous_name)
        self._validate_finished_stage(previous)
        if previous["status"] != "unresolved":
            return key
        pending = [
            (token, row) for token, row in self._request_entries()
            if row.get("status") == "dispatching"
            and row.get("stage") == stage
            and not self._has_request_resolution(token, row)
        ]
        if len(pending) != 1:
            raise WritingStopped("explicit retry requires exactly one unresolved stage request")
        token, row = pending[0]
        receipt_name = f"request-resolutions/{self._safe_key(token)}.json"
        self.write(receipt_name, {
            "disposition": "explicit_retry_unresolved_report",
            "stage": stage,
            "request_sha256": digest(row),
            "retry_generation": retry_generation,
            "created_at": utc_now(),
        })
        return f"{key}-retry-{retry_generation}"

    def acknowledge_failed_search_request(
        self, *, task_key: str, failure_artifact: str,
    ) -> None:
        failure = self.read(failure_artifact)
        payload = failure.get("payload") if isinstance(failure, dict) else None
        if (
            not isinstance(task_key, str) or not task_key
            or not isinstance(payload, dict)
            or payload.get("task_key") != task_key
            or payload.get("status") != "failed"
        ):
            raise WritingStopped("invalid failed research artifact")
        pending = [
            (token, row) for token, row in self._request_entries()
            if row.get("status") == "dispatching" and row.get("stage") == "P3"
        ]
        if not pending:
            return
        if len(pending) != 1:
            raise WritingStopped("ambiguous failed search requests")
        token, row = pending[0]
        name = f"request-resolutions/{self._safe_key(token)}.json"
        if self.exists(name):
            self._has_request_resolution(token, row)
            return
        self.write(name, {
            "disposition": "no_retry_failed_research",
            "stage": "P3",
            "task_key": task_key,
            "request_sha256": digest(row),
            "failure_artifact": failure_artifact,
            "failure_sha256": digest(failure),
            "created_at": utc_now(),
        })

    def before_request(self, stage: str, payload: dict, limits: WritingLimits) -> str:
        if not limits.allow_paid:
            raise WritingStopped("paid execution requires explicit authorization")
        entries = self._request_entries()
        rows = [row for _, row in entries]
        if any(
            row["status"] == "dispatching"
            and not self._has_request_resolution(token, row)
            for token, row in entries
        ):
            raise WritingStopped("unresolved prior request; automatic resend disabled")
        if len(rows) >= limits.max_requests:
            raise WritingStopped("request budget exhausted")
        # UTF-8 bytes are a conservative capacity bound, not measured provider tokens.
        input_bound = len(json.dumps(payload, ensure_ascii=False).encode())
        if input_bound > limits.max_input_tokens:
            raise WritingStopped("input capacity budget exceeded; no truncation")
        if sum(row["input_token_upper_bound"] for row in rows) + input_bound > limits.max_total_input_tokens:
            raise WritingStopped("total input budget exhausted")
        output_reservation = limits.output_tokens(stage)
        if sum(row["output_token_reservation"] for row in rows) + output_reservation > limits.max_total_output_tokens:
            raise WritingStopped("total output budget exhausted")
        token = f"{len(rows) + 1:05d}-{uuid4().hex}"
        self.write(f"requests/{token}.json", {"stage": stage, "status": "dispatching", "started_at": utc_now(), "payload": payload, "input_token_upper_bound": input_bound, "output_token_reservation": output_reservation})
        return token

    def record_progress(self, token: str, progress: dict) -> None:
        name = f"requests/{self._safe_key(token)}.json"
        row = self.read(name)
        if row["status"] != "dispatching":
            raise WritingStopped("request response is immutable")
        row.update(partial_response=progress, progress_at=utc_now())
        self.write(name, row)

    def after_response(self, token: str, body: dict) -> None:
        name = f"requests/{self._safe_key(token)}.json"
        row = self.read(name)
        if row["status"] != "dispatching":
            raise WritingStopped("request response is immutable")
        row.update(status="response_received", response=body, finished_at=utc_now())
        self.write(name, row)

    def record_error(self, token: str, error: BaseException) -> None:
        name = f"requests/{self._safe_key(token)}.json"
        row = self.read(name)
        if row["status"] in {"response_received", "response_error"}:
            return
        if row["status"] != "dispatching":
            raise WritingStopped("request response is immutable")
        if getattr(error, "transport_diagnostics", None):
            row.update(transport_diagnostics=error.transport_diagnostics,
                       transport_error_type=getattr(error, "transport_error_type", None),
                       error_code=getattr(error, "code", "cancelled"), failed_at=utc_now())
            self.write(name, row)
        if getattr(error, "response_incomplete", False):
            return
        http_status = getattr(error, "http_status_code", None)
        raw_response = getattr(error, "partial_response", None)
        if (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or not 100 <= http_status <= 599
            or not isinstance(raw_response, str)
        ):
            return
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", code):
            code = "provider_error"
        row.update(
            status="response_error",
            error_code=code,
            http_status=http_status,
            raw_response=raw_response,
            finished_at=utc_now(),
        )
        self.write(name, row)

    def metrics(self) -> dict:
        rows = self.requests()
        def total(*names, stage=None, exclude_stage=None):
            values = []
            for row in rows:
                if stage is not None and row.get("stage") != stage:
                    continue
                if exclude_stage is not None and row.get("stage") == exclude_stage:
                    continue
                response = row.get("response")
                usage = response.get("usage") if isinstance(response, dict) else None
                if not isinstance(usage, dict):
                    usage = {}
                value = next((usage[name] for name in names if isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool) and usage[name] >= 0), None)
                if value is None:
                    return None
                values.append(value)
            return sum(values)
        tool_calls = 0
        for row in rows:
            response = row.get("response")
            if (
                row.get("stage") == "P3"
                and isinstance(response, dict)
                and isinstance(response.get("search_results"), list)
            ):
                tool_calls += 1
                continue
            choices = response.get("choices") if isinstance(response, dict) else None
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict):
                    continue
                calls = message.get("tool_calls")
                if isinstance(calls, list):
                    tool_calls += sum(isinstance(call, dict) for call in calls)
        return {
            "model_request_count": len(rows),
            "report_request_count": sum(row["stage"] != "P3" for row in rows),
            "search_model_request_count": sum(row["stage"] == "P3" for row in rows),
            "search_tool_call_count": tool_calls,
            "unresolved_request_count": sum(row["status"] == "dispatching" for row in rows),
            "input_tokens": total(
                "prompt_tokens", "input_tokens", exclude_stage="P3"
            ),
            "output_tokens": total(
                "completion_tokens", "output_tokens", exclude_stage="P3"
            ),
            "search_input_tokens": total(
                "prompt_tokens", "input_tokens", stage="P3"
            ),
            "search_output_tokens": total(
                "completion_tokens", "output_tokens", stage="P3"
            ),
            "scoring_model_request_count": 0,
            "counting_note": "Search Pro calls are counted as search tool calls and do not report model tokens. Dispatch intents without a received response retain uncertain billing; upper bounds are not measured tokens.",
        }
