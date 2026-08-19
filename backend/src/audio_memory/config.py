from __future__ import annotations

import platform
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
import unicodedata


WHISPER_MODEL_ID = "mlx-community/whisper-large-v3-turbo"
PRODUCTION_KEYCHAIN_SERVICE = "Audio Memory"
DEVELOPMENT_KEYCHAIN_SERVICE = "Audio Memory Dev"
DIARIZATION_VAD_MODEL = "silero_vad.onnx"
DIARIZATION_SEGMENTATION_MODEL = "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"
DIARIZATION_EMBEDDING_MODEL = (
    "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
)


class UnsupportedPlatformError(RuntimeError):
    """Raised when the backend is started outside the phase-one platform."""


class RuntimeConfigurationError(ValueError):
    """Raised when runtime configuration cannot be resolved safely."""


class UnsafeDevelopmentPathError(RuntimeConfigurationError):
    """Raised when development configuration could touch production data."""


class AppProfile(StrEnum):
    PRODUCTION = "production"
    DEVELOPMENT = "development"


def _macos_path_component(value: str) -> str:
    """Compare names the way a default macOS data volume can alias them."""
    return unicodedata.normalize("NFC", value).casefold()


def _identity_anchored_tails(
    path: Path,
) -> tuple[tuple[tuple[int, int], tuple[str, ...]], ...]:
    """Describe a path from every existing ancestor's filesystem identity."""
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    anchored: list[tuple[tuple[int, int], tuple[str, ...]]] = []
    for ancestor in (absolute, *absolute.parents):
        try:
            metadata = ancestor.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeConfigurationError(
                "无法安全确认开发目录的文件系统身份。"
            ) from exc
        tail = tuple(
            _macos_path_component(part)
            for part in absolute.relative_to(ancestor).parts
        )
        anchored.append(((metadata.st_dev, metadata.st_ino), tail))
    return tuple(anchored)


def _path_is_same_or_within(candidate: Path, container: Path) -> bool:
    candidate_anchors = _identity_anchored_tails(candidate)
    container_anchors = _identity_anchored_tails(container)
    return any(
        candidate_identity == container_identity
        and len(container_tail) <= len(candidate_tail)
        and candidate_tail[: len(container_tail)] == container_tail
        for candidate_identity, candidate_tail in candidate_anchors
        for container_identity, container_tail in container_anchors
    )


def _paths_overlap(first: Path, second: Path) -> bool:
    return _path_is_same_or_within(first, second) or _path_is_same_or_within(
        second, first
    )


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
    models_writable: bool = True

    @classmethod
    def from_home(cls, home: Path) -> "AppPaths":
        root = home / "Library" / "Application Support" / "AudioMemory"
        return cls.from_roots(root)

    @classmethod
    def from_roots(
        cls,
        data_root: Path,
        model_root: Path | None = None,
        *,
        models_writable: bool = True,
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
            models_writable=models_writable,
        )

    @property
    def required_directories(self) -> tuple[Path, ...]:
        directories = (
            self.root,
            self.runtime,
            self.feedback,
            self.staging,
            self.audio,
            self.prompts,
        )
        return directories + ((self.models,) if self.models_writable else ())

    @property
    def local_session(self) -> Path:
        return self.runtime / "local-web-security.sqlite3"

    @property
    def diarization_segmentation_model(self) -> Path:
        return self.models / "diarization" / DIARIZATION_SEGMENTATION_MODEL

    @property
    def diarization_vad_model(self) -> Path:
        return self.models / "diarization" / DIARIZATION_VAD_MODEL

    @property
    def diarization_embedding_model(self) -> Path:
        return self.models / "diarization" / DIARIZATION_EMBEDDING_MODEL

    @property
    def whisper_manifest(self) -> Path:
        return self.models.parent / "whisper-model-manifest.json"

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
    production_data_root: Path | None = field(default=None, repr=False, compare=False)

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
            DEVELOPMENT_KEYCHAIN_SERVICE
            if profile is AppProfile.DEVELOPMENT
            else PRODUCTION_KEYCHAIN_SERVICE
        )

        data_root = cls._path_value(
            values, "AUDIO_MEMORY_DATA_ROOT", default_data_root
        )
        default_model_root = (
            production_root / "models"
            if profile is AppProfile.DEVELOPMENT
            else data_root / "models"
        )
        model_root_is_explicit = "AUDIO_MEMORY_MODEL_ROOT" in values
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

        config = cls(
            paths=AppPaths.from_roots(
                data_root,
                model_root,
                models_writable=(
                    profile is AppProfile.PRODUCTION or model_root_is_explicit
                ),
            ),
            profile=profile,
            port=port,
            keychain_service=service_value,
            production_data_root=production_root,
        )
        config.validate()
        return config

    def with_overrides(
        self,
        *,
        paths: AppPaths | None = None,
        port: int | None = None,
    ) -> "RuntimeConfig":
        effective = replace(
            self,
            paths=self.paths if paths is None else paths,
            port=self.port if port is None else port,
            keychain_service=self.keychain_service.strip(),
        )
        effective.validate()
        return effective

    def validate(self) -> None:
        if (
            not isinstance(self.port, int)
            or isinstance(self.port, bool)
            or not 1 <= self.port <= 65535
        ):
            raise RuntimeConfigurationError(
                "AUDIO_MEMORY_PORT must be an integer between 1 and 65535"
            )
        if not self.keychain_service.strip():
            raise RuntimeConfigurationError(
                "AUDIO_MEMORY_KEYCHAIN_SERVICE must not be blank"
            )
        self.validate_keychain_isolation()
        self.validate_development_isolation()

    def validate_keychain_isolation(self) -> None:
        opposite_service = (
            PRODUCTION_KEYCHAIN_SERVICE
            if self.profile is AppProfile.DEVELOPMENT
            else DEVELOPMENT_KEYCHAIN_SERVICE
        )
        if self.keychain_service.strip() == opposite_service:
            raise RuntimeConfigurationError(
                "Keychain service 不能使用另一个运行环境的受保护命名空间。"
            )

    def validate_development_isolation(self) -> None:
        if self.profile is not AppProfile.DEVELOPMENT:
            return

        production_root = self.production_data_root
        if production_root is None:
            raise RuntimeConfigurationError(
                "development 运行配置必须包含正式数据目录边界。"
            )

        resolved_production_root = production_root.expanduser().resolve()
        resolved_data_root = self.paths.root.expanduser().resolve()
        if _paths_overlap(resolved_data_root, resolved_production_root):
            raise UnsafeDevelopmentPathError(
                "开发数据目录不能与正式数据目录重叠。"
            )

        writable_paths = (
            self.paths.root,
            self.paths.database,
            self.paths.runtime,
            self.paths.lock,
            self.paths.feedback,
            self.paths.staging,
            self.paths.audio,
            self.paths.prompts,
            self.paths.local_session,
        )
        if any(
            not _path_is_same_or_within(
                path.expanduser().resolve(), resolved_data_root
            )
            for path in writable_paths
        ):
            raise UnsafeDevelopmentPathError(
                "开发环境的派生可写路径必须位于开发数据目录中。"
            )

        if self.paths.models_writable:
            resolved_model_root = self.paths.models.expanduser().resolve()
            if not _path_is_same_or_within(
                resolved_model_root, resolved_data_root
            ):
                raise UnsafeDevelopmentPathError(
                    "开发模型目录必须位于开发数据目录中。"
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
