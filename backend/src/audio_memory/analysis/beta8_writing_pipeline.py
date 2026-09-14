from __future__ import annotations

import json
from pathlib import Path

from audio_memory.analysis.beta8_writing_evidence import EvidenceCatalog
from audio_memory.analysis.beta8_writing_scoring import pending_score, RUBRIC_VERSION
from audio_memory.analysis.beta8_writing_store import WritingLimits, WritingStopped, WritingStore, digest
from audio_memory.analysis.beta8_writing_transport import (
    SourceFetcher,
    report_reasoning_effort,
    report_thinking,
    verify_source,
)
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.prompts.beta8_writing_composer import WritingPrompts


class WritingStageOutputError(ValueError):
    code = "model_response_invalid"


class WritingPipeline:
    def __init__(self, *, output_dir: Path, transport, limits: WritingLimits,
                 provider_id: str, model_id: str, search_provider_id: str | None = None,
                 search_model_id: str | None = None, credential_generation: int = 0,
                 search_credential_generation: int | None = None, source_fetcher=None,
                 before_dispatch=None, write_boundary=None, retry_invalid_p1_windows=()):
        self.output_dir = Path(output_dir)
        self.transport = transport
        self.limits = limits
        self.provider_id, self.model_id = provider_id, model_id
        self.search_provider_id, self.search_model_id = search_provider_id, search_model_id
        self.credential_generation = credential_generation
        self.search_credential_generation = search_credential_generation
        self.fetcher = source_fetcher or SourceFetcher()
        self.before_dispatch = before_dispatch
        self.write_boundary = write_boundary
        self.retry_invalid_p1_windows = frozenset(retry_invalid_p1_windows)
        self.prompts = WritingPrompts()

    async def run(self, segments: list[dict], *, user_corrections: list, report_period: str) -> dict:
        identity = {
            "pipeline": "beta8_writing_v1", "protocol_version": 1,
            "input_sha256": digest(segments), "user_corrections": user_corrections,
            "report_period": report_period, "prompt_hash": self.prompts.fixed_rules_hash(),
            "provider_id": self.provider_id, "model_id": self.model_id,
            "search_provider_id": self.search_provider_id, "search_model_id": self.search_model_id,
            "credential_generation": self.credential_generation,
            "search_credential_generation": self.search_credential_generation,
            "core_budget_bytes": self.limits.core_budget_bytes,
            "max_output_tokens": self.limits.max_output_tokens,
            "max_search_output_tokens": self.limits.max_search_output_tokens,
            "max_research_tasks": self.limits.max_research_tasks, "source_limit": self.limits.source_limit,
        }
        self.store = WritingStore(self.output_dir, identity, write_boundary=self.write_boundary)
        with self.store.locked():
            return await self._run_locked(segments, user_corrections, report_period)

    async def _run_locked(self, segments, corrections, report_period):
        initial_count = self.store.metrics()["model_request_count"]
        cards, scoring_inputs = [], []
        if self.store.exists("scoring-packet.json"):
            saved = self.store.read("scoring-packet.json")
            if saved["rubric_version"] != RUBRIC_VERSION:
                raise WritingStopped("saved scoring packet uses a different rubric")
            for item in saved["cards"]:
                card_id = self.store._safe_key(item["card"]["card_id"])
                if (self.store.read(f"cards/{card_id}.json") != item["card"]
                        or self.store.read(f"cards/{card_id}.input.json") != item["input"]):
                    raise WritingStopped("saved draft and scoring input disagree")
                cards.append(item["card"])
                scoring_inputs.append(item)
        result = {"status": "stopped", "cards": cards, "audit_performed": False, "published": False,
                  "rubric_version": RUBRIC_VERSION, "scoring_status": "pending", "failure": None}
        try:
            if not segments or not any(item.get("is_reliable") is not False and str(item.get("text", "")).strip() for item in segments):
                result["status"] = "waiting_for_input"
                result["scoring_status"] = "not_applicable"
                return self._finish(result, scoring_inputs, initial_count)
            catalog = EvidenceCatalog(segments)
            windows = catalog.windows(self.limits.core_budget_bytes)
            self.store.write("input.json", {"segments": segments, "user_corrections": corrections, "report_period": report_period})
            self.store.write("windows.json", windows)
            if not windows:
                result["status"] = "waiting_for_input"
                return self._finish(result, scoring_inputs, initial_count)
            for window in windows:
                self._check_input_capacity("P1", dict(window, user_corrections=corrections))
            understanding = []
            for window in windows:
                value = await self._stage("P1", dict(window, user_corrections=corrections),
                                          validate=lambda x, w=window: catalog.validate_window(x, w),
                                          retry_invalid=window["window_id"] in self.retry_invalid_p1_windows)
                if value.get("status") != "complete":
                    raise WritingStopped("P1 needs context; no automatic completion request")
                understanding.append(value)
            registry = catalog.normalize(understanding, windows)
            self.store.write("content-registry.json", registry)
            planning_input = dict(registry, user_corrections=corrections, report_period=report_period,
                                  research_budget={"max_tasks": self.limits.max_research_tasks, "source_limit": self.limits.source_limit})
            plan = await self._stage("P2", planning_input, validate=lambda x: catalog.validate_plan(x, registry))
            self.store.write("card-plan.json", plan)
            if plan["status"] != "complete":
                raise WritingStopped("P2 needs context; planning incomplete")
            result["budget_gaps"] = plan["budget_gaps"]
            result["accepted_todo_keys"] = plan["accepted_todo_keys"]
            if not plan["cards"]:
                result["status"] = "no_cards_planned"
                result["scoring_status"] = "not_applicable"
                return self._finish(result, scoring_inputs, initial_count)
            material_packets = {}
            for brief in plan["cards"]:
                card_id = "card-" + digest(brief["draft_key"])[:16]
                packet = catalog.assemble(card_id, brief, registry, corrections)
                self._check_input_capacity("P4", packet, brief["scene_id"])
                material_packets[brief["draft_key"]] = packet
                self.store.write(f"cards/{card_id}.material.json", packet)
            packets = {}
            shared_research = {}
            for task in plan["research_tasks"]:
                research_identity = digest({key: value for key, value in task.items() if key not in {"task_key", "target_card_keys"}})
                if research_identity in shared_research:
                    prior_key = shared_research[research_identity]
                    packets[task["task_key"]] = dict(packets[prior_key], task_key=task["task_key"], reused_from_task_key=prior_key)
                    continue
                ordinal = len(shared_research)
                shared_research[research_identity] = task["task_key"]
                if ordinal >= self.limits.max_research_tasks or not self.search_provider_id or not self.search_model_id:
                    packets[task["task_key"]] = {"task_key": task["task_key"], "status": "failed", "findings": [], "sources": [], "unresolved": ["research_budget_or_provider_unavailable"], "execution_status": "not_executed"}
                    continue
                public_task = {key: task[key] for key in (
                    "task_key", "question", "purpose", "public_context", "source_requirements", "jurisdiction", "as_of", "version_constraint")}
                public_task.update(source_limit=self.limits.source_limit, reusable_sources=[])
                packets[task["task_key"]] = await self._research_task(public_task, task)
            self.store.write("research-packets.json", packets)
            for brief in plan["cards"]:
                packet = material_packets[brief["draft_key"]]
                card_id = packet["card_id"]
                relevant = [packets[key] for key in brief["research_decision"]["task_keys"]]
                packet["research_packets"] = [{key: value for key, value in research.items() if key != "sources"} | {
                    "source_ids": [source["source_id"] for source in research["sources"]],
                } for research in relevant]
                packet["verified_sources"] = [self._source_excerpt(source) for research in relevant for source in research["sources"] if source.get("quote_verified")]
                self.store.write(f"cards/{card_id}.input.json", packet)
                card = await self._stage("P4", packet, scene_id=brief["scene_id"], validate=lambda x, p=packet: catalog.validate_card(x, p))
                self.store.write(f"cards/{card_id}.json", card)
                self.store.write_text(f"cards/{card_id}.md", card["markdown"])
                if not any(saved["card_id"] == card_id for saved in cards):
                    cards.append(card)
                    scoring_inputs.append({"card": card, "input": packet, "score": pending_score(card, packet)})
                # Save after each card so later failures cannot erase earlier first drafts.
                self._finish(result, scoring_inputs, initial_count)
            result["status"] = "draft_ready" if all(card["status"] == "complete" for card in cards) else "draft_incomplete"
        except Exception as error:
            result["failure"] = {"type": type(error).__name__, "code": getattr(error, "code", None), "reason": str(error) if isinstance(error, (WritingStopped, ValueError)) else "stage_failed_see_saved_response"}
            if getattr(error, "transport_error_type", None):
                result["failure"]["transport_error_type"] = error.transport_error_type
            self.store.write("failure.json", result["failure"])
        return self._finish(result, scoring_inputs, initial_count)

    def _finish(self, result, scoring_inputs, initial_count):
        result["metrics"] = self.store.metrics()
        result["new_request_count"] = result["metrics"]["model_request_count"] - initial_count
        self.store.write("result.json", result)
        self.store.write("scoring-packet.json", {"rubric_version": RUBRIC_VERSION, "rubric_path": "评分细则.md", "cards": scoring_inputs, "scoring_model_calls": 0, "scope": "per_card_only_no_daily_coverage_acceptance"})
        self.store.write_text("评分细则.md", (self.prompts.root / "Q0.md").read_text())
        self.store.write_text("完整初稿.md", "\n\n---\n\n".join(card["markdown"] for card in result["cards"]))
        return result

    async def _stage(self, stage, data, *, scene_id=None, validate=None, retry_invalid=False):
        system, user = self.prompts.system(stage, scene_id), self.prompts.user(data)
        key = stage + "-" + digest({"system": system, "user": user, "identity": self.store.identity})
        retry_of = None
        if retry_invalid:
            if stage != "P1":
                raise WritingStopped("explicit retry applies only to P1")
            original_name = "stages/" + key + ".json"
            if not self.store.exists(original_name):
                raise WritingStopped("explicit retry requires an existing invalid P1 response")
            original = self.store.read(original_name)
            self.store._validate_finished_stage(original)
            if original["status"] != "invalid":
                raise WritingStopped("explicit retry requires a fully received invalid response")
            retry_of = key
            key += "-retry-1"
        cached = self.store.cached_stage(key, revalidate=validate)
        if cached is not None:
            return validate(cached) if validate else cached
        self._check_input_capacity(stage, data, scene_id)
        if not self.limits.allow_paid:
            raise WritingStopped("paid execution requires explicit authorization")
        if self.store.metrics()["model_request_count"] >= self.limits.max_requests:
            raise WritingStopped("request budget exhausted")
        self.store.begin_stage(key, {"stage": stage, "system": system, "input": data, **({"retry_of": retry_of} if retry_of else {})})
        stage_start_count = self.store.metrics()["model_request_count"]
        request_token = None
        async def before(payload):
            nonlocal request_token
            if self.before_dispatch:
                await self.before_dispatch()
            request_token = self.store.before_request(stage, payload, self.limits)
        async def after(body):
            if request_token is None:
                raise WritingStopped("response without request ledger")
            self.store.after_response(request_token, body)
        async def progress(value):
            if request_token is None:
                raise WritingStopped("progress without request ledger")
            self.store.record_progress(request_token, value)
        raw = None
        try:
            raw = await self.transport.complete(stage=stage, system=system, user=user,
                provider_id=self.search_provider_id if stage == "P3" else self.provider_id,
                model_id=self.search_model_id if stage == "P3" else self.model_id,
                max_output_tokens=self.limits.output_tokens(stage), before_request=before, after_response=after,
                on_progress=progress)
            try:
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise ValueError("Stage output must be a JSON object")
                if validate:
                    value = validate(value)
            except WritingStopped:
                raise
            except (ValueError, TypeError, KeyError) as error:
                raise WritingStageOutputError(str(error)) from error
            self.store.finish_stage(key, raw, value)
            return value
        except BaseException as error:
            raw_error = getattr(error, "partial_response", None)
            if request_token is not None:
                self.store.record_error(request_token, error)
            status = "failed_before_dispatch" if self.store.metrics()["model_request_count"] == stage_start_count else "invalid" if raw is not None else "unresolved"
            failure = {"error_type": type(error).__name__, "error_code": getattr(error, "code", None)}
            if getattr(error, "transport_error_type", None):
                failure["transport_error_type"] = error.transport_error_type
            if getattr(error, "transport_diagnostics", None):
                failure["transport_diagnostics"] = error.transport_diagnostics
            self.store.finish_stage(key, raw if raw is not None else (raw_error or ""), failure, status=status)
            raise

    def _check_input_capacity(self, stage, data, scene_id=None):
        payload = {"model": self.search_model_id if stage == "P3" else self.model_id,
                   "messages": [{"role": "system", "content": self.prompts.system(stage, scene_id)},
                                {"role": "user", "content": self.prompts.user(data)}],
                   "max_tokens": self.limits.output_tokens(stage), "stream": stage != "P3",
                   "temperature": 0, "response_format": {"type": "json_object"}, "thinking": report_thinking(stage)}
        effort = report_reasoning_effort(stage)
        if effort is not None:
            payload["reasoning_effort"] = effort
        if stage != "P3":
            payload["stream_options"] = {"include_usage": True}
        bound = len(json.dumps(payload, ensure_ascii=False).encode())
        if bound > self.limits.max_input_tokens:
            raise WritingStopped(f"{stage} complete input exceeds capacity budget; no request or truncation")
        return bound

    def _validate_research(self, value, task):
        if set(value) != {"task_key", "status", "findings", "sources", "unresolved"} or value["task_key"] != task["task_key"]:
            raise ValueError("Research protocol or task identity mismatch")
        if value["status"] not in {"success", "partial", "failed"}:
            raise ValueError("Invalid research status")
        if any(not isinstance(value[name], list) for name in ("findings", "sources", "unresolved")):
            raise ValueError("Research result lists are required")
        if len(value["sources"]) > self.limits.source_limit:
            raise ValueError("Research source limit exceeded")
        keys = set()
        for source in value["sources"]:
            required = {"local_source_key", "title", "url", "publisher", "published_at", "version", "access_level", "quote", "locator", "context_note"}
            if not isinstance(source, dict) or set(source) != required or not isinstance(source["url"], str):
                raise ValueError("Research source fields are incomplete")
            if not source["local_source_key"] or source["local_source_key"] in keys:
                raise ValueError("Research source key is missing or duplicated")
            keys.add(source["local_source_key"])
            if source["access_level"] not in {"original_text", "search_snippet", "locator_only"}:
                raise ValueError("Invalid research access level")
        for finding in value["findings"]:
            if set(finding) != {"statement", "source_keys", "applicability", "limitations"} or not set(finding["source_keys"]).issubset(keys):
                raise ValueError("Research finding has invalid source references")
        return value

    async def _research_task(self, public_task, task):
        name = "research-failures/" + digest({"task": public_task, "identity": self.store.identity}) + ".json"
        if self.store.exists(name):
            saved = self.store.read(name)
            if digest(saved["payload"]) != saved["sha256"] or saved["payload"].get("task_key") != task["task_key"] or saved["payload"].get("status") != "failed":
                raise WritingStopped("corrupt research failure record")
            return saved["payload"]
        try:
            candidate = await self._stage("P3", public_task, validate=lambda value: self._validate_research(value, task))
        except (ProviderAnalysisError, WritingStageOutputError) as error:
            if getattr(error, "pause_batch", False) or getattr(error, "code", None) in {
                "authentication_failed", "insufficient_balance", "credential_changed", "fixed_rules_changed",
            }:
                raise
            # Keep the original failed stage and request ledger. Resume reuses this
            # explicit failure, so independent first drafts never trigger a retry.
            failed = {"task_key": task["task_key"], "status": "failed", "findings": [], "sources": [],
                      "unresolved": ["Research request failed or returned unusable output; no verified external evidence was obtained."],
                      "execution_status": "failed", "failure_code": error.code}
            self.store.write(name, {"payload": failed, "sha256": digest(failed)})
            return failed
        return await self._verify_research(candidate)

    async def _verify_research(self, candidate):
        result = dict(candidate, findings_are_candidates=True)
        result["sources"] = []
        for source in candidate["sources"]:
            cache_key = digest(source)
            path = self.store.root / "sources" / f"{cache_key}.json"
            if self.store.exists("sources/" + path.name):
                fetched = self.store.read("sources/" + path.name)
                if digest(fetched["payload"]) != fetched["sha256"]:
                    raise WritingStopped("corrupt source cache")
                fetched = fetched["payload"]
            else:
                if not self.limits.allow_paid:
                    raise WritingStopped("source retrieval disabled in cache-only run")
                fetched = await self.fetcher.fetch(source["url"])
                self.store.write(f"sources/{cache_key}.json", {"payload": fetched, "sha256": digest(fetched)})
            verified = verify_source(source, fetched)
            verified["source_id"] = "source-" + cache_key[:16]
            verified["full_source_artifact"] = str(path)
            result["sources"].append(verified)
        verified_count = sum(bool(source.get("quote_verified")) for source in result["sources"])
        if candidate["status"] == "success" and (not verified_count or verified_count != len(candidate["sources"])):
            result["candidate_status"] = candidate["status"]
            result["status"] = "partial" if verified_count else "failed"
            result["unresolved"] = list(candidate["unresolved"]) + ["No verified source text was obtained, or some candidate excerpts could not be verified."]
        return result

    @staticmethod
    def _source_excerpt(source):
        text = " ".join(source["fetch"]["text"].split())
        quote = source["verified_quote"]
        start = text.index(quote)
        left, right = max(0, start - 1600), min(len(text), start + len(quote) + 1600)
        return {key: value for key, value in source.items() if key not in {"fetch", "full_source_artifact"}} | {
            "retrieved_context": text[left:right], "context_start": left, "context_end": right,
            "source_text_sha256": source["fetch"]["content_sha256"],
            "fetched_at": source["fetch"]["fetched_at"], "scope": "verified_excerpt_with_surrounding_context",
        }
