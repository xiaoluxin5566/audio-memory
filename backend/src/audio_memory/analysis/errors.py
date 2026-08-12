from __future__ import annotations


ANALYSIS_RETRYABLE_ERROR_CODES = frozenset(
    {
        "model_analysis_failed",
        "credential_changed",
        "fixed_rules_changed",
        "network_timeout",
        "authentication_failed",
        "insufficient_balance",
        "rate_limited",
        "provider_unavailable",
        "content_rejected",
        "provider_input_rejected",
        "model_response_invalid",
        "model_output_truncated",
        "event_map_schema_invalid",
        "event_map_unknown_segment",
        "event_map_coverage_invalid",
        "analysis_quality_insufficient",
        "autonomous_schema_invalid",
        "autonomous_evidence_invalid",
        "autonomous_notes_schema_invalid",
        "autonomous_notes_evidence_invalid",
        "autonomous_retrieval_schema_invalid",
        "autonomous_retrieval_evidence_invalid",
        "autonomous_final_schema_invalid",
        "autonomous_final_evidence_invalid",
        "autonomous_day_map_evidence_invalid",
        "autonomous_search_state_invalid",
        "autonomous_final_source_invalid",
    }
)


class ProviderAnalysisError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retriable: bool = False,
        code: str = "model_analysis_failed",
        pause_batch: bool = False,
    ) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.code = code
        self.pause_batch = pause_batch
