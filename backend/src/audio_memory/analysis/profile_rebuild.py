from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import delete, select

from audio_memory.db import Database
from audio_memory.models import AnalysisVersion, ProfileCandidate, ProfileFact


class ProfileRebuilder:
    """Build active facts solely from the supplied current-version snapshot."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def rebuild(
        self, current_versions: Sequence[AnalysisVersion]
    ) -> list[ProfileFact]:
        version_ids = sorted({version.id for version in current_versions})
        if not version_ids:
            return []
        versions = {version.id: version for version in current_versions}
        async with self.database.session() as session:
            candidates = list(
                await session.scalars(
                    select(ProfileCandidate)
                    .where(ProfileCandidate.analysis_version_id.in_(version_ids))
                    .order_by(ProfileCandidate.id)
                )
            )
            active_facts = list(
                await session.scalars(
                    select(ProfileFact).where(ProfileFact.status == "active")
                )
            )

        candidate_version_ids = {
            candidate.analysis_version_id for candidate in candidates
        }
        missing_candidate_jobs = {
            version.source_job_id
            for version in current_versions
            if version.id not in candidate_version_ids
            and version.fixed_rules_hash == ""
        }
        retained_legacy = [
            fact
            for fact in active_facts
            if not (source_jobs := _profile_source_jobs(fact.source_audio_json))
            or bool(source_jobs & missing_candidate_jobs)
        ]

        grouped: dict[tuple[str, str, str], list[ProfileCandidate]] = defaultdict(
            list
        )
        for candidate in candidates:
            canonical_value = json.dumps(
                json.loads(candidate.value_json),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            grouped[
                (candidate.subject_id, candidate.dimension, canonical_value)
            ].append(candidate)

        facts: list[ProfileFact] = []
        for (subject_id, dimension, value_json), observations in sorted(
            grouped.items()
        ):
            observed_versions = [
                versions[item.analysis_version_id] for item in observations
            ]
            source_jobs = sorted(
                {version.source_job_id for version in observed_versions}
            )
            first_seen = min(version.created_at for version in observed_versions)
            last_seen = max(
                version.completed_at or version.created_at
                for version in observed_versions
            )
            facts.append(
                ProfileFact(
                    id=str(
                        uuid5(
                            NAMESPACE_URL,
                            f"audio-memory-profile:{subject_id}:{dimension}:{value_json}",
                        )
                    ),
                    subject_id=subject_id,
                    dimension=dimension,
                    value_json=value_json,
                    confidence=max(item.confidence for item in observations),
                    source_audio_json=json.dumps(
                        source_jobs,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    first_seen_at=first_seen,
                    last_seen_at=last_seen,
                    evidence_count=len(observations),
                    origin=(
                        "explicit"
                        if any(item.origin == "explicit" for item in observations)
                        else "inferred"
                    ),
                    status="active",
                )
            )
        by_key = {
            (fact.subject_id, fact.dimension, fact.value_json): fact
            for fact in facts
        }
        for legacy in retained_legacy:
            canonical_value = json.dumps(
                json.loads(legacy.value_json),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            key = (legacy.subject_id, legacy.dimension, canonical_value)
            current = by_key.get(key)
            legacy_sources = _profile_source_jobs(legacy.source_audio_json)
            retained_sources = legacy_sources & missing_candidate_jobs
            if current is None:
                by_key[key] = ProfileFact(
                    id=legacy.id,
                    subject_id=legacy.subject_id,
                    dimension=legacy.dimension,
                    value_json=canonical_value,
                    confidence=legacy.confidence,
                    source_audio_json=(
                        json.dumps(
                            sorted(retained_sources),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        if legacy_sources
                        else legacy.source_audio_json
                    ),
                    first_seen_at=legacy.first_seen_at,
                    last_seen_at=legacy.last_seen_at,
                    evidence_count=legacy.evidence_count,
                    origin=legacy.origin,
                    status="active",
                )
                continue
            current.confidence = max(current.confidence, legacy.confidence)
            current.evidence_count = max(
                current.evidence_count, legacy.evidence_count
            )
            current.first_seen_at = min(
                current.first_seen_at, legacy.first_seen_at
            )
            current.last_seen_at = max(current.last_seen_at, legacy.last_seen_at)
            if legacy.origin == "explicit":
                current.origin = "explicit"
            current.source_audio_json = json.dumps(
                sorted(
                    _profile_source_jobs(current.source_audio_json)
                    | retained_sources
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        return [by_key[key] for key in sorted(by_key)]

    async def swap_active(self, facts: Sequence[ProfileFact]) -> None:
        """Replace the active profile in one rollback-safe transaction."""

        async with self.database.session() as session:
            async with session.begin():
                await session.execute(delete(ProfileFact))
                session.add_all(list(facts))
                await session.flush()


def _profile_source_jobs(raw: str) -> set[str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if isinstance(value, dict):
        job_id = value.get("job_id")
        return {job_id} if isinstance(job_id, str) else set()
    if not isinstance(value, list):
        return set()
    jobs: set[str] = set()
    for item in value:
        if isinstance(item, str):
            jobs.add(item)
        elif isinstance(item, dict) and isinstance(item.get("job_id"), str):
            jobs.add(item["job_id"])
    return jobs
