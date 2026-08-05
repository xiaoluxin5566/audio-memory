from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from audio_memory.db import Database
    from audio_memory.models import AnalysisVersion


@dataclass(frozen=True, slots=True)
class AnalysisSnapshot:
    provider_id: str
    model_id: str
    credential_generation: int
    prompt_snapshot: dict[str, object]
    profile_snapshot: list[dict[str, object]]
    fixed_rules_hash: str


async def require_card_version(
    database: Database,
    *,
    version_id: str | None,
    expected_batch_id: str,
) -> AnalysisVersion:
    """Validate the version reference before a versioned Card write.

    The database column remains nullable so migration 0003 can rebuild legacy
    tables safely. Every new version-aware write path must call this boundary
    before inserting Cards. The pre-version ``AnalysisPublisher`` deliberately
    remains a compatibility path until Task 6 replaces it with
    ``VersionPublisher``.
    """
    from audio_memory.models import AnalysisVersion

    if version_id is None or not version_id.strip():
        raise ValueError("analysis_version_id is required for versioned Card writes")
    async with database.session() as session:
        version = await session.get(AnalysisVersion, version_id)
        if version is None:
            raise LookupError(f"Unknown analysis version: {version_id}")
        if version.batch_id != expected_batch_id:
            raise ValueError("Analysis version does not belong to expected batch")
        return version
