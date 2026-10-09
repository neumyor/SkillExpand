# 实验计划：Planner–Reviewer 协同进化（第一版）

本文件是当前实验的设计与验收标准。本文不声称已有 benchmark 结果；所有数字都必须来自固定 test 集上的独立评测。

## 1. 科研问题与假设

问题：一个只在**预测**中工作的 Reviewer，能否通过与真实环境的最小接触，变得足以替代大部分
真实执行，并在与 Planner 的往返中把 Skill 的演化推向"提议者如实、验证者准确"的状态？

- **H1（配对预测）**：让 Reviewer 看到"改动本身"（旧规则 → 新规则）并报告**配对增量** Δ，
  比让它对两个完整 Skill 各报一个绝对成功率再相减更准。绝对成功率中的大部分方差来自题目难度，
  与"这次改动是否有用"无关。
- **H2（抽检即校准）**：用少量真实抽检结果对预测做无偏修正（prediction-powered inference），
  即使 Reviewer 有系统偏差，决策也不会被带偏；Reviewer 越准，同等置信度所需的抽检越少。
- **H3（可核实的声明）**：要求 Planner 附上"触发条件 + 动作变化"的声明，并由一个冻结的独立
  判定者读取两臂完整轨迹进行核实，能把声明从空口理由变成可被证伪的承诺，从而抑制夸大。
- **H4（双向记忆）**：Reviewer 从自己过去的判断案例中学习（尤其是高估与低估），Planner 从**真实结果**
  中学习（哪类改动真的有效、自己的声明兑现了多少）。两者都以真实结果为锚，构成团队博弈而非对抗。
- **H5（收益来源）**：收益来自"Reviewer 变准 + 抽检修正"，而不是更多候选或更长提示。
  必须与"同样的抽检预算下直接实测验收"（empirical）比较，否则无法排除"钱本来就花在执行上"。

"博弈"在这里是操作性设计：验收规则事先固定、不可被任何一方改动；激励通过**验收**（哪些改动能存活）
和**记忆内容**（下一轮看到什么）起作用。本文不声称形式化均衡，也不把 prompt 更新称为训练。

## 2. 数据边界

沿用固定的 `train` / `val` / `test`。默认比例为 50/25/25；本实验使用固定的显式 split 文件
（SearchQA 400/200/1400，ALFWorld 39/18/134）。

| split | 谁可以用 | 用途 |
|---|---|---|
| `train` | Planner | 跑 L1、产生经验卡、提出改动 |
| `val` | Reviewer、判定者、抽检 | 预测、抽检、决策；**抽检题在 val 内部随机抽样** |
| `test` | 无人 | 仅最终报告，绝不回流 |

对 Planner 的额外限制（防止 val 泄漏）：

- Planner 记忆只保存**改动层**的结论：这个改动是否有效、声明兑现几次、Reviewer 对它的预测与真实
  Δ 的差距。**不保存任何 val 题目原文或 task id。**
- Planner 看不到 Reviewer 的逐题判断与理由（那会泄漏 val 内容，并诱导 Planner 迎合 Reviewer）。

本版**不**另划 calib split：抽检在 val 内进行，一份执行同时用于"本轮决策纠偏"和"下一轮记忆"。
由此带来的轻微 val 污染是已知代价，最终结论一律以 test 为准。

## 3. 一轮的流程

| # | 步骤 | 输入 | 输出 |
|---|---|---|---|
| ① | 提议（Planner） | 当前 Skill、本批 train 经验卡、Planner 记忆 | 一条 structured 改动 + **声明** |
| ② | 预测（Reviewer） | 每道 val 题、改动（旧规则 → 新规则）与改动后的 Skill、声明、Reviewer 记忆 | 逐题：会不会触发、预测 Δ |
| ③ | 抽检（真实环境） | 随机抽到的 val 题 | 旧 / 新 Skill 各跑一次，真实 Δ 与两条轨迹 |
| ④ | 判定（独立判定者） | 规则、声明、**两臂完整轨迹**（只去掉最后一步的环境反馈，其余不截断） | 两臂行为是否不同、首个不同的步骤、五类之一：无差异 / 符合声明 / 不符合声明 / 与规则无关 / 无法判断 |
| ⑤ | 决策（固定规则） | ②的预测、③的真实 Δ | 接受或拒绝 |
| ⑥ | 记账 | ①–⑤ 的全部记录 | 账本新增若干行 |
| ⑦ | 更新记忆 | 账本 | Reviewer 记忆（每轮）与 Planner 记忆（每轮，仅改动层聚合） |

决策规则（实验开始前写定，双方都不得改动）：

1. 点估计：`Δ_hat = mean_over_panel(Δ̂) + mean_over_sample(d − Δ̂)`（PPI 估计）。
2. 置信下界由抽检对的配对差 `d − Δ̂` 的方差给出，方法与小样本处理在实现中固定并记录。
3. **下界 > 0 才接受。**

该规则的两个性质是整个设计的支点：**抽检修正后的估计对 Reviewer 的偏差是无偏的**（因此 Planner
无法通过讨好 Reviewer 让无效改动通过）；**Reviewer 越准，同样的置信度需要的抽检越少**。

## 4. 组件定义

**声明（claim）**：两项必填——`trigger`（什么情况下这条规则会生效）与 `action_change`（触发后动作
从什么变成什么）。由 Planner 与 edit 一并产出，随 batch journal 冻结（不写入 `Skill` 持久格式）。

**Reviewer 输出**：每题一次调用，报告 `trigger_probability` 与 `delta_probability`，附一句理由。同一改动下逐题判断可并行，但每题只调用一次。

**判定者输出**：五类之一（`no_difference` / `claim_confirmed` / `claim_not_confirmed` / `unrelated` /
`indeterminate`）+ 首个不同的步骤（改动后一侧的步号；无差异或无法定位时为空）+ 一句理由。冻结、不看成绩、看不到 Reviewer 的预测。
两条轨迹"有没有差异、差异在哪里"完全由判定者判断，程序不做动作序列比较：SearchQA 的自由文本搜索词与
ALFWorld 的动作序列都难以用程序可靠比较，硬做会引入大量归一化规则与边界情况。

**账本**（`l2/ledger.py`）：每个改动 × 每道 panel 题一行（`sampled` 标记是否被抽中），字段为
`round · batch · family · candidate_id · 改动（节/操作）· 声明的触发条件 · Reviewer 预测 Δ ·
是否被抽中 · 判定类别 · 真实 Δ · 决策（accepted / point / lower）`。
账本只从 batch journal **派生**，不另存文件，可随时重算。

**Reviewer 记忆**：案例库，收录更早轮次每道抽检题的判断，分三类——高估（看似有用，实则无用）、
低估（看似无用，实则有用）、正确。每个案例包含 val 题、改动类型与声明的触发条件、当时的预测、
判定类别、真实结果。检索规则固定：同节、同操作；高估、低估、正确各优先取一例，其余按轮次由近到远
补足，每题最多 3 例；排除当前改动与该题本身。

**Planner 记忆**：提案历史，只到改动这一层。按改动类型（节/操作）汇总全部历史，另逐条列出最近 8 个
提案。内容为：结论计数（有效＝被接受且实测 Δ>0 / 无效＝实测 Δ≤0 / 证据不足）、预测 Δ 与真实 Δ 的均值、
声明兑现（触发 k/N，N 为抽检题数；符合声明 m/k，分母为触发数）。"触发"指判定者认定规则引起了差异，
`no_difference` 与 `unrelated` 不计入；判定者关闭时不报告这两项。

Reviewer 的学习信号只有本节的案例记忆；没有 train-panel 校准。

## 5. 预注册指标

| 层级 | 指标 | 说明 |
|---|---|---|
| **主指标（Reviewer）** | 逐题 Δ-Brier skill score，相对"全部预测 Δ=0"的基线 | 只在配对样本上计算；只报告点值，不计算置信区间（开销与收益不相称） |
| **护栏（决策）** | 候选级错误接受（被接受但抽检实测 Δ ≤ 0）与错误拒绝；regret | 只有错误接受下降、错误拒绝上升时，说明 Reviewer 只是变保守，不算变准 |
| **诊断** | 结果不一致的配对上的排序 AUC；判定类别分布 | 用于定位来源，不参与条件比较 |

实现状态：round summary 的 `reviewer_metrics` 只覆盖本轮候选，含逐题 Δ-Brier、零基线 Brier、skill score、
接受数与错误接受数（个数）。错误拒绝、regret、AUC 与判定类别分布需在分析阶段从账本计算，代码尚未实现。

| **判定者** | 不做事前校准，默认其判断可用 | 判定类别分布作为诊断报告；发现异常再修 |
| **Skill 质量** | test 上 paired 的 对→错 / 错→对 task IDs；最终成功率 | 最终结论一律用固定 test 集与逐题配对分母 |
| **成本** | 抽检次数、总执行次数、token、wall-clock | 用于画"成本—决策质量"曲线 |

主指标在看数据前固定；不得事后从多个口径中挑选。所有配对比较保留原始单元级记录。

## 6. 对照与消融

| 条件 | 候选 | 验收 | 声明核实 | Reviewer 记忆 | Planner 记忆 |
|---|---:|---|---|---|---|
| **A0** 历史参照 | 1 | `predicted`（val 上两侧独立的绝对概率） | — | — | — |
| **A1** PPI | 1 | `sampled` | off | off | off |
| **A2** +Reviewer 记忆 | 1 | `sampled` | on | on | off |
| **A3** 完整方案 | 1 | `sampled` | on | on | on |
| **A4** 无声明核实 | 1 | `sampled` | **off** | on | on |
| **A5** 实测验收 | 1 | `empirical`（全量 panel 实测，无抽样上限） | — | — | — |

- 主比较 **A3 vs A2**（Planner 记忆的价值），**A2 vs A1**（Reviewer 记忆的价值），
  **A1 vs A0**（配对预测 + PPI 的价值），**A4 vs A3**（可核实声明的价值）。
- **A5 是成本逻辑的关键基线**：如果 A3 不能以更少的真实执行达到 A5 的决策质量，
  那么"用预测代替执行"的动机不成立，必须如实报告。两边的 `acceptance.executions` 口径不同：
  empirical 只计未命中缓存的 episode；sampled 固定计"两臂 × 抽样题数"，含缓存复用的旧臂。成本比较
  须统一口径（建议都用未命中缓存的 episode 数，从各自的 usage/score 缓存统计）。
- 每个条件使用新的 run 目录。campaign manifest 冻结 commit、模型与 role 映射、split、candidate count = 1、
  抽样上限、置信水平、记忆模式与判定者开关；val route 冻结在 run 目录的 `routes/val/`；prompt 变更按约定须新建 run 目录，
  journal 的 `protocol_hash` 只覆盖执行协议。

## 7. 审计不变量

以下检查由 `l2/sampled_audit.py` 自动执行；任一条不成立则该条件作废归档。

1. 抽检集 ⊆ 冻结 val panel，且由 journal 中记录的 `sample_key`（panel key + candidate）确定性复现；
   `decision.n_panel` / `n_sample` 给出抽样比例，`n_sample` 必须等于 `min(上限, panel 大小)`。
2. 每个改动对每道 panel 题恰好一行，`sampled` 标记恰好覆盖抽检集。
3. 点估计与置信下界可由逐题记录重算，与 journal 记录一致。
4. 判定者开启时每道抽检题恰好一条判定记录，类别合法、理由非空；关闭时没有任何判定记录。
   （输入不含成绩与 Reviewer 预测由实现保证，审计不复核。）
5. Planner 记忆逐字符等于由更早轮次账本重算的结果；其渲染不读取任何题目，因此不含 val 题目原文或 task id。
6. journal 中的 `reviewer_memory_version` 等于更早轮次中有抽检记录的候选数。
7. 声明不被改动：`claim_id` 可由声明文本重算，且每个候选的验收记录引用的正是其 proposal 的 `claim_id`。
8. 每条预测记录 `reviewer_cache_key`（覆盖 task id、两侧 body、声明与该题记忆块全文），缓存行另记
   `memory_hash`；审计检查其存在。

## 8. 实施步骤

第 1–6 步已实现；任何完整 campaign 都必须在第 7 步通过后启动。

1. **声明**：Planner 输出声明，`parse_plan` 校验，写入 batch journal 并冻结；审计检查第 7 条。
2. **配对 Δ Reviewer**：新的 Reviewer 打分器（看到改动与声明，输出触发概率与 Δ）。
3. **抽检 + PPI**：`acceptance_mode=sampled`，抽样、点估计与置信下界，审计第 1–3 条。
4. **独立判定者**：新 role，读取两臂轨迹，五类输出与首个不同步骤、解析、缓存；审计第 4 条。不做事前校准。
5. **两份记忆**：Planner 聚合记忆与 Reviewer 案例记忆的构建、注入与版本化；审计第 5、6 条。
6. **接线**：CLI、campaign、manifest、文档。
7. **预检**：小样本全链路 smoke、上述全部不变量、对照条件核对，通过后才允许完整运行。
   campaign 的 preflight split 只有 4 题（2 train / 1 val / 1 test），每个 val panel 最多 1 题，evolve 阶段
   的候选必然以 `insufficient_sample` 或空 panel 结束，跑不到 PPI 下界。因此 sampled campaign 的
   `independent-check` 额外运行 `campaign.sampled_acceptance_probe`：用一条固定的通用改动和声明，把全部 4 道
   preflight 题（均取自完整 train，不暴露任何 val/test 题）作为 panel，按冻结的抽样上限、置信水平和判定者
   开关执行一次真实的 sampled 验收——真实的配对 Δ 预测、两臂真实执行、真实轨迹上的判定者——再用
   `sampled_audit` 重放；没有得到 PPI 下界或审计不通过都会让检查失败。改动是否被接受不作判定。

## 9. 风险与失效条件

- **执行非确定性**：执行器是 LLM，改一条规则也可能扰动与它无关的步骤。这类差异由判定者标为
  `unrelated`，不计入"规则触发"；它会让真实 Δ 更嘈杂，但不影响估计的无偏性。
- **判定者自身误差**：只影响记忆，不影响是否接受。本版不做事前校准，默认其判断可用；报告中需说明
  它最多影响哪些结论（两份记忆中的"触发 / 符合声明"统计），发现异常再修。
- **样本量**：抽样规模按 family 自适应——`--acceptance-sample-size` 是上限，panel 更小的 family 直接全量执行
  （`evaluation/ppi.effective_sample_size`，实际数量记在每个候选的 `decision.n_sample`，审计会核对它等于
  `min(请求值, panel 大小)`）。ALFWorld 每个 family 的 val 只有 3–5 题，因此那里 PPI 相对实测没有节省，
  报告需按 family 规模分层说明。panel 只有 1 题时无法给出区间，候选以 `insufficient_sample` 拒绝；
  panel 为空时整批 hold（`hold: frozen val panel is empty`），不产生候选记录，也不进入账本。两者都必须
  如实计入该 family 的接受率，而不是当作"没有改动值得接受"。
- **交互轮数**：信誉与记忆需要多轮才能显现。若总轮数过少，Planner 一侧学不到东西，此时应报告"无效应"而非
  挑选数据。
- **共同盲区**：Planner 与 Reviewer 默认同模型，错误可能高度相关。判定者与真实抽检是唯一的独立信号，不可省。
- **val 轻微污染**：抽检在 val 内进行，Planner 记忆虽只到改动层，仍可能间接吸收 val 分布信息。
  最终结论以 test 为准；报告中必须写明这一限制。

## 10. 与旧协议的关系

旧的 Reviewer 校准（train 反馈、模板规则）与逐卡 Reviewer、JEV 验收都已从代码中删除，可从 git 历史恢复；
`acceptance_mode=predicted` 保留为 A0 历史参照，不参与新协议的主比较。
新协议使用独立的模块与协议版本号；新旧 run 互不兼容，也不复用。
