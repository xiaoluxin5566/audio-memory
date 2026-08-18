from __future__ import annotations

from audio_memory.analysis.audit_model_policy import (
    audit_transcript_budget_chars,
    resolve_audit_model_policy,
)


def test_known_models_receive_distinct_operational_audit_budgets() -> None:
    glm = resolve_audit_model_policy("glm", "glm-5.2")
    deepseek = resolve_audit_model_policy("deepseek", "deepseek-v4-pro")
    kimi = resolve_audit_model_policy("kimi", "kimi-k3")

    assert glm.max_transcript_tokens == 20_000
    assert deepseek.max_transcript_tokens == 24_000
    assert kimi.max_transcript_tokens == 32_000
    assert glm.max_parallel_chunks == 4
    assert deepseek.max_parallel_chunks == 4
    assert kimi.max_parallel_chunks == 3


def test_unknown_model_uses_the_most_conservative_supported_policy() -> None:
    unknown = resolve_audit_model_policy("custom", "future-model")
    glm = resolve_audit_model_policy("glm", "glm-5.2")

    assert unknown.policy_name == "conservative-default"
    assert unknown.max_transcript_tokens == 15_000
    assert unknown.max_transcript_tokens < glm.max_transcript_tokens
    assert unknown.max_parallel_chunks == 2


def test_fixed_prompt_growth_reduces_transcript_budget_without_reaching_zero() -> None:
    policy = resolve_audit_model_policy("glm", "glm-5.2")

    small_prompt = audit_transcript_budget_chars(policy, fixed_prompt_chars=20_000)
    large_prompt = audit_transcript_budget_chars(policy, fixed_prompt_chars=180_000)

    assert small_prompt == 40_000
    assert 0 < large_prompt < small_prompt
    assert large_prompt == policy.minimum_transcript_chars

