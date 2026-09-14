# Beta 8 七场景完整 Prompt 集合

本目录用于保存七个场景各自可独立送给模型的完整 Prompt。它们是效果基线和后续运行链路的候选输入，不覆盖 `docs/beta8/report-scenes/` 中已经确认的原始设计稿。

工作与沟通已由用户确认，直接使用：

- `../work-prompt-composition-review-v3-1/03-work-composed-v1-generation-prompt.md`

其余六个场景在本目录中按同一运行标准整理：

- `parenting-family.md`
- `health-state.md`
- `content-consumption.md`
- `inspiration-insight.md`
- `self-growth.md`
- `life-decisions.md`

统一标准包括：完整可靠逐字稿、输入数据不作为指令、同次完成场景判断与 V1 生成、零到多卡、完整 Markdown、场景内搜索候选、场景内待办候选、五项自检和调用方注入的薄 JSON Schema。

本目录暂不代表生产接入。后续可以直接保留七份完整稿，也可以从中抽取公共源文件；无论采用哪种维护方式，已确认的完整稿都应继续保留为黄金对照。
