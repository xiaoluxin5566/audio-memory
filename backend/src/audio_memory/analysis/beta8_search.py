from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone

from audio_memory.prompts.beta8_pipeline_schema import (
    Beta8OrchestrationResult,
    Beta8RevisionTask,
    Beta8SearchPacket,
    Beta8SearchTask,
    canonicalize_source_url,
    normalize_search_packet,
    stable_source_id,
)


def project_search_policy(
    *, search_candidates: Sequence[Mapping[str, object]],
    orchestration: Beta8OrchestrationResult,
) -> dict[str, object]:
    """Observe the already-validated search planning/adoption policy."""
    candidate_order = [str(item["search_candidate_id"]) for item in search_candidates]
    task_keys = [task.search_task_key for task in orchestration.search_tasks]
    return {
        "candidate_input_order": candidate_order,
        "task_count": len(orchestration.search_tasks),
        "task_order": task_keys,
        "tasks": [{
            "search_task_key": task.search_task_key,
            "source_candidate_ids": task.source_candidate_ids,
            "question": task.question,
            "target_decision_keys": task.target_decision_keys,
        } for task in orchestration.search_tasks],
        "dropped_candidate_ids": [
            candidate_id for dropped in orchestration.dropped_search_candidates
            for candidate_id in dropped.source_candidate_ids
        ],
        "adoption": [{
            "decision_key": task.decision_key,
            "revision_task_key": task.revision_task_key,
            "search_task_keys": task.search_task_keys,
        } for task in orchestration.revision_tasks],
    }


def search_packets_for_revision_task(
    task: Beta8RevisionTask,
    packet_by_id: Mapping[str, Beta8SearchPacket],
) -> tuple[Beta8SearchPacket, ...]:
    return tuple(
        packet_by_id[key] for key in task.search_task_keys
        if key in packet_by_id and packet_by_id[key].status != "failed"
    )


class Beta8SearchExecutor:
    def __init__(
        self,
        provider,
        *,
        provider_id: str | None,
        model_id: str | None,
    ) -> None:
        self.provider = provider
        self.provider_id = provider_id.strip() if provider_id else None
        self.model_id = model_id.strip() if model_id else None

    def with_binding(
        self,
        provider_id: str | None,
        model_id: str | None,
    ) -> Beta8SearchExecutor:
        return Beta8SearchExecutor(
            self.provider,
            provider_id=provider_id,
            model_id=model_id,
        )

    async def execute(
        self,
        tasks: Sequence[Beta8SearchTask],
        *,
        completed: Mapping[str, Beta8SearchPacket],
        persist: Callable[[Beta8SearchPacket], Awaitable[None]],
    ) -> Sequence[Beta8SearchPacket]:
        packets: list[Beta8SearchPacket] = []
        for task in tasks:
            existing = completed.get(task.search_task_key)
            if existing is not None:
                packets.append(normalize_search_packet(existing))
                continue
            packet = await self._execute_one(task)
            await persist(packet)
            packets.append(packet)
        return tuple(packets)

    async def _execute_one(self, task: Beta8SearchTask) -> Beta8SearchPacket:
        if self.provider_id is None:
            return self._failed(task, "Beta 8 search provider is not configured")
        try:
            outcome = await self.provider.native_search(
                self.provider_id,
                queries=[task.question],
                round_number=1,
                model_id=self.model_id,
            )
        except Exception as exc:
            return self._failed(task, f"Search provider failed: {type(exc).__name__}: {exc}")
        if outcome.provider_id != self.provider_id:
            return self._failed(task, "Search provider returned a mismatched provider identity")
        if not outcome.available or not outcome.sources:
            reason = "; ".join(outcome.errors) or "Search provider returned no usable sources"
            return self._failed(task, reason)

        retrieved_at = datetime.now(timezone.utc).isoformat()
        supported_points = []
        sources = []
        unresolved_points = list(outcome.errors)
        for source in outcome.sources:
            if source.provider_id != self.provider_id:
                return self._failed(task, "Search source provider identity does not match the task")
            try:
                canonical_url = canonicalize_source_url(source.url)
            except ValueError as exc:
                unresolved_points.append(f"Search source URL is invalid: {exc}")
                continue
            source_id = stable_source_id(canonical_url)
            point_index: int | None = None
            if source.support_statement:
                point_index = len(supported_points)
                supported_points.append({
                    "point": source.support_statement,
                    "source_ids": [source_id],
                })
            sources.append({
                "source_id": source_id,
                "title": source.title,
                "url": canonical_url,
                "publisher": source.publisher,
                "published_at": source.published_at,
                "retrieved_at": retrieved_at,
                "source_type": "provider_native_search",
                "supports": [] if point_index is None else [point_index],
            })
        if not sources:
            return self._failed(
                task,
                "; ".join(unresolved_points) or "Search provider returned no publishable sources",
            )
        status = "partial" if unresolved_points else "success"
        answer = "\n".join(point["point"] for point in supported_points) or None
        packet = Beta8SearchPacket.model_validate({
            "search_task_id": task.search_task_key,
            "status": status,
            "answer": answer,
            "supported_points": supported_points,
            "unresolved_points": unresolved_points,
            "sources": sources,
            "error_summary": "; ".join(unresolved_points) if unresolved_points else None,
        })
        try:
            return normalize_search_packet(packet)
        except ValueError as exc:
            return self._failed(task, f"Search sources conflict: {exc}")

    @staticmethod
    def _failed(task: Beta8SearchTask, reason: str) -> Beta8SearchPacket:
        return Beta8SearchPacket.model_validate({
            "search_task_id": task.search_task_key,
            "status": "failed",
            "answer": None,
            "supported_points": [],
            "unresolved_points": [],
            "sources": [],
            "error_summary": reason,
        })
