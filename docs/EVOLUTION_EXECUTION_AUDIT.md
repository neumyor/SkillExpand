# 执行链与恢复审计

本文对应当前 train/val/test split 和 Evolve round 实现。

## 执行顺序

1. **冷启动**：只在 train 运行无 Skill L1；保存逐题 checkpoint、经验卡、family 提案/指派和初始 Skill。
2. **round input**：冻结当前 Skill head、train task 集合和卡片身份；第一轮从 v0 开始，后续轮次必须接上一轮输出。
3. **Skill-aware L1**：当前 Skill 注入每个 train task，按配置执行 autonomous/supervised 尝试；所有卡完成并通过 L1 审计后才进入 L2。
4. **Planner/Editor**：按 Skill、task ID 和 batch 顺序生成候选。候选 body/structured edit、pattern、原始响应和 diff 逐项落盘。
5. **Acceptance**：根据 `acceptance_mode` 走 predicted（val 预测）、empirical 或 sampled（配对 Δ 预测 + 随机抽检两臂执行 + 判定者）；候选只和当前 head 在同一口径下比较。
6. **提交**：先保存 batch journal，再追加唯一选中的 Skill 版本；后续 batch 读取最新 head。
7. **round audit**：离线重放所有候选与 acceptance，检查 train 覆盖、版本链、scope、缓存和 summary。审计通过后才能进入下一轮。
8. **test**：显式 `--phase test`，使用初始 description 固定 `routes/test/`，对 test task 独立执行一次；test 不回流 L2。

## 并发边界

- train L1、family discovery、val/test task evaluation 和 predicted-val judge 可并发。
- Planner、Editor、batch commit、Skill library 写入和 round transition 串行。
- predicted-val judge，以及 sampled 的配对 Δ Reviewer 与抽检执行，使用受限 `l2_review_workers` worker pool；结果按输入顺序汇总。sampled 判定者逐题串行调用。

## 恢复与完整性

所有 task、LLM 请求、卡片、候选、预测和 batch journal 都逐单元写盘。`run.pid`/campaign lock 防止重复 writer；`l2_manifest.json` 冻结代码、配置、模型、split 和协议身份。恢复只重跑缺失或失败单元，不重采样已完成的响应。

验证运行时至少检查：

- split assignment 只包含 `train`、`val`、`test`，三者互斥且覆盖全部 task；
- train 卡恰好覆盖 train task，val/test 没有经验卡；
- val/test route manifest 与初始 Skill description 一致；
- predicted-val acceptance 没有 benchmark execution；
- sampled 的 `acceptance.executions` 等于 2 × 实际抽样题数，其余不变量见 `l2/sampled_audit.py`；
- test 总体分母包含路由失败任务；
- summary、batch journal、Skill history 和 audit 彼此一致。

审计通过是使用结果的前置条件；审计不判断自然语言规则本身是否因果正确。
