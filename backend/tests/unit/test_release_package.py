from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


PROJECT_ROOT = Path(__file__).resolve().parents[3]
RELEASE_SCRIPTS = (
    "audio-memory",
    "backup_data.py",
    "build-release.sh",
    "com.audio-memory.local.plist.template",
    "doctor.sh",
    "doctor_checks.py",
    "install-release.sh",
    "install.sh",
    "runtime_config.py",
    "start.sh",
)


def make_isolated_release_checkout(tmp_path: Path) -> Path:
    """Create every release input in tmp_path, including a synthetic frontend build."""
    checkout = tmp_path / "clean-checkout"
    checkout.mkdir()
    for name in ("VERSION", "README.md", "CHANGELOG.md", "PRIVACY.md"):
        shutil.copy2(PROJECT_ROOT / name, checkout / name)
    for relative in (
        Path("backend/pyproject.toml"),
        Path("backend/uv.lock"),
        Path("backend/alembic.ini"),
    ):
        destination = checkout / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    shutil.copytree(
        PROJECT_ROOT / "backend/src",
        checkout / "backend/src",
        symlinks=True,
    )
    shutil.copytree(
        PROJECT_ROOT / "backend/migrations",
        checkout / "backend/migrations",
        symlinks=True,
    )
    scripts = checkout / "scripts"
    scripts.mkdir()
    for name in RELEASE_SCRIPTS:
        shutil.copy2(PROJECT_ROOT / "scripts" / name, scripts / name)
    client = checkout / "prototype/dist/client"
    client.mkdir(parents=True)
    (client / "index.html").write_text(
        "<!doctype html><title>isolated release fixture</title>\n",
        encoding="utf-8",
    )
    (client / "app.js").write_text("console.log('fixture');\n", encoding="utf-8")
    return checkout


def test_release_archive_uses_case_insensitive_runtime_whitelist(
    tmp_path: Path,
) -> None:
    checkout = make_isolated_release_checkout(tmp_path)
    contamination = checkout / "prototype/dist/client/release-exclusion-fixture"
    dependency_target = tmp_path / "local-dependency"
    dependency_target.mkdir()
    fixture_files = (
        ".RUNTIME/state.json",
        ".env.production",
        ".EnV.Secrets/secret.txt",
        "state.sqlite3",
        "state.sqlite3-wal",
        "state.sqlite3-shm",
        "state.sqlite3-journal",
        "UPPER-STATE.SQLITE3-WAL",
        "recording.mp3",
        "UPPER-RECORDING.MP3",
        "recording.flac",
        "server.log",
        "UPPER-SERVER.LOG",
        "server.log.1",
        "MoDeLs/weights.onnx",
        "Node_Modules/package/index.js",
        ".UV-CACHE/package/artifact",
        ".PyTeSt_CaChE/state.json",
        ".MYPY_CACHE/state.json",
        ".RuFf_CaChE/state.json",
        "Fixture.EGG-INFO/PKG-INFO",
        "TeStS/fixture.json",
        "OuTpUtS/report.json",
        "ScReEnShOtS/screenshot.png",
        "DeSiGnS/mockup.png",
        "BuIlD/temporary.bin",
        "AuDiO/recording.raw",
        "__PyCaChE__/cache.pyc",
        ".VeNv/dependency.py",
        ".GiT/config",
    )
    for relative_path in fixture_files:
        path = contamination / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("must not ship\n", encoding="utf-8")
    (contamination / "linked-dependency").symlink_to(
        dependency_target,
        target_is_directory=True,
    )

    result = subprocess.run(
        ["bash", str(checkout / "scripts/build-release.sh")],
        cwd=checkout,
        env={
            **os.environ,
            "AUDIO_MEMORY_RELEASE_DIST": str(tmp_path / "release-output"),
            "AUDIO_MEMORY_ALLOW_DIRTY_RELEASE": "1",
            "AUDIO_MEMORY_SKIP_RELEASE_BUILD": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    version = (checkout / "VERSION").read_text(encoding="utf-8").strip()
    archive = (
        tmp_path / "release-output" / f"audio-memory-v{version}-macos-arm64.tar.gz"
    )
    checksum = archive.with_suffix(archive.suffix + ".sha256")
    assert archive.is_file()
    assert checksum.is_file()
    with tarfile.open(archive) as handle:
        members = handle.getmembers()
        names = {member.name for member in members}
    prefix = f"audio-memory-v{version}"
    required = {
        f"{prefix}/VERSION",
        f"{prefix}/backend/pyproject.toml",
        f"{prefix}/backend/src/audio_memory/main.py",
        f"{prefix}/backend/migrations/versions/0014_app_settings.py",
        f"{prefix}/prototype/dist/client/index.html",
        f"{prefix}/scripts/audio-memory",
        f"{prefix}/scripts/backup_data.py",
        f"{prefix}/scripts/com.audio-memory.local.plist.template",
        f"{prefix}/scripts/doctor.sh",
        f"{prefix}/scripts/doctor_checks.py",
        f"{prefix}/scripts/install-release.sh",
        f"{prefix}/scripts/install.sh",
        f"{prefix}/scripts/runtime_config.py",
        f"{prefix}/scripts/start.sh",
    }
    assert required <= names
    forbidden_parts = {
        ".git",
        ".venv",
        ".runtime",
        ".uv-cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "tests",
        "outputs",
        "screenshots",
        "designs",
        "models",
        "audio",
        "build",
        "audio-memory.sqlite3",
        ".env",
        "__pycache__",
    }
    assert not any(
        forbidden_parts & {part.casefold() for part in Path(name).parts}
        for name in names
    )
    assert not any(
        part.casefold().endswith(".egg-info")
        for name in names
        for part in Path(name).parts
    )
    assert not any(
        Path(name).name.casefold().startswith(".env")
        or name.casefold().endswith(
            (
                ".pyc",
                ".pyo",
                ".sqlite",
                ".sqlite3",
                ".sqlite-wal",
                ".sqlite-shm",
                ".sqlite-journal",
                ".sqlite3-wal",
                ".sqlite3-shm",
                ".sqlite3-journal",
                ".db",
                ".db-wal",
                ".db-shm",
                ".db-journal",
                ".mp3",
                ".aac",
                ".m4a",
                ".wav",
                ".flac",
                ".ogg",
                ".opus",
                ".wma",
                ".caf",
                ".aiff",
                ".log",
            )
        )
        or ".log." in Path(name).name.casefold()
        for name in names
    )
    assert not any(member.issym() or member.islnk() for member in members)
    expected_hash = checksum.read_text(encoding="utf-8").split()[0]
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected_hash
