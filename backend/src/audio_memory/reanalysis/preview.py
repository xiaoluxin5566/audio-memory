from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from audio_memory.analysis.windows import build_analysis_windows
from audio_memory.db import Database
from audio_memory.models import (
    AnalysisJob,
    AnalysisVersion,
    Batch,
    JobFile,
    Transcript,
)
from audio_memory.prompts.autonomous_schema import AutonomousAnalysisResult
from audio_memory.prompts.composer import MODEL_REQUEST_POLICIES, PromptComposer
from audio_memory.prompts.store import PROMPT_SCENES, PromptStore
from audio_memory.providers.types import ProviderState, ProviderStateName
from audio_memory.reanalysis.types import (
    PromptSummary,
    ReanalysisPreview,
    ReanalysisSnapshot,
    SourceSnapshot,
)
from audio_memory.transcript_safety import (
    pending_risk_review_exists,
    safe_active_profile_facts,
)


class PreviewTokenInvalidError(ValueError):
    pass


class PreviewTokenExpiredError(PreviewTokenInvalidError):
    pass


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def current_fixed_rule_hashes() -> dict[str, str]:
    hashes = {
        "system.md": hashlib.sha256(
            PromptComposer._fixed_prompt("system.md").encode("utf-8")
        ).hexdigest(),
        "autonomous_analysis_prompt": hashlib.sha256(
            PromptComposer._approved_prompt("Prompt A", "Prompt B").encode("utf-8")
        ).hexdigest(),
        "autonomous_profile_prompt": hashlib.sha256(
            PromptComposer._approved_prompt("Prompt B", None).encode("utf-8")
        ).hexdigest(),
    }
    hashes["schema_version"] = canonical_hash(
        {"schema_version": PromptComposer.SCHEMA_VERSION}
    )
    hashes["analysis_schemas"] = canonical_hash(
        {
            "autonomous": AutonomousAnalysisResult.model_json_schema(),
        }
    )
    hashes["analysis_parameters"] = canonical_hash(
        {
            name: {
                "max_tokens": policy.max_tokens,
                "timeout_seconds": policy.timeout_seconds,
            }
            for name, policy in MODEL_REQUEST_POLICIES.items()
            if name in {"autonomous", "autonomous-profile"}
        }
    )
    return hashes


async def transcript_fingerprint(database: Database, job_id: str) -> str:
    async with database.session() as session:
        return await transcript_fingerprint_from_session(session, job_id)


class PreviewSigner:
    def __init__(
        self,
        *,
        secret: bytes | None = None,
        clock: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        self._secret = secret or secrets.token_bytes(32)
        if len(self._secret) < 32:
            raise ValueError("Preview HMAC secret must contain at least 256 bits")
        self._clock = clock or (lambda: datetime.now(UTC))
        self._ttl = ttl

    def sign(self, snapshot: ReanalysisSnapshot, snapshot_hash: str) -> tuple[str, str]:
        expires_at = (self._clock() + self._ttl).isoformat()
        snapshot_payload = snapshot.canonical_payload()
        payload = {
            "scope": snapshot.scope,
            "source_batch_ids": [item.batch_id for item in snapshot.sources],
            "provider_id": snapshot.provider_id,
            "model_id": snapshot.model_id,
            "credential_generation": snapshot.credential_generation,
            "prompt_hashes": snapshot.prompt_hashes,
            "prompt_bindings": snapshot_payload["prompt_bindings"],
            "fixed_rule_hashes": snapshot.fixed_rule_hashes,
            "fixed_rules_hash": snapshot.fixed_rules_hash,
            "profile_hash": snapshot.profile_hash,
            "counts": snapshot_payload["counts"],
            "snapshot_hash": snapshot_hash,
            "expires_at": expires_at,
        }
        encoded = _base64_encode(canonical_json(payload))
        signature = _base64_encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{encoded}.{signature}", expires_at

    def verify(self, token: str) -> dict[str, object]:
        try:
            encoded, supplied_signature = token.split(".", 1)
            expected_signature = hmac.new(
                self._secret, encoded.encode("ascii"), hashlib.sha256
            ).digest()
            signature = _base64_decode(supplied_signature)
            if not hmac.compare_digest(signature, expected_signature):
                raise PreviewTokenInvalidError("Preview token signature is invalid")
            payload = json.loads(_base64_decode(encoded))
            expires_at = datetime.fromisoformat(payload["expires_at"])
        except PreviewTokenInvalidError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise PreviewTokenInvalidError("Preview token is malformed") from exc
        if expires_at.tzinfo is None:
            raise PreviewTokenInvalidError("Preview token expiry must include a timezone")
        if self._clock() > expires_at:
            raise PreviewTokenExpiredError("Preview token has expired")
        if not isinstance(payload, dict):
            raise PreviewTokenInvalidError("Preview token payload is invalid")
        return payload


class ReanalysisPreviewBuilder:
    def __init__(
        self,
        *,
        database: Database,
        prompt_store: PromptStore,
        provider_coordinator,
        signer: PreviewSigner | None = None,
    ) -> None:
        self.database = database
        self.prompt_store = prompt_store
        self.provider_coordinator = provider_coordinator
        self.signer = signer or PreviewSigner()

    async def build(self, *, provider_binding=None) -> ReanalysisPreview:
        no_active_provider = False
        if provider_binding is not None:
            provider, generation = provider_binding
        else:
            try:
                provider, generation = (
                    await self.provider_coordinator.snapshot_active_with_generation()
                )
            except LookupError:
                no_active_provider = True
                provider = ProviderState(
                    provider_id="",
                    display_name="",
                    model_id="",
                    state=ProviderStateName.UNCONFIGURED,
                )
                generation = 0
        sources = await self._completed_sources()
        prompt_documents = {
            scene_id: self.prompt_store.get(scene_id) for scene_id in PROMPT_SCENES
        }
        prompt_snapshot = {
            scene_id: {
                "version": document.version,
                "content": document.content,
                "sha256": hashlib.sha256(document.content.encode("utf-8")).hexdigest(),
            }
            for scene_id, document in prompt_documents.items()
        }
        prompt_hashes = {
            scene_id: str(value["sha256"])
            for scene_id, value in prompt_snapshot.items()
        }
        fixed_rule_hashes = current_fixed_rule_hashes()
        profile_snapshot = tuple(await self._profile_snapshot())
        profile_hash = canonical_hash(profile_snapshot)
        source_count = len(sources)
        audio_count = sum(item.audio_file_count for item in sources)
        character_count = sum(item.transcript_character_count for item in sources)
        snapshot = ReanalysisSnapshot(
            sources=tuple(sources),
            provider_id=provider.provider_id,
            provider_display_name=provider.display_name,
            model_id=provider.model_id,
            credential_generation=generation,
            prompt_snapshot=prompt_snapshot,
            prompt_hashes=prompt_hashes,
            fixed_rule_hashes=fixed_rule_hashes,
            fixed_rules_hash=PromptComposer.fixed_rules_hash(),
            profile_snapshot=profile_snapshot,
            profile_hash=profile_hash,
            source_batch_count=source_count,
            audio_file_count=audio_count,
            transcript_character_count=character_count,
            estimated_calls_min=source_count * 2,
            estimated_calls_max=source_count * 6,
        )
        snapshot_hash = canonical_hash(snapshot.canonical_payload())
        token, expires_at = self.signer.sign(snapshot, snapshot_hash)
        blockers: list[str] = []
        if source_count == 0:
            blockers.append("no_completed_history")
        if no_active_provider:
            blockers.append("no_active_provider")
        elif provider.state is not ProviderStateName.AVAILABLE:
            blockers.append(f"provider_{provider.state.value}")
        return ReanalysisPreview(
            source_batch_count=source_count,
            audio_file_count=audio_count,
            transcript_character_count=character_count,
            provider_id=provider.provider_id,
            provider_display_name=provider.display_name,
            model_id=provider.model_id,
            credential_generation=generation,
            prompt_summary={
                scene_id: PromptSummary(document.version, prompt_hashes[scene_id])
                for scene_id, document in prompt_documents.items()
            },
            estimated_calls_min=snapshot.estimated_calls_min,
            estimated_calls_max=snapshot.estimated_calls_max,
            whisper_calls=0,
            diarization_calls=0,
            blockers=blockers,
            preview_token=token,
            snapshot_hash=snapshot_hash,
            expires_at=expires_at,
            snapshot=snapshot,
        )

    async def _completed_sources(self) -> list[SourceSnapshot]:
        async with self.database.session() as session:
            rows = (
                await session.execute(
                    select(Batch.id, Batch.job_id)
                    .join(
                        AnalysisVersion,
                        AnalysisVersion.id == Batch.current_analysis_version_id,
                    )
                    .join(AnalysisJob, AnalysisJob.id == Batch.job_id)
                    .where(
                        AnalysisVersion.status == "completed",
                        AnalysisJob.stage == "completed",
                        ~pending_risk_review_exists(Batch.job_id),
                    )
                    .order_by(Batch.uploaded_at.desc(), Batch.id.desc())
                )
            ).all()
            sources: list[SourceSnapshot] = []
            for batch_id, job_id in rows:
                file_count = int(
                    await session.scalar(
                        select(func.count(JobFile.id)).where(JobFile.job_id == job_id)
                    )
                    or 0
                )
                character_count = int(
                    await session.scalar(
                        select(func.sum(func.length(Transcript.text)))
                        .join(JobFile, JobFile.id == Transcript.job_file_id)
                        .where(JobFile.job_id == job_id)
                    )
                    or 0
                )
                transcript_sha256 = await transcript_fingerprint_from_session(
                    session, job_id
                )
                sources.append(
                    SourceSnapshot(
                        batch_id=batch_id,
                        job_id=job_id,
                        audio_file_count=file_count,
                        transcript_character_count=character_count,
                        transcript_sha256=transcript_sha256,
                    )
                )
            return sources

    async def _analysis_window_count(
        self, sources: list[SourceSnapshot]
    ) -> int:
        total = 0
        async with self.database.session() as session:
            for source in sources:
                rows = (
                    await session.execute(
                        select(
                            JobFile.id,
                            JobFile.position,
                            Transcript.segment_index,
                            Transcript.start_ms,
                            Transcript.end_ms,
                        )
                        .join(Transcript, Transcript.job_file_id == JobFile.id)
                        .where(
                            JobFile.job_id == source.job_id,
                            Transcript.risk_classified.is_(True),
                            Transcript.is_reliable.is_(True),
                        )
                        .order_by(
                            JobFile.position,
                            Transcript.segment_index,
                            Transcript.id,
                        )
                    )
                ).all()
                transcript = [
                    {
                        "segment_id": f"seg_{position}_{segment_index}",
                        "file_id": file_id,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                    }
                    for file_id, position, segment_index, start_ms, end_ms in rows
                ]
                total += len(build_analysis_windows(transcript))
        return total

    async def _profile_snapshot(self) -> list[dict[str, object]]:
        async with self.database.session() as session:
            rows = await safe_active_profile_facts(session)
        return [
            {
                "subject_id": row.subject_id,
                "dimension": row.dimension,
                "value": json.loads(row.value_json),
                "confidence": row.confidence,
                "origin": row.origin,
            }
            for row in rows
        ]


def _base64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _base64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise PreviewTokenInvalidError("Preview token encoding is invalid") from exc


async def transcript_fingerprint_from_session(session, job_id: str) -> str:
    files = list(
        await session.scalars(
            select(JobFile)
            .where(JobFile.job_id == job_id)
            .order_by(JobFile.position, JobFile.id)
        )
    )
    segments = list(
        await session.scalars(
            select(Transcript)
            .join(JobFile, JobFile.id == Transcript.job_file_id)
            .where(JobFile.job_id == job_id)
            .order_by(JobFile.position, Transcript.segment_index, Transcript.id)
        )
    )
    return canonical_hash(
        {
            "version": 1,
            "files": [
                {
                    "id": item.id,
                    "position": item.position,
                    "name": item.original_name,
                    "recording_started_at": item.recording_started_at,
                    "recording_time_source": item.recording_time_source,
                    "timezone": item.timezone,
                    "speech_mapping": json.loads(item.speech_mapping_json or "[]"),
                }
                for item in files
            ],
            "segments": [
                {
                    "job_file_id": item.job_file_id,
                    "segment_index": item.segment_index,
                    "segment_uid": item.segment_uid,
                    "speaker_id": item.speaker_id,
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "text": item.text,
                    "words": json.loads(item.words_json or "[]"),
                }
                for item in segments
            ],
        }
    )
