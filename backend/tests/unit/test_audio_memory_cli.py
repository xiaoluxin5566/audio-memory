from __future__ import annotations

import os
from pathlib import Path
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI = PROJECT_ROOT / "scripts" / "audio-memory"
PLIST_TEMPLATE = PROJECT_ROOT / "scripts" / "com.audio-memory.local.plist.template"


def run_cli(tmp_path: Path, command: str) -> subprocess.CompletedProcess[str]:
    app_root = tmp_path / "app"
    version_root = app_root / "versions" / "0.1.0-beta.1"
    version_root.mkdir(parents=True)
    (version_root / "VERSION").write_text("0.1.0-beta.1\n", encoding="utf-8")
    (app_root / "current").symlink_to(version_root)
    return subprocess.run(
        ["bash", str(CLI), command],
        env={
            **os.environ,
            "AUDIO_MEMORY_APP_ROOT": str(app_root),
            "AUDIO_MEMORY_DATA_ROOT": str(tmp_path / "data"),
            "AUDIO_MEMORY_NO_OPEN": "1",
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
    assert "AUDIO_MEMORY_PORT" in template
    assert "127.0.0.1" in cli
    assert "launchctl bootstrap" in cli
    assert "launchctl bootout" in cli
    assert "app/current" not in template
    assert "__CURRENT_ROOT__" in template
