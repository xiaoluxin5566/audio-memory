from __future__ import annotations


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
