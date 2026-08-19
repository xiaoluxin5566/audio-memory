# 更新日志

## 0.1.0-beta.2

- 发布包自带 uv、ffmpeg 和 ffprobe，用户无需安装 Homebrew、Python、Node.js 或系统 FFmpeg。
- 安装时创建应用私有 Python 环境，并校验随包运行组件；已存在且有效的资源会复用。
- 修复有效 MP3/AAC 因本地音频工具缺失而被误报为格式不支持的问题。
- 开发环境与正式安装环境使用独立端口、数据目录、运行目录和 Keychain 配置，避免互相影响。
- 分析音频期间阻止 Mac 自动休眠，任务结束后自动释放。

## 0.1.0-beta.1

- 首个终端安装 Beta 版本。
- 支持在 macOS Apple Silicon 本地上传和转写音频。
- 支持使用用户配置的模型生成、审核并发布综合报告。
- 保留本机历史音频、转写、报告、Prompt 与反馈数据。
- 支持 GLM、Kimi、DeepSeek 和 OpenAI 的预设模型选择。
- 发布包自带 uv、ffmpeg 和 ffprobe，不要求用户安装 Homebrew、Python 或 Node.js。
- 本地音频组件故障不再被误报为 MP3/AAC 格式不支持。
