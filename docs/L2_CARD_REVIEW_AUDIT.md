# L2 提案、验收与审计

本文描述当前 `serial-card-id-review-v6-relative-outcomes` 实现。L2 只消费当前 Evolve round 的 train 经验卡；test task 永不进入 L2，val 只在明确配置为验收面板时使用。

## 提案生成

每个 batch 固定一个 Skill、固定一组 train 卡和固定 pattern 候选。Planner 先阅读完整当前 Skill，识别已有覆盖、条件、顺序和 AND/OR 分支，再输出不重复的修改假设。每个假设必须引用本 batch 的 card/evidence ID。

- `rewrite`：Planner 生成假设，Editor 为每个假设生成完整候选 body；程序冻结 description、校验版本和去重 body。
- `structured`：Planner 直接输出一个 `{op, section, target_id, text}` edit；程序依据真实 section/rule ID 应用 edit，候选只能新增或替换一个规则，其他规则保持不变。

候选、原始响应、repair 响应和完整 diff 都写入 `l2_proposals/`。不合法假设或候选最多定向修正一次；没有有效候选则 batch hold。

## 三种验收模式

### predicted + val（默认）

`PredictedSkillScorer` 使用 selector 冻结的 `routes/val/` 分组。对当前 head 与每个候选，在同一组 val task 上逐题请求 `l2_reviewer` 模型。prompt 只包含 task 和 Skill，要求判断一次 autonomous attempt 的成功概率；不提供经验卡、轨迹、答案，也不假设 rejected answer 可以重试。

程序将 base/candidate 预测缓存到 `val/predicted_scores.jsonl`，以候选平均 `probability_true` 严格高于 base 为 paired improvement。该模式不会执行 benchmark，`acceptance.executions=0`，请求数量记录在 `acceptance.predicted_requests`。

Reviewer 的最终输出由 JSON Schema 约束为三个字段：`probability_true`（0 到
1 的数字）、`predicted_success`（必须与阈值判断一致）和不超过 80 个字符的
`reason`。Reviewer 请求单独启用 thinking；executor 的全局 thinking 开关不会
覆盖它。若模型返回 fenced JSON、前后 commentary、尾随逗号或 Python 风格
字面量，`_extract_json()` 会提取完整对象；截断对象会触发一次格式修复请求，
仍然无效则该 task 失败并保留错误工件。

### predicted + train_cards

这是兼容的卡片路径。每次 Reviewer 读取一张 train 经验卡、当前 Skill 和全部匿名候选；它为每个候选输出 `old_outcome` 与 `new_outcome`。程序推导：failure→success 为 improve，success→failure 为 regress，两个已知结果相同为 unchanged，其余为 unknown。Reviewer 预测不是实测成功率。

### empirical / jev

两者也使用冻结 `routes/val/` 和相同 paired task IDs。`empirical` 启动真实 executor，`jev` 调用 JEV 服务。每个候选都和旧 Skill 在相同 task panel 上比较，只有严格提高才接受。

## 批次工件

`l2_batches/<batch-id>.json` 至少记录：

- `predicted_review_scope`、`acceptance.mode`、`acceptance.scope`
- `panel`、`task_ids`、`protocol_hash`
- 每个候选的 `candidate_id`、`ValidationResult` 或逐卡判断
- `predicted_requests`、`executions`、选择理由和最终 candidate

`summary.json` 区分 `reviewed_candidates`（train-card review）与 `predicted_val_candidates`；不能把 predicted approval 报成实测提升。

## 离线审计

`skillexpand.l2.audit` 不发模型请求，也不执行 benchmark：

1. 校验 round 的 train 卡覆盖、checkpoint、Skill provenance 和 card hash；
2. 读取已保存的 proposal/review/prediction，重放候选解析和选择；
3. 对 val predicted 检查 scope、冻结 panel、候选覆盖、paired task IDs、`executions=0`；
4. 校验 Skill 版本链、batch 顺序、summary 和最终提交一致。

审计通过后才能把 round 数字用于后续分析。审计只验证记录和程序不变量，不证明 LLM 的自然语言因果判断正确。
