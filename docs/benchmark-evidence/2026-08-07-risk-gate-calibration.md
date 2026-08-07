# 转写风险门离线调优证据（脱敏）

日期：2026-08-07

## 范围与安全边界

本次评测使用 `scripts/evaluate-transcription-risk-gate.py`。该工具是离线
聚合器：不调用转写模型、不读取音频、不导入或写入生产风险门配置。它只读取
人工标注的 JSONL，每行严格限于以下结构：

```json
{"segment_id":"anon-01","expected_risk":"risk","features":{"similarity":0.90,"characters_per_second":15}}
```

`segment_id` 只用于输入校验，永不进入输出。输入对象必须恰有
`segment_id`、`expected_risk`、`features` 三个字段；特征必须恰有数值型
`similarity` 和 `characters_per_second`。包含 `text`、`audio_path` 或任意
未知字段的输入均以通用“input schema violation”拒绝，错误信息不回显输入
内容。输出只含样本计数、阈值和聚合混淆矩阵/指标。

## 扫描方法

每组参数以 `similarity >= threshold` 且
`characters_per_second >= threshold` 判为风险候选，扫描：

- 相似度：0.85、0.90、0.95
- 字符速率：12、14、16

每组输出 `precision`、`recall`、`FPR`、`TPR`，并同时输出 `tp`、`fp`、
`fn`、`tn` 以便审计。某一分母为零时，相关比率输出 `null`，不会虚构概率。

候选资格与指标计算分离：只有风险和正常标签均至少有 10 个稳定样本时，
`eligible_for_candidate` 才为 `true`。在此之前，指标仅为描述性统计，不能作为
阈值推荐。工具没有写入参数的功能，绝不会自动更改生产阈值。

## 可复核的脱敏验收样本

单元测试构造了 10 条匿名 ID 和数值特征的合成数据（5 条风险、5 条正常），
用于固定指标计算和泄露防护；它不是业务数据，不能用于生产调参。在
相似度 0.90、字符速率 14 的组合下，手工核对结果为：

| TP | FP | FN | TN | Precision | Recall / TPR | FPR | 候选资格 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| 3 | 1 | 2 | 4 | 0.75 | 0.60 | 0.20 | 否（每类 5，低于 10） |

验收通过以下命令执行，不访问模型、音频或真实业务数据：

```bash
cd backend && uv run pytest tests/unit/transcription/test_risk_gate_evaluation.py -q
```

## 后续使用条件

若要形成真实候选建议，数据负责人须先提供经过人工标注、去标识化并已获授权的
JSONL；每类标签至少 10 条稳定样本。评测结果仍需人工复核误报/漏报代价并单独
审批，才可在其他变更中调整生产常量。本次工作未执行该步骤。
