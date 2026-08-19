#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import ClassVar


class GovernanceError(RuntimeError):
    """功能轨道状态不安全、不完整或与仓库不一致。"""


@dataclass(frozen=True, slots=True)
class FeatureRecord:
    schema_version: int
    feature_id: str
    branch: str
    base_branch: str
    target_version: str
    status: str
    worktree: str
    base_commit: str
    head_commit: str
    current_step: str
    required_checks: tuple[str, ...]
    passed_checks: tuple[str, ...]
    merge_approved: bool

    VALID_STATUSES: ClassVar[frozenset[str]] = frozenset(
        {"in_progress", "ready_to_merge", "merged", "deferred", "released"}
    )
    REQUIRED_CHECKS: ClassVar[tuple[str, ...]] = (
        "backend",
        "frontend",
        "browser",
        "runtime-isolation",
    )
    _FEATURE_ID: ClassVar[re.Pattern[str]] = re.compile(
        r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"
    )
    _VERSION: ClassVar[re.Pattern[str]] = re.compile(
        r"^v[0-9]+\.[0-9]+\.[0-9]+-beta\.[0-9]+$"
    )
    _SHA: ClassVar[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")

    @classmethod
    def new(
        cls,
        feature_id: str,
        base_commit: str,
        *,
        target_version: str = "v0.1.0-beta.3",
    ) -> FeatureRecord:
        cls._validate_feature_id(feature_id)
        return cls.from_dict(
            {
                "schema_version": 1,
                "feature_id": feature_id,
                "branch": f"codex/{feature_id}",
                "base_branch": "main",
                "target_version": target_version,
                "status": "in_progress",
                "worktree": f".worktrees/{feature_id}",
                "base_commit": base_commit,
                "head_commit": base_commit,
                "current_step": "开始开发",
                "required_checks": list(cls.REQUIRED_CHECKS),
                "passed_checks": [],
                "merge_approved": False,
            }
        )

    @classmethod
    def from_dict(cls, payload: object) -> FeatureRecord:
        if not isinstance(payload, dict):
            raise GovernanceError("功能状态文件必须是 JSON 对象。")
        expected = {item.name for item in fields(cls)}
        if set(payload) != expected:
            raise GovernanceError("功能状态文件字段不完整或包含未知字段。")
        try:
            schema_version = payload["schema_version"]
            feature_id = payload["feature_id"]
            branch = payload["branch"]
            base_branch = payload["base_branch"]
            target_version = payload["target_version"]
            status_value = payload["status"]
            worktree = payload["worktree"]
            base_commit = payload["base_commit"]
            head_commit = payload["head_commit"]
            current_step = payload["current_step"]
            required_checks = payload["required_checks"]
            passed_checks = payload["passed_checks"]
            merge_approved = payload["merge_approved"]
            cls._validate_feature_id(feature_id)
            if schema_version != 1 or isinstance(schema_version, bool):
                raise ValueError
            if branch != f"codex/{feature_id}" or base_branch != "main":
                raise ValueError
            if not isinstance(target_version, str) or not cls._VERSION.fullmatch(
                target_version
            ):
                raise ValueError
            if status_value not in cls.VALID_STATUSES:
                raise ValueError
            if worktree != f".worktrees/{feature_id}":
                raise ValueError
            if not isinstance(base_commit, str) or not cls._SHA.fullmatch(base_commit):
                raise ValueError
            if not isinstance(head_commit, str) or not cls._SHA.fullmatch(head_commit):
                raise ValueError
            if not isinstance(current_step, str) or not current_step.strip():
                raise ValueError
            if required_checks != list(cls.REQUIRED_CHECKS):
                raise ValueError
            if not isinstance(passed_checks, list) or any(
                item not in cls.REQUIRED_CHECKS for item in passed_checks
            ) or len(set(passed_checks)) != len(passed_checks):
                raise ValueError
            if not isinstance(merge_approved, bool):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise GovernanceError("功能状态文件内容无效。") from exc
        return cls(
            schema_version=1,
            feature_id=feature_id,
            branch=branch,
            base_branch=base_branch,
            target_version=target_version,
            status=status_value,
            worktree=worktree,
            base_commit=base_commit,
            head_commit=head_commit,
            current_step=current_step,
            required_checks=tuple(required_checks),
            passed_checks=tuple(passed_checks),
            merge_approved=merge_approved,
        )

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["required_checks"] = list(self.required_checks)
        payload["passed_checks"] = list(self.passed_checks)
        return payload

    @classmethod
    def _validate_feature_id(cls, value: object) -> None:
        if not isinstance(value, str) or not cls._FEATURE_ID.fullmatch(value):
            raise GovernanceError(
                "功能标识符只能使用小写字母、数字和中划线，"
                "且必须以字母或数字开头和结尾。"
            )


class FeatureStore:
    def __init__(self, git_common_dir: Path) -> None:
        self.git_common_dir = git_common_dir.resolve()
        self.governance_root = self.git_common_dir / "audio-memory-governance"
        self.features_root = self.governance_root / "features"

    def load(self, feature_id: str) -> FeatureRecord:
        FeatureRecord._validate_feature_id(feature_id)
        self._validate_directory(self.governance_root, create=False)
        self._validate_directory(self.features_root, create=False)
        path = self.features_root / f"{feature_id}.json"
        self._validate_regular_file(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = FeatureRecord.from_dict(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, GovernanceError) as exc:
            if isinstance(exc, GovernanceError):
                raise
            raise GovernanceError("功能状态文件无法安全读取。") from exc
        if record.feature_id != feature_id:
            raise GovernanceError("功能状态文件与文件名不一致。")
        return record

    def save(self, record: FeatureRecord) -> Path:
        validated = FeatureRecord.from_dict(record.to_dict())
        self._validate_directory(self.governance_root, create=True)
        self._validate_directory(self.features_root, create=True)
        destination = self.features_root / f"{validated.feature_id}.json"
        if destination.exists() or destination.is_symlink():
            self._validate_regular_file(destination)
        body = (
            json.dumps(
                validated.to_dict(), ensure_ascii=False, indent=2, sort_keys=False
            )
            + "\n"
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{validated.feature_id}.",
            suffix=".tmp",
            dir=self.features_root,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory_fd = os.open(self.features_root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return destination

    @staticmethod
    def _validate_directory(path: Path, *, create: bool) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if not create:
                raise GovernanceError("功能状态文件不存在。")
            try:
                path.mkdir(mode=0o700)
            except FileNotFoundError as exc:
                raise GovernanceError("功能状态目录的父目录不存在。") from exc
            metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise GovernanceError("拒绝使用符号链接的功能状态目录。")
        if not stat.S_ISDIR(metadata.st_mode):
            raise GovernanceError("功能状态目录必须是真实目录。")

    @staticmethod
    def _validate_regular_file(path: Path) -> None:
        try:
            metadata = path.lstat()
        except FileNotFoundError as exc:
            raise GovernanceError("功能状态文件不存在。") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise GovernanceError("拒绝读写符号链接的功能状态文件。")
        if not stat.S_ISREG(metadata.st_mode):
            raise GovernanceError("功能状态文件必须是普通文件。")
        if metadata.st_nlink != 1:
            raise GovernanceError("拒绝读写硬链接的功能状态文件。")


__all__ = ["FeatureRecord", "FeatureStore", "GovernanceError"]
