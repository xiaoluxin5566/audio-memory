from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKUP_SCRIPT = PROJECT_ROOT / "scripts" / "backup_data.py"


def test_sqlite_backup_is_consistent_and_keeps_source_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "audio-memory.sqlite3"
    destination = tmp_path / "backups" / "audio-memory.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE reports (id INTEGER PRIMARY KEY, title TEXT)")
        connection.executemany(
            "INSERT INTO reports(title) VALUES (?)",
            [("第一份",), ("第二份",)],
        )
    source_bytes = source.read_bytes()

    result = subprocess.run(
        [str(PROJECT_ROOT / "backend" / ".venv" / "bin" / "python"), str(BACKUP_SCRIPT), str(source), str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert source.read_bytes() == source_bytes
    with sqlite3.connect(f"file:{destination}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT count(*) FROM reports").fetchone() == (2,)
    assert destination.stat().st_mode & 0o777 == 0o600

