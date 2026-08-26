# Beta 6 Zero-Config Managed Storage Hotfix Plan

**Goal:** 让 Beta 6 在全新安装、空 Keychain 下自动建立每设备身份，安全获取单对象 OSS 临时授权，并在上传前准确抦截未就绪状态。

**Architecture:** Broker 签发自包含的安装凭据，客户端使用 Keychain 内的每设备 Ed25519 私钥对每次请求签名。Broker 在签发 OSS URL 前验证凭据、请求签名、时间窗口、nonce、配额和封禁；客户端启动时零配置引导。

**Spec:** `docs/superpowers/specs/2026-08-26-zero-config-managed-storage-hotfix-design.md`

## Tasks

- [ ] 增加设备密钥、安装凭据及规范请求签名的单元测试，先确认失败。
- [ ] 增加 Broker 无感注册、防重放、配额/封禁、对象所有权的集成测试，先确认失败。
- [x] 实现 Broker 凭据签发和验证，保留开发用静态授权适配器，生产不再要求 `BROKER_TOKEN_SHA256`。
- [x] 将 nonce、配额、封禁和注册熔断改为持久化 SQLite 事务帐本；生产未挂载共享卷时拒绝启动。
- [x] 实现客户端后台自动引导、有界退避重试与已签名的 Broker 请求，私钥/凭据仅保存在运行环境对应 Keychain。
- [ ] 将托管存储纳入 readiness 和上传前硬门禁，修正错误文案与恢复路由。
- [ ] 运行定向测试、全量回归、安装包密钥扫描和全新安装真实链路验收。
- [ ] 仅交付热修候选与证据；未获得用户新的发布确认前，不合并、不打标签、不更换公开资产。
