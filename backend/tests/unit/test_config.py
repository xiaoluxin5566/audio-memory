from pathlib import Path

import pytest

from audio_memory.config import (
    AppPaths,
    AppProfile,
    RuntimeConfig,
    RuntimeConfigurationError,
    UnsafeDevelopmentPathError,
    UnsupportedPlatformError,
    assert_supported_platform,
)


def test_runtime_config_preserves_production_defaults(tmp_path: Path) -> None:
    config = RuntimeConfig.from_environment(
        home=tmp_path / "home", project_root=tmp_path / "repo", environ={}
    )

    assert config.profile is AppProfile.PRODUCTION
    assert config.paths.root == tmp_path / "home/Library/Application Support/AudioMemory"
    assert config.paths.models == config.paths.root / "models"
    assert config.port == 8765
    assert config.keychain_service == "Audio Memory"


def test_runtime_config_uses_data_root_override_for_default_model_root(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "custom-data"

    config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "repo",
        environ={"AUDIO_MEMORY_DATA_ROOT": str(data_root)},
    )

    assert config.paths.root == data_root.resolve()
    assert config.paths.models == (data_root / "models").resolve()


def test_runtime_config_uses_isolated_development_defaults(tmp_path: Path) -> None:
    config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "repo",
        environ={"AUDIO_MEMORY_PROFILE": "development"},
    )

    assert config.profile is AppProfile.DEVELOPMENT
    assert config.paths.root == (tmp_path / "repo/.runtime/dev").resolve()
    assert config.paths.models == (
        tmp_path / "home/Library/Application Support/AudioMemory/models"
    ).resolve()
    assert config.paths.models_writable is False
    assert config.port == 8766
    assert config.keychain_service == "Audio Memory Dev"


@pytest.mark.parametrize("profile", ["staging", "Development", "PRODUCTION"])
def test_runtime_config_rejects_unknown_or_case_mismatched_profile(
    tmp_path: Path, profile: str
) -> None:
    with pytest.raises(RuntimeConfigurationError, match="AUDIO_MEMORY_PROFILE"):
        RuntimeConfig.from_environment(
            home=tmp_path / "home",
            project_root=tmp_path / "repo",
            environ={"AUDIO_MEMORY_PROFILE": profile},
        )


@pytest.mark.parametrize("port", ["not-a-number", "0", "65536", "-1"])
def test_runtime_config_rejects_invalid_port_before_creating_directories(
    tmp_path: Path, port: str
) -> None:
    data_root = tmp_path / "data"

    with pytest.raises(RuntimeConfigurationError, match="AUDIO_MEMORY_PORT"):
        RuntimeConfig.from_environment(
            home=tmp_path / "home",
            project_root=tmp_path / "repo",
            environ={
                "AUDIO_MEMORY_PROFILE": "development",
                "AUDIO_MEMORY_PORT": port,
                "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            },
        )

    assert not data_root.exists()


def test_runtime_config_rejects_blank_keychain_service_before_creating_directories(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"

    with pytest.raises(RuntimeConfigurationError, match="AUDIO_MEMORY_KEYCHAIN_SERVICE"):
        RuntimeConfig.from_environment(
            home=tmp_path / "home",
            project_root=tmp_path / "repo",
            environ={
                "AUDIO_MEMORY_PROFILE": "development",
                "AUDIO_MEMORY_DATA_ROOT": str(data_root),
                "AUDIO_MEMORY_KEYCHAIN_SERVICE": "  \t",
            },
        )

    assert not data_root.exists()


def test_runtime_config_accepts_explicit_path_service_and_port_overrides(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "custom-data"
    model_root = data_root / "custom-models"

    config = RuntimeConfig.from_environment(
        home=tmp_path / "home",
        project_root=tmp_path / "repo",
        environ={
            "AUDIO_MEMORY_PROFILE": "development",
            "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            "AUDIO_MEMORY_MODEL_ROOT": str(model_root),
            "AUDIO_MEMORY_KEYCHAIN_SERVICE": "  Audio Memory Test  ",
            "AUDIO_MEMORY_PORT": "9012",
        },
    )

    assert config.paths.root == data_root.resolve()
    assert config.paths.models == model_root.resolve()
    assert config.paths.models_writable is True
    assert config.keychain_service == "Audio Memory Test"
    assert config.port == 9012
    assert not data_root.exists()
    assert not model_root.exists()


def test_app_paths_are_all_under_local_application_support(tmp_path: Path) -> None:
    paths = AppPaths.from_home(tmp_path)

    root = tmp_path / "Library" / "Application Support" / "AudioMemory"
    assert paths.root == root
    assert paths.database == root / "audio-memory.sqlite3"
    assert paths.runtime == root / "runtime"
    assert paths.lock == root / "runtime" / "audio-memory.lock"
    assert paths.feedback == root / "意见反馈"
    assert paths.staging == root / "staging"


def test_ensure_directories_creates_private_local_directories(tmp_path: Path) -> None:
    paths = AppPaths.from_home(tmp_path)

    paths.ensure_directories()

    for directory in paths.required_directories:
        assert directory.is_dir()
        assert directory.stat().st_mode & 0o777 == 0o700


def test_development_directory_setup_never_mutates_shared_models(tmp_path: Path) -> None:
    shared = tmp_path / "production/models"
    paths = AppPaths.from_roots(
        tmp_path / "repo/.runtime/dev", shared, models_writable=False
    )

    paths.ensure_directories()

    assert paths.root.is_dir()
    assert not shared.exists()
    assert shared not in paths.required_directories


def test_development_writable_paths_stay_under_its_data_root(tmp_path: Path) -> None:
    data_root = tmp_path / "repo/.runtime/dev"
    paths = AppPaths.from_roots(
        data_root, tmp_path / "production/models", models_writable=False
    )

    writable_paths = (
        paths.database,
        paths.runtime,
        paths.lock,
        paths.feedback,
        paths.staging,
        paths.audio,
        paths.prompts,
        paths.local_session,
    )

    assert all(path.is_relative_to(data_root) for path in writable_paths)


def test_development_config_rejects_data_root_ancestor_of_production(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    requested_root = home / "Library/Application Support"
    production_root = requested_root / "AudioMemory"

    with pytest.raises(UnsafeDevelopmentPathError):
        RuntimeConfig.from_environment(
            home=home,
            project_root=tmp_path / "repo",
            environ={
                "AUDIO_MEMORY_PROFILE": "development",
                "AUDIO_MEMORY_DATA_ROOT": str(requested_root),
            },
        )

    assert not requested_root.exists()
    assert not production_root.exists()


def test_development_config_rejects_symlinked_data_root_ancestor_of_production(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    resolved_root = home / "Library/Application Support"
    resolved_root.mkdir(parents=True, mode=0o755)
    symlink = tmp_path / "development-link"
    symlink.symlink_to(resolved_root, target_is_directory=True)
    production_root = resolved_root / "AudioMemory"

    with pytest.raises(UnsafeDevelopmentPathError):
        RuntimeConfig.from_environment(
            home=home,
            project_root=tmp_path / "repo",
            environ={
                "AUDIO_MEMORY_PROFILE": "development",
                "AUDIO_MEMORY_DATA_ROOT": str(symlink),
            },
        )

    assert symlink.is_symlink()
    assert resolved_root.stat().st_mode & 0o777 == 0o755
    assert not production_root.exists()


@pytest.mark.parametrize("development_root", ["exact", "child"])
def test_development_config_rejects_data_roots_in_production(
    tmp_path: Path, development_root: str
) -> None:
    home = tmp_path / "home"
    production_root = home / "Library/Application Support/AudioMemory"
    requested_root = (
        production_root
        if development_root == "exact"
        else production_root / "staging/development"
    )

    with pytest.raises(UnsafeDevelopmentPathError):
        RuntimeConfig.from_environment(
            home=home,
            project_root=tmp_path / "repo",
            environ={
                "AUDIO_MEMORY_PROFILE": "development",
                "AUDIO_MEMORY_DATA_ROOT": str(requested_root),
            },
        )

    assert not requested_root.exists()


def test_development_config_rejects_symlinked_data_root_in_production(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    production_root = home / "Library/Application Support/AudioMemory"
    production_root.mkdir(parents=True)
    symlink = tmp_path / "development-link"
    symlink.symlink_to(production_root, target_is_directory=True)
    requested_root = symlink / "development"

    with pytest.raises(UnsafeDevelopmentPathError):
        RuntimeConfig.from_environment(
            home=home,
            project_root=tmp_path / "repo",
            environ={
                "AUDIO_MEMORY_PROFILE": "development",
                "AUDIO_MEMORY_DATA_ROOT": str(requested_root),
            },
        )

    assert not requested_root.exists()
    assert not (production_root / "development").exists()


@pytest.mark.parametrize("model_subpath", ["models", "audio"])
def test_development_config_rejects_explicit_production_model_roots(
    tmp_path: Path, model_subpath: str
) -> None:
    home = tmp_path / "home"
    production_root = home / "Library/Application Support/AudioMemory"
    requested_data_root = tmp_path / "repo/.runtime/dev"

    with pytest.raises(UnsafeDevelopmentPathError):
        RuntimeConfig.from_environment(
            home=home,
            project_root=tmp_path / "repo",
            environ={
                "AUDIO_MEMORY_PROFILE": "development",
                "AUDIO_MEMORY_DATA_ROOT": str(requested_data_root),
                "AUDIO_MEMORY_MODEL_ROOT": str(production_root / model_subpath),
            },
        )

    assert not requested_data_root.exists()


def test_platform_guard_rejects_non_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("audio_memory.config.platform.system", lambda: "Darwin")
    monkeypatch.setattr("audio_memory.config.platform.machine", lambda: "x86_64")

    with pytest.raises(UnsupportedPlatformError, match="Apple Silicon"):
        assert_supported_platform()


def test_platform_guard_accepts_macos_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("audio_memory.config.platform.system", lambda: "Darwin")
    monkeypatch.setattr("audio_memory.config.platform.machine", lambda: "arm64")

    assert_supported_platform()
