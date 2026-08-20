from __future__ import annotations

import errno
import os
import platform
import secrets
import stat
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


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | _directory_nofollow_flag()


def _directory_nofollow_flag() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RuntimeConfigurationError(
            "当前系统不支持安全的 no-follow 目录打开。"
        )
    return nofollow


def _unsafe_directory_error(path: Path, exc: BaseException | None = None) -> None:
    error = UnsafeDevelopmentPathError(
        f"开发可写目录的文件系统身份不安全：{path}"
    )
    if exc is None:
        raise error
    raise error from exc


def _open_absolute_directory(path: Path, *, create: bool) -> int | None:
    """Open a directory chain without following symlinks at any component."""
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    current_fd = os.open(absolute.anchor, _directory_open_flags())
    traversed = Path(absolute.anchor)
    try:
        for component in absolute.parts[1:]:
            traversed /= component
            try:
                next_fd = os.open(
                    component, _directory_open_flags(), dir_fd=current_fd
                )
            except FileNotFoundError:
                if not create:
                    os.close(current_fd)
                    return None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(
                        component, _directory_open_flags(), dir_fd=current_fd
                    )
                except OSError as exc:
                    _unsafe_directory_error(traversed, exc)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    _unsafe_directory_error(traversed, exc)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_directory_at(
    root_fd: int, parts: tuple[str, ...], *, create: bool
) -> int | None:
    current_fd = os.dup(root_fd)
    try:
        for component in parts:
            try:
                next_fd = os.open(
                    component, _directory_open_flags(), dir_fd=current_fd
                )
            except FileNotFoundError:
                if not create:
                    os.close(current_fd)
                    return None
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    next_fd = os.open(
                        component, _directory_open_flags(), dir_fd=current_fd
                    )
                except OSError as exc:
                    _unsafe_directory_error(Path(*parts), exc)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    _unsafe_directory_error(Path(*parts), exc)
                raise
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _reject_unsafe_writable_entries(
    directory_fd: int,
    location: Path,
    *,
    model_cache_exception: Path | None = None,
    _allow_symlinks: bool = False,
) -> None:
    """Reject aliases inside a tree intended to be development-only writable."""
    try:
        entries = list(os.scandir(directory_fd))
    except OSError as exc:
        _unsafe_directory_error(location, exc)
    for entry in entries:
        entry_location = location / entry.name
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            _unsafe_directory_error(entry_location, exc)
        if stat.S_ISLNK(metadata.st_mode):
            if _allow_symlinks:
                assert model_cache_exception is not None
                try:
                    resolved_target = entry_location.resolve(strict=False)
                except (OSError, RuntimeError) as exc:
                    _unsafe_directory_error(entry_location, exc)
                if not _path_is_same_or_within(
                    resolved_target, model_cache_exception.resolve(strict=False)
                ):
                    raise UnsafeDevelopmentPathError(
                        "开发模型缓存符号链接必须保持在独立模型根内："
                        f"{entry_location}"
                    )
                continue
            raise UnsafeDevelopmentPathError(
                f"开发可写资源不能是符号链接：{entry_location}"
            )
        if not stat.S_ISDIR(metadata.st_mode):
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafeDevelopmentPathError(
                    f"开发可写资源必须是普通文件或目录：{entry_location}"
                )
            if metadata.st_nlink > 1:
                raise UnsafeDevelopmentPathError(
                    f"开发可写资源不能是硬链接：{entry_location}"
                )
            continue
        try:
            child_fd = os.open(
                entry.name, _directory_open_flags(), dir_fd=directory_fd
            )
        except OSError as exc:
            _unsafe_directory_error(entry_location, exc)
        try:
            _reject_unsafe_writable_entries(
                child_fd,
                entry_location,
                model_cache_exception=model_cache_exception,
                _allow_symlinks=(
                    _allow_symlinks or entry_location == model_cache_exception
                ),
            )
        finally:
            os.close(child_fd)


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

        data_root_fd = _open_absolute_directory(self.paths.root, create=False)
        if data_root_fd is not None:
            try:
                _reject_unsafe_writable_entries(
                    data_root_fd,
                    self.paths.root,
                    model_cache_exception=(
                        self.paths.models if self.paths.models_writable else None
                    ),
                )
            finally:
                os.close(data_root_fd)

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


class PinnedDevelopmentRoot:
    """A no-follow, identity-pinned boundary for development filesystem writes."""

    def __init__(self, config: RuntimeConfig, root_fd: int) -> None:
        self.config = config
        self.root_fd = root_fd
        metadata = os.fstat(root_fd)
        self._identity = (metadata.st_dev, metadata.st_ino)
        self._closed = False

    @classmethod
    def open(
        cls, config: RuntimeConfig, *, create: bool
    ) -> "PinnedDevelopmentRoot | None":
        if config.profile is not AppProfile.DEVELOPMENT:
            raise RuntimeConfigurationError(
                "只能为 development 运行配置固定可写边界。"
            )
        # This validation must precede even directory creation.
        config.validate_development_isolation()
        root_fd = _open_absolute_directory(config.paths.root, create=create)
        if root_fd is None:
            return None
        boundary = cls(config, root_fd)
        try:
            boundary.verify()
        except BaseException:
            boundary.close()
            raise
        return boundary

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        os.close(self.root_fd)

    def __enter__(self) -> "PinnedDevelopmentRoot":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def verify(self) -> None:
        if self._closed:
            raise RuntimeConfigurationError("开发可写边界已关闭。")
        self.config.validate_development_isolation()
        self._verify_root_identity()
        _reject_unsafe_writable_entries(
            self.root_fd,
            self.config.paths.root,
            model_cache_exception=(
                self.config.paths.models
                if self.config.paths.models_writable
                else None
            ),
        )
        self._verify_root_identity()

    def _verify_root_identity(self) -> None:
        try:
            path_metadata = os.lstat(self.config.paths.root)
        except OSError as exc:
            _unsafe_directory_error(self.config.paths.root, exc)
        if (
            not stat.S_ISDIR(path_metadata.st_mode)
            or (path_metadata.st_dev, path_metadata.st_ino) != self._identity
        ):
            _unsafe_directory_error(self.config.paths.root)

    def open_directory(self, directory: Path, *, create: bool) -> int | None:
        self._verify_root_identity()
        relative_parts = self._relative_parts(directory)
        opened = _open_directory_at(
            self.root_fd, relative_parts, create=create
        )
        try:
            self._verify_root_identity()
        except BaseException:
            if opened is not None:
                os.close(opened)
            raise
        return opened

    def open_regular_file(
        self,
        path: Path,
        flags: int,
        *,
        mode: int = 0o600,
        create_parents: bool = False,
    ) -> int:
        """Open a development file relative to the pinned root without aliases."""
        self._verify_root_identity()
        relative_parts = self._relative_parts(path)
        if not relative_parts:
            raise UnsafeDevelopmentPathError("开发普通文件不能是数据根目录。")
        parent_fd = _open_directory_at(
            self.root_fd, relative_parts[:-1], create=create_parents
        )
        if parent_fd is None:
            raise FileNotFoundError(path.parent)
        try:
            requested_truncate = bool(flags & os.O_TRUNC)
            fd = os.open(
                relative_parts[-1],
                (flags & ~os.O_TRUNC) | _directory_nofollow_flag(),
                mode,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            os.close(parent_fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                _unsafe_directory_error(path, exc)
            raise
        os.close(parent_fd)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise UnsafeDevelopmentPathError(
                    f"开发可写资源必须是普通文件：{path}"
                )
            if metadata.st_nlink > 1:
                raise UnsafeDevelopmentPathError(
                    f"开发可写资源不能是硬链接：{path}"
                )
            if requested_truncate:
                os.ftruncate(fd, 0)
            self._verify_root_identity()
            return fd
        except BaseException:
            os.close(fd)
            raise

    def create_directory(self, directory: Path) -> None:
        self._verify_root_identity()
        relative_parts = self._relative_parts(directory)
        if not relative_parts:
            raise UnsafeDevelopmentPathError("开发数据根目录已存在。")
        parent_fd = _open_directory_at(
            self.root_fd, relative_parts[:-1], create=False
        )
        if parent_fd is None:
            raise FileNotFoundError(directory.parent)
        try:
            os.mkdir(relative_parts[-1], mode=0o700, dir_fd=parent_fd)
            directory_fd = os.open(
                relative_parts[-1],
                _directory_open_flags(),
                dir_fd=parent_fd,
            )
            os.close(directory_fd)
            self._verify_root_identity()
        finally:
            os.close(parent_fd)

    def ensure_regular_file(self, path: Path) -> tuple[int, int]:
        fd = self.open_regular_file(
            path,
            os.O_RDWR | os.O_CREAT,
            create_parents=True,
        )
        try:
            metadata = os.fstat(fd)
            return metadata.st_dev, metadata.st_ino
        finally:
            os.close(fd)

    def verify_regular_file(
        self, path: Path, identity: tuple[int, int] | None = None
    ) -> tuple[int, int]:
        fd = self.open_regular_file(path, os.O_RDWR)
        try:
            metadata = os.fstat(fd)
            current = (metadata.st_dev, metadata.st_ino)
            if identity is not None and current != identity:
                raise UnsafeDevelopmentPathError(
                    f"开发可写文件的文件系统身份已变化：{path}"
                )
            return current
        finally:
            os.close(fd)

    def write_text_atomic(self, path: Path, content: str) -> None:
        self.write_bytes_atomic(path, content.encode("utf-8"))

    def write_bytes_atomic(self, path: Path, content: bytes) -> None:
        relative_parts = self._relative_parts(path)
        if not relative_parts:
            raise UnsafeDevelopmentPathError("开发写入目标不能是数据根目录。")
        self._verify_root_identity()
        parent_fd = _open_directory_at(
            self.root_fd, relative_parts[:-1], create=True
        )
        assert parent_fd is not None
        temporary_name = (
            f".{relative_parts[-1]}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        temporary_fd: int | None = None
        try:
            self._validate_destination_at(parent_fd, relative_parts[-1], path)
            temporary_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _directory_nofollow_flag(),
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(temporary_fd, "wb", closefd=True) as handle:
                temporary_fd = None
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._validate_destination_at(parent_fd, relative_parts[-1], path)
            os.replace(
                temporary_name,
                relative_parts[-1],
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            self._verify_root_identity()
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def unlink_file(self, path: Path, *, missing_ok: bool = False) -> None:
        self._verify_root_identity()
        relative_parts = self._relative_parts(path)
        if not relative_parts:
            raise UnsafeDevelopmentPathError("不能删除开发数据根目录。")
        parent_fd = _open_directory_at(
            self.root_fd, relative_parts[:-1], create=False
        )
        if parent_fd is None:
            if missing_ok:
                return
            raise FileNotFoundError(path)
        try:
            try:
                metadata = os.stat(
                    relative_parts[-1], dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                if missing_ok:
                    return
                raise
            self._validate_regular_metadata(path, metadata)
            os.unlink(relative_parts[-1], dir_fd=parent_fd)
            self._verify_root_identity()
        finally:
            os.close(parent_fd)

    def replace_file(self, source: Path, destination: Path) -> None:
        self._verify_root_identity()
        source_parts = self._relative_parts(source)
        destination_parts = self._relative_parts(destination)
        if not source_parts or not destination_parts:
            raise UnsafeDevelopmentPathError("开发文件移动不能使用数据根目录。")
        source_parent = _open_directory_at(
            self.root_fd, source_parts[:-1], create=False
        )
        if source_parent is None:
            raise FileNotFoundError(source)
        destination_parent = _open_directory_at(
            self.root_fd, destination_parts[:-1], create=True
        )
        assert destination_parent is not None
        try:
            source_metadata = os.stat(
                source_parts[-1],
                dir_fd=source_parent,
                follow_symlinks=False,
            )
            self._validate_regular_metadata(source, source_metadata)
            self._validate_destination_at(
                destination_parent, destination_parts[-1], destination
            )
            os.replace(
                source_parts[-1],
                destination_parts[-1],
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
            self._verify_root_identity()
        finally:
            os.close(destination_parent)
            os.close(source_parent)

    def move_directory(self, source: Path, destination: Path) -> None:
        self._verify_root_identity()
        source_parts = self._relative_parts(source)
        destination_parts = self._relative_parts(destination)
        if not source_parts or not destination_parts:
            raise UnsafeDevelopmentPathError(
                "开发目录移动不能使用数据根目录。"
            )
        source_parent = _open_directory_at(
            self.root_fd, source_parts[:-1], create=False
        )
        if source_parent is None:
            raise FileNotFoundError(source)
        destination_parent = _open_directory_at(
            self.root_fd, destination_parts[:-1], create=False
        )
        if destination_parent is None:
            os.close(source_parent)
            raise FileNotFoundError(destination.parent)
        try:
            metadata = os.stat(
                source_parts[-1],
                dir_fd=source_parent,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(metadata.st_mode):
                _unsafe_directory_error(source)
            source_fd = os.open(
                source_parts[-1],
                _directory_open_flags(),
                dir_fd=source_parent,
            )
            os.close(source_fd)
            try:
                os.stat(
                    destination_parts[-1],
                    dir_fd=destination_parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise UnsafeDevelopmentPathError(
                    f"开发目录移动目标已存在：{destination}"
                )
            os.rename(
                source_parts[-1],
                destination_parts[-1],
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
            )
            self._verify_root_identity()
        finally:
            os.close(destination_parent)
            os.close(source_parent)

    def clear_directory_contents(self, path: Path) -> None:
        directory_fd = self.open_directory(path, create=True)
        assert directory_fd is not None
        try:
            self._clear_directory_fd(directory_fd, path)
            self._verify_root_identity()
        finally:
            os.close(directory_fd)

    def read_text(self, path: Path) -> str:
        fd = self.open_regular_file(path, os.O_RDONLY)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as handle:
            return handle.read()

    def read_bytes(self, path: Path) -> bytes:
        fd = self.open_regular_file(path, os.O_RDONLY)
        with os.fdopen(fd, "rb", closefd=True) as handle:
            return handle.read()

    def regular_file_size(self, path: Path) -> int:
        fd = self.open_regular_file(path, os.O_RDONLY)
        try:
            return os.fstat(fd).st_size
        finally:
            os.close(fd)

    def regular_file_exists(self, path: Path) -> bool:
        try:
            fd = self.open_regular_file(path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        os.close(fd)
        return True

    def list_regular_files(
        self,
        directory: Path,
        *,
        prefix: str = "",
        suffix: str = "",
    ) -> tuple[Path, ...]:
        directory_fd = self.open_directory(directory, create=False)
        if directory_fd is None:
            return ()
        try:
            found: list[Path] = []
            for entry in os.scandir(directory_fd):
                if not entry.name.startswith(prefix) or not entry.name.endswith(suffix):
                    continue
                metadata = entry.stat(follow_symlinks=False)
                entry_path = directory / entry.name
                self._validate_regular_metadata(entry_path, metadata)
                found.append(entry_path)
            return tuple(sorted(found))
        finally:
            os.close(directory_fd)

    def remove_directory_tree(
        self, path: Path, *, missing_ok: bool = False
    ) -> None:
        self._verify_root_identity()
        relative_parts = self._relative_parts(path)
        if not relative_parts:
            raise UnsafeDevelopmentPathError("不能删除开发数据根目录。")
        parent_fd = _open_directory_at(
            self.root_fd, relative_parts[:-1], create=False
        )
        if parent_fd is None:
            if missing_ok:
                return
            raise FileNotFoundError(path)
        try:
            try:
                metadata = os.stat(
                    relative_parts[-1],
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if missing_ok:
                    return
                raise
            if not stat.S_ISDIR(metadata.st_mode):
                _unsafe_directory_error(path)
            try:
                directory_fd = os.open(
                    relative_parts[-1],
                    _directory_open_flags(),
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                _unsafe_directory_error(path, exc)
            try:
                self._clear_directory_fd(directory_fd, path)
            finally:
                os.close(directory_fd)
            os.rmdir(relative_parts[-1], dir_fd=parent_fd)
            self._verify_root_identity()
        finally:
            os.close(parent_fd)

    def _relative_parts(self, path: Path) -> tuple[str, ...]:
        root = Path(os.path.abspath(os.fspath(self.config.paths.root.expanduser())))
        absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
        try:
            return absolute.relative_to(root).parts
        except ValueError as exc:
            raise UnsafeDevelopmentPathError(
                "开发可写路径必须位于已固定的数据根目录中。"
            ) from exc

    def _clear_directory_fd(self, directory_fd: int, path: Path) -> None:
        for entry in list(os.scandir(directory_fd)):
            entry_path = path / entry.name
            metadata = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(
                    entry.name,
                    _directory_open_flags(),
                    dir_fd=directory_fd,
                )
                try:
                    self._clear_directory_fd(child_fd, entry_path)
                finally:
                    os.close(child_fd)
                os.rmdir(entry.name, dir_fd=directory_fd)
                continue
            self._validate_regular_metadata(entry_path, metadata)
            os.unlink(entry.name, dir_fd=directory_fd)

    @staticmethod
    def _validate_regular_metadata(path: Path, metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise UnsafeDevelopmentPathError(
                f"开发可写资源必须是普通文件：{path}"
            )
        if metadata.st_nlink > 1:
            raise UnsafeDevelopmentPathError(
                f"开发可写资源不能是硬链接：{path}"
            )

    @classmethod
    def _validate_destination_at(
        cls, parent_fd: int, name: str, path: Path
    ) -> None:
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        cls._validate_regular_metadata(path, metadata)

    def ensure_directories(self) -> None:
        self._verify_root_identity()
        os.fchmod(self.root_fd, 0o700)
        for directory in self.config.paths.required_directories:
            directory_fd = self.open_directory(directory, create=True)
            assert directory_fd is not None
            try:
                os.fchmod(directory_fd, 0o700)
            finally:
                os.close(directory_fd)
        self.verify()


def assert_supported_platform() -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise UnsupportedPlatformError(
            "Audio Memory 第一阶段仅支持 macOS Apple Silicon。"
        )
