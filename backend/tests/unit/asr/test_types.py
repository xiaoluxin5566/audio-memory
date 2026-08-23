from audio_memory.asr.types import ASR_PROVIDER_CONFIGS, AsrProviderId


def test_volcano_standard_model_contract_is_fixed() -> None:
    config = ASR_PROVIDER_CONFIGS[AsrProviderId.VOLCANO]

    assert config.resource_id == "volc.seedasr.auc"
    assert config.max_duration_ms == 5 * 60 * 60 * 1000
    assert config.max_size_bytes == 512 * 1024 * 1024
    assert config.supported_extensions == (".mp3", ".aac")

