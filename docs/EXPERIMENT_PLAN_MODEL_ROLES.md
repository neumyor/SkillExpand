# 实验计划：按 LLM 角色替换的能力归因

## 1. 科研问题与假设

本分支研究一个问题：SkillExpand 中不同 LLM 角色的能力是否同等重要。当前系统把多个逻辑角色配置为独立的模型入口，但默认使用同一个模型。我们将保持执行环境、数据、提示协议和验收口径不变，只把指定角色替换为更强模型 `glm-5.3-ali`，从而测量每个角色的边际贡献。

主要假设：

- **H1，L1 经验提取假设**：更强模型用于 L1 的执行后反思和经验卡合成，会提高有效经验卡比例、证据完整性和冷启动 Skill 的质量。
- **H2，冷启动归纳假设**：更强模型用于 family discovery 和初始 Skill synthesis，会减少错误 family 归类和初始 Skill 的过度泛化。
- **H3，L2 Planner 假设**：更强模型用于 Hypothesis Planner，会提高候选修改的可执行性、证据对应性和真实改进率。
- **H4，Reviewer 假设**：更强模型用于 Reviewer，会提高“预测候选是否优于旧 Skill”的准确率，尤其减少 false positive 回归。
- **H5，交互假设**：角色之间可能存在交互。例如 Planner 的收益只有在 Reviewer 足够准确时才能转化为最终 Skill 收益。因此在单角色实验后，运行少量组合条件验证交互，而不从组合结果反推单角色因果效果。

这里的“更强模型”是指定的 `glm-5.3-ali`；基线模型和模型端点写入每个运行目录的 manifest，不以目录名代替模型身份。

## 2. 当前实现中的角色边界

当前代码支持以下独立模型入口：

| 配置角色 | CLI 参数 | 当前职责 |
|---|---|---|
| `l1_executor` | `--l1-model` | 任务执行、尝试后的反思/修复，以及 L1 经验形成所依赖的 agent 会话 |
| `cold_start` | `--cold-start-model` | family discovery、初始 Skill synthesis 及冷启动归纳 |
| `l2_planner` | `--l2-planner-model` | 读取当前 Skill、经验卡和 pattern，提出修改假设 |
| `l2_editor` | `--l2-editor-model` | rewrite 模式下生成候选 Skill；structured 模式下由程序应用 Planner 的 edit |
| `l2_reviewer` | `--l2-reviewer-model` | predicted-val / train_cards 的预测验收、sampled 的配对 Δ 预测、旧协议的校准规则压缩 |
| `l2_verifier` | `--l2-verifier-model` | 仅 sampled 协议：读取抽检题两臂轨迹，判断差异是否由该规则引起并符合声明 |
| `selector` | `--selector-model` | 为 val/test task 固定 Skill family 路由 |

经验卡提取目前不是一个单独的模型开关：L1 执行、反思/修复和最终 synthesis 属于同一条 L1 agent 流程。因此本实验把 `l1_executor` 作为这个逻辑角色的可观测替代，并在报告中明确这一限制，不能把它声称为“只替换经验卡提取模型”。

`cold_start` 也覆盖多个冷启动子步骤。若后续需要区分 Pattern Miner 与 Initial Skill Synthesizer，必须先增加独立配置入口；本计划的第一阶段不把一个模型入口拆成两个未实现的变量。

## 3. 固定实验协议

每个 benchmark 独立运行 SearchQA 和 ALFWorld，使用相同的冻结输入：

- SearchQA：`train/val/test = 400/200/1400`
- ALFWorld：`train/val/test = 39/18/134`
- 冷启动使用 train；L1/Evolve 使用同一批 train task。
- predicted Reviewer 默认使用 val scope；Reviewer 只看到 task 和 Skill，不读取经验卡、轨迹、答案或重试信息。
- test 只在所有演化结束后做独立评测，不参与 Skill 选择、Reviewer prompt 或模型选择。
- 冷启动 + Evolve 2 轮；保留当前默认的 Skill 编辑模式、候选数量、batch 顺序、并发设置和 retry 规则。
- SearchQA 使用 128/128/128，ALFWorld 使用 32/32/32；L2 reviewer worker pool 为 8。
- 每个条件使用同一冻结 task/split、相同的 route 生成协议、相同代码提交和相同请求协议；route assignment 在条件内生成后冻结。由于 cold-start 和 selector 本身可能改变 task→Skill assignment，跨条件不强行复用不兼容的 route；assignment 差异、route failure 和其对 test paired 结果的影响必须单独报告。若模型服务随机性不能完全关闭，至少使用固定 temperature/seed，并保存原始请求和响应。

基线条件为所有角色使用当前默认模型 `qwen3.6-flash-distill`。干预条件只改变一个角色为 `glm-5.3-ali`，其余角色保持基线模型。所有条件都使用新的独立 run 目录，不覆盖既有结果。

## 4. 条件与执行顺序

第一阶段采用 one-factor-at-a-time，避免在样本不足时把多个角色收益混在一起：

1. baseline：所有角色为默认模型；
2. `l1_executor-strong`；
3. `cold_start-strong`；
4. `l2_planner-strong`；
5. `l2_reviewer-strong`；
6. 可选的 `l2_editor-strong`、`l2_verifier-strong` 和 `selector-strong`，作为边界条件。矩阵协议固定为
   `acceptance_mode=predicted`，从不调用判定者，所以 `l2_verifier-strong` 在本矩阵中与 baseline 等价；
7. 根据第一阶段的最大边际收益，运行一个 all-selected 条件和最多两个有理论依据的二角色组合，检验交互。

冷启动和 Evolve 的模型身份必须在同一条件内保持一致。不能先用一种模型产生冷启动，再无记录地换另一种模型继续 Evolve；若要研究只替换 Evolve 角色，应显式复用冻结冷启动工件，并在 manifest 中记录“冷启动来源条件”和“Evolve 条件”。

## 5. 评价指标

指标在查看结果前固定：

### L1 / 冷启动

- train task 覆盖率和经验卡有效率；
- 经验卡 claim 的证据引用完整率、invalid/partial/repaired 比例；
- family discovery 的 task 覆盖、未归类率和错误归类率；
- 初始 Skill 的数量、规则数量和人工/程序审计失败数。

### L2 / Reviewer

对每个候选保存逐 task 的 prediction 和最终 paired 判断。重点指标为：

- Reviewer 预测“candidate 优于 old”的 precision、recall、false positive rate；
- 真实 accepted candidate 的 val/test paired improvement；
- Brier score、平均 `probability_true` 误差和 predicted/realized agreement；
- candidate 被接受的比例及其后续回归比例。

### 最终效果

- cold-start、Evolve-1、Evolve-2 在 test 上的成功率；
- 逐 task paired 的对→错、错→对、保持正确、保持错误；
- test route assignment 的差异、routing failure 数量及其 task IDs；
- 相对 baseline 的配对差异，而不只比较均值；
- 成本：各角色请求数、token、失败重试和 wall-clock。

主结果是 test 上预先指定的成功率和逐 task paired improvement；val 只承担 predicted acceptance，不作为最终性能报告。

## 6. 归因与统计

每个 benchmark 先报告单角色相对 baseline 的差值，再报告 paired task IDs。对最终 test 指标使用 McNemar 配对表或等价的 bootstrap 置信区间；对 Reviewer 使用逐候选的 confusion matrix 和 Brier score。不能从多个条件中挑最好的一项作为“ours”。

如果一个角色在冷启动卡片质量改善但最终 test 不改善，应分别报告中间指标与端到端结果，不能把中间信号解释成 Skill 能力提升。若模型替换改变了输出格式或导致大量解析失败，解析失败率本身是结果，不能静默删除样本。

## 7. 可复现性与停止条件

每个条件都保存：

- commit、模型名和端点身份、prompt/code hash；
- split hash、task→Skill route、并发和 retry 配置；
- 逐 task L1、card、proposal、review、acceptance、test 结果；
- 每阶段 integrity/usage audit 和 token 累计。

campaign manifest 会冻结 `EXPE_LLM_BASE_URL`；恢复或启动时若当前端点与 manifest 不同会直接拒绝。最终 paired 汇总同时给出端到端成功率、两边都成功路由后的结果，以及两边路由到同一 Skill family 的 execution-only 结果，避免把 selector failure 或 route assignment 改变误当成 executor failure。

长任务启动前必须先做 1–2 task 的完整 smoke、不变量检查和真实模型健康检查；每个单元完成即落盘并支持 resume。任何审计失败、覆盖不完整或重复 writer 都使该条件失效，不能进入汇总表。

只有在两个 benchmark 的 baseline 和至少四个主要单角色条件都完成审计后，才进行组合条件。若 `glm-5.3-ali` 服务不可用，不切换到其他模型冒充干预，保留条件为未完成并记录原因。

## 8. 执行编排

矩阵由 `scripts/model_role_matrix.py` 生成。默认只生成 baseline 和四个主要单角色条件；编辑器、判定者和 selector 条件必须显式使用 `--include-optional` 加入。每个条件都有独立的 campaign 根目录，不共享运行目录或可写状态。

先生成并审阅矩阵文件：

```bash
.venv/bin/python scripts/model_role_matrix.py plan \
  --root runs/model-role-matrix \
  --default-model qwen3.6-flash-distill \
  --strong-model glm-5.3-ali
```

再只准备各条件的冻结输入、代码、模型映射和 manifest，不启动长任务：

```bash
.venv/bin/python scripts/model_role_matrix.py prepare \
  --root runs/model-role-matrix \
  --matrix runs/model-role-matrix/matrix.json \
  --inputs /absolute/path/to/frozen-inputs
```

确认矩阵后，加入 `--execute` 才会为每个条件调用现有的 `run_campaign.py prepare`。准备完成后，逐条件运行 preflight；每个 benchmark 的 cold-start、Evolve-1、Evolve-2 都必须通过 audit，并运行独立 resume/reviewer 检查，才能启动该条件的 full campaign。矩阵编排器只负责条件定义和准备，不自动启动长任务，也不根据中间结果选择“最好”条件。

现有 full supervisor 只负责 cold-start 和两轮 Evolve。full 演化完成后，按生成的 `test-commands.json` 分别运行 SearchQA 和 ALFWorld 的 held-out test；这些命令调用冻结 campaign 自带的 wrapper，由 wrapper 重新检查 endpoint、发送真实 health request 并执行 test audit。test 结果必须各自生成 `summary.json` 与 `audit.json`，矩阵状态才会变为 `test_complete`。

每个条件的状态由以下出口决定：

1. `prepared`：manifest 和冻结输入已建立，尚未完成 preflight；
2. `preflight_complete`：两个 benchmark 的小样本全链路、阶段审计和独立检查均通过；
3. `full_evolution_complete`：两个 benchmark 的完整 cold-start、Evolve-1、Evolve-2 均通过审计；
4. `test_complete`：在该条件的完整演化结果上，两个 benchmark 的 held-out test 都完成且通过 `audit.json`。

只有 `test_complete` 条件进入汇总。汇总程序必须读取各条件的逐 task 原始结果，按预先固定的 test 指标和 paired task IDs 比较；不能从条件、轮次或指标候选中挑最大值。

使用 `scripts/summarize_model_role_matrix.py` 生成汇总。它会拒绝任何未完成或审计失败的条件，重建 routing failure、逐 task 四格表、exact McNemar p-value 和相对 baseline 的 route assignment 变化：

```bash
.venv/bin/python scripts/summarize_model_role_matrix.py \
  --matrix runs/model-role-matrix/matrix.json \
  --root runs/model-role-matrix \
  --output runs/model-role-matrix/summary.json
```
