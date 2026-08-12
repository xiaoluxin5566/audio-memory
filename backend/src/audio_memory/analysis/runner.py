from __future__ import annotations

import asyncio
import json
import logging
import re
from hashlib import sha256
from typing import Protocol
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import delete, select, update

from audio_memory.analysis.profile import validate_profile_delta
from audio_memory.analysis.autonomous_context import (
    DirectContext,
    LongContextPlan,
    plan_autonomous_context,
)
from audio_memory.analysis.provider import ProviderAnalysisError
from audio_memory.analysis.publisher import AnalysisOutcome
from audio_memory.analysis.native_search import validate_search_round
from audio_memory.analysis.clusters import (
    build_transcript_clusters,
    event_hints_for_cluster,
)
from audio_memory.analysis.director import (
    DirectorSelectionError,
    attach_event_anchors,
    normalize_director_results,
)
from audio_memory.analysis.dossiers import (
    DossierBuildError,
    SceneDossier,
    build_scene_dossiers,
    dossiers_for_scene,
)
from audio_memory.analysis.windows import (
    AnalysisQualityError,
    AnalysisWindow,
    AnalysisWindowError,
    EVENT_MAP_SEMANTIC_REPAIR_ATTEMPTS,
    build_analysis_windows,
    complete_window_event_map,
    merge_window_event_maps,
    validate_analysis_quality,
)
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    JobFile,
    ProfileCandidate,
    ReanalysisBatch,
    ReanalysisItem,
    Transcript,
)
from audio_memory.prompts.composer import PromptComposer
from audio_memory.prompts.autonomous_schema import (
    AutonomousAnalysisResult,
    AutonomousRetrievalPlan,
    InformationNotebook,
)
from audio_memory.prompts.day_map_schema import (
    MAX_SEARCH_ROUNDS,
    AutonomousDayMap,
    ExternalSource,
    NativeSearchDecision,
    SearchResultItem,
    SearchRound,
)
from audio_memory.prompts.director_schema import DirectorResult
from audio_memory.prompts.evidence import (
    EvidenceIntegrityError,
    SCENE_SEMANTIC_REPAIR_ATTEMPTS,
    validate_evidence_integrity,
)
from audio_memory.prompts.event_schema import EventMap, EventMapDraft
from audio_memory.transcript_safety import pending_risk_review_exists
from audio_memory.prompts.schemas import (
    ContentSceneResult,
    GrowthSceneResult,
    InspirationSceneResult,
    MeetingSceneResult,
    ParentingSceneResult,
    SceneResultBase,
    TodoSceneResult,
)
from audio_memory.prompts.store import PROMPT_SCENES, PromptDocument
from audio_memory.providers.adapters.base import NativeSearchCallResult


logger = logging.getLogger("uvicorn.error")


class CredentialChangedError(RuntimeError):
    pass


class FixedRulesChangedError(RuntimeError):
    pass


class LeaseLostError(RuntimeError):
    pass


class StrictAnalysisProvider(Protocol):
    async def analyze_autonomous_day_map(
        self, request, provider_snapshot
    ) -> AutonomousDayMap: ...

    async def analyze_autonomous_search_loop(
        self, request, provider_snapshot
    ) -> NativeSearchDecision: ...

    async def native_search(
        self,
        provider_id: str,
        *,
        queries: list[str],
        round_number: int,
        model_id: str | None = None,
        timeout_seconds: float = 60,
    ): ...

    async def analyze_autonomous_final_analysis(
        self,
        request,
        provider_snapshot,
        *,
        persisted_sources: list[ExternalSource],
    ) -> AutonomousAnalysisResult: ...

    async def analyze_autonomous(
        self, request, provider_snapshot
    ) -> AutonomousAnalysisResult: ...

    async def analyze_autonomous_notes(
        self, request, provider_snapshot
    ) -> InformationNotebook: ...

    async def analyze_autonomous_retrieval_plan(
        self, request, provider_snapshot
    ) -> AutonomousRetrievalPlan: ...

    async def analyze_autonomous_final(
        self, request, provider_snapshot
    ) -> AutonomousAnalysisResult: ...

    async def analyze_event_map(self, request, provider_snapshot) -> EventMapDraft: ...

    async def analyze_director(self, request, provider_snapshot) -> DirectorResult: ...

    async def analyze_scene(
        self, scene_id, request, provider_snapshot
    ) -> SceneResultBase: ...


class ProfileExtractor(Protocol):
    async def extract(self, transcript, cards, existing, provider_snapshot): ...


class Publisher(Protocol):
    async def publish(
        self,
        version_id,
        results,
        profile_delta,
        *,
        worker_owner_id: str | None = None,
    ) -> AnalysisOutcome: ...


class GenerationSource(Protocol):
    async def credential_generation(self, provider_id: str) -> int: ...

    def publication_guard(self, provider_id: str): ...


_SCENE_MODELS = {
    "todo": TodoSceneResult,
    "meeting": MeetingSceneResult,
    "parenting": ParentingSceneResult,
    "content": ContentSceneResult,
    "growth": GrowthSceneResult,
    "inspiration": InspirationSceneResult,
}
_SCENE_ADAPTERS = {
    scene_id: TypeAdapter(model) for scene_id, model in _SCENE_MODELS.items()
}


class AnalysisRunner:
    def __init__(
        self,
        *,
        database: Database,
        provider: StrictAnalysisProvider,
        profile_extractor: ProfileExtractor,
        publisher: Publisher,
        generation_source: GenerationSource,
    ) -> None:
        self.database = database
        self.provider = provider
        self.profile_extractor = profile_extractor
        self.publisher = publisher
        self.generation_source = generation_source
        self.composer = PromptComposer()

    async def run(
        self, version_id: str, worker_owner_id: str | None = None
    ) -> AnalysisOutcome:
        version = await self._version(version_id, worker_owner_id)
        await self._require_fixed_rules(version, worker_owner_id)
        provider_snapshot = {
            "provider_id": version.provider_id,
            "model_id": version.model_id,
            "credential_generation": version.credential_generation,
        }
        transcript = await self._transcript(version.source_job_id)
        segment_ids = {str(item["segment_id"]) for item in transcript}
        profile = self._json_list(version.profile_snapshot_json)
        staged = self._json_object(version.staged_results_json)
        try:
            await self._require_generation(version, worker_owner_id)
            result, profile_transcript = await self._autonomous_with_fallback(
                version,
                transcript,
                profile,
                provider_snapshot,
                staged,
                worker_owner_id,
            )

            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            raw_delta = await self.profile_extractor.extract(
                profile_transcript, [], profile, provider_snapshot
            )
            raw_delta = self._filter_external_profile_candidates(
                raw_delta,
                [
                    ExternalSource.model_validate(item)
                    for item in staged.get("external_sources", [])
                ],
            )
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            verified_delta = await self._save_profile_candidates(
                version.id, raw_delta, segment_ids, worker_owner_id
            )
            delta = validate_profile_delta(verified_delta)
            await self._require_ownership(version.id, worker_owner_id)
            async with self.generation_source.publication_guard(
                version.provider_id
            ) as final_generation:
                if final_generation != version.credential_generation:
                    await self._mark_credential_changed(version, worker_owner_id)
                outcome = await self.publisher.publish(
                    version.id,
                    result,
                    delta,
                    worker_owner_id=worker_owner_id,
                )
            return outcome
        except asyncio.CancelledError:
            raise
        except FixedRulesChangedError:
            raise
        except LeaseLostError:
            raise
        except CredentialChangedError:
            raise
        except ProviderAnalysisError as exc:
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            await self._mark_failed(
                version.id,
                worker_owner_id,
                error_code=exc.code,
                pause_history=exc.pause_batch,
            )
            raise
        except BaseException:
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            await self._mark_failed(version.id, worker_owner_id)
            raise

    async def _autonomous_with_fallback(
        self,
        version,
        transcript,
        profile,
        provider_snapshot,
        staged,
        worker_owner_id,
    ) -> tuple[AutonomousAnalysisResult, list[dict[str, object]]]:
        """Prefer two full reads; compact only after a typed provider rejection."""
        try:
            result = await self._day_map_autonomous(
                version,
                transcript,
                profile,
                provider_snapshot,
                staged,
                worker_owner_id,
            )
            return result, transcript
        except ProviderAnalysisError as exc:
            if exc.code != "provider_input_rejected":
                raise

        context = plan_autonomous_context(transcript)
        if isinstance(context, DirectContext):
            result = await self._direct_autonomous(
                version,
                transcript,
                profile,
                provider_snapshot,
                staged,
                worker_owner_id,
            )
            profile_transcript = transcript
        else:
            result, profile_transcript = await self._long_autonomous(
                version,
                context,
                transcript,
                profile,
                provider_snapshot,
                staged,
                worker_owner_id,
            )
        await self._stage_compact_fallback(
            version, result, staged, worker_owner_id
        )
        return result, profile_transcript

    async def _stage_compact_fallback(
        self,
        version,
        result: AutonomousAnalysisResult,
        staged: dict[str, object],
        worker_owner_id: str | None,
    ) -> None:
        fallback_decision = NativeSearchDecision(
            action="finalize",
            rationale="Provider rejected full input; use the compatible audio-only result.",
        )
        if "day_map" not in staged:
            staged["day_map"] = AutonomousDayMap.model_validate(
                {
                    "overview": {
                        "title": "本次概览",
                        "summary": (
                            "完整转写超出当前模型的单次输入范围，"
                            "已通过兼容分析流程生成以下结果。"
                        ),
                        "scene_ids": [],
                    },
                    "scenes": [],
                    "search_action": fallback_decision.model_dump(mode="json"),
                }
            ).model_dump(mode="json")
        staged.setdefault("search_rounds", [])
        staged.setdefault("external_sources", [])
        staged.setdefault(
            "search_phase",
            {
                "status": "finalized",
                "decision": fallback_decision.model_dump(mode="json"),
                "completed_rounds": len(staged["search_rounds"]),
            },
        )
        staged["fallback"] = {
            "route": "compact",
            "reason": "provider_input_rejected",
        }
        staged["autonomous"] = result.model_dump(mode="json")
        await self._save_staged(version.id, staged, worker_owner_id)

    async def _day_map_autonomous(
        self,
        version,
        transcript,
        profile,
        provider_snapshot,
        staged,
        worker_owner_id,
    ) -> AutonomousAnalysisResult:
        if "day_map" in staged:
            day_map = AutonomousDayMap.model_validate(staged["day_map"])
        else:
            request = self.composer.compose_autonomous_day_map(
                transcript=transcript,
                schema=AutonomousDayMap.model_json_schema(),
            )
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            day_map = await self.provider.analyze_autonomous_day_map(
                request, provider_snapshot
            )
            self._validate_day_map_evidence(day_map, transcript)
            staged["day_map"] = day_map.model_dump(mode="json")
            staged["search_rounds"] = []
            staged["external_sources"] = []
            await self._save_staged(version.id, staged, worker_owner_id)
        self._validate_day_map_evidence(day_map, transcript)

        rounds, external_sources = self._staged_search_state(staged)
        terminal_decision = self._staged_terminal_decision(staged, rounds)
        pending = self._pending_search_round(rounds)
        if terminal_decision is not None:
            decision = terminal_decision
        elif pending is not None:
            decision = pending.decision
        elif not rounds:
            decision = day_map.search_action
        elif len(rounds) >= MAX_SEARCH_ROUNDS:
            decision = NativeSearchDecision(
                action="finalize",
                rationale="Search round limit reached; finalize with available sources.",
            )
        elif rounds[-1].errors and not rounds[-1].sources:
            decision = NativeSearchDecision(
                action="finalize",
                rationale="Native search was unavailable; continue from audio only.",
            )
        else:
            decision = await self._next_search_decision(
                version,
                day_map,
                rounds,
                external_sources,
                provider_snapshot,
                worker_owner_id,
            )

        while decision.action == "search" and len(rounds) <= MAX_SEARCH_ROUNDS:
            pending = self._pending_search_round(rounds)
            if pending is None:
                if len(rounds) >= MAX_SEARCH_ROUNDS:
                    break
                pending = SearchRound(
                    round_number=len(rounds) + 1,
                    decision=decision,
                )
                rounds.append(pending)
                staged["search_rounds"] = [
                    item.model_dump(mode="json") for item in rounds
                ]
                await self._save_staged(version.id, staged, worker_owner_id)

            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            search_result = await self._native_search_with_retry(
                provider_snapshot=provider_snapshot,
                decision=pending.decision,
                round_number=pending.round_number,
            )
            sources = list(search_result.sources)
            configured_provider = str(provider_snapshot["provider_id"])
            if search_result.provider_id != configured_provider or any(
                item.provider_id != configured_provider for item in sources
            ):
                raise ProviderAnalysisError(
                    "Native search source provider does not match the analysis provider",
                    code="autonomous_search_state_invalid",
                )
            search_errors = list(search_result.errors)
            if search_result.available and not sources and not search_errors:
                search_errors.append("Native search returned no persistable sources.")
            results = [
                SearchResultItem(
                    provider_result_id=item.provider_result_id,
                    title=item.title,
                    url=item.url,
                    publisher=item.publisher,
                    published_at=item.published_at,
                    snippet=item.support_statement,
                )
                for item in sources
            ]
            completed = validate_search_round(
                SearchRound(
                    round_number=pending.round_number,
                    decision=pending.decision,
                    results=results,
                    sources=sources,
                    errors=search_errors,
                )
            )
            rounds[-1] = completed
            external_sources = self._canonical_external_sources(
                [source for item in rounds for source in item.sources]
            )
            staged["search_rounds"] = [
                item.model_dump(mode="json") for item in rounds
            ]
            staged["external_sources"] = [
                item.model_dump(mode="json") for item in external_sources
            ]
            await self._save_staged(version.id, staged, worker_owner_id)

            if not search_result.available or len(rounds) >= MAX_SEARCH_ROUNDS:
                break
            decision = await self._next_search_decision(
                version,
                day_map,
                rounds,
                external_sources,
                provider_snapshot,
                worker_owner_id,
            )

        if terminal_decision is None:
            if decision.action == "search":
                if len(rounds) >= MAX_SEARCH_ROUNDS:
                    decision = NativeSearchDecision(
                        action="finalize",
                        rationale=(
                            "Search round limit reached; finalize with available sources."
                        ),
                    )
                else:
                    decision = NativeSearchDecision(
                        action="finalize",
                        rationale=(
                            "Native search was unavailable; continue from audio only."
                        ),
                    )
            staged["search_phase"] = {
                "status": "finalized",
                "decision": decision.model_dump(mode="json"),
                "completed_rounds": len(rounds),
            }
            await self._save_staged(version.id, staged, worker_owner_id)

        retry_from_empty_checkpoint = False
        if "autonomous" in staged:
            raw_result = AutonomousAnalysisResult.model_validate(staged["autonomous"])
            try:
                result = self._validated_canonical_autonomous_result(
                    raw_result, transcript
                )
            except ValueError:
                if not self._canonical_autonomous_result_is_empty(
                    raw_result, transcript
                ):
                    raise
                staged.pop("autonomous")
                await self._save_staged(version.id, staged, worker_owner_id)
                retry_from_empty_checkpoint = True
            else:
                self._validate_external_source_references(result, external_sources)
                return result

        for attempt in range(1 if retry_from_empty_checkpoint else 2):
            request = self.composer.compose_autonomous_final_analysis(
                transcript=transcript,
                day_map=day_map,
                external_sources=external_sources,
                profile=profile,
                schema=AutonomousAnalysisResult.model_json_schema(),
                semantic_retry=retry_from_empty_checkpoint or attempt > 0,
            )
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            raw_result = await self.provider.analyze_autonomous_final_analysis(
                request,
                provider_snapshot,
                persisted_sources=external_sources,
            )
            try:
                result = self._validated_canonical_autonomous_result(
                    raw_result, transcript
                )
            except ValueError as exc:
                if attempt == 0 and not retry_from_empty_checkpoint:
                    continue
                raise ProviderAnalysisError(
                    "Autonomous final evidence is invalid",
                    code="autonomous_final_evidence_invalid",
                ) from exc
            self._validate_external_source_references(result, external_sources)
            staged["autonomous"] = result.model_dump(mode="json")
            await self._save_staged(version.id, staged, worker_owner_id)
            return result
        raise AssertionError("autonomous final analysis retry loop exhausted")

    async def _native_search_with_retry(
        self,
        *,
        provider_snapshot: dict[str, object],
        decision: NativeSearchDecision,
        round_number: int,
    ):
        last_error: ProviderAnalysisError | None = None
        for attempt in range(2):
            try:
                result = await self.provider.native_search(
                    str(provider_snapshot["provider_id"]),
                    queries=[item.query for item in decision.queries],
                    round_number=round_number,
                    model_id=str(provider_snapshot.get("model_id") or "") or None,
                )
                if result.retriable and attempt == 0:
                    await asyncio.sleep(0.25 * (2**attempt))
                    continue
                return result
            except ProviderAnalysisError as exc:
                last_error = exc
                if not exc.retriable:
                    raise
                if attempt == 1:
                    return NativeSearchCallResult(
                        provider_id=str(provider_snapshot["provider_id"]),
                        model_id=(
                            str(provider_snapshot.get("model_id") or "unknown")
                        ),
                        tool_name=None,
                        available=False,
                        errors=("Native web search remained temporarily unavailable.",),
                        retriable=True,
                    )
                await asyncio.sleep(0.25 * (2**attempt))
        raise last_error or ProviderAnalysisError("Native search request failed")

    async def _next_search_decision(
        self,
        version,
        day_map,
        rounds,
        external_sources,
        provider_snapshot,
        worker_owner_id,
    ) -> NativeSearchDecision:
        request = self.composer.compose_autonomous_search_loop(
            day_map=day_map,
            search_rounds=rounds,
            external_sources=external_sources,
            remaining_rounds=MAX_SEARCH_ROUNDS - len(rounds),
            schema=NativeSearchDecision.model_json_schema(),
        )
        await self._require_ownership(version.id, worker_owner_id)
        await self._require_generation(version, worker_owner_id)
        return await self.provider.analyze_autonomous_search_loop(
            request, provider_snapshot
        )

    @staticmethod
    def _staged_terminal_decision(
        staged: dict[str, object], rounds: list[SearchRound]
    ) -> NativeSearchDecision | None:
        raw_phase = staged.get("search_phase")
        if raw_phase is None:
            return None
        if not isinstance(raw_phase, dict):
            raise ProviderAnalysisError(
                "Stored autonomous search phase is invalid",
                code="autonomous_search_state_invalid",
            )
        try:
            decision = NativeSearchDecision.model_validate(raw_phase.get("decision"))
        except ValidationError as exc:
            raise ProviderAnalysisError(
                "Stored autonomous search phase is invalid",
                code="autonomous_search_state_invalid",
            ) from exc
        if (
            raw_phase.get("status") != "finalized"
            or decision.action != "finalize"
            or raw_phase.get("completed_rounds") != len(rounds)
        ):
            raise ProviderAnalysisError(
                "Stored autonomous search phase is invalid",
                code="autonomous_search_state_invalid",
            )
        return decision

    @staticmethod
    def _validate_day_map_evidence(
        day_map: AutonomousDayMap, transcript: list[dict[str, object]]
    ) -> None:
        segment_ids = {str(item["segment_id"]) for item in transcript}
        file_ids = {str(item["file_id"]) for item in transcript}
        if any(
            not set(scene.evidence_segment_ids).issubset(segment_ids)
            or not set(scene.file_ids).issubset(file_ids)
            for scene in day_map.scenes
        ):
            raise ProviderAnalysisError(
                "Autonomous Day Map references unknown transcript evidence",
                code="autonomous_day_map_evidence_invalid",
            )

    @staticmethod
    def _pending_search_round(rounds: list[SearchRound]) -> SearchRound | None:
        if not rounds:
            return None
        latest = rounds[-1]
        if (
            latest.decision.action == "search"
            and not latest.results
            and not latest.sources
            and not latest.errors
        ):
            return latest
        return None

    @classmethod
    def _staged_search_state(
        cls, staged: dict[str, object]
    ) -> tuple[list[SearchRound], list[ExternalSource]]:
        raw_rounds = staged.get("search_rounds", [])
        raw_sources = staged.get("external_sources", [])
        if not isinstance(raw_rounds, list) or not isinstance(raw_sources, list):
            raise ProviderAnalysisError(
                "Stored autonomous search state is invalid",
                code="autonomous_search_state_invalid",
            )
        try:
            rounds = [
                validate_search_round(SearchRound.model_validate(item))
                for item in raw_rounds
            ]
            external_sources = cls._canonical_external_sources(
                [ExternalSource.model_validate(item) for item in raw_sources]
            )
        except (ValueError, ValidationError) as exc:
            raise ProviderAnalysisError(
                "Stored autonomous search state is invalid",
                code="autonomous_search_state_invalid",
            ) from exc
        if [item.round_number for item in rounds] != list(range(1, len(rounds) + 1)):
            raise ProviderAnalysisError(
                "Stored autonomous search rounds are not contiguous",
                code="autonomous_search_state_invalid",
            )
        expected = cls._canonical_external_sources(
            [source for item in rounds for source in item.sources]
        )
        if [item.model_dump(mode="json") for item in external_sources] != [
            item.model_dump(mode="json") for item in expected
        ]:
            raise ProviderAnalysisError(
                "Stored external sources do not match completed search rounds",
                code="autonomous_search_state_invalid",
            )
        return rounds, external_sources

    @staticmethod
    def _canonical_external_sources(
        sources: list[ExternalSource],
    ) -> list[ExternalSource]:
        canonical: dict[str, ExternalSource] = {}
        for source in sources:
            previous = canonical.get(source.source_id)
            if previous is None:
                canonical[source.source_id] = source
                continue
            if source.model_dump(exclude={"search_round"}) != previous.model_dump(
                exclude={"search_round"}
            ):
                raise ProviderAnalysisError(
                    "Conflicting external sources share a source ID",
                    code="autonomous_search_state_invalid",
                )
            if source.search_round < previous.search_round:
                canonical[source.source_id] = source
        return list(canonical.values())

    @staticmethod
    def _validate_external_source_references(
        result: AutonomousAnalysisResult, external_sources: list[ExternalSource]
    ) -> None:
        allowed = {item.source_id for item in external_sources}
        referenced = {
            source_id for card in result.cards for source_id in card.external_source_ids
        }
        if not referenced.issubset(allowed):
            raise ProviderAnalysisError(
                "Final cards reference an unknown external source",
                code="autonomous_final_source_invalid",
            )

    @staticmethod
    def _filter_external_profile_candidates(
        candidates: list[dict[str, object]], external_sources: list[ExternalSource]
    ) -> list[dict[str, object]]:
        external_values = {
            value
            for source in external_sources
            for value in (
                source.url,
                source.title,
                source.publisher,
                source.published_at,
                source.support_statement,
            )
            if isinstance(value, str) and value
        }
        retained: list[dict[str, object]] = []
        for candidate in candidates:
            serialized = json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            if re.search(r"https?://", serialized, flags=re.IGNORECASE):
                continue
            if any(value in serialized for value in external_values):
                continue
            retained.append(candidate)
        return retained

    async def _direct_autonomous(
        self, version, transcript, profile, provider_snapshot, staged, worker_owner_id
    ) -> AutonomousAnalysisResult:
        retry_from_empty_checkpoint = False
        if "autonomous" in staged:
            raw_result = AutonomousAnalysisResult.model_validate(staged["autonomous"])
            try:
                result = self._validated_canonical_autonomous_result(
                    raw_result, transcript
                )
            except ValueError:
                if not self._canonical_autonomous_result_is_empty(
                    raw_result, transcript
                ):
                    raise
                staged.pop("autonomous")
                await self._save_staged(version.id, staged, worker_owner_id)
                retry_from_empty_checkpoint = True
            else:
                return result

        for attempt in range(1 if retry_from_empty_checkpoint else 2):
            request = self.composer.compose_autonomous_analysis(
                transcript=transcript, profile=profile,
                schema=AutonomousAnalysisResult.model_json_schema(),
                semantic_retry=retry_from_empty_checkpoint or attempt > 0,
            )
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            raw_result = await self.provider.analyze_autonomous(request, provider_snapshot)
            try:
                result = self._validated_canonical_autonomous_result(
                    raw_result, transcript
                )
            except ValueError as exc:
                if attempt == 0 and not retry_from_empty_checkpoint:
                    continue
                raise ProviderAnalysisError(
                    "Autonomous result evidence is invalid",
                    code="autonomous_evidence_invalid",
                ) from exc
            staged["autonomous"] = result.model_dump(mode="json")
            await self._save_staged(version.id, staged, worker_owner_id)
            return result
        raise AssertionError("autonomous direct analysis retry loop exhausted")

    async def _long_autonomous(
        self, version, context: LongContextPlan, transcript, profile,
        provider_snapshot, staged, worker_owner_id,
    ) -> tuple[AutonomousAnalysisResult, list[dict[str, object]]]:
        raw_notes = staged.get("autonomous_notes")
        note_payloads = (
            dict(raw_notes)
            if isinstance(raw_notes, dict)
            else self._json_object(raw_notes)
        )
        notebooks: list[InformationNotebook] = []
        for window in context.windows:
            note_was_staged = window.window_id in note_payloads
            if note_was_staged:
                notebook = InformationNotebook.model_validate(note_payloads[window.window_id])
            else:
                request = self.composer.compose_autonomous_notes(
                    window=window, profile=profile,
                    schema=InformationNotebook.model_json_schema(),
                )
                await self._require_ownership(version.id, worker_owner_id)
                await self._require_generation(version, worker_owner_id)
                notebook = await self.provider.analyze_autonomous_notes(
                    request, provider_snapshot
                )
            allowed = {str(item["segment_id"]) for item in window.segments}
            if notebook.window_id != window.window_id or any(
                not note.evidence_segment_ids
                or not set(note.evidence_segment_ids).issubset(allowed)
                for note in notebook.notes
            ):
                raise ProviderAnalysisError(
                    "Information notebook evidence is invalid",
                    code="autonomous_notes_evidence_invalid",
                )
            if not note_was_staged:
                note_payloads[window.window_id] = notebook.model_dump(mode="json")
                staged["autonomous_notes"] = note_payloads
                await self._save_staged(version.id, staged, worker_owner_id)
            notebooks.append(notebook)

        retrieval_was_staged = "autonomous_retrieval_plan" in staged
        if retrieval_was_staged:
            retrieval = AutonomousRetrievalPlan.model_validate(
                staged["autonomous_retrieval_plan"]
            )
        else:
            notebook_payloads = [item.model_dump(mode="json") for item in notebooks]
        note_ids = {
            segment_id for notebook in notebooks for note in notebook.notes
            for segment_id in note.evidence_segment_ids
        }
        if not retrieval_was_staged:
            for attempt in range(2):
                request = self.composer.compose_autonomous_retrieval_plan(
                    notebooks=notebook_payloads, profile=profile,
                    schema=AutonomousRetrievalPlan.model_json_schema(),
                    allowed_segment_ids=sorted(note_ids),
                    semantic_retry=attempt > 0,
                )
                await self._require_ownership(version.id, worker_owner_id)
                await self._require_generation(version, worker_owner_id)
                retrieval = await self.provider.analyze_autonomous_retrieval_plan(
                    request, provider_snapshot
                )
                requested = {
                    segment_id for card in retrieval.cards
                    for segment_id in card.required_segment_ids
                }
                if requested and requested.issubset(note_ids):
                    break
                if attempt == 1:
                    raise ProviderAnalysisError(
                        "Autonomous retrieval plan requested invalid evidence",
                        code="autonomous_retrieval_evidence_invalid",
                    )
        requested_ids = {
            segment_id for card in retrieval.cards
            for segment_id in card.required_segment_ids
        }
        if not requested_ids or not requested_ids.issubset(note_ids):
            raise ProviderAnalysisError(
                "Autonomous retrieval plan requested invalid evidence",
                code="autonomous_retrieval_evidence_invalid",
            )
        if not retrieval_was_staged:
            staged["autonomous_retrieval_plan"] = retrieval.model_dump(mode="json")
            await self._save_staged(version.id, staged, worker_owner_id)
        retrieved = [
            item for item in transcript if str(item["segment_id"]) in requested_ids
        ]
        retry_from_empty_checkpoint = False
        if "autonomous" in staged:
            raw_result = AutonomousAnalysisResult.model_validate(staged["autonomous"])
            try:
                result = self._validated_canonical_autonomous_result(
                    raw_result, retrieved
                )
            except ValueError:
                if not self._canonical_autonomous_result_is_empty(
                    raw_result, retrieved
                ):
                    raise
                staged.pop("autonomous")
                await self._save_staged(version.id, staged, worker_owner_id)
                retry_from_empty_checkpoint = True
            else:
                return result, retrieved

        notebook_payloads = [item.model_dump(mode="json") for item in notebooks]
        for attempt in range(1 if retry_from_empty_checkpoint else 2):
            request = self.composer.compose_autonomous_final(
                transcript=retrieved, notebooks=notebook_payloads,
                retrieval_plan=retrieval.model_dump(mode="json"), profile=profile,
                schema=AutonomousAnalysisResult.model_json_schema(),
                semantic_retry=retry_from_empty_checkpoint or attempt > 0,
            )
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            raw_result = await self.provider.analyze_autonomous_final(
                request, provider_snapshot
            )
            try:
                result = self._validated_canonical_autonomous_result(
                    raw_result, retrieved
                )
            except ValueError as exc:
                if attempt == 0 and not retry_from_empty_checkpoint:
                    continue
                raise ProviderAnalysisError(
                    "Autonomous final evidence is invalid",
                    code="autonomous_final_evidence_invalid",
                ) from exc
            staged["autonomous"] = result.model_dump(mode="json")
            await self._save_staged(version.id, staged, worker_owner_id)
            return result, retrieved
        raise AssertionError("autonomous long analysis retry loop exhausted")
    @staticmethod
    def _validate_autonomous_evidence(
        result: AutonomousAnalysisResult,
        transcript: list[dict[str, object]],
    ) -> None:
        if sum(len(str(item["text"])) for item in transcript) >= 1_000 and not result.cards:
            raise ValueError("large transcript produced no autonomous cards")
        lookup = {str(item["segment_id"]): str(item["text"]) for item in transcript}
        for card in result.cards:
            evidenced = [card, *card.content, *card.quotes, *card.recommendations]
            for item in evidenced:
                if not set(item.evidence_segment_ids).issubset(lookup):
                    raise ValueError("unknown autonomous evidence segment")
            for quote in card.quotes:
                source = "".join(lookup[item] for item in quote.evidence_segment_ids)
                normalized_quote = re.sub(r"[^\w\u4e00-\u9fff]", "", quote.quote)
                normalized_source = re.sub(r"[^\w\u4e00-\u9fff]", "", source)
                if not normalized_quote or normalized_quote not in normalized_source:
                    raise ValueError("autonomous quote is not verbatim evidence")

    @classmethod
    def _validated_canonical_autonomous_result(
        cls,
        result: AutonomousAnalysisResult,
        transcript: list[dict[str, object]],
    ) -> AutonomousAnalysisResult:
        cls._validate_autonomous_evidence(result, transcript)
        canonical = cls._sanitize_autonomous_evidence(result, transcript)
        cls._validate_autonomous_evidence(canonical, transcript)
        return canonical

    @classmethod
    def _canonical_autonomous_result_is_empty(
        cls,
        result: AutonomousAnalysisResult,
        transcript: list[dict[str, object]],
    ) -> bool:
        canonical = cls._sanitize_autonomous_evidence(result, transcript)
        return (
            sum(len(str(item["text"])) for item in transcript) >= 1_000
            and not canonical.cards
        )

    @staticmethod
    def _sanitize_autonomous_evidence(
        result: AutonomousAnalysisResult,
        transcript: list[dict[str, object]],
    ) -> AutonomousAnalysisResult:
        lookup = {str(item["segment_id"]): str(item["text"]) for item in transcript}
        cleaned = result.model_copy(deep=True)
        retained_cards = []
        for card in cleaned.cards:
            card.evidence_segment_ids = [
                item for item in card.evidence_segment_ids if item in lookup
            ]
            for item in [*card.content, *card.recommendations]:
                item.evidence_segment_ids = [
                    segment_id
                    for segment_id in item.evidence_segment_ids
                    if segment_id in lookup
                ]
            retained_quotes = []
            for quote in card.quotes:
                quote.evidence_segment_ids = [
                    item for item in quote.evidence_segment_ids if item in lookup
                ]
                source = "".join(lookup[item] for item in quote.evidence_segment_ids)
                normalized_quote = re.sub(r"[^\w\u4e00-\u9fff]", "", quote.quote)
                normalized_source = re.sub(r"[^\w\u4e00-\u9fff]", "", source)
                if normalized_quote and normalized_quote in normalized_source:
                    retained_quotes.append(quote)
            card.quotes = retained_quotes
            supported = {
                evidence_id
                for item in [card, *card.content, *card.quotes, *card.recommendations]
                for evidence_id in item.evidence_segment_ids
            }
            if supported:
                card.evidence_segment_ids = list(
                    dict.fromkeys([*card.evidence_segment_ids, *supported])
                )
                retained_cards.append(card)
        cleaned.cards = retained_cards
        return cleaned

    async def _event_map(
        self,
        version: AnalysisVersion,
        transcript: list[dict[str, object]],
        profile: list[dict[str, object]],
        provider_snapshot: dict[str, object],
        worker_owner_id: str | None,
    ) -> EventMap:
        if version.event_map_json:
            return EventMap.model_validate_json(version.event_map_json)
        windows = build_analysis_windows(transcript)
        completed_maps: list[EventMap] = []
        for window in windows:
            completed_maps.append(
                await self._completed_window_event_map(
                    version,
                    window,
                    profile,
                    provider_snapshot,
                    worker_owner_id,
                )
            )
        try:
            event_map = merge_window_event_maps(windows, completed_maps)
        except AnalysisWindowError as exc:
            raise ProviderAnalysisError(
                "Merged event map evidence is invalid",
                code=exc.code,
            ) from exc

        transcript_ids = {str(item["segment_id"]) for item in transcript}
        assigned = {
            segment_id
            for event in event_map.events
            for segment_id in event.evidence_segment_ids
        }
        covered = assigned | set(event_map.unassigned_segment_ids)
        if covered != transcript_ids:
            raise ProviderAnalysisError(
                "Event map coverage is incomplete",
                code="event_map_coverage_invalid",
            )
        logger.info(
            "event_map_coverage windows=%d events=%d known=%d assigned=%d "
            "unassigned=%d unknown=0",
            len(windows),
            len(event_map.events),
            len(transcript_ids),
            len(assigned),
            len(event_map.unassigned_segment_ids),
        )
        serialized = json.dumps(
            event_map.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        async with self.database.session() as session:
            statement = (
                update(AnalysisVersion)
                .where(
                    AnalysisVersion.id == version.id,
                    AnalysisVersion.status == "running",
                )
                .values(
                    event_map_json=serialized,
                    event_map_hash=sha256(serialized.encode("utf-8")).hexdigest(),
                )
            )
            if worker_owner_id is not None:
                statement = statement.where(
                    AnalysisVersion.worker_owner_id == worker_owner_id
                )
            stored = await session.execute(statement)
            if int(stored.rowcount) != 1:
                await session.rollback()
                raise LeaseLostError("Analysis worker lease was lost")
            await session.commit()
        version.event_map_json = serialized
        version.event_map_hash = sha256(serialized.encode("utf-8")).hexdigest()
        return event_map

    async def _completed_window_event_map(
        self,
        version: AnalysisVersion,
        window: AnalysisWindow,
        profile: list[dict[str, object]],
        provider_snapshot: dict[str, object],
        worker_owner_id: str | None,
    ) -> EventMap:
        for attempt in range(EVENT_MAP_SEMANTIC_REPAIR_ATTEMPTS + 1):
            request = self.composer.compose_event_map(
                transcript=list(window.segments),
                profile=profile,
                schema=EventMapDraft.model_json_schema(),
                window_id=window.window_id,
                semantic_retry=attempt > 0,
            )
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            generated = await self.provider.analyze_event_map(
                request, provider_snapshot
            )
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            raw_event_map = (
                generated.model_dump(mode="python")
                if hasattr(generated, "model_dump")
                else generated
            )
            if isinstance(generated, EventMap):
                raw_event_map.pop("unassigned_segment_ids", None)
            try:
                local_map = EventMapDraft.model_validate(raw_event_map)
            except ValidationError as exc:
                raise ProviderAnalysisError(
                    "Event map violates the required schema",
                    code="event_map_schema_invalid",
                ) from exc
            try:
                return complete_window_event_map(window, local_map)
            except AnalysisWindowError as exc:
                if (
                    exc.code == "event_map_unknown_segment"
                    and attempt < EVENT_MAP_SEMANTIC_REPAIR_ATTEMPTS
                ):
                    logger.warning(
                        "event_map_semantic_repair window_id=%s attempt=%d "
                        "known_segments=%d reason=%s",
                        window.window_id,
                        attempt + 1,
                        len(window.segments),
                        exc.code,
                    )
                    continue
                raise ProviderAnalysisError(
                    "Local event map evidence is invalid",
                    code=exc.code,
                ) from exc
        raise AssertionError("event map semantic repair loop exhausted")

    async def _scene_context(
        self,
        version: AnalysisVersion,
        transcript: list[dict[str, object]],
        event_map: EventMap,
        provider_snapshot: dict[str, object],
        staged: dict[str, object],
        worker_owner_id: str | None,
    ) -> tuple[EventMap, list[SceneDossier]]:
        segment_lookup = {
            str(item["segment_id"]): item for item in transcript
        }
        stored_context = staged.get("_scene_context")
        if stored_context is not None:
            if not isinstance(stored_context, dict):
                raise ProviderAnalysisError(
                    "Stored scene context is invalid",
                    code="scene_dossier_invalid",
                )
            raw_dossiers = stored_context.get("dossiers")
            if not isinstance(raw_dossiers, list):
                raise ProviderAnalysisError(
                    "Stored scene dossiers are invalid",
                    code="scene_dossier_invalid",
                )
            try:
                dossiers = [
                    SceneDossier.model_validate(item) for item in raw_dossiers
                ]
            except ValidationError as exc:
                raise ProviderAnalysisError(
                    "Stored scene dossiers violate the required schema",
                    code="scene_dossier_invalid",
                ) from exc
            known_events = {event.event_id for event in event_map.events}
            for dossier in dossiers:
                if not set(dossier.allowed_segment_ids).issubset(segment_lookup):
                    raise ProviderAnalysisError(
                        "Stored scene dossier references unknown evidence",
                        code="scene_dossier_invalid",
                    )
                if dossier.primary_event_id not in known_events or not set(
                    dossier.source_event_ids
                ).issubset(known_events):
                    raise ProviderAnalysisError(
                        "Stored scene dossier references unknown events",
                        code="scene_dossier_invalid",
                    )
            return event_map, dossiers

        clusters = build_transcript_clusters(transcript)
        director_results: list[tuple[str, DirectorResult]] = []
        for cluster in clusters:
            request = self.composer.compose_director(
                cluster=cluster,
                event_hints=event_hints_for_cluster(
                    cluster, event_map, segment_lookup
                ),
                schema=DirectorResult.model_json_schema(),
            )
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            generated = await self.provider.analyze_director(
                request, provider_snapshot
            )
            await self._require_ownership(version.id, worker_owner_id)
            await self._require_generation(version, worker_owner_id)
            try:
                validated = DirectorResult.model_validate(
                    generated.model_dump(mode="python")
                    if hasattr(generated, "model_dump")
                    else generated
                )
            except ValidationError as exc:
                raise ProviderAnalysisError(
                    "Director output violates the required schema",
                    code="director_schema_invalid",
                ) from exc
            director_results.append((cluster.cluster_id, validated))

        try:
            selections = normalize_director_results(
                clusters=clusters,
                event_map=event_map,
                results=director_results,
            )
            anchored_map, anchored = attach_event_anchors(
                selections=selections,
                clusters=clusters,
                event_map=event_map,
                segment_lookup=segment_lookup,
            )
            dossiers = build_scene_dossiers(
                selections=anchored,
                clusters=clusters,
            )
        except (DirectorSelectionError, DossierBuildError, ValidationError) as exc:
            raise ProviderAnalysisError(
                "Director selection or scene dossier is invalid",
                code=getattr(exc, "code", "director_selection_invalid"),
            ) from exc

        if not dossiers and self._requires_director_selection(transcript):
            raise ProviderAnalysisError(
                "Director returned no valuable selection for a large transcript",
                code="analysis_quality_insufficient",
            )

        staged["_scene_context"] = {
            "selections": [
                {
                    "selection": item.selection.model_dump(mode="json"),
                    "primary_event_id": item.primary_event_id,
                    "source_event_ids": list(item.source_event_ids),
                }
                for item in anchored
            ],
            "dossiers": [
                dossier.model_dump(mode="json") for dossier in dossiers
            ],
        }
        await self._save_scene_context(
            version,
            anchored_map,
            staged,
            worker_owner_id,
        )
        logger.info(
            "scene_context_coverage clusters=%d selections=%d dossiers=%d "
            "known=%d allowed_unique=%d",
            len(clusters),
            len(selections),
            len(dossiers),
            len(segment_lookup),
            len(
                {
                    segment_id
                    for dossier in dossiers
                    for segment_id in dossier.allowed_segment_ids
                }
            ),
        )
        return anchored_map, dossiers

    async def _save_scene_context(
        self,
        version: AnalysisVersion,
        event_map: EventMap,
        staged: dict[str, object],
        worker_owner_id: str | None,
    ) -> None:
        serialized = json.dumps(
            event_map.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        event_map_hash = sha256(serialized.encode("utf-8")).hexdigest()
        staged_json = json.dumps(
            staged, ensure_ascii=False, separators=(",", ":")
        )
        async with self.database.session() as session:
            statement = (
                update(AnalysisVersion)
                .where(
                    AnalysisVersion.id == version.id,
                    AnalysisVersion.status == "running",
                )
                .values(
                    event_map_json=serialized,
                    event_map_hash=event_map_hash,
                    staged_results_json=staged_json,
                )
            )
            if worker_owner_id is not None:
                statement = statement.where(
                    AnalysisVersion.worker_owner_id == worker_owner_id
                )
            stored = await session.execute(statement)
            if int(stored.rowcount) != 1:
                await session.rollback()
                raise LeaseLostError("Analysis worker lease was lost")
            await session.commit()
        version.event_map_json = serialized
        version.event_map_hash = event_map_hash
        version.staged_results_json = staged_json

    @staticmethod
    def _requires_director_selection(
        transcript: list[dict[str, object]],
    ) -> bool:
        if sum(len(str(item.get("text", ""))) for item in transcript) >= 10_000:
            return True
        file_bounds: dict[str, tuple[int, int]] = {}
        for item in transcript:
            file_id = str(item["file_id"])
            start_ms = int(item["start_ms"])
            end_ms = int(item["end_ms"])
            if file_id not in file_bounds:
                file_bounds[file_id] = (start_ms, end_ms)
            else:
                earliest, latest = file_bounds[file_id]
                file_bounds[file_id] = (
                    min(earliest, start_ms),
                    max(latest, end_ms),
                )
        return sum(end - start for start, end in file_bounds.values()) >= 7_200_000

    async def _transcript(self, job_id: str) -> list[dict[str, object]]:
        async with self.database.session() as session:
            review_pending = bool(
                await session.scalar(select(pending_risk_review_exists(job_id)))
            )
            if review_pending:
                raise ValueError("Analysis requires a completed transcript")
            rows = list(
                await session.scalars(
                    select(Transcript)
                    .join(JobFile, JobFile.id == Transcript.job_file_id)
                    .where(
                        JobFile.job_id == job_id,
                        Transcript.risk_classified.is_(True),
                        Transcript.is_reliable.is_(True),
                    )
                    .order_by(JobFile.position, Transcript.segment_index)
                )
            )
            files = {
                item.id: item
                for item in await session.scalars(
                    select(JobFile).where(JobFile.job_id == job_id)
                )
            }
        if not rows:
            raise ValueError("Analysis requires a completed transcript")
        file_positions = {file.id: file.position for file in files.values()}
        structured: list[dict[str, object]] = []
        for row in rows:
            file = files[row.job_file_id]
            started = file.recording_started_at
            local_date = started[:10] if started else None
            structured.append(
                {
                    "segment_id": f"seg_{file_positions[file.id]}_{row.segment_index}",
                    "file_id": file.id,
                    "file_name": file.original_name,
                    "recording_started_at": started,
                    "local_date": local_date,
                    "timezone": file.timezone,
                    "start_ms": row.start_ms,
                    "end_ms": row.end_ms,
                    "text": row.text,
                    "reliability_weight": row.reliability_weight,
                }
            )
        return structured

    async def _require_generation(
        self, version: AnalysisVersion, worker_owner_id: str | None
    ) -> None:
        current = await self.generation_source.credential_generation(
            version.provider_id
        )
        if current == version.credential_generation:
            return
        await self._mark_credential_changed(version, worker_owner_id)

    async def _mark_credential_changed(
        self, version: AnalysisVersion, worker_owner_id: str | None
    ) -> None:
        async with self.database.session() as session:
            statement = (
                update(AnalysisVersion)
                .where(
                    AnalysisVersion.id == version.id,
                    AnalysisVersion.status == "running",
                )
                .values(
                    status="credential_changed",
                    error_code="credential_changed",
                    staged_results_json="{}",
                )
            )
            if worker_owner_id is not None:
                statement = statement.where(
                    AnalysisVersion.worker_owner_id == worker_owner_id
                )
            transitioned = await session.execute(statement)
            if int(transitioned.rowcount) != 1:
                await session.rollback()
                raise LeaseLostError("Analysis worker lease was lost")
            stored = await session.get(AnalysisVersion, version.id)
            if stored is not None:
                if stored.reanalysis_batch_id is not None:
                    history = await session.get(
                        ReanalysisBatch, stored.reanalysis_batch_id
                    )
                    if history is not None:
                        history.status = "paused"
                    item = await session.scalar(
                        select(ReanalysisItem).where(
                            ReanalysisItem.analysis_version_id == stored.id
                        )
                    )
                    if item is not None:
                        item.status = "pending"
                        item.error_code = "credential_changed"
                else:
                    job = await session.get(AnalysisJob, stored.source_job_id)
                    if job is not None:
                        job.stage = "failed"
                        job.error_code = "credential_changed"
                await session.commit()
        raise CredentialChangedError(
            f"Credential generation changed for {version.provider_id}"
        )

    async def _require_fixed_rules(
        self, version: AnalysisVersion, worker_owner_id: str | None
    ) -> None:
        current_hash = PromptComposer.fixed_rules_hash()
        if version.fixed_rules_hash == current_hash:
            return
        async with self.database.session() as session:
            statement = (
                update(AnalysisVersion)
                .where(
                    AnalysisVersion.id == version.id,
                    AnalysisVersion.status == "running",
                )
                .values(
                    status="fixed_rules_changed",
                    error_code="fixed_rules_changed",
                    event_map_json=None,
                    event_map_hash=None,
                    staged_results_json="{}",
                )
            )
            if worker_owner_id is not None:
                statement = statement.where(
                    AnalysisVersion.worker_owner_id == worker_owner_id
                )
            transitioned = await session.execute(statement)
            if int(transitioned.rowcount) != 1:
                await session.rollback()
                raise LeaseLostError("Analysis worker lease was lost")
            stored = await session.get(AnalysisVersion, version.id)
            if stored is not None:
                if stored.reanalysis_batch_id is not None:
                    history = await session.get(
                        ReanalysisBatch, stored.reanalysis_batch_id
                    )
                    if history is not None:
                        history.status = "paused"
                    item = await session.scalar(
                        select(ReanalysisItem).where(
                            ReanalysisItem.analysis_version_id == stored.id
                        )
                    )
                    if item is not None:
                        item.status = "pending"
                        item.error_code = "fixed_rules_changed"
                else:
                    job = await session.get(AnalysisJob, stored.source_job_id)
                    if job is not None:
                        job.stage = "failed"
                        job.error_code = "fixed_rules_changed"
                await session.commit()
        raise FixedRulesChangedError("Fixed analysis rules changed")

    async def _version(
        self, version_id: str, worker_owner_id: str | None
    ) -> AnalysisVersion:
        async with self.database.session() as session:
            version = await session.get(AnalysisVersion, version_id)
            if version is None:
                raise LookupError(f"Unknown analysis version: {version_id}")
            if version.status != "running":
                raise ValueError(f"Analysis version is not running: {version.status}")
            if (
                worker_owner_id is not None
                and version.worker_owner_id != worker_owner_id
            ):
                raise LeaseLostError("Analysis worker lease was lost")
            return version

    async def _require_ownership(
        self, version_id: str, worker_owner_id: str | None
    ) -> None:
        if worker_owner_id is None:
            return
        async with self.database.session() as session:
            owned = await session.scalar(
                select(AnalysisVersion.id).where(
                    AnalysisVersion.id == version_id,
                    AnalysisVersion.status == "running",
                    AnalysisVersion.worker_owner_id == worker_owner_id,
                )
            )
        if owned is None:
            raise LeaseLostError("Analysis worker lease was lost")

    async def _save_staged(
        self,
        version_id: str,
        staged: dict[str, object],
        worker_owner_id: str | None,
    ) -> None:
        async with self.database.session() as session:
            statement = (
                update(AnalysisVersion)
                .where(
                    AnalysisVersion.id == version_id,
                    AnalysisVersion.status == "running",
                )
                .values(
                    staged_results_json=json.dumps(
                        staged, ensure_ascii=False, separators=(",", ":")
                    )
                )
            )
            if worker_owner_id is not None:
                statement = statement.where(
                    AnalysisVersion.worker_owner_id == worker_owner_id
                )
            stored = await session.execute(statement)
            if int(stored.rowcount) != 1:
                await session.rollback()
                raise LeaseLostError("Analysis worker lease was lost")
            await session.commit()

    async def _save_profile_candidates(
        self,
        version_id: str,
        raw_candidates: list[dict[str, object]],
        segment_ids: set[str],
        worker_owner_id: str | None,
    ) -> list[dict[str, object]]:
        accepted: list[ProfileCandidate] = []
        verified: list[dict[str, object]] = []
        for item in raw_candidates:
            if item.get("subject_id") != "user":
                continue
            dimension = item.get("dimension")
            value = item.get("value")
            confidence = item.get("confidence")
            evidence = item.get("evidence_segment_ids", [])
            if (
                not isinstance(dimension, str)
                or not isinstance(value, dict)
                or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1
                or not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(item_id, str) for item_id in evidence)
                or not set(evidence).issubset(segment_ids)
            ):
                continue
            accepted.append(
                ProfileCandidate(
                    id=str(uuid4()),
                    analysis_version_id=version_id,
                    subject_id="user",
                    dimension=dimension,
                    value_json=json.dumps(
                        value, ensure_ascii=False, separators=(",", ":")
                    ),
                    confidence=float(confidence),
                    evidence_segment_ids_json=json.dumps(
                        evidence, ensure_ascii=False, separators=(",", ":")
                    ),
                    origin="explicit" if item.get("explicit") else "inferred",
                )
            )
            verified.append(item)
        async with self.database.session() as session:
            if worker_owner_id is not None:
                fenced = await session.execute(
                    update(AnalysisVersion)
                    .where(
                        AnalysisVersion.id == version_id,
                        AnalysisVersion.status == "running",
                        AnalysisVersion.worker_owner_id == worker_owner_id,
                    )
                    .values(worker_owner_id=worker_owner_id)
                )
                if int(fenced.rowcount) != 1:
                    await session.rollback()
                    raise LeaseLostError("Analysis worker lease was lost")
            await session.execute(
                delete(ProfileCandidate).where(
                    ProfileCandidate.analysis_version_id == version_id
                )
            )
            session.add_all(accepted)
            await session.commit()
        return verified

    async def _mark_failed(
        self,
        version_id: str,
        worker_owner_id: str | None,
        *,
        error_code: str = "model_analysis_failed",
        pause_history: bool = False,
    ) -> None:
        async with self.database.session() as session:
            statement = (
                update(AnalysisVersion)
                .where(
                    AnalysisVersion.id == version_id,
                    AnalysisVersion.status == "running",
                )
                .values(
                    status="provider_paused" if pause_history else "failed",
                    error_code=error_code,
                )
            )
            if worker_owner_id is not None:
                statement = statement.where(
                    AnalysisVersion.worker_owner_id == worker_owner_id
                )
            failed = await session.execute(statement)
            if int(failed.rowcount) != 1:
                await session.rollback()
                return
            version = await session.get(AnalysisVersion, version_id)
            if version is None:
                await session.rollback()
                return
            if version.reanalysis_batch_id is not None:
                item = await session.scalar(
                    select(ReanalysisItem).where(
                        ReanalysisItem.analysis_version_id == version.id
                    )
                )
                if item is not None:
                    item.status = "pending" if pause_history else "failed"
                    item.error_code = error_code
                history = await session.get(
                    ReanalysisBatch, version.reanalysis_batch_id
                )
                if history is not None and history.status != "stopping":
                    history.status = "paused" if pause_history else "running"
            job = await session.get(AnalysisJob, version.source_job_id)
            if job is not None and version.batch_id is None:
                job.stage = "failed"
                job.error_code = error_code
            await session.commit()

    @staticmethod
    def _prompt_from_snapshot(
        scene_id: str, snapshot: dict[str, object]
    ) -> PromptDocument:
        value = snapshot.get(scene_id)
        if not isinstance(value, dict):
            raise ValueError(f"Prompt snapshot is missing scene: {scene_id}")
        content = value.get("content")
        version = value.get("version", 0)
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"Prompt snapshot has invalid content: {scene_id}")
        if not isinstance(version, int):
            raise ValueError(f"Prompt snapshot has invalid version: {scene_id}")
        return PromptDocument(scene_id, version, content)

    @staticmethod
    def _json_object(raw: str) -> dict[str, object]:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_list(raw: str) -> list[dict[str, object]]:
        parsed = json.loads(raw or "[]")
        return parsed if isinstance(parsed, list) else []
