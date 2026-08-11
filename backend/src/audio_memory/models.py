from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def transcript_segment_uid(context) -> str:
    parameters = context.get_current_parameters()
    return f"{parameters['job_file_id']}:{parameters['segment_index']}"


class Base(DeclarativeBase):
    pass


class ProviderMetadata(Base):
    __tablename__ = "provider_metadata"

    provider_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_status: Mapped[str] = mapped_column(String(40), default="unconfigured")
    last_validated_at: Mapped[str | None] = mapped_column(String(40))
    last_validation_error_code: Mapped[str | None] = mapped_column(String(80))
    last_validation_error_message: Mapped[str | None] = mapped_column(Text)
    default_model_id: Mapped[str | None] = mapped_column(String(120))
    credential_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )


Index(
    "uq_provider_single_active",
    ProviderMetadata.active,
    unique=True,
    sqlite_where=ProviderMetadata.active.is_(True),
)


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_id: Mapped[str | None] = mapped_column(String(32))
    model_id: Mapped[str | None] = mapped_column(String(120))
    prompt_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    staged_results_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(String(40), default=utc_now, onupdate=utc_now)


class JobFile(Base):
    __tablename__ = "job_files"
    __table_args__ = (UniqueConstraint("job_id", "sha256", name="uq_job_file_hash"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_jobs.id", ondelete="CASCADE"), index=True
    )
    original_name: Mapped[str] = mapped_column(Text, nullable=False)
    extension: Mapped[str] = mapped_column(String(8), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    recording_started_at: Mapped[str | None] = mapped_column(String(40))
    recording_time_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown"
    )
    timezone: Mapped[str | None] = mapped_column(String(64))
    speech_mapping_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    compact_checkpoint_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    vad_speech_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    vad_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    vad_energy_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    temporary_path: Mapped[str] = mapped_column(Text, nullable=False)


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (
        UniqueConstraint("job_file_id", "segment_index", name="uq_transcript_segment"),
        CheckConstraint(
            "risk_state IS NULL OR risk_state IN "
            "('REJECTED', 'HIGH_RISK_PENDING', 'POST_EDIT_PASSED', "
            "'POST_EDIT_FAILED')",
            name="ck_transcripts_risk_state",
        ),
        CheckConstraint(
            "risk_state IS NULL OR "
            "(risk_state IN ('REJECTED', 'HIGH_RISK_PENDING', "
            "'POST_EDIT_FAILED') AND is_reliable = 0) OR "
            "(risk_state = 'POST_EDIT_PASSED' AND is_reliable = 1)",
            name="ck_transcripts_risk_reliability",
        ),
        CheckConstraint(
            "is_reliable = 1 OR (text = '' AND words_json = '[]')",
            name="ck_transcripts_unreliable_content",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_file_id: Mapped[str] = mapped_column(
        ForeignKey("job_files.id", ondelete="CASCADE"), index=True
    )
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False)
    segment_uid: Mapped[str] = mapped_column(
        String(96), nullable=False, unique=True, default=transcript_segment_uid
    )
    speaker_id: Mapped[str | None] = mapped_column(String(40))
    start_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    end_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    words_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    no_speech_prob: Mapped[float | None] = mapped_column(Float)
    avg_logprob: Mapped[float | None] = mapped_column(Float)
    risk_state: Mapped[str | None] = mapped_column(String(40))
    risk_classified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_reliable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reliability_weight: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0
    )
    risk_reason: Mapped[str | None] = mapped_column(Text)


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_jobs.id", ondelete="RESTRICT"), unique=True
    )
    provider_id: Mapped[str | None] = mapped_column(String(32))
    model_id: Mapped[str | None] = mapped_column(String(120))
    current_analysis_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="RESTRICT")
    )
    uploaded_at: Mapped[str] = mapped_column(String(40), default=utc_now, index=True)
    natural_date: Mapped[str] = mapped_column(String(10), nullable=False)


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), index=True
    )
    analysis_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="CASCADE"), index=True
    )
    scene_id: Mapped[str] = mapped_column(String(40), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)


class Todo(Base):
    __tablename__ = "todos"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), index=True
    )
    source_card_id: Mapped[str | None] = mapped_column(
        ForeignKey("cards.id", ondelete="SET NULL")
    )
    analysis_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="SET NULL"), index=True
    )
    source_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("analysis_jobs.id", ondelete="CASCADE")
    )
    source_event_id: Mapped[str | None] = mapped_column(String(80))
    evidence_segment_ids_json: Mapped[str | None] = mapped_column(Text)
    normalized_action: Mapped[str | None] = mapped_column(Text)
    normalized_object: Mapped[str | None] = mapped_column(Text)
    normalized_assignee: Mapped[str | None] = mapped_column(Text)
    source_fingerprint: Mapped[str | None] = mapped_column(String(128))
    user_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completion_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="model"
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[str | None] = mapped_column(String(40))
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


Index(
    "uq_todos_source_fingerprint_non_null",
    Todo.source_fingerprint,
    unique=True,
    sqlite_where=Todo.source_fingerprint.is_not(None),
)


class QAMessage(Base):
    __tablename__ = "qa_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    card_id: Mapped[str] = mapped_column(
        ForeignKey("cards.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ReanalysisBatch(Base):
    __tablename__ = "reanalysis_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(48), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    credential_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    profile_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    fixed_rules_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(
        String(40), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[str | None] = mapped_column(String(40))


class AnalysisVersion(Base):
    __tablename__ = "analysis_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_job_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_jobs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), index=True
    )
    provider_id: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    credential_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    profile_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    fixed_rules_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    event_map_json: Mapped[str | None] = mapped_column(Text)
    event_map_hash: Mapped[str | None] = mapped_column(String(64))
    staged_results_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    published_card_count: Mapped[int | None] = mapped_column(Integer)
    published_todo_count: Mapped[int | None] = mapped_column(Integer)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    worker_owner_id: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    reanalysis_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("reanalysis_batches.id", ondelete="SET NULL")
    )
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    completed_at: Mapped[str | None] = mapped_column(String(40))


Index(
    "uq_analysis_versions_running_source_job",
    AnalysisVersion.source_job_id,
    unique=True,
    sqlite_where=AnalysisVersion.status == "running",
)

Index(
    "uq_analysis_versions_active_source_job",
    AnalysisVersion.source_job_id,
    unique=True,
    sqlite_where=AnalysisVersion.status.in_(("pending", "running")),
)


class TodoCandidate(Base):
    __tablename__ = "todo_candidates"
    __table_args__ = (
        UniqueConstraint(
            "analysis_version_id",
            "source_fingerprint",
            name="uq_todo_candidate_version_fingerprint",
        ),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    analysis_version_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_job_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_jobs.id", ondelete="CASCADE"), nullable=False
    )
    source_event_id: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_segment_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_action: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_object: Mapped[str | None] = mapped_column(Text)
    normalized_assignee: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[str | None] = mapped_column(String(40))
    source_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)


class TodoTombstone(Base):
    __tablename__ = "todo_tombstones"

    source_fingerprint: Mapped[str] = mapped_column(String(128), primary_key=True)
    deleted_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class ProfileCandidate(Base):
    __tablename__ = "profile_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_version_id: Mapped[str] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subject_id: Mapped[str] = mapped_column(String(80), nullable=False)
    dimension: Mapped[str] = mapped_column(String(80), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_segment_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)


class ReanalysisItem(Base):
    __tablename__ = "reanalysis_items"
    __table_args__ = (
        UniqueConstraint(
            "reanalysis_batch_id",
            "source_batch_id",
            name="uq_reanalysis_item_source_batch",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    reanalysis_batch_id: Mapped[str] = mapped_column(
        ForeignKey("reanalysis_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_batch_id: Mapped[str] = mapped_column(
        ForeignKey("batches.id", ondelete="CASCADE"), nullable=False
    )
    analysis_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("analysis_versions.id", ondelete="SET NULL")
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    updated_at: Mapped[str] = mapped_column(
        String(40), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[str | None] = mapped_column(String(40))


class ProfileFact(Base):
    __tablename__ = "profile_facts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    dimension: Mapped[str] = mapped_column(String(80), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    source_audio_json: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen_at: Mapped[str] = mapped_column(String(40), nullable=False)
    last_seen_at: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")


class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("scene_id", "version", name="uq_prompt_scene_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scene_id: Mapped[str] = mapped_column(String(40), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)


class TempFileManifest(Base):
    __tablename__ = "temp_file_manifest"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_uuid: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
    cleanup_status: Mapped[str] = mapped_column(String(24), default="pending")


class FeedbackIndex(Base):
    __tablename__ = "feedback_index"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    card_id: Mapped[str | None] = mapped_column(String(80), index=True)
    scene_id: Mapped[str] = mapped_column(String(40), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    rating: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), default=utc_now)
