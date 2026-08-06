# Audio Memory

Audio Memory 是面向 macOS Apple Silicon 的本地音频记忆 Demo。它在本机完成音频上传、MLX Whisper 转写、说话人分段、六场景结构化分析和历史重分析；原始音频不发送给模型厂商。

## 运行环境

- macOS，Apple Silicon（M 系列）
- Python 3.12、`uv`
- Node.js、npm
- ffmpeg

## 安装与启动

```bash
./scripts/install.sh
./scripts/start.sh
```

默认只监听 `127.0.0.1:8765`。安装脚本会准备前后端依赖、Whisper 模型和本地说话人分段模型。需要排查环境时运行：

```bash
./scripts/doctor.sh
```

诊断仅读，会检查模型、迁移、历史重分析恢复、本地会话安全和服务健康状态，不打印 API Key 或厂商原始响应。

## 模型配置与隐私

应用固定支持 Kimi、DeepSeek 和 OpenAI。API Key 仅保存在 macOS Keychain；SQLite、Prompt、反馈文件和日志不保存明文 Key。音频、转写、卡片、画像和反馈位于本机 `~/Library/Application Support/AudioMemory`。

历史重分析复用已保存的结构化转写，不重复运行 Whisper。待办过期后仅标红并在未完成区优先展示，不会自动完成。

## 离线 Prompt 发布门禁

以下评测只读取仓库中的已保存 JSON 样例，不读取 Keychain，不调用任何外部模型：

```bash
cd backend
UV_CACHE_DIR=../.uv-cache uv run python ../scripts/evaluate-prompts.py \
  --fixture tests/fixtures/prompt-eval/multi-scene.json \
  --fixture tests/fixtures/prompt-eval/negative-cases.json \
  --fixture tests/fixtures/prompt-eval/injection.json
```

门禁验证 Schema、证据 ID、跨事件隔离、用户待办归属、过期状态、历史重分析零 Whisper 调用与零密钥泄漏。真实厂商 Prompt 对比未在该脚本中实现，也不是默认发布步骤；必须在用户明确授权后另行设计和执行。

## 完整验收

完整命令和通过标准见 `docs/qa/phase-1-acceptance.md`。其中包含安装烟雾测试、后端测试、前端单元/浏览器测试、生产构建、离线 Prompt 门禁和本地诊断。
