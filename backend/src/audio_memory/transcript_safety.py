from __future__ import annotations

import json

from sqlalchemy import exists, select

from audio_memory.models import JobFile, ProfileFact, Transcript


def pending_risk_review_exists(job_id):
    """Return a correlated predicate for any transcript not through the gate."""
    return exists(
        select(Transcript.id)
        .join(JobFile, JobFile.id == Transcript.job_file_id)
        .where(
            JobFile.job_id == job_id,
            Transcript.risk_classified.is_(False),
        )
    )


async def safe_active_profile_facts(session) -> list[ProfileFact]:
    """Hide facts whose provenance includes a source awaiting risk review."""
    unsafe_job_ids = set(
        await session.scalars(
            select(JobFile.job_id)
            .join(Transcript, Transcript.job_file_id == JobFile.id)
            .where(Transcript.risk_classified.is_(False))
            .distinct()
        )
    )
    facts = list(
        await session.scalars(
            select(ProfileFact)
            .where(ProfileFact.status == "active")
            .order_by(ProfileFact.subject_id, ProfileFact.dimension, ProfileFact.id)
        )
    )
    if not unsafe_job_ids:
        return facts
    return [
        fact
        for fact in facts
        if (sources := _profile_source_job_ids(fact.source_audio_json))
        and sources.isdisjoint(unsafe_job_ids)
    ]


def _profile_source_job_ids(raw: str) -> set[str]:
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
