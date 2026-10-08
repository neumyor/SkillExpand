> **DEPRECATED (2026-10-08).** 本文件描述的是旧协议：Reviewer 在 **train** family panel 上取反馈、
> 以模板化的"失败类别"规则做校准、验收用两侧独立的绝对成功概率相比。该协议已被
> [EXPERIMENT_PLAN_PLANNER_REVIEWER_COEVOLVE.md](EXPERIMENT_PLAN_PLANNER_REVIEWER_COEVOLVE.md)
> 取代。注意 `reviewer_update_mode=rules` 仍是 predicted/empirical/jev 协议下的 CLI 与 campaign 默认值；
> 新实验若不想启用它，须显式传 `--reviewer-update-mode none`，或改用 `sampled`（该协议下只能为 `none`）。
> 本文只作为历史记录，不得据其设计新实验。

# 实验计划：单候选 Reviewer 协同演化

## 1. 科研问题与假设

本分支研究 Reviewer 是否可以利用每轮真实任务反馈持续修正自己的判断，从而减少 Skill 演化中的回归。当前多候选流程同时引入“候选生成”和“候选选择”两个变量，难以判断回归来自 Planner、Editor 还是 Reviewer。本分支先把主线简化为**每个 batch 只生成一个候选**，让每轮只有“提出一个修改 → 验收 → 接受或拒绝”的清晰因果链。

主要假设：

- **H1，单候选可归因假设**：单候选不会因为在多个候选中挑选最好者而产生选择偏差，能更直接测量一次修改是否真的改善 Skill。
- **H2，反馈校准假设**：Reviewer 读取上一轮已落盘的预测与真实 paired 结果后，更新的是可审计的 prompt 规则和证据摘要，而不是模型权重；这会降低 false positive acceptance 和后续回归。
- **H3，对抗协同假设**：Planner 负责提出可证伪的修改，Reviewer 负责独立质疑其是否在一次自主尝试中有效。两者有不同的目标和上下文，形成“提案者—验证者”的轻量博弈；只有经过真实反馈校准的 Reviewer 判断才能转化为 Skill 提交。
- **H4，收益来源假设**：协同演化的主要收益来自 Reviewer 的校准，而不是更多候选或更长的提示。因而必须与单候选但固定 Reviewer prompt 的对照条件比较。

这里的“博弈”是操作性设计：Planner 最大化真实任务成功，Reviewer 最大化预测与真实 paired outcome 的一致性。它不声称实现形式化均衡，也不把 Reviewer 的提示更新称为训练。

## 2. 目标流程

每个 Evolve round 对每个 Skill family 按如下顺序执行：

1. L1 在 train 上用当前 head 执行固定次数的一次自主尝试，逐 task 生成经验卡。
2. Planner 读取当前 Skill、本批经验卡和带证据 ID 的 pattern，输出**一个**可执行候选及其修改理由。
3. 程序校验 candidate schema、目标 section/rule ID、变更范围和 body hash；候选无效则该 batch 记录为 rejected，不生成第二个候选补位。
4. Reviewer 在冻结的 val panel 上分别对 old Skill 和 candidate Skill 做一次预测，输出 `probability_true`、`predicted_success` 和短 reason。两者使用同一 task route，benchmark execution 为 0。
5. 只有 candidate 的 paired val 预测严格优于 old head 才接受；tie、解析失败或 scope 不完整均拒绝。
6. 接受或拒绝、预测、prompt version 和所有输入输出逐单元落盘。
7. 在下一轮开始前，对上一轮**已有真实 paired feedback**生成一份结构化 Reviewer update。更新只允许增加/修改有限的可审计规则和证据摘要，不允许改变历史 prediction，也不能读取 test。

实现时应保留 rewrite/structured 两种 Skill 编辑路径。单候选是候选数量协议的改变，不应删除已有编辑模式；但本分支的主实验固定一种编辑模式，并把另一种作为独立敏感性条件。

## 3. 真实反馈与防止泄漏

仅用 Reviewer 自己的 val 预测更新 Reviewer 会造成自我循环，不能作为真实反馈。本计划采用独立的 train feedback：

- 对每个已提出的 candidate，在 round boundary 的固定 train family panel 上保存 old head 和 candidate 的**同 task、一次尝试、无重试** paired outcome；该 panel 在 manifest 中冻结，反馈在当前 round 完成后落盘，并只被下一 round 消费。若成本不允许全量执行，预先固定一个 `reviewer_feedback` 子集，所有条件使用同一子集。
- 反馈记录 task ID、old/candidate Skill key、一次执行是否成功、prediction、错误类别和轨迹 hash。
- 真实反馈只用于下一轮 Reviewer prompt 的 calibration block，例如“在某类 task 上，带有某种规则的修改曾被高估/低估”。程序先按固定模板聚合统计，C3 的 Reviewer LLM 只负责把已验证事实压缩成简短规则；不能让 LLM 从原始轨迹自由编造经验。C2 仍生成并审计 update，但保持初始 prompt，不把摘要或规则发送给 Reviewer。
- val 仍只用于当前 round 的 acceptance，test 仍只用于最终报告。Reviewer update 不读取 test，也不把 val 结果写回 prompt。
- 若上一轮没有已完成的真实 feedback，Reviewer 使用初始 prompt，不能假装已经校准。

为避免接受后的 Skill 改变使 paired 反馈无法解释，旧 head 和 candidate 必须使用同一 task、同一 route、同一 executor 配置；失败、超时和路由失败按预注册规则计入分母，不能挑选可用样本。

## 4. 对照条件与实验矩阵

每个 benchmark 使用相同的 train/val/test 划分和固定路由，执行冷启动 + Evolve 2 轮：

| 条件 | 候选数 | Reviewer prompt | 真实反馈更新 |
|---|---:|---|---|
| C0 | 1 | 固定初始 prompt | 无 |
| C1 | 1 | 固定初始 prompt + 结构化历史摘要 | 无新增校准规则 |
| C2 | 1 | 固定初始 prompt（update 仅作审计对照） | 有，按上一轮 train paired feedback 更新 |
| C3 | 1 | 固定初始 prompt + 校准规则 | 有，主方案 |
| 可选 M | 3 | 当前多候选基线 | 无，作为历史协议参照 |

主比较是 C3 vs C2：两者都单候选且都有真实反馈，唯一差异是 calibration block 是否进入 Reviewer prompt。C2 的 update 只作为固定反馈和版本链的审计对照，C3 由 Reviewer LLM 按预注册模板压缩规则。C0 用于衡量“只简化候选数”本身的影响。多候选 M 仅用于解释与历史结果的关系，不从它挑最佳候选与 C3 比较。

如果实现成本只允许两个条件，至少运行 C0 和 C3，并保留逐候选、逐 task 原始记录；这时不能把 C3 的收益单独归因于反馈校准与单候选简化。

## 5. Reviewer 更新规则

Reviewer update 必须是版本化工件，包含：

- `reviewer_prompt_version`、父版本和生成轮次；
- 反馈样本数、old/candidate 成功率、false positive/negative；
- 由程序计算的 calibration summary；
- 最多若干条短规则，每条带 feedback IDs；
- 适用范围和失效条件。

更新提示要求 Reviewer 只总结已观察到的条件性证据，禁止生成“如果第一次被拒绝就换方案”之类依赖多次重试的规则。每个评测 task 只允许一次自主尝试，因此 Reviewer 只能判断一次尝试是否成功，以及 candidate 是否相对 old 更可能成功。程序拒绝无 feedback 引用、含有未观测事实或改变输出 schema 的更新。

这使 Reviewer 的 meta 层只维护“哪些修改模式曾被高估/低估”的有限记忆；Skill 本身仍由 Planner 修改。Reviewer 不能直接编辑 Skill，也不能通过反馈修改历史 verdict。

## 6. 指标与判定

指标在查看结果前固定：

### Reviewer 质量

- predicted improve 的 precision：预测 candidate 优于 old 且真实 paired outcome 改善的比例；
- false positive regression rate：预测 improve 但真实 candidate 低于 old 的比例；
- paired accuracy、Brier score、ECE；
- 每轮 prompt update 前后的 calibration 变化。

“预测 improve 是否真的 improve”是主 Reviewer 指标；不能只报告总体 predicted success accuracy。

### Skill 演化质量

- 每轮接受率和拒绝原因；
- val acceptance 与独立 train feedback 的一致性；
- test 上 cold-start→Evolve-1、Evolve-1→Evolve-2、cold-start→Evolve-2 的对→错和错→对 task IDs；
- 最终 test 成功率和逐 task paired improvement；
- Planner proposal invalid rate、目标 rule/section 变更范围和候选 body diff 大小；
- token、请求数和 wall-clock 成本。

主结论必须基于固定 test 集和 paired task 分母。val 是验收面板，不能同时被当成最终效果或 Reviewer update 数据。

## 7. 可复现性、审计与实现边界

每个条件使用新的 run 目录，manifest 冻结：

- commit、模型和 provider 身份；
- split、route、candidate count=1、batch size、并发、retry；
- Reviewer prompt version 和 feedback subset；
- 每个 task 的旧/候选 Skill key、prediction、真实 outcome 和轨迹 hash。

长任务启动前先完成 1–2 task 的全链路 smoke、不变量检查和真实模型健康检查；每个 task 完成即落盘并支持 resume。每阶段先做完整性审计，再使用结果。发现重复 writer、scope 错误、反馈覆盖不完整或 prompt 版本漂移时，保留现场并废弃该条件。

本分支已实现单候选开关、train paired feedback 和版本化 Reviewer update 的协议骨架；本文件仍是研究设计与实现验收标准，不代表已有 benchmark 实验结果。

## 8. 实施拆解与运行入口

本分支的实现按以下顺序推进，任何完整 benchmark 运行都必须在这些步骤通过后进行：

1. **协议层**：开启 `--candidate-count 1 --single-candidate`，在 batch journal 中冻结单候选协议；若 Planner 返回多个候选，直接使该 batch 无效，不生成补位候选。
2. **反馈层**：对每个已落盘候选，在固定的 train family panel 上先保存 Reviewer 的 old/candidate 概率，再以同一 task、同一 executor、一次自主尝试执行两侧。反馈只允许写入 `reviewer_feedback.jsonl`。
3. **校准层**：由程序计算 paired precision、false-positive regression，以及 old/candidate 绝对成功概率的 Brier 和 ECE，生成版本化 `reviewer_updates.jsonl`。`summary` 模式保持初始 Reviewer prompt，`rules` 模式让 Reviewer LLM 只压缩带 feedback ID 的有限规则。
4. **审计层**：round audit 必须检查 candidate count、train-only feedback、唯一 feedback ID、old/candidate task 覆盖和 prompt version；test 不进入任何 update。
5. **预检层**：先用 1–2 个 train task 完成全链路 smoke，再做 prompt 改变不变量、候选/反馈覆盖和对照条件检查，最后才允许启动完整的两轮 campaign。

推荐的主方案入口是：

```bash
.venv/bin/python -m skillexpand \
  --benchmark searchqa --run-dir runs/reviewer-c3-searchqa \
  --phase evolve --evolve-rounds 2 --resume \
  --candidate-count 1 --single-candidate \
  --acceptance-mode predicted --predicted-review-scope val \
  --reviewer-update-mode rules --reviewer-feedback-size 0
```

其中 `--reviewer-update-mode summary` 对应 C2，`none` 对应 C0，`rules` 对应 C3。完整实验前必须分别冻结 C0/C2/C3 的 manifest、feedback subset、模型和 route，并只在固定 test 集上报告最终 paired 结果。

当前实现覆盖 C0、C2、C3 三个可归因条件；C1 的 history-only 敏感性条件暂不进入主实验，避免把结构化历史摘要与真实反馈校准混在同一个主比较中。

## 9. Reviewer 更新恢复约定

- 规则模板仍只提取“预测改善、实际退化”的 train 配对记录；修复不扩展模板、不改变历史预测、缓存身份或实验比较口径。
- 没有任何候选 feedback 时沿用上一份有效更新。有 feedback 但模板生成空观察时，跳过 LLM，保存 `generator=program`、`skip_reason=no_observed_rules`、`rules=[]` 的新版本；统计摘要来自真实 feedback，版本与 parent 链连续。
- 逐任务预测和规则压缩共用输出重试机制，默认每个请求最多 32 次尝试，`EXPE_REVIEWER_ATTEMPTS` 可调整。每次 prompt 相同，失败等待依次为 1、2、4、8、16、30 秒，随后固定 30 秒；所有实际请求仍写入原始 usage ledger。
- 网络恢复仍由底层 provider wrapper 处理。输出重试耗尽后抛出 `ReviewerUpdateError`，由 supervisor 从缓存与 journal 续跑；默认可恢复阶段失败持续重试，等待依次为 15、60、180、300 秒，之后固定 300 秒。`EXPE_STAGE_ATTEMPTS=0` 表示无次数上限，正整数可设置每次 job 启动的阶段尝试上限。
- 请求契约/认证错误及实验完整性错误保持停止；放宽恢复预算不放宽真实 feedback 引用、任务覆盖、冻结配置、轨迹及版本链校验。历史阶段审计只使用其边界之前的 feedback 与 update。
- 每次修复记录旧/新代码签名、恢复策略及已保留证据的哈希。续跑仅补齐失败部分，完成后核对阶段审计及证据不变量。
