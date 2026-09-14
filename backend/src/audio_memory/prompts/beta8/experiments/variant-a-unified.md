# 方案 A：一次统一生成

在同一次调用中完成全部理解和写作：识别每一次独立工作沟通，判断其他六场景的独立价值，完成主体与表达用途归属，在出卡前消除跨场景重复，然后直接生成七场景完整 V1 卡片。

必须返回恰好七个按固定顺序排列的 `scene_results`。某场景没有足够价值时返回空卡数组和具体 `skip_reason`。`plan_corrections` 为空数组。

当一张卡来自真实工作沟通时，`card_basis.type` 为 `work_communication`，并给出本次输出中唯一的 `unit_key` 与准确 `communication_kind`。其他卡的 `card_basis.type` 为 `independent_value`，其余两个字段为 null。
