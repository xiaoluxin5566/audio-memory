# Audio Memory v0.1.0-beta.1 验证记录

日期：2026-08-18
候选分支：`codex/release-v0-1-beta`
基线：`main` at `07835bc`

## 已通过

- 后端完整测试：954 passed，28 skipped；跳过项均为旧 Event Map 兼容链路。
- 前端生产构建：通过，生成 `prototype/dist/client/index.html` 及哈希资产。
- 前端无网络监听测试：82 passed。
- 安装烟雾测试：通过。
- 离线 Prompt 门禁：35/35 Schema 有效；跨事件污染、错误用户待办、未知证据、密钥泄漏和历史重分析 Whisper 调用均为 0。
- 版本一致性：`VERSION`、后端 `__version__` 和健康接口均为 `0.1.0-beta.1`。
- 报告详情页不显示意见反馈入口，后端和历史反馈数据保留。
- SQLite 在线备份：完整性检查通过，源数据库不被修改。
- 安装器：无效包不切换版本；重复安装幂等；历史数据库保留；创建升级前备份。
- Release 白名单：不包含测试、缓存、数据库、日志、输出、截图、设计文件或环境文件。
- Release SHA-256：校验通过。
- 从真实 tar.gz 在临时 HOME 安装：版本命令成功，历史测试记录保留，备份文件生成。
- 安装包内应用生命周期：迁移至 `0014` 并成功进入 lifespan。
- 最终归档的全局命令：在隔离 HOME 与独立端口执行 `audio-memory start/status/stop` 均成功。
- 最终归档的 HTTP 健康检查：返回 `status=ok`、版本 `0.1.0-beta.1`、平台 `macOS arm64`。
- macOS LaunchAgent 显式固定安装用户的 HOME，并直接使用发布版本自己的 Python 环境，不依赖终端 PATH。
- Release 强制包含并校验 Apple Silicon `ffmpeg`/`ffprobe`；运行时优先使用随包路径，不依赖 Homebrew。

## 当前环境限制

- `dev-proxy-security.test.mjs` 同样因监听端口返回 `EPERM`，不是断言失败。
- 未读取 Keychain、未调用真实模型、未上传真实音频。

## 发布前仍需完成

- 完成一次用户明确授权的真实音频上传、转写、报告审核与发布，重启后确认历史仍在。
- 创建 GitHub 仓库和 Release；确定公开/私有策略与许可证。
