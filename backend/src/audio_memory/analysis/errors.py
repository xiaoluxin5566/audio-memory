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
        "model_response_invalid",
        "model_output_truncated",
        "event_map_schema_invalid",
        "event_map_unknown_segment",
        "event_map_coverage_invalid",
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
