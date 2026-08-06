# Audio Memory 第一阶段验收清单

## 1. 安装与启动

- 在 macOS Apple Silicon 上执行 `./scripts/install.sh`，安装完成且不修改 shell 配置。
- 连续执行两次安装，第二次不破坏已有模型、Prompt、Keychain 或本地历史。
- 执行 `./scripts/start.sh` 后只启动一个 `127.0.0.1` 服务，并自动打开首页。
- `/`、`/history`、`/settings/prompts` 和 `/api/health` 均可访问；未知 `/api/*` 返回 404。
- 端口被占用时给出可复制的换端口命令；已有健康实例时不重复启动。
- `./scripts/doctor.sh` 不修改数据、不输出 Key 或厂商原始响应。
- `./scripts/doctor.sh` 同时检查 Whisper/说话人分段模型、分析迁移链、历史重分析恢复组件和本地会话安全组件。

## 2. 首次使用与模型配置

- 未配置时首页右侧只显示“先上传音频”，上传区不可用。
- 配置弹窗固定显示 Kimi、DeepSeek、OpenAI，不支持新增或删除厂商。
- 新 Key 校验失败时输入内容在弹窗打开期间保留，旧 Key 和正式状态不变。
- 关闭配置弹窗后清除候选 Key；再次打开不回显 Key。
- 新厂商配置成功后自动设为当前模型并关闭弹窗；无需再次点击“设为当前厂商”。
- 每次程序启动、保存、重新校验和开始分析前都会验证当前 Key。
- 限流时显示倒计时；填写新 Key 可绕过旧 Key 的冷却期。
- Keychain 不可访问时显示独立提示，不误报为“未配置”。

## 3. 上传、转写与分析

- 仅接受真实 MP3/AAC；扩展名与内容均校验。
- 多文件按选择顺序逐个上传，每条显示独立进度。
- 遇到不支持格式时暂停后续上传；移除错误文件后可继续。
- 上传、转写、分析期间右侧保留旧信息流，不显示半成品卡片。
- 三个厂商都使用本地 MLX Whisper；原始音频不发送给模型厂商。
- 批次开始后锁定厂商、模型和 Prompt 版本快照。
- 转写中断后需用户明确继续，并复用已保存的片段。
- 模型分析失败后保留完整转写；可切换厂商重新分析，不重复 Whisper。
- 历史重分析仅复用已保存的结构化转写和说话人结果，Whisper 调用数始终为 0；中断后可从检查点恢复。
- 失败、取消和未提交批次不进入音频历史与信息流。

## 4. 信息流与内容操作

- 待办全局置顶；可编辑、完成、删除；过期标红、未完成区优先、不自动完成。
- 日期按自然日分割；批次越新越靠上；同批卡片按会议、家庭教育、内容推荐、成长建议、闲聊灵感排列。
- 没有命中的场景不生成空卡；右侧只在整批原子发布完成后刷新。
- 所有卡片可打开覆盖式详情页，关闭后回到原信息流位置。
- 详情顶部只有一个意见反馈入口；继续追问位于内容底部。
- 用户消息右对齐、AI 消息左对齐，均为气泡；重新打开详情后完整 QA 仍存在。
- 追问只使用当前卡片、当前音频转写、相关画像和该卡历史 QA。

## 5. Prompt、反馈与清除

- Prompt 页面固定六个场景，只支持编辑与保存自然语言内容。
- 保存生成新版本；只影响之后的新批次，不改 Schema、不重算历史。
- “完全准确”可直接提交；“内容不准”必须填写具体原因。
- 每条反馈文件包含场景、音频信息、完整转写、生成内容、Prompt/模型快照和完整 QA。
- 清除历史会删除音频、转写、卡片、待办、QA 和隐藏画像。
- 清除历史保留三厂商 Keychain 配置、Prompt 版本与意见反馈文件。

## 6. 隐私与安全

- API Key 只存在于 macOS Keychain，SQLite、Prompt、反馈、诊断输出和日志中均不存在明文 Key。
- 本地服务仅绑定 `127.0.0.1`；前端和 API 同源。
- 页面会话令牌只以哈希形式持久化；所有写 API 要求同源会话和幂等键，重启后重放不会重复写入。
- 音频、转写、卡片、画像与反馈均存放在本机应用数据目录。
- 诊断缓冲区只保留归一化信息，不落盘厂商原始响应。
- 清理临时文件前进行应用目录边界校验，不能删除目录外文件。

## 7. 自动化质量门禁

```bash
bash tests/install-smoke.sh
cd backend && UV_CACHE_DIR=../.uv-cache uv run pytest -q
cd ../prototype && node --test tests/*.test.mjs && npm run build && npm run test:e2e -- --reporter=line
cd ..
cd backend && UV_CACHE_DIR=../.uv-cache uv run python ../scripts/evaluate-prompts.py \
  --fixture tests/fixtures/prompt-eval/multi-scene.json \
  --fixture tests/fixtures/prompt-eval/negative-cases.json \
  --fixture tests/fixtures/prompt-eval/injection.json
cd ..
./scripts/doctor.sh
```

通过标准：所有命令退出码为 0；页面无控制台错误；离线评测 Schema 通过率为 100%，未知证据、跨事件污染、错误归属用户待办、历史重分析 Whisper 调用、过期自动完成和密钥泄漏均为 0；正式历史只包含完整成功批次。

Prompt 评测默认且当前仅支持离线已保存样例，不读取 Keychain，不调用 Kimi、DeepSeek 或 OpenAI。真实厂商对比须另行获得用户明确授权，不属于本次离线发布门禁。
