#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = PROJECT_ROOT / "backend" / "src"
sys.path.insert(0, str(BACKEND_SOURCE))

from audio_memory.config import RuntimeConfig, RuntimeConfigurationError  # noqa: E402


def _shell_assignment(name: str, value: str) -> str:
    if any(character in value for character in ("\n", "\r", "\x00")):
        raise RuntimeConfigurationError(f"{name} contains an unsupported control character")
    return f"{name}={shlex.quote(value)}"


def _development_assignments(*, project_root: Path, home: Path) -> tuple[str, ...]:
    environment = dict(os.environ)
    environment["AUDIO_MEMORY_PROFILE"] = "development"
    config = RuntimeConfig.from_environment(
        home=home,
        project_root=project_root,
        environ=environment,
    )
    runtime = config.paths.runtime
    values = (
        ("AUDIO_MEMORY_PROFILE", config.profile.value),
        ("AUDIO_MEMORY_DATA_ROOT", str(config.paths.root)),
        ("AUDIO_MEMORY_MODEL_ROOT", str(config.paths.models)),
        (
            "AUDIO_MEMORY_MODELS_WRITABLE",
            "1" if config.paths.models_writable else "0",
        ),
        ("AUDIO_MEMORY_KEYCHAIN_SERVICE", config.keychain_service),
        ("AUDIO_MEMORY_PORT", str(config.port)),
        ("AUDIO_MEMORY_RUNTIME_DIR", str(runtime)),
        ("AUDIO_MEMORY_PID_FILE", str(runtime / "audio-memory-dev.pid")),
        ("AUDIO_MEMORY_LOG_FILE", str(runtime / "audio-memory-dev.log")),
    )
    return tuple(_shell_assignment(name, value) for name, value in values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve Audio Memory runtime settings")
    subparsers = parser.add_subparsers(dest="command", required=True)
    development = subparsers.add_parser("development-env")
    development.add_argument("--project-root", type=Path, required=True)
    development.add_argument("--home", type=Path, required=True)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    try:
        if arguments.command == "development-env":
            assignments = _development_assignments(
                project_root=arguments.project_root,
                home=arguments.home,
            )
        else:  # pragma: no cover - argparse enforces the command set
            raise RuntimeConfigurationError("未知的运行配置命令。")
    except RuntimeConfigurationError as exc:
        print(f"启动配置无效：{exc}", file=sys.stderr)
        return 2

    print("\n".join(assignments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
