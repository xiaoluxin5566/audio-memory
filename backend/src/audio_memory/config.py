from __future__ import annotations

import platform
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


WHISPER_MODEL_ID = "mlx-community/whisper-large-v3-turbo"
DIARIZATION_VAD_MODEL = "silero_vad.onnx"
DIARIZATION_SEGMENTATION_MODEL = "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"
DIARIZATION_EMBEDDING_MODEL = (
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)


class UnsupportedPlatformError(RuntimeError):
    """Raised when the backend is started outside the phase-one platform."""


class RuntimeConfigurationError(ValueError):
    """Raised when runtime configuration cannot be resolved safely."""


class AppProfile(StrEnum):
    PRODUCTION = "production"
    DEVELOPMENT = "development"


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    database: Path
    runtime: Path
    lock: Path
    feedback: Path
    staging: Path
    audio: Path
    models: Path
    prompts: Path

    @classmethod
    def from_home(cls, home: Path) -> "AppPaths":
        root = home / "Library" / "Application Support" / "AudioMemory"
        return cls.from_roots(root)

    @classmethod
    def from_roots(
        cls,
        data_root: Path,
        model_root: Path | None = None,
    ) -> "AppPaths":
        root = data_root
        runtime = root / "runtime"
        return cls(
            root=root,
            database=root / "audio-memory.sqlite3",
            runtime=runtime,
            lock=runtime / "audio-memory.lock",
            feedback=root / "意见反馈",
            staging=root / "staging",
            audio=root / "audio",
            models=model_root if model_root is not None else root / "models",
            prompts=root / "prompts",
        )

    @property
    def required_directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.runtime,
            self.feedback,
            self.staging,
            self.audio,
            self.models,
            self.prompts,
        )

    @property
    def diarization_segmentation_model(self) -> Path:
        return self.models / "diarization" / DIARIZATION_SEGMENTATION_MODEL

    @property
    def diarization_vad_model(self) -> Path:
        return self.models / "diarization" / DIARIZATION_VAD_MODEL

    @property
    def diarization_embedding_model(self) -> Path:
        return self.models / "diarization" / DIARIZATION_EMBEDDING_MODEL

    def ensure_directories(self) -> None:
        for directory in self.required_directories:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    paths: AppPaths
    profile: AppProfile
    port: int
    keychain_service: str

    @classmethod
    def from_environment(
        cls,
        *,
        home: Path,
        project_root: Path,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimeConfig":
        values: Mapping[str, str] = dict(os.environ) if environ is None else environ

        profile_value = values.get("AUDIO_MEMORY_PROFILE", AppProfile.PRODUCTION.value)
        try:
            profile = AppProfile(profile_value)
        except ValueError as exc:
            raise RuntimeConfigurationError(
                "AUDIO_MEMORY_PROFILE must be exactly 'production' or 'development'"
            ) from exc

        resolved_home = home.expanduser().resolve()
        resolved_project_root = project_root.expanduser().resolve()
        production_root = (
            resolved_home / "Library" / "Application Support" / "AudioMemory"
        ).resolve()
        default_data_root = (
            resolved_project_root / ".runtime" / "dev"
            if profile is AppProfile.DEVELOPMENT
            else production_root
        )
        default_port = 8766 if profile is AppProfile.DEVELOPMENT else 8765
        default_keychain_service = (
            "Audio Memory Dev" if profile is AppProfile.DEVELOPMENT else "Audio Memory"
        )

        data_root = cls._path_value(
            values, "AUDIO_MEMORY_DATA_ROOT", default_data_root
        )
        default_model_root = (
            production_root / "models"
            if profile is AppProfile.DEVELOPMENT
            else data_root / "models"
        )
        model_root = cls._path_value(
            values, "AUDIO_MEMORY_MODEL_ROOT", default_model_root
        )

        service_value = values.get(
            "AUDIO_MEMORY_KEYCHAIN_SERVICE", default_keychain_service
        ).strip()
        if not service_value:
            raise RuntimeConfigurationError(
                "AUDIO_MEMORY_KEYCHAIN_SERVICE must not be blank"
            )

        port_value = values.get("AUDIO_MEMORY_PORT")
        if port_value is None:
            port = default_port
        else:
            try:
                port = int(port_value)
            except (TypeError, ValueError) as exc:
                raise RuntimeConfigurationError(
                    "AUDIO_MEMORY_PORT must be an integer between 1 and 65535"
                ) from exc
            if not 1 <= port <= 65535:
                raise RuntimeConfigurationError(
                    "AUDIO_MEMORY_PORT must be an integer between 1 and 65535"
                )

        return cls(
            paths=AppPaths.from_roots(data_root, model_root),
            profile=profile,
            port=port,
            keychain_service=service_value,
        )

    @staticmethod
    def _path_value(
        values: Mapping[str, str], name: str, default: Path
    ) -> Path:
        raw_value = values.get(name)
        if raw_value is None or not raw_value.strip():
            if raw_value is not None:
                raise RuntimeConfigurationError(f"{name} must not be blank")
            raw_path = default
        else:
            raw_path = Path(raw_value.strip())
        return raw_path.expanduser().resolve()


def assert_supported_platform() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise UnsupportedPlatformError(
            "Audio Memory 第一阶段仅支持 macOS Apple Silicon。"
        )
