#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, fields, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Callable, ClassVar


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
        r"^v[0-9]+\.[0-9]+\.[0-9]+-beta\.[0-9]+(?:-hotfix\.[0-9]+)?$"
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

    def prepare(self) -> None:
        self._validate_directory(self.governance_root, create=True)
        self._validate_directory(self.features_root, create=True)

    def exists(self, feature_id: str) -> bool:
        FeatureRecord._validate_feature_id(feature_id)
        path = self.features_root / f"{feature_id}.json"
        return path.exists() or path.is_symlink()

    def list(self) -> tuple[FeatureRecord, ...]:
        if not self.governance_root.exists():
            return ()
        self._validate_directory(self.governance_root, create=False)
        if not self.features_root.exists():
            return ()
        self._validate_directory(self.features_root, create=False)
        records = [self.load(path.stem) for path in self.features_root.glob("*.json")]
        return tuple(sorted(records, key=lambda item: item.feature_id))

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


@dataclass(frozen=True, slots=True)
class WorktreeInfo:
    path: Path
    head_commit: str
    branch: str | None


class GitRepository:
    def __init__(self, checkout: Path) -> None:
        self.checkout = checkout.resolve()
        self.top_level = Path(self._git("rev-parse", "--show-toplevel")).resolve()
        raw_common = Path(self._git("rev-parse", "--git-common-dir"))
        self.common_dir = (
            raw_common if raw_common.is_absolute() else self.top_level / raw_common
        ).resolve()
        self.repository_root = self.common_dir.parent

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.checkout), *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def _git(self, *args: str) -> str:
        result = self._run(*args)
        if result.returncode != 0:
            diagnostic = (result.stderr or result.stdout).strip()
            raise GovernanceError(f"Git 操作失败：{diagnostic}")
        return result.stdout.strip()

    @property
    def current_branch(self) -> str:
        branch = self._git("branch", "--show-current")
        if not branch:
            raise GovernanceError("当前 Git 检出处于 detached HEAD。")
        return branch

    @property
    def head_commit(self) -> str:
        return self._git("rev-parse", "HEAD")

    @property
    def is_clean(self) -> bool:
        return not self._git("status", "--porcelain")

    def branch_exists(self, branch: str) -> bool:
        result = self._run("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        if result.returncode not in {0, 1}:
            raise GovernanceError("Git 分支检查失败。")
        return result.returncode == 0

    def tag_exists(self, tag: str) -> bool:
        result = self._run("show-ref", "--verify", "--quiet", f"refs/tags/{tag}")
        if result.returncode not in {0, 1}:
            raise GovernanceError("Git 标签检查失败。")
        return result.returncode == 0

    def worktree_root_is_ignored(self) -> bool:
        result = self._run(
            "check-ignore", "--quiet", ".worktrees/governance-probe"
        )
        if result.returncode not in {0, 1}:
            raise GovernanceError("Git ignore 规则检查失败。")
        return result.returncode == 0

    def worktrees(self) -> tuple[WorktreeInfo, ...]:
        entries: list[WorktreeInfo] = []
        current: dict[str, str] = {}
        output = self._git("worktree", "list", "--porcelain")
        for line in [*output.splitlines(), ""]:
            if not line:
                if current:
                    branch_ref = current.get("branch")
                    entries.append(
                        WorktreeInfo(
                            path=Path(current["worktree"]).resolve(),
                            head_commit=current["HEAD"],
                            branch=(
                                branch_ref.removeprefix("refs/heads/")
                                if branch_ref
                                else None
                            ),
                        )
                    )
                    current = {}
                continue
            key, _, value = line.partition(" ")
            if key in {"worktree", "HEAD", "branch"}:
                current[key] = value
        return tuple(entries)

    def create_worktree(self, feature_id: str) -> Path:
        branch = f"codex/{feature_id}"
        path = (self.repository_root / ".worktrees" / feature_id).resolve()
        result = self._run(
            "worktree", "add", str(path), "-b", branch, "main"
        )
        if result.returncode != 0:
            diagnostic = (result.stderr or result.stdout).strip()
            raise GovernanceError(f"创建功能 worktree 失败：{diagnostic}")
        return path


@dataclass(frozen=True, slots=True)
class FeatureStatus:
    record: FeatureRecord
    path: Path
    valid: bool
    diagnostic: str | None = None


class FeatureService:
    def __init__(self, repository: GitRepository) -> None:
        self.repository = repository
        self.store = FeatureStore(repository.common_dir)

    def start(self, feature_id: str, target_version: str) -> FeatureStatus:
        FeatureRecord._validate_feature_id(feature_id)
        self.store.prepare()
        if self.store.exists(feature_id):
            record = self.store.load(feature_id)
            if record.target_version != target_version:
                raise GovernanceError("功能开发进度记录的目标版本不一致。")
            return self._verified_status(record)
        if self.repository.current_branch != "main" or not self.repository.is_clean:
            raise GovernanceError(
                "新功能必须从 main 的干净检出创建；"
                "当前 main 存在未提交修改或当前不在 main。"
            )
        if not self.repository.worktree_root_is_ignored():
            raise GovernanceError(
                ".worktrees 未被 Git ignore 规则保护，拒绝创建功能 worktree。"
            )
        branch = f"codex/{feature_id}"
        path = (self.repository.repository_root / ".worktrees" / feature_id).resolve()
        if self.repository.branch_exists(branch) or path.exists():
            raise GovernanceError(
                "功能分支或 worktree 已存在，但缺少开发进度记录。"
            )
        base_commit = self.repository.head_commit
        created_path = self.repository.create_worktree(feature_id)
        record = FeatureRecord.new(
            feature_id,
            base_commit,
            target_version=target_version,
        )
        self.store.save(record)
        return FeatureStatus(record=record, path=created_path, valid=True)

    def status(self, feature_id: str | None = None) -> tuple[FeatureStatus, ...]:
        records = (
            (self.store.load(feature_id),)
            if feature_id is not None
            else self.store.list()
        )
        statuses: list[FeatureStatus] = []
        for record in records:
            try:
                statuses.append(self._verified_status(record))
            except GovernanceError as exc:
                statuses.append(
                    FeatureStatus(
                        record=record,
                        path=self._expected_path(record),
                        valid=False,
                        diagnostic=str(exc),
                    )
                )
        return tuple(statuses)

    def finish(
        self,
        feature_id: str,
        gate_runner: Callable[[Path], tuple[str, ...]],
    ) -> FeatureStatus:
        record = self.store.load(feature_id)
        verified = self._verified_status(record)
        feature_repository = GitRepository(verified.path)
        if feature_repository.current_branch != record.branch:
            raise GovernanceError("当前 worktree 不属于记录的功能分支。")
        if not feature_repository.is_clean:
            raise GovernanceError("功能 worktree 存在未提交修改。")
        passed = tuple(gate_runner(verified.path))
        missing = [
            check for check in FeatureRecord.REQUIRED_CHECKS if check not in passed
        ]
        unexpected = [
            check for check in passed if check not in FeatureRecord.REQUIRED_CHECKS
        ]
        if missing or unexpected or len(set(passed)) != len(passed):
            details = ", ".join([*missing, *unexpected])
            raise GovernanceError(f"功能完成门禁未全部通过：{details}")
        completed = replace(
            record,
            status="ready_to_merge",
            head_commit=feature_repository.head_commit,
            current_step="等待合并",
            passed_checks=FeatureRecord.REQUIRED_CHECKS,
            merge_approved=False,
        )
        self.store.save(completed)
        return FeatureStatus(record=completed, path=verified.path, valid=True)

    def adopt_preview(
        self, feature_id: str, target_version: str
    ) -> tuple[FeatureRecord, str]:
        FeatureRecord._validate_feature_id(feature_id)
        if self.store.exists(feature_id):
            raise GovernanceError("功能轨道已经纳管。")
        expected_branch = f"codex/{feature_id}"
        expected_path = (
            self.repository.repository_root / ".worktrees" / feature_id
        ).resolve()
        if (
            self.repository.current_branch != expected_branch
            or self.repository.top_level != expected_path
            or not self.repository.is_clean
        ):
            raise GovernanceError(
                "审计纳管必须从同名、干净的功能 worktree 执行。"
            )
        base_commit = self.repository._git("merge-base", "main", "HEAD")
        record = replace(
            FeatureRecord.new(
                feature_id, base_commit, target_version=target_version
            ),
            head_commit=self.repository.head_commit,
            current_step="已审计现有轨道，等待继续开发",
        )
        digest = hashlib.sha256(
            json.dumps(
                record.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return record, digest

    def adopt(
        self,
        feature_id: str,
        target_version: str,
        approval_token: str | None,
    ) -> FeatureStatus:
        record, digest = self.adopt_preview(feature_id, target_version)
        if approval_token is None or approval_token != digest:
            raise GovernanceError("当前轨道未获得精确的纳管确认。")
        self.store.save(record)
        return self._verified_status(record)

    def _verified_status(self, record: FeatureRecord) -> FeatureStatus:
        expected_path = self._expected_path(record)
        matching = [
            item
            for item in self.repository.worktrees()
            if item.path == expected_path and item.branch == record.branch
        ]
        if not self.repository.branch_exists(record.branch) or len(matching) != 1:
            raise GovernanceError(
                f"功能开发进度记录与 worktree 或分支不一致：{record.feature_id}"
            )
        effective_record = record
        if (
            record.status == "ready_to_merge"
            and matching[0].head_commit != record.head_commit
        ):
            effective_record = replace(
                record,
                status="in_progress",
                current_step="代码已变更，需重新验收",
                passed_checks=(),
                merge_approved=False,
            )
        return FeatureStatus(
            record=effective_record, path=expected_path, valid=True
        )

    def _expected_path(self, record: FeatureRecord) -> Path:
        return (self.repository.repository_root / record.worktree).resolve()


@dataclass(frozen=True, slots=True)
class ReleaseFeature:
    feature_id: str
    tested_commit: str

    @classmethod
    def from_dict(cls, payload: object) -> ReleaseFeature:
        if not isinstance(payload, dict) or set(payload) != {
            "feature_id", "tested_commit"
        }:
            raise GovernanceError("候选清单功能项无效。")
        FeatureRecord._validate_feature_id(payload["feature_id"])
        commit = payload["tested_commit"]
        if not isinstance(commit, str) or not FeatureRecord._SHA.fullmatch(commit):
            raise GovernanceError("候选清单提交无效。")
        return cls(payload["feature_id"], commit)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    schema_version: int
    target_version: str
    main_commit: str
    features: tuple[ReleaseFeature, ...]

    @classmethod
    def from_dict(cls, payload: object) -> ReleaseManifest:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "target_version", "main_commit", "features"
        }:
            raise GovernanceError("候选清单字段无效。")
        version = payload["target_version"]
        main_commit = payload["main_commit"]
        raw_features = payload["features"]
        if (
            payload["schema_version"] != 1
            or not isinstance(version, str)
            or not FeatureRecord._VERSION.fullmatch(version)
            or not isinstance(main_commit, str)
            or not FeatureRecord._SHA.fullmatch(main_commit)
            or not isinstance(raw_features, list)
            or not raw_features
        ):
            raise GovernanceError("候选清单内容无效。")
        features = tuple(ReleaseFeature.from_dict(item) for item in raw_features)
        if len({item.feature_id for item in features}) != len(features):
            raise GovernanceError("候选清单不能包含重复功能。")
        return cls(1, version, main_commit, features)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target_version": self.target_version,
            "main_commit": self.main_commit,
            "features": [item.to_dict() for item in self.features],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    merged: tuple[str, ...]
    failed: str | None
    main_commit: str


class ReleaseStore:
    def __init__(self, common_dir: Path) -> None:
        self.root = common_dir.resolve() / "audio-memory-governance"
        self.releases = self.root / "releases"

    def manifest_path(self, version: str) -> Path:
        if not FeatureRecord._VERSION.fullmatch(version):
            raise GovernanceError("发布版本号无效。")
        return self.releases / f"{version}-candidate.json"

    def save_manifest(self, manifest: ReleaseManifest) -> Path:
        path = self.manifest_path(manifest.target_version)
        self._atomic_json(path, manifest.to_dict())
        return path

    def load_manifest(self, path: Path) -> ReleaseManifest:
        resolved = path.resolve()
        expected_parent = self.releases.resolve()
        if resolved.parent != expected_parent or path.is_symlink():
            raise GovernanceError("候选清单路径无效。")
        FeatureStore._validate_regular_file(resolved)
        try:
            manifest = ReleaseManifest.from_dict(
                json.loads(resolved.read_text(encoding="utf-8"))
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GovernanceError("候选清单无法读取。") from exc
        if resolved != self.manifest_path(manifest.target_version).resolve():
            raise GovernanceError("候选清单文件名与版本不一致。")
        return manifest

    def save_receipt(self, manifest: ReleaseManifest, main_commit: str) -> Path:
        path = self.releases / f"{manifest.target_version}-integrated.json"
        self._atomic_json(path, {
            "schema_version": 1,
            "target_version": manifest.target_version,
            "candidate_digest": manifest.digest(),
            "main_commit": main_commit,
            "features": [item.feature_id for item in manifest.features],
        })
        return path

    def load_receipt(self, version: str) -> dict[str, object]:
        path = self.releases / f"{version}-integrated.json"
        FeatureStore._validate_regular_file(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GovernanceError("集成回执无法读取。") from exc
        expected = {
            "schema_version", "target_version", "candidate_digest",
            "main_commit", "features",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload["schema_version"] != 1
            or payload["target_version"] != version
            or not isinstance(payload["candidate_digest"], str)
            or not isinstance(payload["main_commit"], str)
            or not FeatureRecord._SHA.fullmatch(payload["main_commit"])
            or not isinstance(payload["features"], list)
        ):
            raise GovernanceError("集成回执内容无效。")
        return payload

    def _atomic_json(self, path: Path, payload: object) -> None:
        FeatureStore._validate_directory(self.root, create=True)
        FeatureStore._validate_directory(self.releases, create=True)
        if path.exists() or path.is_symlink():
            FeatureStore._validate_regular_file(path)
        body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.releases)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)


class ReleaseService:
    def __init__(self, repository: GitRepository) -> None:
        self.repository = repository
        self.features = FeatureService(repository)
        self.store = ReleaseStore(repository.common_dir)

    def prepare(
        self, version: str, feature_ids: list[str]
    ) -> tuple[ReleaseManifest, Path]:
        if self.repository.current_branch != "main" or not self.repository.is_clean:
            raise GovernanceError("候选清单必须从干净的 main 生成。")
        if not feature_ids or len(set(feature_ids)) != len(feature_ids):
            raise GovernanceError("候选功能不能为空或重复。")
        selected: list[ReleaseFeature] = []
        for feature_id in feature_ids:
            status = self.features.status(feature_id)[0]
            record = status.record
            if (
                not status.valid
                or record.status != "ready_to_merge"
                or record.target_version != version
                or record.head_commit != GitRepository(status.path).head_commit
            ):
                raise GovernanceError(f"功能 {feature_id} 尚未通过当前提交验收。")
            selected.append(ReleaseFeature(feature_id, record.head_commit))
        manifest = ReleaseManifest(1, version, self.repository.head_commit, tuple(selected))
        return manifest, self.store.save_manifest(manifest)

    def preview_integrated_main(
        self, version: str, feature_ids: list[str]
    ) -> ReleaseManifest:
        if self.repository.current_branch != "main" or not self.repository.is_clean:
            raise GovernanceError("最终候选必须从干净的 main 固化。")
        if (
            not FeatureRecord._VERSION.fullmatch(version)
            or not feature_ids
            or len(set(feature_ids)) != len(feature_ids)
        ):
            raise GovernanceError("候选版本或已集成功能列表无效。")
        for feature_id in feature_ids:
            FeatureRecord._validate_feature_id(feature_id)
        head_commit = self.repository.head_commit
        return ReleaseManifest(
            1,
            version,
            head_commit,
            tuple(ReleaseFeature(feature_id, head_commit) for feature_id in feature_ids),
        )

    def seal_integrated_main(
        self,
        version: str,
        feature_ids: list[str],
        approval_token: str | None,
        gate_runner: Callable[[Path], bool],
    ) -> tuple[ReleaseManifest, Path]:
        manifest = self.preview_integrated_main(version, feature_ids)
        if approval_token is None or approval_token != manifest.digest():
            raise GovernanceError("最终候选未获得摘要精确确认。")
        if not gate_runner(self.repository.top_level):
            raise GovernanceError("最终候选全量验收失败。")
        if (
            self.repository.current_branch != "main"
            or not self.repository.is_clean
            or self.repository.head_commit != manifest.main_commit
        ):
            raise GovernanceError("验收期间 main 已变更，请重新生成候选摘要。")
        path = self.store.save_manifest(manifest)
        self.store.save_receipt(manifest, manifest.main_commit)
        return manifest, path

    def records_to_mark_released(
        self, manifest: ReleaseManifest
    ) -> tuple[FeatureRecord, ...]:
        records: list[FeatureRecord] = []
        for item in manifest.features:
            if not self.features.store.exists(item.feature_id):
                continue
            record = self.features.store.load(item.feature_id)
            if (
                record.target_version != manifest.target_version
                or record.status not in {"merged", "released"}
            ):
                raise GovernanceError(
                    f"功能 {item.feature_id} 的发布状态无效。"
                )
            records.append(record)
        return tuple(records)

    def integrate(
        self,
        manifest_path: Path,
        approval_token: str | None,
        gate_runner: Callable[[str, Path], bool],
    ) -> IntegrationResult:
        manifest = self.store.load_manifest(manifest_path)
        if approval_token is None or approval_token != manifest.digest():
            raise GovernanceError("候选清单未获得精确合并确认。")
        if (
            self.repository.current_branch != "main"
            or not self.repository.is_clean
            or self.repository.head_commit != manifest.main_commit
        ):
            raise GovernanceError("main 已变更或不干净，请重新生成候选清单。")
        for item in manifest.features:
            status = self.features.status(item.feature_id)[0]
            if (
                not status.valid
                or status.record.status != "ready_to_merge"
                or status.record.head_commit != item.tested_commit
                or GitRepository(status.path).head_commit != item.tested_commit
            ):
                raise GovernanceError(f"功能 {item.feature_id} 的验收证据已过期。")

        merged: list[str] = []
        for item in manifest.features:
            before = self.repository.head_commit
            merge = self.repository._run(
                "merge", "--no-ff", "--no-edit", item.tested_commit
            )
            if merge.returncode != 0:
                self.repository._run("merge", "--abort")
                self._invalidate(item.feature_id, "合并冲突，需要修复")
                return IntegrationResult(tuple(merged), item.feature_id, self.repository.head_commit)
            if not gate_runner(item.feature_id, self.repository.top_level):
                self.repository._git("reset", "--hard", before)
                self._invalidate(item.feature_id, "集成验收失败，需要修复")
                return IntegrationResult(tuple(merged), item.feature_id, self.repository.head_commit)
            record = self.features.store.load(item.feature_id)
            self.features.store.save(replace(
                record,
                status="merged",
                current_step="已合并至 main，等待发布",
                merge_approved=True,
            ))
            merged.append(item.feature_id)
        self.store.save_receipt(manifest, self.repository.head_commit)
        return IntegrationResult(tuple(merged), None, self.repository.head_commit)

    def _invalidate(self, feature_id: str, step: str) -> None:
        record = self.features.store.load(feature_id)
        self.features.store.save(replace(
            record,
            status="in_progress",
            current_step=step,
            passed_checks=(),
            merge_approved=False,
        ))

    def authorize_build(
        self, manifest_path: Path, approval_token: str | None
    ) -> ReleaseManifest:
        manifest = self.store.load_manifest(manifest_path)
        if approval_token is None or approval_token != manifest.digest():
            raise GovernanceError("候选版本未获得独立的发布确认。")
        if self.repository.current_branch != "main" or not self.repository.is_clean:
            raise GovernanceError("发布必须从干净的 main 执行。")
        receipt = self.store.load_receipt(manifest.target_version)
        if (
            receipt["candidate_digest"] != manifest.digest()
            or receipt["main_commit"] != self.repository.head_commit
            or receipt["features"] != [item.feature_id for item in manifest.features]
        ):
            raise GovernanceError("候选清单与已验收集成结果不一致。")
        version_file = self.repository.top_level / "VERSION"
        try:
            repository_version = version_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise GovernanceError("无法读取发布版本号。") from exc
        if f"v{repository_version}" != manifest.target_version:
            raise GovernanceError("VERSION 与候选版本不一致。")
        if self.repository.tag_exists(manifest.target_version):
            raise GovernanceError("发布版本标签已存在，不可覆盖。")
        return manifest


def _repository_from_current_directory() -> GitRepository:
    return GitRepository(Path.cwd())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audio Memory 功能轨道管理")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("feature_id")
    start_parser.add_argument(
        "--target-version", default="v0.1.0-beta.3"
    )
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("feature_id", nargs="?")
    status_parser.add_argument("--adopt-preview", action="store_true")
    status_parser.add_argument("--adopt", action="store_true")
    status_parser.add_argument("--approve")
    status_parser.add_argument(
        "--target-version", default="v0.1.0-beta.3"
    )
    finish_parser = subparsers.add_parser("finish")
    finish_parser.add_argument("feature_id")
    prepare_parser = subparsers.add_parser("release-prepare")
    prepare_parser.add_argument("version")
    prepare_parser.add_argument("feature_ids", nargs="+")
    seal_parser = subparsers.add_parser("release-seal")
    seal_parser.add_argument("version")
    seal_parser.add_argument("feature_ids", nargs="+")
    seal_parser.add_argument("--preview", action="store_true")
    seal_parser.add_argument("--approve")
    integrate_parser = subparsers.add_parser("release-integrate")
    integrate_parser.add_argument("manifest", type=Path)
    integrate_parser.add_argument("--approve", required=True)
    build_parser = subparsers.add_parser("release-build")
    build_parser.add_argument("version")
    build_parser.add_argument("--approve", required=True)
    arguments = parser.parse_args(argv)
    try:
        service = FeatureService(_repository_from_current_directory())
        if arguments.command == "start":
            status = service.start(arguments.feature_id, arguments.target_version)
            print(json.dumps({
                "feature_id": status.record.feature_id,
                "branch": status.record.branch,
                "worktree": str(status.path),
                "status": status.record.status,
            }, ensure_ascii=False), flush=True)
            if os.environ.get("AUDIO_MEMORY_FEATURE_NO_RUNTIME") != "1":
                controller_root = Path(__file__).resolve().parent.parent
                runtime_script = controller_root / "scripts/feature_runtime.py"
                os.execve(
                    sys.executable,
                    [
                        sys.executable,
                        str(runtime_script),
                        "start",
                        "--feature-id",
                        status.record.feature_id,
                        "--git-common-dir",
                        str(service.repository.common_dir),
                        "--controller-root",
                        str(controller_root),
                        "--feature-root",
                        str(status.path),
                        "--home",
                        str(Path.home()),
                    ],
                    dict(os.environ),
                )
        elif arguments.command == "status":
            if arguments.adopt_preview or arguments.adopt:
                if not arguments.feature_id:
                    raise GovernanceError("审计纳管必须指定功能标识。")
                if arguments.adopt_preview and arguments.adopt:
                    raise GovernanceError("纳管预览与写入不能同时执行。")
                if arguments.adopt_preview:
                    record, digest = service.adopt_preview(
                        arguments.feature_id, arguments.target_version
                    )
                    print(json.dumps({
                        "mode": "read_only_preview",
                        "preview_digest": digest,
                        "record": record.to_dict(),
                    }, ensure_ascii=False))
                else:
                    adopted = service.adopt(
                        arguments.feature_id,
                        arguments.target_version,
                        arguments.approve,
                    )
                    print(json.dumps({
                        "feature_id": adopted.record.feature_id,
                        "branch": adopted.record.branch,
                        "worktree": str(adopted.path),
                        "status": adopted.record.status,
                        "head_commit": adopted.record.head_commit,
                    }, ensure_ascii=False))
            else:
                statuses = service.status(arguments.feature_id)
                print(json.dumps([
                    {
                        "feature_id": item.record.feature_id,
                        "branch": item.record.branch,
                        "worktree": str(item.path),
                        "status": item.record.status,
                        "valid": item.valid,
                        "diagnostic": item.diagnostic,
                    }
                    for item in statuses
                ], ensure_ascii=False))
        elif arguments.command == "finish":
            controller_root = Path(__file__).resolve().parent.parent
            gate = controller_root / "scripts/quality-gate.sh"

            def run_gate(feature_root: Path) -> tuple[str, ...]:
                environment = dict(os.environ)
                environment.setdefault(
                    "AUDIO_MEMORY_TOOLCHAIN_ROOT", str(controller_root)
                )
                result = subprocess.run(
                    [str(gate)],
                    cwd=feature_root,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                if result.returncode != 0:
                    diagnostic = (result.stderr or result.stdout).strip()
                    raise GovernanceError(f"功能门禁失败：{diagnostic}")
                return tuple(result.stdout.splitlines()[-4:])

            finished = service.finish(arguments.feature_id, run_gate)
            print(json.dumps({
                "feature_id": finished.record.feature_id,
                "status": finished.record.status,
                "tested_commit": finished.record.head_commit,
                "passed_checks": list(finished.record.passed_checks),
            }, ensure_ascii=False))
        elif arguments.command == "release-prepare":
            release = ReleaseService(service.repository)
            manifest, path = release.prepare(
                arguments.version, arguments.feature_ids
            )
            print(json.dumps({
                "manifest": str(path),
                "candidate_digest": manifest.digest(),
                "main_commit": manifest.main_commit,
                "features": [item.to_dict() for item in manifest.features],
            }, ensure_ascii=False))
        elif arguments.command == "release-seal":
            release = ReleaseService(service.repository)
            if arguments.preview:
                manifest = release.preview_integrated_main(
                    arguments.version, arguments.feature_ids
                )
                print(json.dumps({
                    "mode": "read_only_preview",
                    "candidate_digest": manifest.digest(),
                    "main_commit": manifest.main_commit,
                    "features": [item.to_dict() for item in manifest.features],
                }, ensure_ascii=False))
            else:
                controller_root = Path(__file__).resolve().parent.parent
                gate = controller_root / "scripts/quality-gate.sh"

                def final_gate(checkout: Path) -> bool:
                    environment = dict(os.environ)
                    environment["AUDIO_MEMORY_TOOLCHAIN_ROOT"] = str(controller_root)
                    return subprocess.run(
                        [str(gate)], cwd=checkout, env=environment, check=False
                    ).returncode == 0

                manifest, path = release.seal_integrated_main(
                    arguments.version,
                    arguments.feature_ids,
                    arguments.approve,
                    final_gate,
                )
                print(json.dumps({
                    "manifest": str(path),
                    "candidate_digest": manifest.digest(),
                    "main_commit": manifest.main_commit,
                    "features": [item.to_dict() for item in manifest.features],
                }, ensure_ascii=False))
        elif arguments.command == "release-integrate":
            controller_root = Path(__file__).resolve().parent.parent
            gate = controller_root / "scripts/quality-gate.sh"

            def integration_gate(_: str, checkout: Path) -> bool:
                environment = dict(os.environ)
                environment["AUDIO_MEMORY_TOOLCHAIN_ROOT"] = str(controller_root)
                return subprocess.run(
                    [str(gate)], cwd=checkout, env=environment, check=False
                ).returncode == 0

            result = ReleaseService(service.repository).integrate(
                arguments.manifest, arguments.approve, integration_gate
            )
            print(json.dumps({
                "merged": list(result.merged),
                "failed": result.failed,
                "main_commit": result.main_commit,
            }, ensure_ascii=False))
        else:
            controller_root = Path(__file__).resolve().parent.parent
            release = ReleaseService(service.repository)
            manifest_path = release.store.manifest_path(arguments.version)
            manifest = release.authorize_build(manifest_path, arguments.approve)
            release_records = release.records_to_mark_released(manifest)
            environment = dict(os.environ)
            environment["AUDIO_MEMORY_TOOLCHAIN_ROOT"] = str(controller_root)
            gate_result = subprocess.run(
                [str(controller_root / "scripts/quality-gate.sh")],
                cwd=service.repository.top_level,
                env=environment,
                check=False,
            )
            if gate_result.returncode != 0:
                raise GovernanceError("发布前全量验收失败。")
            build_result = subprocess.run(
                [str(service.repository.top_level / "scripts/build-release.sh")],
                cwd=service.repository.top_level,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            if build_result.returncode != 0:
                diagnostic = (build_result.stderr or build_result.stdout).strip()
                raise GovernanceError(f"发布包构建失败：{diagnostic}")
            output_lines = [line for line in build_result.stdout.splitlines() if line]
            if not output_lines:
                raise GovernanceError("发布构建未返回安装包路径。")
            archive = Path(output_lines[-1]).resolve()
            checksum = archive.with_suffix(archive.suffix + ".sha256")
            if not archive.is_file() or not checksum.is_file():
                raise GovernanceError("发布包或校验文件缺失。")
            expected_hash = checksum.read_text(encoding="utf-8").split()[0]
            if hashlib.sha256(archive.read_bytes()).hexdigest() != expected_hash:
                raise GovernanceError("发布包校验失败。")
            with tarfile.open(archive) as bundle:
                forbidden = {"audio-memory-governance", ".worktrees", ".runtime"}
                if any(
                    forbidden.intersection(Path(member.name).parts)
                    for member in bundle.getmembers()
                ):
                    raise GovernanceError("发布包携带了开发治理或运行数据。")
            service.repository._git(
                "tag", "-a", manifest.target_version,
                "-m", f"Release {manifest.target_version}",
            )
            for record in release_records:
                release.features.store.save(replace(
                    record,
                    status="released",
                    current_step=f"已发布 {manifest.target_version}",
                ))
            print(json.dumps({
                "version": manifest.target_version,
                "tag": manifest.target_version,
                "archive": str(archive),
                "checksum": str(checksum),
            }, ensure_ascii=False))
        return 0
    except GovernanceError as exc:
        print(f"功能轨道操作失败：{exc}", file=sys.stderr)
        return 1


__all__ = [
    "FeatureRecord",
    "FeatureService",
    "FeatureStatus",
    "FeatureStore",
    "GitRepository",
    "GovernanceError",
    "IntegrationResult",
    "ReleaseFeature",
    "ReleaseManifest",
    "ReleaseService",
    "ReleaseStore",
]


if __name__ == "__main__":
    raise SystemExit(main())
