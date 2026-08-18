from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuditModelPolicy:
    policy_name: str
    context_window_tokens: int
    reserved_output_tokens: int
    safety_ratio: float
    max_transcript_tokens: int
    max_parallel_chunks: int
    max_split_depth: int = 3
    minimum_segment_count: int = 1
    minimum_transcript_chars: int = 4_000
    estimated_chars_per_token: float = 2.0


_POLICIES = {
    "glm": AuditModelPolicy(
        policy_name="glm-5.2-output-bounded-v2",
        context_window_tokens=128_000,
        reserved_output_tokens=32_768,
        safety_ratio=0.75,
        max_transcript_tokens=8_000,
        max_parallel_chunks=4,
    ),
    "deepseek": AuditModelPolicy(
        policy_name="deepseek-v4",
        context_window_tokens=128_000,
        reserved_output_tokens=32_768,
        safety_ratio=0.78,
        max_transcript_tokens=24_000,
        max_parallel_chunks=4,
    ),
    "kimi": AuditModelPolicy(
        policy_name="kimi-long-context",
        context_window_tokens=256_000,
        reserved_output_tokens=32_768,
        safety_ratio=0.75,
        max_transcript_tokens=32_000,
        max_parallel_chunks=3,
    ),
}

_DEFAULT_POLICY = AuditModelPolicy(
    policy_name="conservative-default",
    context_window_tokens=64_000,
    reserved_output_tokens=24_576,
    safety_ratio=0.70,
    max_transcript_tokens=15_000,
    max_parallel_chunks=2,
    max_split_depth=3,
)


def resolve_audit_model_policy(
    provider_id: str, model_id: str
) -> AuditModelPolicy:
    provider = provider_id.strip().lower()
    model = model_id.strip().lower()
    if provider == "glm" or model.startswith("glm-"):
        return _POLICIES["glm"]
    if provider == "deepseek" or model.startswith("deepseek-"):
        return _POLICIES["deepseek"]
    if provider == "kimi" or model.startswith("kimi-"):
        return _POLICIES["kimi"]
    return _DEFAULT_POLICY


def audit_transcript_budget_chars(
    policy: AuditModelPolicy, *, fixed_prompt_chars: int
) -> int:
    if fixed_prompt_chars < 0:
        raise ValueError("fixed_prompt_chars must not be negative")
    fixed_tokens = int(fixed_prompt_chars / policy.estimated_chars_per_token)
    safe_context_tokens = int(policy.context_window_tokens * policy.safety_ratio)
    available_tokens = max(
        0,
        safe_context_tokens - policy.reserved_output_tokens - fixed_tokens,
    )
    transcript_tokens = min(policy.max_transcript_tokens, available_tokens)
    estimated_chars = int(transcript_tokens * policy.estimated_chars_per_token)
    return max(policy.minimum_transcript_chars, estimated_chars)
