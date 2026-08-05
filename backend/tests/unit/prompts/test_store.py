from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from audio_memory.prompts.store import (
    PROMPT_SCENES,
    PromptConflictError,
    PromptStore,
)


def test_initialization_creates_exactly_six_non_empty_prompts(tmp_path: Path) -> None:
    store = PromptStore(tmp_path)

    documents = store.initialize()

    assert {document.scene_id for document in documents} == set(PROMPT_SCENES)
    assert len(documents) == 6
    assert all(document.version == 1 and document.content.strip() for document in documents)


def test_save_archives_previous_content_and_increments_version(tmp_path: Path) -> None:
    store = PromptStore(tmp_path)
    original = {item.scene_id: item for item in store.initialize()}["meeting"]

    updated = store.save("meeting", expected_version=1, content="新的会议分析要求")

    assert updated.version == 2
    assert store.get("meeting").content == "新的会议分析要求"
    versions = list((tmp_path / "meeting" / "versions").glob("1-*.md"))
    assert len(versions) == 1
    assert versions[0].read_text() == original.content


def test_stale_version_and_blank_content_are_rejected(tmp_path: Path) -> None:
    store = PromptStore(tmp_path)
    store.initialize()

    with pytest.raises(PromptConflictError):
        store.save("todo", expected_version=0, content="新提示词")
    with pytest.raises(ValueError):
        store.save("todo", expected_version=1, content="   ")
    with pytest.raises(ValueError):
        store.get("unknown")


def test_concurrent_edits_allow_only_one_version_winner(tmp_path: Path) -> None:
    store = PromptStore(tmp_path)
    store.initialize()

    def save(content: str):
        try:
            return store.save("content", expected_version=1, content=content)
        except PromptConflictError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ["版本 A", "版本 B"]))

    assert sum(result is not None for result in results) == 1
    assert store.get("content").version == 2
