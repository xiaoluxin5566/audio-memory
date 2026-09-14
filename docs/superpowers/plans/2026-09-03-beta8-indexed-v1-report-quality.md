# Beta 8 两次 V1 与报告质量改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Beta 8 V1 改为“全量事实索引＋统一完整 V1 写作”两次正常调用，并把已确认的覆盖、归属、价值、丰富度、结构、建议、表达、审核、搜索来源和前端呈现要求全部落到可执行、可测试、可恢复的正式链路。

**Architecture:** 第一次模型调用只生成薄的全量事实索引，以连续片段范围覆盖全部逐字稿，不做出卡或价值筛选。第二次同时读取完整逐字稿、索引、共享质量契约和七场景能力，直接产生可展示的完整 V1。后续逐字稿分片审核、全局编辑审核、Kimi 2.6 搜索、定向修订和终审保持职责分离，不把审核当作 V1 的主要代写者。

**Tech Stack:** Python 3.12、Pydantic v2、asyncio、SQLAlchemy、pytest/pytest-asyncio、React/JavaScript、Markdown、DeepSeek 报告模型、Kimi 2.6 原生联网搜索。

**Spec:** `docs/superpowers/specs/2026-09-03-beta8-unified-v1-report-quality-design.md`

## Global Constraints

- 实施工作只在 `/Users/liujinxin/.codex/worktrees/beta8-report-pipeline/音频Always on Demo` 的 `codex/beta8-report-pipeline` 分支上进行。
- 当前工作树已有大量用户和本任务的未提交改动；不执行 reset、clean、checkout 覆盖、merge、commit、push、tag 或发布。
- 本计划不授权任何付费模型或真实联网搜索；真实豆包逐字稿运行前必须再获得明确付费调用授权。
- `SingleReportRunner` 保持默认且行为不变；历史 `beta8_multi_scene_v1` 仍可读；新链路只在 `pipeline_kind=beta8_indexed_scene_v2` 时生效；未知管线值失败关闭。
- 两次 V1 调用都必须获得字节级一致的完整可靠逐字稿；索引、摘要、候选或档案都不是第二次调用的输入边界。
- JSON 只保存路由、溯源、审核、待办、搜索和恢复字段；用户可见的完整内容必须保存在 Markdown，不得为适配 JSON 压缩内容。
- 正常 V1 路径恰好两次报告模型调用；索引修复、Schema 修复、网络重试和恢复必须单独计量。
- 搜索逻辑本轮冻结：不改变搜索候选、任务数量、Kimi 2.6 执行方式和采用决策；仅修复全局 `source_id` 碰撞与发布一致性。
- 所有生产规则必须是全局能力，不为“陈震”、“离职”、“犬儒”、“生日”或其他单一样本写特判。
- 实施顺序严格使用 TDD：先写失败测试，确认失败原因，再写最小实现，最后运行目标与回归测试。

---

## 实施前需求追溯审计

下表是实施的完整覆盖清单。任何一项没有 Prompt、程序契约或验收用例时，不得宣布改造完成。

| ID | 已确认优化方向 | 主要落点 | 验收证据 |
|---|---|---|---|
| R01 | V1 正常路径拆为两次，而不是七场景各调一次 | Composer、Runner、checkpoint | 假提供商恰好收到 2 次 V1 请求 |
| R02 | 第一次只建立全量事实索引，不过滤价值、不写卡 | EventIndex Schema/Prompt/覆盖门禁 | 播客、案例、低价值实质单元都进索引 |
| R03 | 两次调用均使用完整逐字稿，索引只是导航 | Composer | 两次请求的 transcript SHA-256 相同 |
| R04 | 第二次直接输出可展示的七场景完整 V1，不是草稿或大纲 | All-scenes V1 Prompt/Schema | 七场景齐全，Markdown 可直接渲染 |
| R05 | 一次会议、拜访、电话、线上沟通或一对一对应一张“工作沟通”卡 | Index 边界、V1 Prompt、audit | 同沟通多议题不拆卡；同项目多次沟通不误合并 |
| R06 | 非工作卡只按独立帮助价值出卡，不设 5 张或任何数量上限 | V1 Prompt/Schema/audit | 无价值日期计算不成卡；6 张高价值卡可全部保留 |
| R07 | 主体归属依据谁说、在讲谁、表达类型和表达用途，不依赖关键词 | Index、V1、audit | 会议中的第三方案例不归于用户；用户真实自述仍可被分析 |
| R08 | 一旦出卡，必须有高信息密度的事实、判断、依据、分歧、权衡、影响、已知、未知和帮助 | Shared quality Prompt/audit | `thin_content` 无未解决项，人工丰富度达标 |
| R09 | 标题下先展示核心洞察或核心信息组，不限定为一句 | Markdown 契约/前端 | 首个二级标题前存在非空核心信息区 |
| R10 | 呈现形式与内容类型匹配，不再只给泛化“适当用表格” | Shared quality Prompt、quality gate、audit | 多议题/对比/时间/因果/行动/风险/话术/复盘/操作夹具逐项通过 |
| R11 | 建议必须具体，优先给表格、话术、清单、步骤、模板、实验或验证方法 | Shared quality Prompt/revision/audit | “继续保持/多关注/加强沟通”无具体载体时不通过 |
| R12 | 正文直接与用户交流，先结论后论据，不使用报告腔和录音复述腔 | Shared quality Prompt/quality gate/audit | 报告腔失败夹具被拦截 |
| R13 | 七场景保留已确认的独立能力，不被统一模板抹平 | 七场景 Prompt + 统一 V1 组合 | Prompt 哈希、场景夹具、人工场景评审 |
| R14 | 家庭卡可做可验证的互动分析并给具体建议，但不贴性格、疾病或长期关系标签 | Parenting Prompt/audit | 冲突样本有分析+话术，无过度标签 |
| R15 | 健康既覆盖身体也覆盖日常心理负荷、困倦和反复宣泄，但不做医疗诊断 | Health Prompt/audit | 低强度累积信号不被机械丢弃，无诊断/处方建议 |
| R16 | 内容消费不默认等于学习；媒体表达不写成用户观点 | Content Prompt/index/audit | 播客不遗漏，创作者观点与用户反应分离 |
| R17 | 灵感先接住并发散，再按目标收敛到假设和验证；所有卡帮助用户而不是批判用户 | Inspiration Prompt/audit | 灵感样本同时有扩展和可验证收敛 |
| R18 | 自我成长只处理当前报告期有证据的自我理解，建议必须有实质工具 | Self-growth Prompt/audit | 有证据、其他解释、记录模板或小实验 |
| R19 | 生活决策是决策与执行顾问，证据足时直接建议，信息缺失时给条件化判断 | Life-decisions Prompt/audit | 选项/标准/权衡/待验证信息完整 |
| R20 | 全局编辑审核保留，但只做 keep/revise/merge/drop/create、去重、归属和搜索规划，不重写全报告 | Orchestration Prompt/Schema | keep 卡字节级不变，仅需修改的卡进修订 |
| R21 | 待办只来自用户已决定/接受/安排/承诺的行动，建议、愿望和开放问题不是待办 | V1/orchestration/revision | 每个候选恰好被保留或记录丢弃原因 |
| R22 | 搜索仅补充外部可验证信息，不证明内部事实、动机和决定；来源全局唯一 | Kimi 2.6 adapter/search/publisher | `source_id=sha256(canonical_url)`，所有展示引用可回溯 |
| R23 | Markdown 不重复主标题、不手工给二至四级标题编号，前端表格可读 | Parser/cleanup/frontend | 无 `1 一、`，一级标题不在详情重复，窄屏表格可滚动 |
| R24 | 每次运行独立展示各阶段时间、Token、费用、正常/修复/重试和终审分数 | Metrics/API/frontend | 新 `run_id` 的本次数据与历史累计分开 |
| R25 | 最终验收必须使用真正的豆包录音文件识别 2.0 合并逐字稿，不得再用 Whisper 样本冒充 | Evaluation harness/manifest | 输入路径、ASR 来源、文件数、片段数和 SHA-256 被持久化 |

任务覆盖关系：Task 1 固化 R05–R23 的共享规则；Task 2 主要实现 R02、R03、R07 和 R16；Task 3 主要实现 R01、R03、R04、R06、R09 和 R13；Task 4 实现 R08–R12 和 R23；Task 5 实现 R05、R07 以及 R13–R21 的场景能力；Task 6 实现 R01–R04 和 R24 的运行时部分；Task 7 实现 R07–R13、R20 和 R21 的审核与修订部分；Task 8 实现 R22；Task 9 实现 R09、R10、R12 和 R23 的渲染部分；Task 10 实现 R24；Task 11 实现管线隔离和发布闭包；Task 12 实现 R25；Task 13 对 R01–R25 做最终反向追溯。

### 内容类型到呈现方式的正式映射

这张表必须以完整文字进入正式 V1 Prompt、审核 Prompt 和定向修订 Prompt，不得再压缩成“根据内容灵活使用表格”。

| 内容类型 | 优先呈现方式 |
|---|---|
| 多项结论 | 要点列表或结论表 |
| 多个议题 | 分级小标题 |
| 方案比较 | 对比表 |
| 时间推进 | 时间线 |
| 因果关系 | 因果链，并区分事实、推断和待验证环节 |
| 明确行动 | 行动表，字段为行动、负责人、时间/条件、目的或状态 |
| 风险与应对 | 风险—影响—对策表 |
| 沟通建议 | 符合对象、语境和目标的可复制话术 |
| 复盘建议 | 可直接填写的记录模板 |
| 操作建议 | 有先后顺序的分步骤清单 |
| 单一深入判断 | 短段落加证据、边界和验证方式 |

一张卡可以同时命中多种内容类型并组合多种呈现方式。不为只有一个简单判断的内容强制制表，也不允许为达到格式数量而生成空表。

---

### Task 1: 固化共享质量契约和失败样本

**Files:**
- Create: `docs/beta8/pipeline-prompts-v2/00-shared-report-quality-contract.md`
- Create: `backend/src/audio_memory/prompts/beta8/report-quality.md`
- Create: `backend/tests/fixtures/beta8/report_quality_cases.json`
- Modify: `backend/tests/unit/prompts/test_beta8_composer.py`

**Interfaces:**
- Consumes: 上文 R01–R25、七场景黄金 Prompt、现有 `Beta8PromptComposer.approved_source_mappings()`。
- Produces: 唯一可执行的共享质量契约文件，以及用于 V1、审核、修订的同源包装 Prompt。

- [ ] **Step 1: 建立失败夹具**

  `report_quality_cases.json` 至少包含：同一周会多议题、同项目两次沟通、会议中第三方案例、用户真实自述、播客播放、无价值日期计算、6 个高价值非工作事件、报告腔、抽象建议和 10 种结构映射。每个样本只声明全局能力期望，不写人名或关键词特判。

- [ ] **Step 2: 写 Prompt 组合失败测试**

```python
def test_v1_audit_and_revision_embed_exact_shared_quality_contract():
    approved = Path("docs/beta8/pipeline-prompts-v2/00-shared-report-quality-contract.md").read_text()
    composer = Beta8PromptComposer()
    sample = prompt_test_inputs()
    assert approved in composer.compose_all_scenes_v1(
        transcript_markdown=sample.transcript, event_index=sample.event_index
    ).instructions
    assert approved in composer.compose_audit(
        phase="initial", scope="global_report", audit_unit_id="global_report",
        cards=[], transcript_segments=[], revision_requirements=[]
    ).instructions
    assert approved in composer.compose_revision(
        target_scene_id="parenting_family", source_cards=[sample.card],
        revision_task=sample.revision_task, transcript_segments=sample.segments,
        search_packets=[]
    ).instructions

def test_shared_contract_contains_every_presentation_mapping():
    prompt = Path("backend/src/audio_memory/prompts/beta8/report-quality.md").read_text()
    for phrase in ("方案比较", "对比表", "时间线", "因果链",
                   "行动表", "风险—影响—对策表", "可复制话术",
                   "记录模板", "分步骤清单"):
        assert phrase in prompt
```

- [ ] **Step 3: 运行测试并确认因文件不存在而失败**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_composer.py -q`

- [ ] **Step 4: 完整写入共享契约并建立字节级来源映射**

  共享契约必须包含 R05–R23，包括本计划中完整的“内容类型→呈现方式”表。`approved_source_mappings()` 将文档源文件映射到包装文件，测试比对 bytes 和 SHA-256，防止下次实验再把完整规则压缩成一句。

- [ ] **Step 5: 运行 Prompt 契约测试并检查差异**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_composer.py -q`

  Check: `git diff --check` and `git diff -- docs/beta8/pipeline-prompts-v2 backend/src/audio_memory/prompts/beta8/report-quality.md backend/tests/fixtures/beta8/report_quality_cases.json`

---

### Task 2: 实现薄的全量事实索引契约与覆盖门禁

**Files:**
- Create: `docs/beta8/pipeline-prompts-v2/01-event-index-prompt.md`
- Create: `backend/src/audio_memory/prompts/beta8/event-index.md`
- Create: `backend/src/audio_memory/prompts/beta8_event_index_schema.py`
- Create: `backend/src/audio_memory/analysis/beta8_event_index.py`
- Create: `backend/tests/unit/prompts/test_beta8_event_index_schema.py`
- Create: `backend/tests/unit/analysis/test_beta8_event_index.py`

**Interfaces:**
- Produces: `Beta8EventIndex`、`validate_event_index(index, transcript)`、`EventIndexCoverageError`。
- Contract: 每份输入录音的每个可靠 `segment_id` 恰好被一个实质单元范围或一个明确排除范围覆盖。

- [ ] **Step 1: 写索引 Schema 失败测试**

```python
def test_event_index_covers_every_segment_exactly_once():
    transcript = fixture_transcript("work_then_podcast")
    index = Beta8EventIndex.model_validate(valid_index_payload())
    validate_event_index(index, transcript)

def test_event_index_rejects_gap_overlap_unknown_id_and_value_filtering_language():
    with pytest.raises(EventIndexCoverageError):
        validate_event_index(index_with_gap(), fixture_transcript())
    with pytest.raises(EventIndexCoverageError):
        validate_event_index(index_with_overlap(), fixture_transcript())
    request = Beta8PromptComposer().compose_event_index(
        transcript_markdown=build_test_transcript()
    )
    assert "不值得出卡" not in request.instructions
```

- [ ] **Step 2: 确认新测试因类和函数不存在而失败**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_event_index_schema.py backend/tests/unit/analysis/test_beta8_event_index.py -q`

- [ ] **Step 3: 实现最小薄 Schema**

```python
class Beta8SegmentRange(_StrictModel):
    source_file: str
    start_segment_id: str
    end_segment_id: str

class Beta8EventUnit(_StrictModel):
    unit_id: str
    kind: Literal["work_communication", "content_playback", "family_interaction",
                  "user_self_expression", "life_decision", "work_thinking", "other"]
    ranges: list[Beta8SegmentRange]
    subject: str
    expression_mode: Literal["user_experience", "third_party_case", "quotation",
                             "media_playback", "model_content", "mixed", "unknown"]
    description: str

class Beta8ExcludedRange(_StrictModel):
    range: Beta8SegmentRange
    reason: Literal["noise", "duplicate", "unintelligible", "empty"]

class Beta8EventIndex(_StrictModel):
    input_complete: bool
    input_error: str | None
    units: list[Beta8EventUnit]
    excluded_ranges: list[Beta8ExcludedRange]
```

  不增加洞察、价值分、场景候选、建议、待办、搜索和大段证据文字字段。同一次跨文件工作沟通使用一个 `unit_id` 和多个 `ranges`。

- [ ] **Step 4: 实现覆盖门禁**

  按逐字稿文件和片段顺序展开所有范围，验证不越文件、起止有效、无缺口、无重叠、`unit_id` 唯一、`input_complete` 与 `input_error` 一致。门禁只检查覆盖和结构，不在代码里根据关键词判断语义。

- [ ] **Step 5: 实现索引 Prompt 并固化禁止项**

  Prompt 明确：“所有实质单元先登记，无论后续是否出卡；不产生 scene skip reason、card candidate、价值结论和呈现计划。”

- [ ] **Step 6: 运行索引单元测试**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_event_index_schema.py backend/tests/unit/analysis/test_beta8_event_index.py -q`

---

### Task 3: 实现统一完整 V1 Schema 和两次 Prompt 组合

**Files:**
- Create: `docs/beta8/pipeline-prompts-v2/02-all-scenes-v1-prompt.md`
- Create: `backend/src/audio_memory/prompts/beta8/all-scenes-v1.md`
- Modify: `backend/src/audio_memory/prompts/beta8_scene_schema.py`
- Modify: `backend/src/audio_memory/prompts/beta8_composer.py`
- Modify: `backend/tests/unit/prompts/test_beta8_scene_schema.py`
- Modify: `backend/tests/unit/prompts/test_beta8_composer.py`

**Interfaces:**
- Consumes: `Beta8EventIndex`、完整 transcript Markdown、`report-quality.md`、七份场景 Prompt。
- Produces: `Beta8UnifiedV1Result`、`compose_event_index()`、`compose_all_scenes_v1()`。

- [ ] **Step 1: 写统一 V1 契约失败测试**

```python
def test_unified_v1_requires_exactly_seven_ordered_scenes():
    payload = valid_unified_v1_payload()
    payload["scene_results"].pop()
    with pytest.raises(ValueError, match="seven ordered scenes"):
        Beta8UnifiedV1Result.model_validate(payload)

def test_every_index_unit_is_used_or_explicitly_omitted():
    result = Beta8UnifiedV1Result.model_validate(valid_unified_v1_payload())
    validate_unified_v1_against_index(result, event_index_with_two_units())

def test_work_card_references_exactly_one_work_communication_unit():
    result = unified_result_with_work_card(source_unit_ids=["comm_1", "comm_2"])
    with pytest.raises(ValueError, match="one work communication"):
        validate_unified_v1_against_index(result, event_index_with_two_communications())

def test_non_work_scene_has_no_card_count_cap():
    result = unified_result_with_non_work_cards(count=6)
    assert len(result.scene_results[1].cards) == 6
def test_both_v1_calls_receive_identical_full_transcript():
    first = composer.compose_event_index(transcript_markdown=raw)
    second = composer.compose_all_scenes_v1(transcript_markdown=raw, event_index=index)
    assert extract_transcript(first.user_data) == extract_transcript(second.user_data) == raw
```

- [ ] **Step 2: 确认旧 Composer 仍只能生成单场景请求而测试失败**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_scene_schema.py backend/tests/unit/prompts/test_beta8_composer.py -q`

- [ ] **Step 3: 实现薄路由+完整 Markdown 输出契约**

```python
class Beta8CardBasis(_StrictModel):
    type: Literal["work_communication", "independent_value"]
    source_unit_ids: list[str]
    communication_kind: Literal["meeting", "visit", "call", "online", "one_on_one",
                                "interview", "customer", "supplier", "other"] | None

class Beta8UnifiedCard(Beta8SceneCard):
    draft_card_key: str
    card_basis: Beta8CardBasis

class Beta8UnifiedSceneResult(_StrictModel):
    scene_id: Beta8SceneId
    cards: list[Beta8UnifiedCard]
    todo_candidates: list[Beta8TodoCandidate]
    skip_reason: str | None

class Beta8OmittedUnit(_StrictModel):
    unit_id: str
    reason: str

class Beta8UnifiedV1Result(_StrictModel):
    input_complete: bool
    input_error: str | None
    scene_results: list[Beta8UnifiedSceneResult]
    omitted_units: list[Beta8OmittedUnit]
```

  索引单元可以在不同场景中被必要引用，但每个单元至少出现在一张卡的 `source_unit_ids` 或 `omitted_units` 中。这只是防止静默遗漏的路由账本，不要求模型把索引文字复制到正文。

- [ ] **Step 4: 组合正式 V1 Prompt**

  `compose_all_scenes_v1()` 的 instructions 顺序固定为：任务与安全边界 → 完整共享质量契约 → 七场景完整能力 → 统一取舍与去重规则 → Markdown 契约 → JSON Schema。七份场景 Prompt 必须作为 instructions，不放入 untrusted user data。

- [ ] **Step 5: 新增 Prompt 反退化测试**

  断言正式 V1 instructions 含有完整结构映射、核心信息区、直接对话语气、实质建议、待办边界、主体/媒体归属和七场景特有能力。删除或改写任一核心短语都必须使测试失败。

- [ ] **Step 6: 运行 Schema/Composer 单元测试**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_scene_schema.py backend/tests/unit/prompts/test_beta8_composer.py -q`

---

### Task 4: 在 V1 本身落地丰富度、结构匹配和具体帮助

**Files:**
- Modify: `backend/src/audio_memory/prompts/beta8/report-quality.md`
- Modify: `backend/src/audio_memory/prompts/beta8/all-scenes-v1.md`
- Create: `backend/src/audio_memory/analysis/beta8_quality.py`
- Create: `backend/tests/unit/analysis/test_beta8_quality.py`

**Interfaces:**
- Produces: `inspect_card_markdown(markdown: str) -> CardQualitySignals`、`validate_v1_structure(result) -> None`。
- Boundary: 确定性检查只拦截可靠判断的格式/结构错误；“这个内容是否应该用对比表”交给语义审核，不用关键词代码硬判。

- [ ] **Step 1: 写结构失败测试**

```python
def test_rejects_manual_ordinals_and_duplicate_h1():
    with pytest.raises(CardStructureError):
        validate_card_markdown("# 标题\n\n结论\n\n## 一、议题\n\n# 标题")

def test_rejects_complex_card_as_one_long_paragraph():
    markdown = "# 标题\n\n核心判断\n\n## 分析\n\n" + "复杂信息" * 150
    with pytest.raises(CardStructureError, match="long paragraph"):
        validate_card_markdown(markdown)

def test_accepts_core_information_block_with_multiple_judgments():
    markdown = "# 标题\n\n- 结论 A\n- 结论 B\n\n## 依据\n\n事实。"
    assert inspect_card_markdown(markdown).h2_count == 1

def test_reports_structures_without_claiming_semantic_correctness():
    signals = inspect_card_markdown(structured_markdown_fixture())
    assert signals.table_count == 1
    assert signals.ordered_step_count == 3
    assert signals.semantic_correctness is None
```

- [ ] **Step 2: 先运行测试确认失败**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_quality.py -q`

- [ ] **Step 3: 实现只读信号提取和硬格式门禁**

```python
class CardQualitySignals(BaseModel):
    h2_count: int
    h3_count: int
    table_count: int
    ordered_step_count: int
    blockquote_count: int
    long_paragraph_count: int
    reporting_tone_spans: list[str]
    manual_ordinal_headings: list[str]
```

  检查一级标题、核心信息区、手工编号、超长连续段落、表格语法和重复主标题。报告腔信号进入审核输入，不在清理器中机械删句。

- [ ] **Step 4: 把完整结构决策写入 V1 Prompt 自检**

  要求模型在生成每张卡前内部识别内容形态，但不把过程写入用户正文。卡片可组合多种呈现形式；简单卡不为格式而凑表格。

- [ ] **Step 5: 运行质量门禁和 Prompt 契约测试**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_quality.py backend/tests/unit/prompts/test_beta8_composer.py -q`

---

### Task 5: 把七场景能力和语义归属接入统一 V1

**Files:**
- Modify: `backend/src/audio_memory/prompts/beta8/scenes/work-communication.md`
- Modify: `backend/src/audio_memory/prompts/beta8/scenes/parenting-family.md`
- Modify: `backend/src/audio_memory/prompts/beta8/scenes/health-state.md`
- Modify: `backend/src/audio_memory/prompts/beta8/scenes/content-consumption.md`
- Modify: `backend/src/audio_memory/prompts/beta8/scenes/inspiration-insight.md`
- Modify: `backend/src/audio_memory/prompts/beta8/scenes/self-growth.md`
- Modify: `backend/src/audio_memory/prompts/beta8/scenes/life-decisions.md`
- Modify: `backend/tests/unit/prompts/test_beta8_composer.py`
- Create: `backend/tests/fixtures/beta8/scene_semantic_cases.json`

**Interfaces:**
- Consumes: 七场景黄金 Prompt 和 `report-quality.md`。
- Produces: 统一 V1 调用中可区分但不互相隔离的七场景能力。

- [ ] **Step 1: 写场景语义失败测试**

  夹具至少包含：会议讲第三方案例与聊天讲自己状态的对照；自动播放的播客与用户明确评价的对照；长时间困倦/宣泄；亲子冲突；未完整灵感；条件不足的生活决策。

- [ ] **Step 2: 确认旧单场景组合测试不能保证统一 V1 能力**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_composer.py -q`

- [ ] **Step 3: 保留黄金 Prompt 的完整能力并去除实验压缩版**

  保留：工作沟通的真实沟通单位；家庭的可验证互动分析；健康的日常高频状态；内容消费的媒体与用户反应分离；灵感先发散后收敛；自我成长的当期证据和工具；生活决策的条件化建议。

- [ ] **Step 4: 添加组合哈希和来源一致性测试**

  `fixed_rules_hash()` 包含全量索引 Prompt、共享质量 Prompt、统一 V1 Prompt、七场景 Prompt、审核、编辑、搜索和修订 Prompt 以及全部 Schema。

- [ ] **Step 5: 运行场景和组合回归**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts -q`

---

### Task 6: 把 Beta8ReportRunner 改为可恢复的两次 V1 路径

**Files:**
- Modify: `backend/src/audio_memory/analysis/beta8_runner.py`
- Modify: `backend/src/audio_memory/analysis/beta8_state.py`
- Modify: `backend/src/audio_memory/analysis/pipeline_state.py`
- Modify: `backend/tests/integration/test_beta8_report_runner.py`

**Interfaces:**
- Consumes: `compose_event_index()`、`validate_event_index()`、`compose_all_scenes_v1()`、`Beta8UnifiedV1Result`。
- Produces: `_run_event_index()`、`_run_unified_v1()`，检查点 `beta8_event_index` 和 `beta8_all_scenes_v1`。

- [ ] **Step 1: 用假提供商写两次调用失败测试**

```python
async def test_runner_calls_event_index_then_complete_v1_exactly_once_each(tmp_path):
    runner, provider = build_runner(tmp_path, responses=[index_json(), unified_v1_json()])
    await runner.run(version_id)
    assert [call.scene_id for call in provider.calls[:2]] == ["event_index", "all_scenes_v1"]
    assert provider.calls[0].transcript_hash == provider.calls[1].transcript_hash

async def test_index_gap_blocks_v1_call(tmp_path):
    runner, provider = build_runner(tmp_path, responses=[index_json_with_gap()])
    with pytest.raises(EventIndexCoverageError):
        await runner.run(version_id)
    assert [call.scene_id for call in provider.calls] == ["event_index"]

async def test_resume_from_index_checkpoint_only_calls_v1(tmp_path):
    runner, provider = build_runner_with_index_checkpoint(tmp_path, response=unified_v1_json())
    await runner.run(version_id)
    assert [call.scene_id for call in provider.calls] == ["all_scenes_v1"]

async def test_resume_from_v1_checkpoint_calls_neither_v1_stage(tmp_path):
    runner, provider = build_runner_with_v1_checkpoint(tmp_path)
    await runner.run(version_id)
    assert not {"event_index", "all_scenes_v1"}.intersection(call.scene_id for call in provider.calls)

async def test_v1_failure_never_silently_falls_back_to_seven_scene_calls(tmp_path):
    runner, provider = build_runner(tmp_path, responses=[valid_index_json(), truncated_response()])
    with pytest.raises(ProviderAnalysisError):
        await runner.run(version_id)
    assert all(call.scene_id not in BETA8_SCENE_IDS for call in provider.calls)
```

- [ ] **Step 2: 确认旧 `_run_scenes()` 会执行七次而测试失败**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/integration/test_beta8_report_runner.py -q`

- [ ] **Step 3: 用两个边界清晰的方法替换 `_run_scenes()`**

```python
async def _run_event_index(
    self, version, staged, transcript, transcript_markdown,
    transcript_fingerprint, worker_owner_id,
) -> Beta8EventIndex:
    cached = self._stage_payload(
        staged, "beta8_event_index", transcript_fingerprint, _EMPTY_HASH
    )
    if cached is not None:
        return Beta8EventIndex.model_validate(cached)
    request = self.composer.compose_event_index(transcript_markdown=transcript_markdown)
    index = Beta8EventIndex.model_validate(await self._generate(version, request))
    validate_event_index(index, transcript)
    await self._save_stage(
        version.id, staged, "beta8_event_index", index.model_dump(mode="json"),
        transcript_fingerprint, canonical_hash(index.model_dump(mode="json")),
        worker_owner_id, upstream_hash=_EMPTY_HASH,
    )
    return index

async def _run_unified_v1(
    self, version, staged, transcript_markdown, event_index,
    transcript_fingerprint, known_segment_ids, worker_owner_id,
) -> Beta8UnifiedV1Result:
    index_hash = canonical_hash(event_index.model_dump(mode="json"))
    cached = self._stage_payload(
        staged, "beta8_all_scenes_v1", transcript_fingerprint, index_hash
    )
    if cached is not None:
        return Beta8UnifiedV1Result.model_validate(cached)
    request = self.composer.compose_all_scenes_v1(
        transcript_markdown=transcript_markdown, event_index=event_index
    )
    result = Beta8UnifiedV1Result.model_validate(await self._generate(version, request))
    validate_unified_v1(result, known_segment_ids=known_segment_ids, event_index=event_index)
    await self._save_stage(
        version.id, staged, "beta8_all_scenes_v1", result.model_dump(mode="json"),
        transcript_fingerprint, canonical_hash(result.model_dump(mode="json")),
        worker_owner_id, upstream_hash=index_hash,
    )
    return result
```

  索引检查点的 upstream hash 为空哈希；V1 检查点的 upstream hash 为索引哈希。任一 Prompt、Schema、逐字稿或上游哈希变化均使对应下游检查点失效。

- [ ] **Step 4: 实现有界修复而不隐藏业务阶段**

  索引缺口修复请求必须包含程序检出的精确文件和区间，最多一次；V1 只有可定位的 JSON/Schema 错误才可修复一次。截断、网络中断和语义不完整必须显式失败，不分裂成七次隐式重跑。

- [ ] **Step 5: 运行 Runner 集成测试**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/integration/test_beta8_report_runner.py -q`

---

### Task 7: 扩展审核、全局编辑与定向修订，但不让它们替 V1 写作

**Files:**
- Modify: `backend/src/audio_memory/prompts/beta8/unified-audit.md`
- Modify: `backend/src/audio_memory/prompts/beta8/cross-card-orchestration.md`
- Modify: `backend/src/audio_memory/prompts/beta8/targeted-revision.md`
- Modify: `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`
- Modify: `backend/src/audio_memory/prompts/beta8_composer.py`
- Modify: `backend/src/audio_memory/analysis/beta8_audit.py`
- Modify: `backend/tests/unit/analysis/test_beta8_audit.py`
- Modify: `backend/tests/unit/prompts/test_beta8_pipeline_schema.py`

**Interfaces:**
- Produces issue types: `wrong_subject`、`wrong_communication_boundary`、`low_independent_value`、`thin_content`、`dense_unstructured_body`、`structure_content_mismatch`、`non_actionable_help`、`reporting_tone`、`duplicate_heading_number`、`source_registry_mismatch`。
- Keeps: `keep|revise|merge|drop|create` 编辑决策，未受影响卡 Markdown 字节级保留。

- [ ] **Step 1: 写质量审核和修订组合失败测试**

```python
def test_audit_supports_all_quality_issue_types():
    expected = {"wrong_subject", "wrong_communication_boundary", "low_independent_value",
                "thin_content", "dense_unstructured_body", "structure_content_mismatch",
                "non_actionable_help", "reporting_tone", "duplicate_heading_number",
                "source_registry_mismatch"}
    assert expected.issubset(get_args(Beta8AuditIssue.model_fields["issue_type"].annotation))

def test_structure_content_mismatch_targets_one_card_and_one_location():
    issue = Beta8AuditIssue.model_validate(structure_mismatch_issue_payload())
    assert issue.card_id == "card_1"
    assert issue.target_section == "方案比较"
def test_revision_prompt_places_shared_and_scene_rules_in_instructions():
    request = composer.compose_revision(
        target_scene_id="parenting_family", source_cards=[source_card_payload()],
        revision_task=revision_task_payload(), transcript_segments=segment_payloads(),
        search_packets=[]
    )
    assert shared_quality in request.instructions
    assert parenting_prompt in request.instructions
    assert "target_scene_prompt" not in json.loads_untrusted(request.user_data)
```

- [ ] **Step 2: 确认现有修订 Composer 把场景 Prompt 放在 untrusted data 中导致失败**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_composer.py backend/tests/unit/prompts/test_beta8_pipeline_schema.py -q`

- [ ] **Step 3: 把共享质量和目标场景规则放入指令层**

  修订请求的 user data 只保留来源卡、修订任务、逐字稿证据和搜索资料包。共享质量 Prompt 和目标场景 Prompt 与修订 Prompt 一起作为 instructions，从而保证修订不会把卡片变回空泛报告。

- [ ] **Step 4: 明确 V1 与审核的职责差异**

  审核只输出有证据的问题；全局编辑只决定处置和修订要求；定向修订只重写受影响的一张卡。不增加“全报告重写”调用，不将差的 V1 视为正常中间产物。

- [ ] **Step 5: 运行审核和编辑契约测试**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_audit.py backend/tests/unit/prompts/test_beta8_pipeline_schema.py backend/tests/unit/prompts/test_beta8_composer.py -q`

---

### Task 8: 保持 Kimi 2.6 搜索逻辑不变，只修复来源全局唯一性

**Files:**
- Modify: `backend/src/audio_memory/analysis/beta8_search.py`
- Modify: `backend/src/audio_memory/prompts/beta8_pipeline_schema.py`
- Modify: `backend/src/audio_memory/analysis/publisher.py`
- Modify: `backend/tests/unit/analysis/test_beta8_search.py`
- Modify: `backend/tests/integration/test_beta8_publisher.py`

**Interfaces:**
- Produces: `canonicalize_source_url(url: str) -> str`、`stable_source_id(url: str) -> str`。
- Invariant: 搜索候选、任务数、问题去重、Kimi 2.6 调用和资料采用规则不变。

- [ ] **Step 1: 写跨搜索任务 ID 碰撞失败测试**

```python
def test_same_vendor_local_id_with_different_urls_gets_different_source_ids():
    assert stable_source_id("https://a.example/one") != stable_source_id("https://b.example/two")

def test_same_canonical_url_across_tasks_gets_one_source_id():
    assert stable_source_id("HTTPS://Example.com/a?utm_source=x") == stable_source_id("https://example.com/a")

def test_publish_rejects_missing_source_registry_entry():
    bundle = publication_bundle(used_source_ids=["source_missing"], external_sources=[])
    with pytest.raises(ValueError, match="source_missing"):
        validate_publication_bundle(bundle)

def test_search_task_count_and_adoption_policy_are_unchanged():
    before = load_search_policy_snapshot("beta8_multi_scene_v1")
    after = load_search_policy_snapshot("beta8_indexed_scene_v2")
    assert after == before
```

- [ ] **Step 2: 确认旧供应商局部 ID 在不同任务中会覆盖**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_search.py backend/tests/integration/test_beta8_publisher.py -q`

- [ ] **Step 3: 实现稳定来源 ID 和发布前闭包**

```python
def stable_source_id(url: str) -> str:
    canonical = canonicalize_source_url(url)
    return "source_" + sha256(canonical.encode("utf-8")).hexdigest()
```

  发布时验证每个 `used_source_id` 存在且唯一对应一个 canonical URL；无可规范化 URL 的来源可保留在调试资料包，但不进最终来源表。

- [ ] **Step 4: 运行搜索和发布回归**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_search.py backend/tests/integration/test_beta8_publisher.py -q`

---

### Task 9: 修复 Markdown 清理和前端阅读呈现

**Files:**
- Modify: `backend/src/audio_memory/analysis/beta8_cleanup.py`
- Modify: `backend/tests/unit/analysis/test_beta8_cleanup.py`
- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/App.jsx`
- Modify: `prototype/src/styles.css`
- Modify: `prototype/tests/beta8-card-state.test.mjs`

**Interfaces:**
- Produces: 历史 Markdown 手工编号兼容、一级标题去重展示、表格横向滚动、明确的 H2/H3/正文/列表/引用层级。

- [ ] **Step 1: 写截图中两个前端问题的失败测试**

```javascript
test("does not render model H1 when card header already shows title", () => {
  const card = normalizeBeta8Card({ markdown: "# 标题\n\n核心\n\n## 结论\n\n正文" });
  assert.equal(card.title, "标题");
  assert.equal(card.body.includes("<h1>"), false);
});

test("normalizes historical manual ordinal before UI numbering", () => {
  assert.equal(normalizeSectionTitle("一、议题"), "议题");
  assert.equal(numberSection(1, normalizeSectionTitle("一、议题")), "1 议题");
});

test("wraps markdown tables in an accessible horizontal scroller", () => {
  const html = renderMarkdown("| A | B |\n|---|---|\n| 1 | 2 |");
  assert.match(html, /class="markdown-table-scroll"/);
});
```

- [ ] **Step 2: 确认旧渲染会出现重复标题/双编号**

  Run: `node --test prototype/tests/beta8-card-state.test.mjs`

- [ ] **Step 3: 实现可恢复的展示层兼容**

  原始 Markdown 不被前端改写；只在渲染派生数据中隐藏已提取的 H1，对历史 H2–H4 的手工序号先规范化再使用 UI 序号。表格容器在窄屏使用 `overflow-x: auto`，不压缩单元格到无法阅读。

- [ ] **Step 4: 运行后端清理与前端测试**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_cleanup.py -q`

  Run: `node --test prototype/tests/beta8-card-state.test.mjs`

---

### Task 10: 落地分运行、分阶段计量与终审评分

**Files:**
- Modify: `backend/src/audio_memory/analysis/pipeline_state.py`
- Modify: `backend/src/audio_memory/analysis/beta8_runner.py`
- Modify: `backend/src/audio_memory/analysis/beta8_evaluation.py`
- Modify: `backend/src/audio_memory/api/jobs.py`
- Modify: `prototype/src/api/state.js`
- Modify: `prototype/src/App.jsx`
- Modify: `backend/tests/integration/test_beta8_report_runner.py`
- Modify: `backend/tests/unit/analysis/test_beta8_evaluation.py`
- Modify: `prototype/tests/beta8-card-state.test.mjs`

**Interfaces:**
- Produces: 每次 `run_id` 的 `model_calls[]`、阶段耗时、Token、费用或“不可得”、Kimi 模型响应数、Web Search 工具数、检查点命中和终审分数。

- [ ] **Step 1: 写计量分类失败测试**

```python
async def test_metrics_separate_event_index_v1_repairs_audits_search_and_revisions(tmp_path):
    metrics = await run_fake_pipeline_with_one_schema_repair(tmp_path)
    assert [(item.stage, item.attempt_kind) for item in metrics.model_calls[:3]] == [
        ("event_index", "normal"), ("all_scenes_v1", "normal"),
        ("all_scenes_v1", "schema_repair")]

async def test_resumed_run_does_not_double_count_previous_provider_diagnostics(tmp_path):
    first, resumed = await run_then_resume_fake_pipeline(tmp_path)
    assert resumed.model_call_count == first.model_call_count + resumed.new_model_call_count

def test_missing_provider_cost_is_null_not_zero():
    metric = model_call_metric(cost=None, cost_unavailable_reason="provider did not report cost")
    assert metric.cost is None
    assert metric.cost_unavailable_reason
```

- [ ] **Step 2: 确认现有累计字段无法完整回答“两次正常调用分别多久”**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_evaluation.py backend/tests/integration/test_beta8_report_runner.py -q`

- [ ] **Step 3: 扩展指标契约**

```python
class ModelCallMetric(BaseModel):
    run_id: str
    stage: str
    attempt_kind: Literal["normal", "index_repair", "schema_repair", "retry", "resume"]
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int
    cost: Decimal | None
    checkpoint_reused: bool
```

  费用无法从提供商或配置得到时存 `null` 和原因，不猜测、不当作 0。报告总耗时以本次 `run_id` 从开始到可发布的墙钟时间计算。

- [ ] **Step 4: 前端展示正常、修复/重试、搜索和历史累计四个分组**

  默认展示本次报告总时间、索引时间、V1 写作时间、审核/修订/搜索时间、Token、费用、终审分数。调试展开项才显示具体每次调用。

- [ ] **Step 5: 运行计量和前端回归**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_evaluation.py backend/tests/integration/test_beta8_report_runner.py -q`

  Run: `node --test prototype/tests/beta8-card-state.test.mjs`

---

### Task 11: 完成新管线隔离、端到端恢复和发布阻断

**Files:**
- Modify: `backend/src/audio_memory/analysis/version_runner_router.py`
- Modify: `backend/src/audio_memory/main.py`
- Modify: `backend/src/audio_memory/analysis/task_coordinator.py`
- Modify: `backend/src/audio_memory/reanalysis/worker.py`
- Modify: `backend/tests/unit/analysis/test_version_runner_router.py`
- Modify: `backend/tests/unit/analysis/test_task_coordinator.py`
- Modify: `backend/tests/integration/test_reanalysis_worker.py`
- Modify: `backend/tests/e2e/test_beta8_pipeline_contract.py`

**Interfaces:**
- Produces: `pipeline_kind=beta8_indexed_scene_v2` 路由，不影响 `single_report_v1` 和 `beta8_multi_scene_v1`。

- [ ] **Step 1: 写版本隔离和端到端失败测试**

```python
def test_indexed_v2_routes_to_beta8_runner():
    assert type(router.resolve("beta8_indexed_scene_v2")) is Beta8ReportRunner

def test_legacy_missing_kind_routes_to_single_report():
    assert type(router.resolve(None)) is SingleReportRunner

def test_unknown_kind_fails_closed():
    with pytest.raises(ValueError, match="unknown pipeline kind"):
        router.resolve("not_a_pipeline")

async def test_fake_provider_runs_two_v1_calls_then_downstream_stages(tmp_path):
    result, provider = await run_complete_fake_beta8(tmp_path)
    assert provider.stage_names[:2] == ["event_index", "all_scenes_v1"]
    assert "initial_audit" in provider.stage_names
    assert "global_editorial_review" in provider.stage_names
    assert provider.stage_names[-1] == "final_audit"
    assert result.published is True

async def test_any_hard_blocker_prevents_atomic_publication(tmp_path):
    publisher = RecordingPublisher()
    with pytest.raises(ValueError, match="publication is blocked"):
        await run_fake_beta8_with_wrong_subject(tmp_path, publisher=publisher)
    assert publisher.calls == []
```

- [ ] **Step 2: 确认新管线值尚未受支持而失败**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_version_runner_router.py backend/tests/e2e/test_beta8_pipeline_contract.py -q`

- [ ] **Step 3: 加入新冻结路由并保留历史行为**

  不把旧 `Beta8ReportRunner` 生成的历史数据当作新索引管线产物；不尝试自动迁移旧 checkpoint。未知值不回退默认 Runner。

- [ ] **Step 4: 验证原子发布和硬阻断**

  主体错误、沟通边界错误、未解决事实错误、来源错配和未完成修订要求均阻断发布。卡片、待办、证据和外部来源在同一事务中发布。

- [ ] **Step 5: 运行路由、集成和 E2E 测试**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_version_runner_router.py backend/tests/unit/analysis/test_task_coordinator.py backend/tests/integration/test_reanalysis_worker.py backend/tests/e2e/test_beta8_pipeline_contract.py -q`

---

### Task 12: 建立真豆包逐字稿 Ground Truth 和不付费的验收工具

**Files:**
- Create: `backend/tests/fixtures/beta8/doubao-long-audio-2-manifest.json`
- Create: `backend/tests/fixtures/beta8/doubao-long-audio-2-ground-truth.json`
- Modify: `backend/src/audio_memory/analysis/beta8_evaluation.py`
- Create: `tests/evaluate-beta8-indexed-v1.py`
- Modify: `backend/tests/unit/analysis/test_beta8_evaluation.py`

**Interfaces:**
- Consumes: `/Users/liujinxin/Documents/音频Always on Demo/outputs/cloud-asr-july31/volcano/正式报告链路输入逐字稿.md`。
- Produces: 输入来源证明、工作沟通 Ground Truth、必须覆盖的播客/媒体单元、主体归属对照、低价值排除样本和单方案真实报告人工质量验收表。

- [ ] **Step 1: 写输入来源与哈希失败测试**

```python
def test_evaluation_refuses_whisper_manifest_for_doubao_acceptance():
    manifest = valid_doubao_manifest() | {"provider": "local_whisper"}
    with pytest.raises(ValueError, match="Doubao"):
        validate_doubao_manifest(manifest)

def test_manifest_requires_asr_model_file_count_segment_count_and_sha256():
    manifest = valid_doubao_manifest()
    assert manifest["model"] == "录音文件识别 2.0"
    assert manifest["file_count"] == 4
    assert manifest["segment_count"] == 6373
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"])

def test_evaluation_refuses_paid_run_without_explicit_confirmation():
    with pytest.raises(PermissionError, match="explicit confirmation"):
        require_paid_confirmation(False)
```

- [ ] **Step 2: 确认当前评测工具只校验付费确认，没有强制豆包来源**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_evaluation.py -q`

- [ ] **Step 3: 从现有 README 和逐字稿生成静态 manifest**

  Manifest 固定记录：`provider=豆包`、`model=录音文件识别 2.0`、`resource_id=volc.seedasr.auc`、4 份录音、6,373 个片段、逐字稿 SHA-256 `6cb6881073d769f91639eb478d5c8dadf96be0431016885ddddd7e8f16cb20b8`。工具每次运行先重算哈希，不匹配立即停止。

- [ ] **Step 4: 人工标注工作沟通和关键非工作单元**

  Ground Truth 只记录可验证的边界和期望：沟通起止、沟通类型、是否跨文件、必须包含的议题、播客范围、第三方案例主体和应排除的低价值卡。不预写模型应生成的具体文案。

- [ ] **Step 5: 实现无网络 dry-run 和结果评估**

  `tests/evaluate-beta8-indexed-v1.py --dry-run` 只校验输入、Prompt 哈希、Ground Truth、预计调用阶段和输出目录，不访问网络。`--run-paid --confirmed` 才允许真实运行，且每次创建全新 `run_id` 和输出目录。

- [ ] **Step 6: 运行 dry-run 和评估单元测试**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_beta8_evaluation.py -q`

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python tests/evaluate-beta8-indexed-v1.py --dry-run`

---

### Task 13: 完整回归、需求覆盖复核和待授权真实运行

**Files:**
- Modify: `docs/superpowers/specs/2026-09-03-beta8-unified-v1-report-quality-design.md`
- Modify: `docs/superpowers/plans/2026-09-03-beta8-indexed-v1-report-quality.md`
- Create after authorized run: `outputs/beta8-indexed-v1/<run-id>/acceptance-report.md`

**Interfaces:**
- Produces: 自动化回归证据、R01–R25 完成对照、真实运行计量和人工评审结果。

- [x] **Step 1: 运行全部 Beta 8 后端测试**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/prompts/test_beta8_event_index_schema.py backend/tests/unit/prompts/test_beta8_scene_schema.py backend/tests/unit/prompts/test_beta8_pipeline_schema.py backend/tests/unit/prompts/test_beta8_composer.py backend/tests/unit/analysis/test_beta8_event_index.py backend/tests/unit/analysis/test_beta8_quality.py backend/tests/unit/analysis/test_beta8_audit.py backend/tests/unit/analysis/test_beta8_cleanup.py backend/tests/unit/analysis/test_beta8_search.py backend/tests/unit/analysis/test_beta8_evaluation.py backend/tests/integration/test_beta8_report_runner.py backend/tests/integration/test_beta8_publisher.py backend/tests/e2e/test_beta8_pipeline_contract.py -q`

  2026-09-04 Task 13 指定范围回归：`293 passed in 9.74s`。该命令不包含独立实验模块 `backend/tests/unit/analysis/test_beta8_v1_ab_experiment.py`，也不替代 Task 11 已执行的更广后端、前端、代理与混合历史回归；最终整分支审查需另行汇总这些证据。

- [x] **Step 2: 运行历史链路和前端回归**

  Run: `PYTHONPATH=backend/src backend/.venv/bin/python -m pytest -p no:cacheprovider backend/tests/unit/analysis/test_version_runner_router.py backend/tests/unit/analysis/test_task_coordinator.py backend/tests/integration/test_reanalysis_worker.py -q`

  Run: `node --test prototype/tests/beta8-card-state.test.mjs`

  2026-09-04 非付费回归：历史路由、协调与重分析 `95 passed in 4.75s`；前端卡片、Markdown 与计量 `10 passed, 0 failed`。

- [x] **Step 3: 重做需求追溯审计**

  对 R01–R25 每项填写实际 Prompt 路径、生产代码路径、测试名和验证结果。任何一项只存在于设计文档而没有进入实际 Prompt/代码时，将该项标为未完成并回到对应 Task，不得用终审“代为实现”。

  完整反向追溯表已写入设计文档第 20 节，逐项列出实际运行 Prompt、生产代码、具体通过测试和当前验证边界。审计结论：R01–R25 均已有实际 Prompt/代码落点和自动化证据，没有发现纯文档占位；R05–R19、R21–R25 中依赖真实模型内容、真实搜索或浏览器观感的部分仍明确标为待授权验收，不用自动化结果冒充最终质量结论。历史 deferred minor 已在 SDD ledger 的 Task 13 对账表中逐项标为 `open`、`resolved later` 或 `accepted boundary`。

- [x] **Step 4: 执行格式和工作树保护检查**

  Run: `git diff --check`

  Run: `git status --short --branch`

  2026-09-04 检查：`git diff --check` 退出码为 0；分支仍为 `codex/beta8-report-pipeline`，既有已修改和未跟踪文件保持在位，没有执行 reset、clean、checkout 覆盖、commit、merge、push、tag 或发布。

  非付费审查结果：Task 6 修复范围已由新的独立审查员另行复审，`/private/tmp/beta8-task-6-final-review.md` 结论为 PASS、0 Critical、0 Important，此前因四次审查服务 HTTP 404 保留的门禁已关闭。复审确认重复 asyncio decorator 已解决；`BETA8_STAGE_KEYS` 缺少 legacy key 仍是 maintenance-only 的 open Minor，不重新打开 Task 6 实现门禁，交由最终整分支审查裁决。

- [ ] **Step 5: 在获得新的明确付费授权后，用真豆包逐字稿运行全链路**

  Run only after authorization: `PYTHONPATH=backend/src backend/.venv/bin/python tests/evaluate-beta8-indexed-v1.py --run-paid --confirmed`

  正常路径应记录两次 V1 调用；搜索、审核和修订调用另行记录。如果发生修复或重试，验收报告必须单独列出原因、环境、次数和费用，不与正常调用合并。

  授权前离线状态：dry-run 通过；豆包录音文件识别 2.0、4 个文件、6,373 个片段、逐字稿 SHA-256 `6cb6881073d769f91639eb478d5c8dadf96be0431016885ddddd7e8f16cb20b8`、Ground Truth SHA-256 `c232c736e42c0676d8c77d3250030ba025542ec73f79d78e0d9b75210fba8cb0`；输出声明 `network_accessed=false`、`writes_performed=false`，计划目录当时不存在。

  2026-09-06 获得用户单次授权后，只运行了正式 `beta8_indexed_scene_v2` 方案，未生成 X/Y 对照。运行目录为 `outputs/beta8-indexed-v1/run-0e547f99-0c9c-4c0b-a05b-c9595fc67106/`。第一阶段 `event_index` 在 DeepSeek V4 Pro 输出达到 16,000 Token 时以 `model_output_truncated` 终止；实际计量为 235,520 输入 Token、16,000 输出 Token、1 次 DeepSeek 调用、0 次 Kimi/搜索，未写入检查点、卡片、待办或发布产物。失败报告已写入该目录的 `acceptance-report.md`。

  定位后以 TDD 将索引阶段改为显式关闭思考并将输出安全上限提高到 32,000；定向回归 `136 passed`，Beta 8 相关回归 `337 passed`。

  2026-09-06 至 2026-09-07 获得用户第二次单次授权后，仍只运行同一正式方案，运行目录为 `outputs/beta8-indexed-v1/run-2c386603-13f2-428b-ab72-1490b0097285/`。`event_index` 已成功保存检查点，实际为 235,441 输入 Token、6,400 输出 Token、74,380 ms；`all_scenes_v1` 模型调用实际为 259,449 输入 Token、61,786 输出 Token、612,469 ms，但管线在该响应后、V1 检查点保存前终止。本次共 2 次 DeepSeek 调用，Kimi 响应、Web Search、卡片、待办和发布均为 0。因旧验收 CLI 未持久化终止异常，本计划不根据 61,786 接近 64k 就推断为截断，也不在没有证据时排除 JSON/Schema、引用、隐私或其他确定性门禁。失败证据已事后写入该目录的 `evaluation-result.json` 和 `acceptance-report.md`。

  验收 CLI 已按 TDD 补齐失败收口：后续异常会将 AnalysisVersion 和 AnalysisJob 置为 `failed`，清除 worker/lease，并将异常类型、错误码、错误消息与阶段计量写入 `evaluation-result.json`；`test_failed_paid_run_is_persisted_and_clears_running_state` 已从 RED 转为 GREEN。当时针对 61,786 Token 接近 64k 的未知风险曾暂时提高完整 V1 上限；第三次已证明 55,209 Token 的完整 JSON 失败于结构层级而非截断，因此上限已恢复为 64k，避免无证据的输出膨胀和费用风险。

  2026-09-07 获得用户第三次单次授权后，验收入口先通过输入、Ground Truth、Prompt、固定规则、模型和运行目录绑定检查，然后复用第二次的 `beta8_event_index` 检查点，未重复付费生成索引。本次只新增 1 次 `all_scenes_v1` DeepSeek 调用：259,449 输入 Token、55,209 输出 Token、574,020 ms。完整 JSON 将 `todo_candidates` 错放在 23 个卡片对象，严格 Schema 产生 23 个 `extra_forbidden` 错误，精确错误码为 `model_response_invalid`；这证实该次失败是待办字段嵌套层级错误，不是输出截断。Kimi、Web Search、卡片、待办和发布均为 0；失败状态、worker/lease 清理、错误和计量已在真实环境正常持久化。独立报告为该运行目录下的 `acceptance-attempt-3.md`。

  根因修正已按 TDD 落地：运行器仅将数组类型的 `cards[].todo_candidates` 按卡片顺序归位到所属场景，不改文字、负责人、时间、证据或 Markdown；其他错误仍严格拒绝。归位位于运行器层，固定规则哈希保持 `a9b2cceb92591e22b8637d2a48ac93061ff850b86966ea8190c0c236a5490cfa`，已付费索引仍可安全复用。非付费证据为相关回归 `173 passed`、Beta 8 全相关回归 `362 passed`、`git diff --check` 通过。由于仍无可验收报告，Step 5 保持未完成；不自动发起第四次付费调用。

  2026-09-07 用户同意先用便宜模型跑通链路后，验收 CLI 按 TDD 新增隔离的 `mechanical-smoke` 层级，只允许 `deepseek-v4-flash`，并在计划和结果中固定写入 `qualifies_as_final_acceptance=false`；正式 `final` 层级仍只允许 `deepseek-v4-pro`。随后发起一次新的 Flash 真实逐字稿运行，目录为 `outputs/beta8-indexed-v1/run-95084ea8-b428-4963-ae20-105dea6a989d/`。本次 `event_index` 实际为 235,441 输入 Token、32,000 输出 Token、140,390 ms，输出恰好达到上限后以 `model_output_truncated` 终止；共 1 次 DeepSeek Flash 调用，Kimi、Web Search、卡片、待办和发布均为 0。失败状态、错误、计量及 worker/lease 清理已持久化，独立证据为该目录下的 `acceptance-attempt-4-flash.md`。

  当前最小修正只把 `deepseek-v4-flash` 的索引上限从 32k 提高到 64k，Pro 仍为 32k，且两者索引都关闭思考。TDD 证据包括 Flash 目录、验证层级、模型持久化和 Flash-only 容量测试的 RED→GREEN；最新 Beta 8 回归为 `367 passed, 1417 deselected`，`git diff --check` 通过。由于仍无检查点或可验收报告，Step 5 保持未完成；不自动发起下一次 Flash 或 V4 Pro 付费调用。

  随后用户明确要求保持索引关闭思考、查明截断并用 Pro 重跑 V1。无费用取证确认：已成功 Pro 索引只有 52 单元、52 范围、约 13,365 JSON 字符和 6,400 Token；Flash 同输入顶格 32k 不是任务必需长度，而是模型生成行为差异。本次因此不重做索引，而是复用哈希绑定的 Pro 索引。验收入口还新增 `--stop-after-v1`，在 V1 检查点保存后以可恢复暂停收口，不允许未授权的审核或搜索调用。

  第五次付费尝试只新增 1 次 `all_scenes_v1` V4 Pro 调用：259,449 输入 Token、42,583 输出 Token、622,548 ms；未截断，此前的卡片层待办嵌套错误未再出现，且未进入审核、Kimi、Web Search 或发布。本次被 `ReservedMappingPrivacyError` 拦截；程序根因是 V1 Schema 合法要求后端路由账本 `omitted_units`，但临时下游隐私投影未剔除该账本，随后又禁止其中的内部单元 ID，导致合法输出被确定性误判。修正后原始 V1 仍保留 `omitted_units` 供完整性校验和检查点，仅隐私扫描投影移除它。独立失败证据为 `acceptance-attempt-5-pro-v1.md`；定向 47 项通过，Beta 8 全相关回归为 `371 passed, 1417 deselected`，`git diff --check` 通过。未自动发起下一次付费调用。

  第六次付费尝试仍只新增 1 次 Pro V1：259,449 输入 Token、44,463 输出 Token、475,415 ms。输出未截断，但 18 张工作沟通卡均未填 `communication_kind`，被结构校验拦截。同时发现新增的原始响应隔离保存误接到索引分支，导致该次 V1 原文仍未落盘。非付费修正已将保存点移到 V1 返回后、所有解析与校验前；仅对工作沟通卡缺失或 `null` 的类型使用契约已允许的 `other` 兜底。Beta 8 回归为 `374 passed, 1417 deselected`，固定规则哈希未变。独立证据为 `acceptance-attempt-6-pro-v1.md`。

  第七次在用户新的单次授权下恢复同一运行，只新增 1 次 Pro V1：259,449 输入 Token、43,210 输出 Token、715,682 ms。V1 通过结构、覆盖、证据引用、Markdown 和隐私门禁，成功保存 `beta8_all_scenes_v1` 检查点后在审核和搜索前受控暂停。共生成 22 张 V1 卡和 10 个待办候选；模型本次已对 18 张工作沟通卡全部显式返回具体类型，`other` 兜底未触发。原始 V1 响应已在校验前以 `0600` 落盘，84,118 bytes，SHA-256 为 `667d537846b4c8ce760d622c4fd86760b9b0d242e1c455bd2cc215b94dc01637`，元数据哈希与文件匹配。Kimi、Web Search、审核、修订和发布仍为 0。独立证据为 `acceptance-attempt-7-pro-v1-success.md`。Step 5 的全链路仍未完成，但 V1 付费阶段已有可恢复成功检查点。

- [ ] **Step 6: 对单一正式方案的真实报告做人工质量与硬门禁验收**

  本步只评审 `beta8_indexed_scene_v2` 产生的唯一真实报告，不再生成或比较 X/Y。人工先不看生成架构，按真实逐字稿、Ground Truth 和以下质量标准检查：

  1. Y 的优点：内容无重要遗漏、每张卡核心判断清楚、小标题组织好；
  2. X 的优点：单卡有足够事实、分析、依据、权衡和具体帮助；
  3. X 的缺点已消除：播客不遗漏、核心洞察存在、结构不差于 Y；
  4. Y 的缺点已消除：卡片不再单薄，建议不再只有原则；
  5. 用户本轮八类问题全部通过：工作沟通聚合、结构化、Markdown、搜索来源、具体建议、主体归属、低价值卡、整体卡片精简与丰富。

  主体错误、工作沟通边界错误、未解决事实错误和搜索来源错配必须为 0。任一项失败，当次不视为方案成功，不进入生产发布。

  当前状态：第七次尝试已生成并保存可恢复 V1 检查点，但人工初审发现工作沟通边界硬阻断：Ground Truth 中 5 次真实工作沟通被事件索引和 V1 建模为 18 次。其中六段同事职业/生活聊天被误标为工作沟通，两次车型电话被合并为一次，一次跨文件眼镜 OKR 与 Always-on 工作坊被拆为九次。最终统筹契约禁止合并不同工作沟通索引单元，因此下游付费审核无法修复该上游错误，当前不继续调用。另发现必须覆盖的无限流小说作者访谈片段遗漏，以及一项家人建议被越界转为用户待办。详细证据为 `v1-human-quality-review-attempt-7.md`。Step 6 仍未通过，不宣告整个计划完成。

---

## 计划完成的最终证据

实施只有在以下证据同时存在时才算完成：

1. R01–R25 每项都能指向实际 Prompt/代码和至少一个通过的验收用例；
2. 新管线自动化、集成、E2E 和前端测试通过；
3. `SingleReportRunner` 和历史 Beta 8 路由回归通过；
4. 真实豆包长录音 2.0 逐字稿的输入哈希与 manifest 一致；
5. 全链路生成报告在开发环境网页打开，可直接查看表格、标题、引用和运行计量；
6. 验收报告列出总生成时间、每阶段时间、Token、费用、正常/修复/重试次数、Kimi 模型响应数、Web Search 工具调用数和终审评分；
7. 真实报告同时达到“Y 的完整清晰＋X 的单卡丰富”，且不保留二者已知缺点；
8. 未获明确授权时，不执行付费运行、提交、合并、推送或发布。
