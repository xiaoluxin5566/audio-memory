from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from audio_memory.prompts.store import (
    KNOWN_LEGACY_DEFAULT_HASHES,
    PROMPT_SCENES,
    PromptConflictError,
    PromptStore,
)


PACKAGED_MEETING_V2 = """你负责识别本次音频中的独立会议，并为每场会议生成一张高质量会议纪要卡片。

本场景覆盖围绕相对明确工作或事务目标展开的正式会议和有回顾价值的工作沟通，包括招聘面谈、职业讨论、产品或业务深度交流、负责人沟通。客观回顾价值信号包括明确结论或决策、任务分配、方案比较或关键分歧、跨角色协调，或者围绕明确议题进行的高信息密度持续讨论。形式和时长本身不是判断标准；非正式交流只要包含可靠事实、权衡、开放问题或行动价值也可以生成。普通寒暄、短暂问答和媒体播放不自动视为会议。

每个独立会议生成一张卡，不得合并不同时间、参与者或目标的会议。同一会议的多个议题保留在同一详情中。

外部 title 用一句话表达最重要结果；summary 直接概括核心结论。禁止使用“产品会议纪要”“今日会议总结”等无信息标题。

详情提取 topic、background、participants、core_conclusions、decisions、open_questions、meeting_todos 和 discussion_topics。

core_conclusions 是会议形成的核心判断或共识，每一条都必须单独绑定 evidence_segment_ids。不得把多个离散结论合成一条；没有明确证据的判断不得进入 core_conclusions，应降级为 open_questions 或 discussion_topics。

decisions 只记录已经明确确认或拍板的事项；提议、假设、未确认方案和单方面偏好不算决策。没有形成结论的事项写入 open_questions。

决策的有效信号包括：“就这么定了”“好，就这么办”“确认一下”等明确确认；多人达成一致且无后续反对；某人被明确授权执行；方案被选中且其他方案被排除。“我觉得可以”“应该没问题”等倾向表达、未获回应的单方提议以及“先试试”“看看效果再说”等保留态度均不算决策。

meeting_todos 只记录明确行动、负责人和截止时间。属于用户的明确待办可同时写入顶层 todos，后端负责去重。

忠于原始对话，保留关键分歧，不补造共识。无法确认参与者姓名时使用 speaker_id。role 只有在说话人被明确称为主持人、汇报人或负责人，或者持续主持流程、汇报主体内容、作出最终决策声明时填写；不得依据姓名或猜测的职位推断，否则 role=null。

不分析表达能力，不提供表达建议；这些内容属于成长建议。

形成明确结论、决策、待办或具有回顾价值的结构化讨论时生成。零散工作闲聊没有回顾价值时不生成。"""


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
        "packaged_default_version": 7,
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
        "packaged_default_version": 7,
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
        "packaged_default_version": 7,
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


def test_packaged_meeting_v2_upgrades_once_without_touching_user_edits(
    tmp_path: Path,
) -> None:
    write_existing_prompt(
        tmp_path,
        "meeting",
        content=PACKAGED_MEETING_V2,
        version=8,
    )
    store = PromptStore(tmp_path)

    upgraded = store.get("meeting")
    repeated = store.get("meeting")
    archives = list((tmp_path / "meeting" / "versions").iterdir())
    metadata = json.loads((tmp_path / "meeting" / "metadata.json").read_text())

    assert upgraded == repeated
    assert upgraded.version == 9
    assert upgraded.content != PACKAGED_MEETING_V2
    assert len(archives) == 1
    assert archives[0].read_text() == PACKAGED_MEETING_V2
    assert metadata == {
        "version": 9,
        "packaged_default_version": 7,
        "current_source": "packaged",
    }


def test_packaged_meeting_v3_is_recognized_for_one_time_v4_upgrade() -> None:
    assert (
        "8e1cf25d9ce1dc777cccf10605914c7c4f4f6a343d8619d8b077420f72c9b6ea"
        in KNOWN_LEGACY_DEFAULT_HASHES["meeting"]
    )
