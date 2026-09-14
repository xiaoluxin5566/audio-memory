from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from audio_memory.analysis.beta8_writing_store import (
    WritingLimits,
    WritingStopped,
    WritingStore,
)
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.config import PinnedDevelopmentRoot, RuntimeConfig


def test_metrics_preserve_counts_for_malformed_parsed_envelopes(tmp_path: Path) -> None:
    store = WritingStore(tmp_path, {"run": "malformed"})
    limits = WritingLimits(allow_paid=True, max_requests=2)
    first = store.before_request("P1", {"messages": []}, limits)
    store.after_response(first, ["valid JSON", "invalid provider envelope"])
    second = store.before_request("P3", {"messages": []}, limits)
    store.after_response(second, {
        "usage": "invalid usage",
        "choices": [
            "invalid choice",
            {"message": "invalid message"},
            {"message": {"tool_calls": "invalid tool calls"}},
        ],
    })

    metrics = store.metrics()

    assert metrics["model_request_count"] == 2
    assert metrics["report_request_count"] == 1
    assert metrics["search_model_request_count"] == 1
    assert metrics["search_tool_call_count"] == 0
    assert metrics["unresolved_request_count"] == 0
    assert metrics["input_tokens"] is None
    assert metrics["output_tokens"] is None


def test_atomic_write_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fsynced_types: list[int] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        fsynced_types.append(os.fstat(fd).st_mode)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    store = WritingStore(tmp_path, {"run": "durable"})
    store.write("result.json", {"status": "complete"})

    assert any(stat.S_ISREG(mode) for mode in fsynced_types)
    assert any(stat.S_ISDIR(mode) for mode in fsynced_types)
    assert json.loads((tmp_path / "result.json").read_text()) == {
        "status": "complete"
    }


def test_boundary_owns_exists_checks_and_directory_fsync(tmp_path: Path) -> None:
    class Boundary:
        def __init__(self) -> None:
            self.exists_calls: list[Path] = []
            self.open_directory_calls: list[Path] = []

        def create_directory(self, path: Path) -> None:
            path.mkdir(parents=True, exist_ok=True)

        def write_text_atomic(self, path: Path, content: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

        def read_text(self, path: Path) -> str:
            return path.read_text()

        def regular_file_exists(self, path: Path) -> bool:
            self.exists_calls.append(path)
            return path.is_file()

        def open_directory(self, path: Path, *, create: bool) -> int | None:
            self.open_directory_calls.append(path)
            if create:
                path.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                return None
            return os.open(path, os.O_RDONLY)

        def open_regular_file(self, path: Path, flags: int) -> int:
            return os.open(path, flags, 0o600)

        def list_regular_files(
            self, path: Path, *, prefix: str = "", suffix: str = ""
        ) -> tuple[Path, ...]:
            return tuple(
                item for item in sorted(path.iterdir())
                if item.name.startswith(prefix) and item.name.endswith(suffix)
            )

    boundary = Boundary()
    store = WritingStore(tmp_path, {"run": "boundary"}, write_boundary=boundary)

    assert store.exists("identity.json") is True
    assert boundary.exists_calls[-1] == tmp_path / "identity.json"
    assert tmp_path in boundary.open_directory_calls


def test_real_development_boundary_creates_nested_store_and_reopens_it(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    data_root = project_root / ".runtime/dev"
    config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=project_root,
        environ={
            "AUDIO_MEMORY_PROFILE": "development",
            "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            "AUDIO_MEMORY_MODEL_ROOT": str(data_root / "models"),
        },
    )
    boundary = PinnedDevelopmentRoot.open(config, create=True)
    assert boundary is not None
    root = config.paths.runtime / "beta8-p1-p5-v1/version/artifacts"
    try:
        first = WritingStore(root, {"run": "real-boundary"}, write_boundary=boundary)
        first.write("result.json", {"status": "started"})

        reopened = WritingStore(
            root, {"run": "real-boundary"}, write_boundary=boundary
        )

        assert reopened.read("result.json") == {"status": "started"}
        assert (root / "stages").is_dir()
        assert (root / "requests").is_dir()
    finally:
        boundary.close()


def test_failed_before_dispatch_is_archived_before_a_safe_new_attempt(
    tmp_path: Path,
) -> None:
    store = WritingStore(tmp_path, {"run": "recoverable"})
    store.begin_stage("p1", {"stage": "P1"})
    store.finish_stage(
        "p1", "", {"error_code": "keychain_unavailable"},
        status="failed_before_dispatch",
    )

    assert store.cached_stage("p1") is None
    store.begin_stage("p1", {"stage": "P1", "attempt": 2})

    current = store.read("stages/p1.json")
    archives = sorted((tmp_path / "stages").glob(
        "p1-failed-before-dispatch-*.json"
    ))
    assert current["status"] == "started"
    assert current["inputs"]["attempt"] == 2
    assert len(archives) == 1
    assert json.loads(archives[0].read_text())["status"] == "failed_before_dispatch"


@pytest.mark.parametrize("status", ["started", "invalid", "unresolved"])
def test_uncertain_or_invalid_stage_is_never_archived_for_resend(
    tmp_path: Path, status: str
) -> None:
    store = WritingStore(tmp_path / status, {"run": status})
    store.begin_stage("p1", {"stage": "P1"})
    if status != "started":
        store.finish_stage("p1", "raw", {"error_code": "failure"}, status=status)

    with pytest.raises(WritingStopped, match="automatic resend disabled"):
        store.cached_stage("p1")
    with pytest.raises(WritingStopped, match="cannot be overwritten"):
        store.begin_stage("p1", {"stage": "P1", "attempt": 2})
    assert list((tmp_path / status / "stages").glob(
        "p1-failed-before-dispatch-*.json"
    )) == []


def test_record_error_binds_received_http_failure_to_request(tmp_path: Path) -> None:
    store = WritingStore(tmp_path, {"run": "http-error"})
    token = store.before_request(
        "P2", {"messages": []},
        WritingLimits(allow_paid=True, max_requests=1),
    )
    error = ProviderAnalysisError(
        "safe message", code="provider_unavailable",
        partial_response="raw provider body",
    )
    error.http_status_code = 503

    store.record_error(token, error)

    row = store.requests()[0]
    assert row["status"] == "response_error"
    assert row["error_code"] == "provider_unavailable"
    assert row["http_status"] == 503
    assert row["raw_response"] == "raw provider body"
    assert "safe message" not in json.dumps(row)
    assert store.metrics()["unresolved_request_count"] == 0


def test_record_error_leaves_no_response_failure_uncertain(tmp_path: Path) -> None:
    store = WritingStore(tmp_path, {"run": "network-error"})
    token = store.before_request(
        "P2", {"messages": []},
        WritingLimits(allow_paid=True, max_requests=1),
    )

    store.record_error(
        token,
        ProviderAnalysisError("network failed", code="provider_unavailable"),
    )

    row = store.requests()[0]
    assert row["status"] == "dispatching"
    assert "raw_response" not in row
    assert store.metrics()["unresolved_request_count"] == 1


def test_explicit_failed_search_receipt_allows_later_stage_without_retry(
    tmp_path: Path,
) -> None:
    store = WritingStore(tmp_path, {"run": "search-timeout"})
    limits = WritingLimits(allow_paid=True, max_requests=3)
    token = store.before_request("P3", {"messages": []}, limits)
    store.record_error(
        token,
        ProviderAnalysisError("network timed out", code="network_timeout"),
    )
    failure_name = "research-failures/task.json"
    failure = {
        "payload": {"task_key": "task", "status": "failed"},
        "sha256": "failure-sha",
    }
    store.write(failure_name, failure)

    store.acknowledge_failed_search_request(
        task_key="task", failure_artifact=failure_name,
    )
    next_token = store.before_request("P4", {"messages": []}, limits)
    store.after_response(next_token, {"usage": {}})

    assert next_token != token
    assert store.metrics()["unresolved_request_count"] == 1
    receipt = store.read(f"request-resolutions/{token}.json")
    assert receipt["disposition"] == "no_retry_failed_research"


def test_explicit_analysis_retry_allows_one_report_resend_and_preserves_original(
    tmp_path: Path,
) -> None:
    store = WritingStore(tmp_path, {"run": "report-disconnect"})
    limits = WritingLimits(allow_paid=True, max_requests=3)
    key = "P4-card"
    store.begin_stage(key, {"stage": "P4", "system": "P4", "input": {}})
    token = store.before_request("P4", {"messages": []}, limits)
    store.finish_stage(
        key, "", {"error_type": "ProviderAnalysisError"}, status="unresolved"
    )

    retry_key = store.prepare_explicit_unresolved_retry(
        key=key, stage="P4", retry_generation=1,
    )
    next_token = store.before_request("P4", {"messages": []}, limits)

    assert retry_key == "P4-card-retry-1"
    assert next_token != token
    assert store.read(f"stages/{key}.json")["status"] == "unresolved"
    receipt = store.read(f"request-resolutions/{token}.json")
    assert receipt["disposition"] == "explicit_retry_unresolved_report"
    assert receipt["retry_generation"] == 1


def test_unresolved_report_never_resends_without_explicit_retry(tmp_path: Path) -> None:
    store = WritingStore(tmp_path, {"run": "no-automatic-report-resend"})
    limits = WritingLimits(allow_paid=True, max_requests=3)
    store.before_request("P4", {"messages": []}, limits)

    with pytest.raises(WritingStopped, match="automatic resend disabled"):
        store.before_request("P4", {"messages": []}, limits)


def test_record_error_does_not_replace_an_already_saved_response(tmp_path: Path) -> None:
    store = WritingStore(tmp_path, {"run": "saved-response"})
    token = store.before_request(
        "P1", {"messages": []},
        WritingLimits(allow_paid=True, max_requests=1),
    )
    body = {"choices": [{"message": {"content": "{broken"}}]}
    store.after_response(token, body)

    store.record_error(token, ValueError("later JSON validation failed"))

    row = store.requests()[0]
    assert row["status"] == "response_received"
    assert row["response"] == body
    assert "error_code" not in row


def test_complete_invalid_response_can_be_revalidated_locally_without_resend(tmp_path):
    store = WritingStore(tmp_path, {"run": "local-revalidation"})
    store.begin_stage("p1", {"stage": "P1"})
    store.finish_stage("p1", '{"anchors":["s1"]}', {"error": "old shape"}, status="invalid")
    before = (tmp_path / "stages/p1.json").read_bytes()
    def validate(value):
        assert value == {"anchors": ["s1"]}
        return value
    assert store.cached_stage("p1", revalidate=validate) == {"anchors": ["s1"]}
    assert (tmp_path / "stages/p1.json").read_bytes() == before
    assert store.metrics()["model_request_count"] == 0
    assert store.exists("raw-revalidations/p1.json")


def test_failed_local_validation_preserves_invalid_record(tmp_path):
    store = WritingStore(tmp_path, {"run": "invalid"})
    store.begin_stage("p1", {"stage": "P1"})
    store.finish_stage("p1", '{"anchors":["unknown"]}', {}, status="invalid")
    def reject(value):
        raise ValueError("unknown evidence")
    with pytest.raises(ValueError, match="unknown evidence"):
        store.cached_stage("p1", revalidate=reject)
    assert not store.exists("local-revalidations/p1.json")
    assert store.read("stages/p1.json")["status"] == "invalid"


def test_unresolved_response_is_never_locally_promoted(tmp_path):
    store = WritingStore(tmp_path, {"run": "unresolved"})
    store.begin_stage("p1", {"stage": "P1"})
    store.finish_stage("p1", '{"anchors":["s1"]}', {}, status="unresolved")
    with pytest.raises(WritingStopped):
        store.cached_stage("p1", revalidate=lambda value: pytest.fail("must not validate partial responses"))


def test_recorded_local_correction_cannot_replace_original_model_response(tmp_path):
    from audio_memory.analysis.beta8_writing_store import digest
    store = WritingStore(tmp_path, {"run": "recorded-correction"})
    store.begin_stage("p1", {"stage": "P1"})
    store.finish_stage("p1", '{"start":"s2","anchor":"s1"}', {}, status="invalid")
    original = store.read("stages/p1.json")
    corrected = {"start": "s1", "anchor": "s1"}
    receipt = {"original_stage_sha256": digest(original), "result": corrected,
               "result_sha256": digest(corrected), "model_requests": 0,
               "corrections": [{"reason": "Read the original adjacent exchange", "field": "start", "before": "s2", "after": "s1"}]}
    store.write("local-revalidations/p1.json", receipt)
    def validate(value):
        assert value == {"start": "s2", "anchor": "s1"}
        raise ValueError("original anchor remains outside range")
    with pytest.raises(ValueError, match="original anchor"):
        store.cached_stage("p1", revalidate=validate)
    assert store.read("stages/p1.json") == original
    assert store.read("local-revalidations/p1.json") == receipt
    assert not store.exists("raw-revalidations/p1.json")
