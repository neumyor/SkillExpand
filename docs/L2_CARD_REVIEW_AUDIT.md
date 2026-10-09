# L2 提案、验收与审计

本文描述当前 L2 实现。L2 只消费当前 Evolve round 的 train 经验卡；test task 永不进入 L2，val 只在明确配置为验收面板时使用。

## 提案生成

每个 batch 固定一个 Skill、固定一组 train 卡和固定 pattern 候选。Planner 先阅读完整当前 Skill，识别已有覆盖、条件、顺序和 AND/OR 分支，再输出不重复的修改假设。每个假设必须引用本 batch 的 card/evidence ID。

- `rewrite`：Planner 生成假设，Editor 为每个假设生成完整候选 body；程序冻结 description、校验版本和去重 body。
- `structured`：Planner 直接输出一个 `{op, section, target_id, text}` edit；程序依据真实 section/rule ID 应用 edit，候选只能新增或替换一个规则，其他规则保持不变。

候选、原始响应、repair 响应和完整 diff 都写入 `l2_proposals/`。不合法假设或候选最多定向修正一次；没有有效候选则 batch hold。

## 验收模式

### predicted（默认）

`PredictedSkillScorer` 使用 selector 冻结的 `routes/val/` 分组。对当前 head 与每个候选，在同一组 val task 上逐题请求 `l2_reviewer` 模型。prompt 只包含 task 和 Skill，要求判断一次 autonomous attempt 的成功概率；不提供经验卡、轨迹、答案，也不假设 rejected answer 可以重试。

程序将 base/candidate 预测缓存到 `val/predicted_scores.jsonl`，以候选平均 `probability_true` 严格高于 base 为 paired improvement。该模式不会执行 benchmark，`acceptance.executions=0`，请求数量记录在 `acceptance.predicted_requests`。

Reviewer 的最终输出由 JSON Schema 约束为三个字段：`probability_true`（0 到
1 的数字）、`predicted_success`（必须与阈值判断一致）和不超过 80 个字符的
`reason`。Reviewer 请求单独启用 thinking；executor 的全局 thinking 开关不会
覆盖它。若模型返回 fenced JSON、前后 commentary、尾随逗号或 Python 风格
字面量，`extract_json()` 会提取完整对象；不合格的输出按 `reviewer.predicted_val` 策略重新采样（最多 32 次，
可用 `EXPE_REVIEWER_ATTEMPTS` 覆盖），用尽后该 task 记为可重试失败并保留错误工件。

### empirical

`empirical` 使用冻结 `routes/val/` 和相同 paired task IDs，启动真实 executor。每个候选都和旧 Skill 在相同 task panel 上比较实测成功率，只有严格提高才接受；它只看 val 的配对实测，不做逐卡 review。

### sampled

配对 Δ 预测 + 随机抽检修正 + 判定者，见 [ARCHITECTURE](ARCHITECTURE.md) 第 4 节；离线审计由 `l2/sampled_audit.py` 执行。

## 批次工件

`l2_batches/<batch-id>.json` 至少记录：

- `acceptance.mode`
- `panel`、`task_ids`、`protocol_hash`
- 每个候选的 `candidate_id` 与 `ValidationResult`
- `predicted_requests`、`executions`、选择理由和最终 candidate
- sampled 批次另记录每个候选的 `claim_id`、`executions`、`reviewer_requests`，以及 `sample_size`、`confidence`、`planner_memory`、`reviewer_memory_version`

`summary.json` 的 `predicted_val_candidates` 统计 predicted 与 sampled 的候选，`empirically_validated` 标明实测验收；不能把 predicted approval 报成实测提升。sampled 的实测数字见 `val_executions` 与 `reviewer_metrics`。

## 离线审计

`skillexpand.l2.audit` 不发模型请求，也不执行 benchmark：

1. 校验 round 的 train 卡覆盖、checkpoint、Skill provenance 和 card hash；
2. 读取已保存的 proposal 与验收记录，重放候选解析和选择；
3. 对 predicted 检查冻结 panel、候选覆盖、paired task IDs、`executions=0`；
4. 校验 Skill 版本链、batch 顺序、summary 和最终提交一致。

审计通过后才能把 round 数字用于后续分析。审计只验证记录和程序不变量，不证明 LLM 的自然语言因果判断正确。
