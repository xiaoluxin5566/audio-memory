# Beta 10 · RRC V4 速度优化封板

封板策略为 `rrc-v4`。启用 `AUDIO_MEMORY_RRC_ENABLED=1` 后，调用 `POST /api/recall/sessions?strategy=rrc-v4` 创建会话。使用既有种子索引时配置 `AUDIO_MEMORY_RRC_INDEX` 与 `AUDIO_MEMORY_RRC_SOURCES`。新建会话显式选择 `rrc-v3` 可切回冻结对照。

410 题产品入口交错实测，同一裁判评分。V4 准确率 311/410，交付率 409/410，最终轮答对且 7 秒内返回 214/410。V3 对应为 317/410、409/410、207/410。直答 P95 从 11.70 秒降至 10.19 秒。其余 356 题剔除了先期优化用的 54 题。分组结果见同目录的两张汇总表。本次封板用于版本追溯，线上放量另行决定。
