from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json

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


def write_existing_prompt(
    root: Path, scene_id: str, *, content: str, version: int
) -> None:
    scene_root = root / scene_id
    (scene_root / "versions").mkdir(parents=True)
    (scene_root / "current.md").write_text(content)
    (scene_root / "metadata.json").write_text(json.dumps({"version": version}))


def test_new_install_records_packaged_default_provenance(tmp_path: Path) -> None:
    store = PromptStore(tmp_path)

    document = store.get("meeting")
    metadata = json.loads((tmp_path / "meeting" / "metadata.json").read_text())

    assert document.version == 1
    assert metadata == {
        "version": 1,
        "packaged_default_version": 2,
        "current_source": "packaged",
    }


def test_untouched_known_legacy_default_is_archived_then_upgraded(tmp_path: Path) -> None:
    legacy = (
        "识别本次音频中的独立会议，判断会议开始与结束范围。总结会议主题、核心结论、明确决策和会议待办；"
        "忠于原始对话，不补造未讨论的事实。多个独立会议分别生成结果。"
    )
    write_existing_prompt(tmp_path, "meeting", content=legacy, version=4)
    store = PromptStore(tmp_path)

    upgraded = store.get("meeting")
    metadata = json.loads((tmp_path / "meeting" / "metadata.json").read_text())
    archives = list((tmp_path / "meeting" / "versions").glob("4-*.md"))

    assert upgraded.version == 5
    assert upgraded.content != legacy
    assert len(archives) == 1
    assert archives[0].read_text() == legacy
    assert metadata == {
        "version": 5,
        "packaged_default_version": 2,
        "current_source": "packaged",
    }


def test_user_edited_legacy_prompt_is_preserved_byte_for_byte(tmp_path: Path) -> None:
    edited = "  用户自定义会议角度\n保留这些空白  \n"
    write_existing_prompt(tmp_path, "meeting", content=edited, version=7)
    store = PromptStore(tmp_path)

    document = store.get("meeting")
    metadata = json.loads((tmp_path / "meeting" / "metadata.json").read_text())

    assert document.version == 7
    assert (tmp_path / "meeting" / "current.md").read_text() == edited
    assert document.content == edited
    assert list((tmp_path / "meeting" / "versions").iterdir()) == []
    assert metadata == {
        "version": 7,
        "packaged_default_version": 2,
        "current_source": "user",
    }


def test_legacy_upgrade_is_idempotent_on_repeated_initialization(tmp_path: Path) -> None:
    legacy = (
        "识别本次音频中的独立会议，判断会议开始与结束范围。总结会议主题、核心结论、明确决策和会议待办；"
        "忠于原始对话，不补造未讨论的事实。多个独立会议分别生成结果。"
    )
    write_existing_prompt(tmp_path, "meeting", content=legacy, version=1)
    store = PromptStore(tmp_path)

    first = store.get("meeting")
    first_metadata = (tmp_path / "meeting" / "metadata.json").read_bytes()
    second = store.get("meeting")
    second_metadata = (tmp_path / "meeting" / "metadata.json").read_bytes()

    assert first == second
    assert first_metadata == second_metadata
    assert len(list((tmp_path / "meeting" / "versions").iterdir())) == 1
