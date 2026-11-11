# Audio Memory

当前发布版本：**`0.1.0-beta.9`**。仅支持 macOS Apple Silicon，属于小范围 Beta，请保留原始音频。

[下载 Beta 9 发布包](https://github.com/xiaoluxin5566/audio-memory/releases/download/v0.1.0-beta.9/audio-memory-v0.1.0-beta.9-macos-arm64.tar.gz) · [版本变化](CHANGELOG.md) · [隐私说明](PRIVACY.md)

Audio Memory 是面向 macOS Apple Silicon 的音频记忆 Demo。本机保存音频、转写和报告；正常产品转写使用火山引擎豆包录音文件识别 2.0，报告使用 DeepSeek V4 Pro，按需使用 Kimi 搜索公开资料。云端处理涉及音频和文字传输，具体边界见下方说明。

## Beta 9 内容回溯

打开「内容回溯」，选择录制时间并上传逐字稿。索引就绪后可以提问；需要更多线索时，页面会等待你的回复，再继续原问题。展开答案的原文引用即可核对。替换或删除逐字稿后，旧版本退出检索。

回溯需要访问公司模型网关，并在 macOS 钥匙串中配置通用密码，服务为 `Audio Memory`，账户为 `recall:corporate:volcengine-deepseek-v4_1-flash`。密码为获授权的公司网关凭据。导入和查询时，模型服务会处理逐字稿、问题、对话和相关证据。运行提示词见 [RRC 源码](backend/src/audio_memory/recall/rrc)。

## 长音频报告

长音频报告采用 P1–P5 链路：

1. **P1 内容识别**：提取活动、主题、原文锚点和注意力信号。
2. **P2 全局规划**：整合活动、决定报告卡片，并规划必要的外部搜索。
3. **P3 按需搜索**：使用 Kimi 搜索公开资料，验证来源并保留搜索失败状态。
4. **P4 分场景写作**：结合活动原文和规划要求，为每张卡片生成报告。
5. **P5 逐卡评分**：评估事实、覆盖、深度、行动和表达；评分不自动改写报告。

支持工作沟通、育儿家庭、内容消费、生活决策、自我成长、灵感洞察和健康状态七类场景，按内容动态生成多张卡片。报告详情提供评分、扣分项、资料来源和原音证据。说话人簇和情绪仅作为理解参考，不用于确认人物身份。

历史重分析复用已保存的转写；转写完成后，整理或报告阶段失败可复用已有结果，避免重复转写。

## 运行环境

- macOS，Apple Silicon（M 系列）
- 发布包自带 `ffmpeg`、`ffprobe` 和 `uv`，无需预先安装 Homebrew、Python、uv、Node.js 或 npm
- 安装器会准备应用私有的 Python 环境及随版本要求的依赖；安装与云端处理需要网络

## 安装与启动

从 [Beta 9 发布页](https://github.com/xiaoluxin5566/audio-memory/releases/tag/v0.1.0-beta.9) 下载以下两个文件，放在同一目录：

- `audio-memory-v0.1.0-beta.9-macos-arm64.tar.gz`
- `audio-memory-v0.1.0-beta.9-macos-arm64.tar.gz.sha256`

在该目录中先验证校验值；只有校验成功后才继续解压和安装：

```bash
shasum -a 256 -c audio-memory-v0.1.0-beta.9-macos-arm64.tar.gz.sha256
```

```bash
tar -xzf audio-memory-v0.1.0-beta.9-macos-arm64.tar.gz
cd audio-memory-v0.1.0-beta.9
./scripts/install-release.sh
export PATH="$HOME/.local/bin:$PATH"
audio-memory start
```

安装器将程序放到 `~/Library/Application Support/AudioMemory/app`，升级前备份数据库，并保留历史音频、转写、报告、Prompt、反馈和 Keychain 配置。

首次使用需在应用中配置并校验 DeepSeek 内容分析 API 和火山语音 API；用户无需自行配置 OSS。正常产品转写只走豆包云端识别，不启动 Whisper。

常用命令：

```bash
audio-memory start
audio-memory stop
audio-memory status
audio-memory doctor
audio-memory logs
audio-memory version
```

默认只监听 `127.0.0.1:8765`。诊断仅读，不打印 API Key 或厂商原始响应。

## 模型配置与隐私

- **音频转写**：音频会通过托管上传服务进入云端对象存储，供火山引擎豆包录音文件识别 2.0 读取和转写。
- **报告分析**：生成报告所需的转写及上下文文字会发送给配置的 DeepSeek 服务；原始音频不作为报告模型输入。
- **按需搜索**：默认使用 Kimi 搜索服务，只在规划需要外部资料时调用；搜索任务要求去标识化并限于公开资料。
- **本机保存**：音频、转写、报告、Prompt、反馈和运行数据默认位于 `~/Library/Application Support/AudioMemory`。
- **凭据保护**：API Key 保存在 macOS Keychain，数据库、日志、反馈文件和发布包不应包含明文 Key。

音频和相关文字会离开本机，因此应在上传前确认适合交由上述服务处理。完整说明见 [PRIVACY.md](PRIVACY.md)。

## 源码开发与发布验收

源码开发环境需要 Python 3.12、`uv`、Node.js、npm 和音频处理组件，可运行 `./scripts/install.sh` 和 `./scripts/start.sh`。该安装脚本仍包含本地模型准备步骤；这不代表 Beta 9 正常产品转写使用本地模型。

开发与发布流程以 [AGENTS.md](AGENTS.md) 和统一脚本为准。发布验收包括后端、前端、浏览器、运行环境隔离，以及对同一归档执行固定 MP3 + AAC 的真实转写和报告流程；真实流程会调用外部服务。Beta 9 的实际验证结果和归档校验值见 [发布记录](https://github.com/xiaoluxin5566/audio-memory/releases/tag/v0.1.0-beta.9)。

旧版离线 Prompt 样例门禁仍可用于对应契约回归，但不能替代 Beta 9 P1–P5 的测试或正式发布验收。历史第一阶段验收说明见 [docs/qa/phase-1-acceptance.md](docs/qa/phase-1-acceptance.md)。
