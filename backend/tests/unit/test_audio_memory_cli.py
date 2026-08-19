from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI = PROJECT_ROOT / "scripts" / "audio-memory"
PLIST_TEMPLATE = PROJECT_ROOT / "scripts" / "com.audio-memory.local.plist.template"
START_SCRIPT = PROJECT_ROOT / "scripts" / "start.sh"


def run_cli(
    tmp_path: Path, command: str, *, overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    app_root = tmp_path / "app"
    version_root = app_root / "versions" / "0.1.0-beta.1"
    version_root.mkdir(parents=True)
    (version_root / "VERSION").write_text("0.1.0-beta.1\n", encoding="utf-8")
    (app_root / "current").symlink_to(version_root)
    return subprocess.run(
        ["bash", str(CLI), command],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "AUDIO_MEMORY_APP_ROOT": str(app_root),
            "AUDIO_MEMORY_DATA_ROOT": str(tmp_path / "data"),
            "AUDIO_MEMORY_NO_OPEN": "1",
            "AUDIO_MEMORY_RUNTIME_PYTHON": sys.executable,
            **(overrides or {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_version_reads_the_installed_current_release(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "version")

    assert result.returncode == 0
    assert result.stdout.strip() == "Audio Memory 0.1.0-beta.1"


def test_unknown_command_prints_stable_usage(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "unknown")

    assert result.returncode == 2
    assert "start|stop|restart|status|doctor|logs|version" in result.stderr


def test_launch_agent_is_user_scoped_and_loopback_only() -> None:
    template = PLIST_TEMPLATE.read_text(encoding="utf-8")
    cli = CLI.read_text(encoding="utf-8")

    assert "com.audio-memory.local" in template
    assert "AUDIO_MEMORY_NO_OPEN" in template
    assert "AUDIO_MEMORY_PROFILE" in template
    assert "<string>production</string>" in template
    assert "AUDIO_MEMORY_DATA_ROOT" in template
    assert "AUDIO_MEMORY_PORT" in template
    assert "<key>HOME</key>" in template
    assert "__HOME__" in template
    assert 'replace("__HOME__", home)' in cli
    assert "127.0.0.1" in cli
    assert "launchctl bootstrap" in cli
    assert "launchctl bootout" in cli
    assert "app/current" not in template
    assert "__CURRENT_ROOT__" in template


def test_rendered_launch_agent_preserves_the_production_runtime_contract(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "app"
    version_root = app_root / "versions" / "0.1.0-beta.1"
    scripts = version_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(PLIST_TEMPLATE, scripts / PLIST_TEMPLATE.name)
    (version_root / "VERSION").write_text("0.1.0-beta.1\n", encoding="utf-8")
    (app_root / "current").symlink_to(version_root)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    counter = tmp_path / "curl-count"
    (fake_bin / "curl").write_text(
        "#!/bin/bash\n"
        "set -eu\n"
        f"count_file={counter!s}\n"
        "count=0; [ ! -f \"$count_file\" ] || count=\"$(cat \"$count_file\")\"\n"
        "count=$((count + 1)); printf '%s\\n' \"$count\" > \"$count_file\"\n"
        "[ \"$count\" -gt 1 ] || exit 1\n"
        "printf '%s\\n' '{\"status\":\"ok\",\"profile\":\"production\"}'\n",
        encoding="utf-8",
    )
    (fake_bin / "curl").chmod(0o755)
    (fake_bin / "launchctl").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    (fake_bin / "launchctl").chmod(0o755)
    home = tmp_path / "home"
    data_root = tmp_path / "data"

    result = subprocess.run(
        ["bash", str(CLI), "start"],
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "AUDIO_MEMORY_APP_ROOT": str(app_root),
            "AUDIO_MEMORY_DATA_ROOT": str(data_root),
            "AUDIO_MEMORY_NO_OPEN": "1",
            "AUDIO_MEMORY_RUNTIME_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    rendered = (
        home / "Library" / "LaunchAgents" / "com.audio-memory.local.plist"
    ).read_text(encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "<string>com.audio-memory.local</string>" in rendered
    assert "<key>AUDIO_MEMORY_PROFILE</key>\n    <string>production</string>" in rendered
    assert f"<key>AUDIO_MEMORY_DATA_ROOT</key>\n    <string>{data_root}</string>" in rendered
    assert "<key>AUDIO_MEMORY_PORT</key>\n    <string>8765</string>" in rendered


def test_status_recognizes_only_an_ok_production_health_payload(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "curl").write_text(
        "#!/bin/bash\nprintf '%s\\n' \"$FAKE_HEALTH\"\n", encoding="utf-8"
    )
    (fake_bin / "curl").chmod(0o755)
    common = {"PATH": f"{fake_bin}:{os.environ['PATH']}", "AUDIO_MEMORY_PORT": "9123"}

    production = run_cli(
        tmp_path / "production",
        "status",
        overrides={
            **common,
            "FAKE_HEALTH": '{"status":"ok","profile":"production"}',
        },
    )
    development = run_cli(
        tmp_path / "development",
        "status",
        overrides={
            **common,
            "FAKE_HEALTH": '{"status":"ok","profile":"development"}',
        },
    )

    assert production.returncode == 0
    assert production.stdout == "Audio Memory 正在运行：http://127.0.0.1:9123/\n"
    assert development.returncode == 1
    assert development.stdout == "Audio Memory 未运行。\n"


def test_start_uses_the_release_virtual_environment_without_shell_path() -> None:
    script = START_SCRIPT.read_text(encoding="utf-8")

    assert 'PYTHON="$PROJECT_ROOT/backend/.venv/bin/python"' in script
    assert '"$PYTHON" -m uvicorn' in script
    assert 'AUDIO_MEMORY_FFMPEG="$PROJECT_ROOT/runtime/ffmpeg/bin/ffmpeg"' in script
    assert 'AUDIO_MEMORY_FFPROBE="$PROJECT_ROOT/runtime/ffmpeg/bin/ffprobe"' in script
    assert 'PATH="$PROJECT_ROOT/runtime/ffmpeg/bin:/usr/bin:/bin:/usr/sbin:/sbin"' in script


def test_release_install_uses_bundled_uv_and_ffmpeg() -> None:
    installer = (PROJECT_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

    assert 'UV="$PROJECT_ROOT/runtime/uv/uv"' in installer
    assert 'FFMPEG_BIN="$PROJECT_ROOT/runtime/ffmpeg/bin"' in installer
