from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from audio_memory.analysis.beta8_p1_p5_evidence import P1P5EvidenceCatalog
from audio_memory.analysis.beta8_writing_scoring import pending_score, RUBRIC_VERSION
from audio_memory.analysis.beta8_writing_scoring import (
    apply_objective_deductions,
    validate_score,
)
from audio_memory.analysis.beta8_writing_store import WritingLimits, WritingStopped, WritingStore, digest
from audio_memory.analysis.beta8_writing_transport import (
    SourceFetcher,
    report_reasoning_effort,
    report_thinking,
    verify_source,
)
from audio_memory.analysis.errors import ProviderAnalysisError
from audio_memory.prompts.beta8_pipeline_schema import (
    canonicalize_source_url,
    stable_source_id,
)
from audio_memory.prompts.beta8_p1_p5_composer import P1P5Prompts


class WritingStageOutputError(ValueError):
    code = "model_response_invalid"


class P1P5Pipeline:
    def __init__(self, *, output_dir: Path, transport, limits: WritingLimits,
                 provider_id: str, model_id: str, search_provider_id: str | None = None,
                 search_model_id: str | None = None, credential_generation: int = 0,
                 search_credential_generation: int | None = None, source_fetcher=None,
                 before_dispatch=None, write_boundary=None,
                 explicit_retry_generation: int = 0):
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
        self.explicit_retry_generation = explicit_retry_generation
        self.prompts = P1P5Prompts()

    def _output_tokens(self, stage):
        if stage == "P4":
            return min(self.limits.max_output_tokens, 24_000)
        if stage == "P5":
            return min(self.limits.max_output_tokens, 16_000)
        return self.limits.output_tokens(stage)

    def _stage_limits(self, stage):
        return replace(self.limits, max_output_tokens=self._output_tokens(stage))

    async def run(self, segments: list[dict], *, user_corrections: list, report_period: str) -> dict:
        identity = {
            "pipeline": "beta8_p1_p5_v1", "protocol_version": 1,
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
            "reasoning_policy": {
                stage: {
                    "thinking": report_thinking(stage)["type"],
                    "effort": report_reasoning_effort(stage),
                }
                for stage in ("P1", "P2", "P4", "P5")
            },
        }
        self.store = WritingStore(self.output_dir, identity, write_boundary=self.write_boundary)
        with self.store.locked():
            return await self._run_locked(segments, user_corrections, report_period)

    async def _run_locked(self, segments, corrections, report_period):
        initial_count = self.store.metrics()["model_request_count"]
        cards, scoring_inputs, scores = [], [], []
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
                  "revision_performed": False, "rubric_version": RUBRIC_VERSION,
                  "scoring_status": "pending", "scores": scores, "failure": None}
        try:
            if not segments or not any(item.get("is_reliable") is not False and str(item.get("text", "")).strip() for item in segments):
                result["status"] = "waiting_for_input"
                result["scoring_status"] = "not_applicable"
                return self._finish(result, scoring_inputs, initial_count)
            catalog = P1P5EvidenceCatalog(segments)
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
                                          validate=lambda x, w=window: catalog.validate_window(x, w))
                if value.get("status") != "complete":
                    raise WritingStopped("P1 needs context; no automatic completion request")
                understanding.append(value)
            registry = catalog.normalize(understanding, windows)
            self.store.write("content-registry.json", registry)
            planning_input = dict(catalog.planning_input(registry), user_corrections=corrections, report_period=report_period,
                                  research_budget={"max_tasks": self.limits.max_research_tasks, "source_limit": self.limits.source_limit})
            self._check_input_capacity("P2", planning_input)
            plan = await self._stage("P2", planning_input, validate=lambda x: catalog.validate_plan(x, registry))
            self.store.write("card-plan.json", plan)
            if plan["status"] != "complete":
                raise WritingStopped("P2 needs context; planning incomplete")
            result["budget_gaps"] = plan["budget_gaps"]
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
                    packets[task["task_key"]] = {"task_key": task["task_key"], "status": "failed", "answer": "研究预算或搜索模型不可用。", "findings": [], "sources": [], "unresolved_questions": ["research_budget_or_provider_unavailable"], "execution_status": "not_executed"}
                    continue
                public_task = {key: task[key] for key in (
                    "task_key", "question", "purpose", "public_context", "source_requirements", "jurisdiction", "as_of", "version_constraint", "stop_condition")}
                public_task.update(source_limit=self.limits.source_limit, reusable_sources=[])
                packets[task["task_key"]] = await self._focused_research(public_task, task)
            self.store.write("research-packets.json", packets)
            planned_card_count = len(plan["cards"])
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
                result["status"] = (
                    "draft_ready"
                    if len(cards) == planned_card_count
                    else "draft_incomplete"
                )
                # Save after each card so later failures cannot erase earlier first drafts.
                self._finish(result, scoring_inputs, initial_count)
            result["status"] = "draft_ready" if all(card["status"] == "written" for card in cards) else "draft_incomplete"
            result["scores"] = scores
            for item in scoring_inputs:
                scoring_payload = self._scoring_payload(item)
                self._check_input_capacity("P5", scoring_payload)
                assessment = await self._stage(
                    "P5",
                    scoring_payload,
                    validate=lambda value, card=item["card"], packet=item["input"]: self._validate_assessment(value, card, packet),
                )
                scores.append(assessment["card"])
                self.store.write("scores.json", {
                    "status": "complete" if all(score["status"] == "scored" for score in scores) else "insufficient_input",
                    "cards": scores,
                })
                # Preserve every completed per-card score if a later P5 call fails.
                self._finish(result, scoring_inputs, initial_count)
            result["scoring_status"] = "scored" if all(item["status"] == "scored" for item in scores) else "pending"
            result["status"] = "scored_v1" if result["scoring_status"] == "scored" else "draft_ready_scoring_incomplete"
        except Exception as error:
            if cards and result.get("status") == "draft_ready":
                result["status"] = "draft_ready_scoring_failed"
                result["scoring_status"] = "failed"
            result["failure"] = {"type": type(error).__name__, "code": getattr(error, "code", None), "reason": str(error) if isinstance(error, (WritingStopped, ValueError)) else "stage_failed_see_saved_response"}
            if getattr(error, "transport_error_type", None):
                result["failure"]["transport_error_type"] = error.transport_error_type
            self.store.write("failure.json", result["failure"])
        return self._finish(result, scoring_inputs, initial_count)

    def _finish(self, result, scoring_inputs, initial_count):
        result["metrics"] = self.store.metrics()
        result["metrics"]["scoring_model_request_count"] = sum(row["stage"] == "P5" for row in self.store.requests())
        result["new_request_count"] = result["metrics"]["model_request_count"] - initial_count
        self.store.write("result.json", result)
        self.store.write("scoring-packet.json", {
            "rubric_version": RUBRIC_VERSION,
            "rubric_path": "评分细则.md",
            "cards": scoring_inputs,
            "scoring_model_calls": len(result.get("scores", [])),
            "expected_scoring_model_calls": len(scoring_inputs),
            "scope": "one_model_call_per_main_card_against_its_complete_source_activities",
        })
        self.store.write_text("评分细则.md", (self.prompts.root / "Q0.md").read_text())
        self.store.write_text(
            "完整初稿.md",
            "\n\n---\n\n".join(
                f"# {card['title']}\n\n{card['markdown'].lstrip()}" for card in result["cards"]
            ),
        )
        return result

    def _scoring_payload(self, item):
        packet = item["input"]
        return {
            "rubric_version": RUBRIC_VERSION,
            "card": {**item["card"], "card_sha256": item["score"]["card_sha256"]},
            "card_plan": packet["card_brief"],
            "complete_transcript": packet["complete_transcript"],
            "source_activities": packet["source_activities"],
            "topic_registry": packet["topic_registry"],
            "attention_signals": packet["attention_signals"],
            "commitments": packet["commitments"],
            "user_corrections": packet["user_corrections"],
            "research_packets": packet["research_packets"],
            "verified_sources": packet["verified_sources"],
        }

    def _validate_assessment(self, value, card, packet=None):
        misplaced = {"deductions", "read_scope", "strengths", "weaknesses", "missing_inputs"}
        # Some structured responses close the nested `card` object immediately
        # after `dimensions` and place the remaining score fields beside it.
        # Move only this exact, lossless shape; never infer or rewrite content.
        top_fields = set(value) - {"status", "card"} if isinstance(value, dict) else set()
        nested_fields = set(value.get("card", {})) & misplaced if isinstance(value, dict) and isinstance(value.get("card"), dict) else set()
        if (
            isinstance(value, dict)
            and {"status", "card"} <= set(value)
            and set(value) <= {"status", "card"} | misplaced
            and isinstance(value.get("card"), dict)
            and not (top_fields & nested_fields)
            and top_fields | nested_fields == misplaced
        ):
            value = {
                "status": value["status"],
                "card": {**value["card"], **{name: value[name] for name in top_fields}},
            }
        if set(value) != {"status", "card"} or value["status"] not in {"complete", "insufficient_input"}:
            raise ValueError("P5 assessment protocol is invalid")
        if not isinstance(value["card"], dict) or value["card"].get("card_id") != card["card_id"]:
            raise ValueError("P5 must return exactly the current main card assessment")
        score = (
            apply_objective_deductions(value["card"], card, packet)
            if packet is not None
            else validate_score(value["card"], card)
        )
        return {"status": value["status"], "card": score}

    async def _stage(self, stage, data, *, scene_id=None, validate=None, request_limit=None):
        system, user = self.prompts.system(stage, scene_id), self.prompts.user(data)
        key = stage + "-" + digest({"system": system, "user": user, "identity": self.store.identity})
        if self.explicit_retry_generation:
            key = self.store.prepare_explicit_unresolved_retry(
                key=key,
                stage=stage,
                retry_generation=self.explicit_retry_generation,
            )
        cached = self.store.cached_stage(key, revalidate=validate)
        if cached is not None:
            return validate(cached) if validate else cached
        self._check_input_capacity(stage, data, scene_id)
        if not self.limits.allow_paid:
            raise WritingStopped("paid execution requires explicit authorization")
        if self.store.metrics()["model_request_count"] >= self.limits.max_requests:
            raise WritingStopped("request budget exhausted")
        self.store.begin_stage(key, {"stage": stage, "system": system, "input": data})
        stage_start_count = self.store.metrics()["model_request_count"]
        request_token = None
        stage_dispatch_count = 0
        async def before(payload):
            nonlocal request_token, stage_dispatch_count
            dispatch_limit = 6 if request_limit is None else request_limit
            if stage == "P3" and stage_dispatch_count >= dispatch_limit:
                raise WritingStopped("P3 focused-search request limit reached")
            if self.before_dispatch:
                await self.before_dispatch()
            request_token = self.store.before_request(stage, payload, self._stage_limits(stage))
            stage_dispatch_count += 1
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
                max_output_tokens=self._output_tokens(stage), before_request=before, after_response=after,
                on_progress=progress, research_task=data if stage == "P3" else None)
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
                   "max_tokens": self._output_tokens(stage), "stream": stage != "P3",
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
        if set(value) != {"task_key", "status", "answer", "findings", "sources", "unresolved_questions"} or value["task_key"] != task["task_key"]:
            raise ValueError("Research protocol or task identity mismatch")
        if value["status"] not in {"sufficient", "partial", "conflicting", "not_found", "invalid_task", "failed"}:
            raise ValueError("Invalid research status")
        if not isinstance(value["answer"], str) or any(not isinstance(value[name], list) for name in ("findings", "sources", "unresolved_questions")):
            raise ValueError("Research result lists are required")
        if len(value["sources"]) > self.limits.source_limit:
            raise ValueError("Research source limit exceeded")
        normalized = dict(value)
        normalized["sources"] = []
        keys = set()
        for source in value["sources"]:
            source = dict(source) if isinstance(source, dict) else source
            if not isinstance(source, dict):
                raise ValueError("Research source fields are incomplete")
            # Kimi sometimes mirrors the input task's `version_constraint`
            # field in a source row. It is a lossless alias for the output
            # contract's `version`, so normalize the field name locally while
            # retaining the immutable raw provider response in the stage record.
            if isinstance(source, dict) and "version" not in source and "version_constraint" in source:
                source["version"] = source.pop("version_constraint")
            required = {"local_source_key", "title", "url", "publisher", "published_at", "version", "access_level", "quote", "locator", "context_note"}
            search_pro_fields = {"retrieval_provenance", "retrieved_text"}
            allowed_fields = required | search_pro_fields if source.get("retrieval_provenance") == "kimi_search_pro" else required
            if set(source) != allowed_fields or not isinstance(source["url"], str):
                raise ValueError("Research source fields are incomplete")
            if source.get("retrieval_provenance") == "kimi_search_pro" and (
                not isinstance(source.get("retrieved_text"), str)
                or not source["retrieved_text"].strip()
                or source["access_level"] != "original_text"
            ):
                raise ValueError("Search Pro source text is invalid")
            if not source["local_source_key"] or source["local_source_key"] in keys:
                raise ValueError("Research source key is missing or duplicated")
            keys.add(source["local_source_key"])
            if source["access_level"] not in {"original_text", "search_snippet", "locator_only"}:
                raise ValueError("Invalid research access level")
            normalized["sources"].append(source)
        for finding in value["findings"]:
            if set(finding) != {"statement", "source_keys", "applicability", "limitations"} or not set(finding["source_keys"]).issubset(keys):
                raise ValueError("Research finding has invalid source references")
        return normalized

    async def _research_task(self, public_task, task, *, request_limit=6):
        name = "research-failures/" + digest({"task": public_task, "identity": self.store.identity}) + ".json"
        if self.store.exists(name):
            saved = self.store.read(name)
            if digest(saved["payload"]) != saved["sha256"] or saved["payload"].get("task_key") != task["task_key"] or saved["payload"].get("status") != "failed":
                raise WritingStopped("corrupt research failure record")
            self.store.acknowledge_failed_search_request(
                task_key=task["task_key"], failure_artifact=name,
            )
            return saved["payload"]
        try:
            candidate = await self._stage(
                "P3", public_task,
                validate=lambda value: self._validate_research(value, task),
                request_limit=request_limit,
            )
        except (ProviderAnalysisError, WritingStageOutputError) as error:
            if getattr(error, "code", None) in {
                "credential_changed", "fixed_rules_changed",
            }:
                raise
            # Keep the original failed stage and request ledger. Resume reuses this
            # explicit failure, so independent first drafts never trigger a retry.
            failed = {"task_key": task["task_key"], "status": "failed", "answer": "研究请求失败，未取得可验证的外部证据。", "findings": [], "sources": [],
                      "unresolved_questions": ["Research request failed or returned unusable output; no verified external evidence was obtained."],
                      "execution_status": "failed", "failure_code": error.code}
            self.store.write(name, {"payload": failed, "sha256": digest(failed)})
            self.store.acknowledge_failed_search_request(
                task_key=task["task_key"], failure_artifact=name,
            )
            return failed
        return await self._verify_research(candidate)

    async def _focused_research(self, public_task, task):
        outcome = await self._research_task(public_task, task, request_limit=1)
        return {
            **outcome,
            "execution_status": "search_pro_complete",
            "focused_task_count": 1,
            "verified_source_count": sum(
                bool(source.get("quote_verified"))
                for source in outcome.get("sources", [])
            ),
        }

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
            elif source.get("retrieval_provenance") == "kimi_search_pro":
                text = source["retrieved_text"]
                fetched = {
                    "status": 200,
                    "url": source["url"],
                    "final_url": source["url"],
                    "text": text,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "content_sha256": sha256(text.encode("utf-8")).hexdigest(),
                    "error": None,
                    "safety_reason": None,
                    "retrieval_method": "kimi_search_pro",
                }
                self.store.write(f"sources/{cache_key}.json", {"payload": fetched, "sha256": digest(fetched)})
            else:
                if not self.limits.allow_paid:
                    raise WritingStopped("source retrieval disabled in cache-only run")
                fetched = await self.fetcher.fetch(source["url"])
                self.store.write(f"sources/{cache_key}.json", {"payload": fetched, "sha256": digest(fetched)})
            verified = verify_source(source, fetched)
            canonical_url = canonicalize_source_url(source["url"])
            verified["url"] = canonical_url
            verified["source_id"] = stable_source_id(canonical_url)
            verified["full_source_artifact"] = str(path)
            result["sources"].append(verified)
        verified_count = sum(bool(source.get("quote_verified")) for source in result["sources"])
        if candidate["sources"] and verified_count != len(candidate["sources"]):
            result["candidate_status"] = candidate["status"]
            result["status"] = "partial" if verified_count else "failed"
            result["unresolved_questions"] = list(candidate["unresolved_questions"]) + ["No verified source text was obtained, or some candidate excerpts could not be verified."]
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
