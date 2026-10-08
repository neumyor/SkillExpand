# 当前系统架构

## 1. 数据边界

每次运行在模型调用前冻结一个互斥的 `train`、`val`、`test` 划分，默认比例为 50/25/25。

| Split | 作用 | 是否可进入 L1/L2 | 是否用于最终报告 |
|---|---|---:|---:|
| `train` | 冷启动经验卡、family、初始 Skill，以及每轮 Skill-aware L1 | 是 | 否 |
| `val` | predicted/empirical/JEV 的候选验收面板 | 仅验收时读取 | 否，结果会被选择过程污染 |
| `test` | 独立路由和单次真实评测 | 否 | 是 |

CLI 使用 `--phase test` 执行独立评测；它读取 `test` split，并写入 `test/<library-hash>/`。

## 2. 冷启动

1. `ColdStart.collect()` 在所有 train task 上运行无 Skill L1。每题保存尝试、动作、观察、奖励、反思和最终提取检查点。
2. `experience.py`、`protocol.py` 和 `learning.py` 通过证据 ID 组装经验卡。程序保留实际事件，LLM 只能提出有引用的 task-local claim；没有可靠证据时卡片可以没有 claim。
3. `family_discovery` 从 train 卡提取能力标签，生成 family 提案，再并发让每个 train task 从全部 family 中选择一个 family。程序检查覆盖和唯一归属。
4. `patterns.py` 在同一 family 的 train 卡批次上生成跨卡 pattern 候选。pattern 只是编辑线索，不是已经证明的通用事实。
5. `ColdStart.synthesize()` 生成初始 Skill Bank v0，冻结 description、body、family 映射和来源卡片 hash。

冷启动结束后，`cold_start_complete.json`、`clusters.json`、`initial_skills.json`、`task_skill_map.json` 和 discovery 审计共同定义可导入的冷启动输入。

## 3. Evolve round

每一轮按以下顺序串行执行：

1. 写入 `evolution/round-N/input.json`，冻结本轮输入 Skill 和 train task 集合。
2. 用当前 Skill head 在所有 train task 上重新运行 Skill-aware L1；卡片写入 `evolution/round-N/cards/`，每题都有独立 checkpoint。
3. 按 family、task ID 和固定 batch size 生成 `batches.json`。默认每批最多 50 张卡、1 个候选（`--candidate-count`）。
4. L2 Planner 读取当前 Skill、本批经验卡和 pattern 候选，提出不同机制的修改假设。
5. `rewrite` 模式由 Editor 生成完整候选 body；`structured` 模式由 Planner 直接输出 schema 约束的单个 edit，程序依据真实 section/rule ID 应用它。description 不可修改。
6. 相同 body 去重后进入 acceptance。每个 batch 事务写入 `l2_batches/<batch-id>.json`，只有选中的 candidate 才追加到 `skills.jsonl`。
7. round summary 和离线 audit 完成后，下一轮才允许开始。

## 4. L2 acceptance

`EvolutionConfig.acceptance_mode` 支持 `predicted`、`empirical`、`jev`、`sampled`。

### predicted + val（默认）

`PredictedSkillScorer` 使用冻结的 val route。对同一 Skill family 的旧 Skill 和每个候选 Skill，逐 task 请求独立的 `l2_reviewer` 模型。prompt 只包含 task 与 Skill，要求预测“一次自主尝试是否成功”，不提供经验卡、执行轨迹、答案或重试假设。程序缓存每个 `(panel, task, Skill body, protocol)` 结果，并以候选平均 `probability_true` 严格高于旧 Skill 为接受条件。

这条路径不执行 benchmark 环境，`acceptance.executions` 必须为 0；LLM 请求数记录为 `predicted_requests`。

### predicted + train_cards

这是兼容路径。`CardReviewer` 每次只读取一张 train 经验卡、当前 Skill 和匿名候选。reviewer 输出每个候选的 `old_outcome` 与 `new_outcome`，程序推导相对 effect 并聚合选择。它是预测性验收，不是实测成功率。

### empirical / jev

两者使用同一套冻结 val route 和 paired task IDs。`empirical` 运行真实 executor；`jev` 调用 JEV endpoint。每个候选都与当前 head 在同一 task panel 上比较，严格提高才接受。

### sampled（协同进化协议）

`l2/sampled.py` 定义该协议，全部实现与旧路径分开：

1. **声明**：Planner 在 structured edit 之外必须给出 `claim`（`trigger` 触发条件 + `action_change` 动作变化），两项单行、各不超过 400 字符。`claim_id` 由程序计算，写在 batch journal 的 hypothesis 行上，不进入 `Skill` 持久格式；audit 会按文本重算并比对。
2. **配对 delta 预测**：`evaluation/delta_review.PairedDeltaReviewer` 每题一次调用，输入是"旧规则 → 新规则"这一条改动、改动后的 body 和声明，输出 `trigger_probability` 与 `delta_probability`（配对增量），不再对旧/新 Skill 各报一个绝对成功率再相减。声明、改动、改后 body 都进入缓存身份，换声明即换缓存。
3. **抽样与修正**：`evaluation/ppi` 从冻结 val panel 中按 panel key + candidate 确定的种子抽取 `--acceptance-sample-size` 道题（该值是上限：panel 更小的 family 全量执行，实际数量记在 `decision.n_sample` 并由 audit 校验），两臂各真实执行一次（旧 head 的结果按 body 缓存复用），用样本上的成对误差修正 panel 全体预测：
   `Δ̂ = mean_panel(Δ̂_i) + mean_sample(d_i − Δ̂_i)`。
4. **判定规则**：修正后的单侧置信下界（Student-t，`--acceptance-confidence`，默认 0.9）必须大于 0；比对带 `1e-9` 的舍入保护，避免浮点残差把"零改进"判成改进。Reviewer 越准，样本误差的方差越小，同样置信度需要的执行次数越少。
5. **独立判定者**：`evaluation/divergence.first_divergence` 在两条执行的动作序列上找首个分歧步；只有存在分歧时才调用 `evaluation/claim_check.TrajectoryVerifier`，它在四种结论中选择（`claim_confirmed` / `claim_not_confirmed` / `unrelated` / `indeterminate`）。判定者冻结、不看成绩、看不到 Reviewer 预测，输入里的 context observation 取自分歧点之前（两臂相同），分歧动作本身产生的 observation 不给出。`--claim-verification off` 关闭该步骤，用于单独度量其贡献。
6. **两份记忆**：`l2/memory.py` 从 batch journal 派生。Planner 得到"改动层聚合"——各类改动的真实有效率、声明触发与兑现次数、Reviewer 预测与实测的差距；该记录类型只有数字和固定词表，**不含任何 val 题目文本或 task id**。Reviewer 得到检索式案例——被高估（看似有用实则无用）与被低估（看似无用实则有用的）val 题案例，每例附判定结论与实测差值，检索固定为一类一例且排除当前改动与当前题。`--planner-memory-mode`、`--reviewer-memory-mode` 可分别关闭。

评审记忆只由**当前轮之前**的 batch journal 派生（`before_round`）。每个 batch 的 journal 记录当轮使用的 Planner 记忆文本与 Reviewer 记忆覆盖的候选数，`audit_round` 会重算：Planner 记忆必须逐字符等于账本聚合的结果，Reviewer 记忆必须恰好覆盖更早轮次的提案。

## 5. test 评测

显式 `--phase test` 时，selector 只根据初始 Skill description 固定 test task→Skill 路由，随后每道 test task 使用当前 Skill body 独立执行一次。test 不提供 train 卡、反思、答案或指导，也不写回 L2。每题结果、轨迹、route、score cache 和 `audit.json` 都落盘；路由失败保留在总体分母中。

CLI test 阶段和 `scripts/evaluate_snapshot.py`（评测第 N 轮结束时的快照，N=0 为冷启动库）共用 `evaluation/snapshots.py:evaluate_library`。同一 run 的所有快照复用同一组冻结 test 路由；某个 Skill 路由组执行失败时，其他组照常评测，失败单元不入缓存，结束后抛错，`--resume` 只补跑缺失单元。

## 6. 并发与持久化

冷启动 train L1、family 请求、每轮 train L1 和 val/test task evaluation 可并发。Planner、Editor、batch commit 和 round transition 保持串行；train-card Reviewer 与 predicted-val judge 使用受限 `l2_review_workers` pool。每个请求和每个 task 都先落盘再汇总，目录锁防止重复 writer，恢复依赖 manifest、job lock 和逐单元缓存。

## 7. 关键工件

- `manifest.json` / `l2_manifest.json`：输入、split、代码、模型和协议指纹。
- `discovery/results/<task>.json`：冷启动 train 经验卡和 checkpoint。
- `evolution/round-N/cards/`：本轮 train 卡；各轮相互隔离。
- `l2_proposals/`：hypothesis、candidate 和原始 reviewer/judge 响应。
- `l2_batches/`：候选别名、acceptance scope、panel、task IDs、预测/实测结果和提交事务。
- `routes/val/`、`routes/test/`：冻结的 selector 路由。
- `val/predicted_scores.jsonl`：predicted-val 逐 task 缓存。
- `val/delta_predictions.jsonl`、`val/sampled_scores.jsonl`、`val/verifications.jsonl`：sampled 协议的预测、实测与判定缓存。
- `skills.jsonl`：Skill 版本链；`test/<library-hash>/`：独立 test 评测。

## 8. Reviewer 协同演化

> `reviewer_feedback.jsonl` / `reviewer_updates.jsonl` 描述的 train-panel 校准属于旧协议，
> 已由 [EXPERIMENT_PLAN_REVIEWER_COEVOLVE.md](EXPERIMENT_PLAN_REVIEWER_COEVOLVE.md)（deprecated）
> 取代；新的协同进化协议见第 4 节 `sampled` 与
> [EXPERIMENT_PLAN_PLANNER_REVIEWER_COEVOLVE.md](EXPERIMENT_PLAN_PLANNER_REVIEWER_COEVOLVE.md)。
> 本条保留为 `predicted` + `rules` 组合的说明，该组合不再参与主比较。

`--single-candidate` 把每个 batch 限制为一个候选。`--reviewer-update-mode` 为 `summary` 或 `rules` 时，每轮 L2 结束后 `SerialEvolutionLoop._collect_reviewer_feedback` 在固定的 train family panel（`--reviewer-feedback-size` 截取前 N 题）上，用同一路由各执行一次旧 head 和本轮候选，把预测与真实 paired outcome 写入 `reviewer_feedback.jsonl`。`l2/reviewer_coevolution.py` 据此计算 paired precision、false-positive regression、Brier、ECE，生成版本化的 `reviewer_updates.jsonl`。

- `none`：不收集反馈；
- `summary`：反馈和 update 只用于审计，Reviewer prompt 保持初始版本；
- `rules`：Reviewer LLM 把带 feedback ID 的统计压缩成有限规则，作为 calibration block 进入下一轮 predicted-val Reviewer 的 prompt，并计入其 protocol hash。

反馈只来自 train；val 只用于当轮验收，test 只用于报告。`audit_round` 校验反馈只引用冻结 train split 中、属于同一 family 的 task，feedback ID 不重复，update 只引用已存在的 feedback，且 version/parent 版本链连续。

## 9. 模型角色

`cfg.models` 记录七个角色：`l1_executor`、`cold_start`、`l2_planner`、`l2_editor`、`l2_reviewer`、`l2_verifier`、`selector`。`runtime/agent_factory.build_reasoning_host(cfg, usage_path, role=...)` 按角色选模型；执行 agent 使用 `cfg.agent.llm`。角色映射冻结在 `config.json` 中，因此进入 manifest 与 protocol hash；campaign 的 `audit_stage` 会核对冻结的 `config.json` 角色映射与 campaign manifest 请求的角色模型一致（不核对请求账本）。

## 10. 代码分层

```text
schema, structured_skill    记录类型、序列化、结构化 Skill 格式（仅标准库）
persistence                 io.py：原子写、freeze、JSONL、锁、源码指纹；store.py；usage.py
reliability                 异常分类与处置、重试/修复策略、单元失败记录（与 persistence 同层）
benchmarks                  SearchQA / ALFWorld 环境与任务表
runtime                     ReAct 执行器、LLM 客户端、JSON 输出解析、通用任务池、prompt 注册表
l1                          修复循环、经验卡、family 发现、冷启动、冷启动工件导入、L1 worker
evaluation                  路由、val/test 打分、JEV、快照评测、fixed-Skill worker
l2                          Planner/Editor/Reviewer、batch 事务、多轮闭环、审计、Reviewer 协同演化、sampled 协议与两份记忆
campaign, cli               冻结 campaign 启动器；单次运行 CLI
```

依赖只能向下（包括函数内导入），由 `tests/test_layering.py` 检查。每个 worker 函数放在拥有该单元的层：L1 单元在 `l1/workers.py`，val/test 执行在 `evaluation/workers.py`；`runtime/parallel.py` 只提供与业务无关的进程池/线程池。

执行器继承自 ExpeL，但只保留 L1 修复循环实际使用的部分：静态 few-shot、Skill 以 rules 形式注入、逐步 prompt 构建。ExpeL 的轨迹检索、critique/规则学习和它自己的 reflection 循环已删除；`RepairAgent`（`l1/agent.py`）直接继承 `ReactAgent`。

## 11. 冻结与来源

`persistence/io.freeze` 对冻结身份逐字段比较，`code` 源码指纹除外：

- 协议字段（split、config、prompt、模型、预算、验收模式等）任一变化都拒绝续跑，必须新建运行目录；
- 只有源码指纹变化时默认拒绝；显式 `--allow-code-change` 后继续，并在冻结文件旁的 `code_changes.jsonl` 追加一条漂移记录（变更/新增/删除的文件、前后指纹），原 manifest 不改写。每次切换到与上一条记录不同的版本都会追加一条。

**注意**：L2 的 Planner/Editor/Reviewer prompt、family discovery 与初始 Skill 合成 prompt、验收逻辑都只体现在源码指纹里，不在 manifest 的协议字段中。改动这些内容属于协议变更，必须新建运行目录；`--allow-code-change` 只用于不改变模型输入与判定的修复（崩溃、日志、性能）。当前代码不读取旧版本的工件格式；2026-10-08 之前产生的 run（包括 campaign）只能用产生它的代码续跑或审计。

campaign（`skillexpand.campaign`）在 prepare 时把源码复制到 `<root>/code/src` 并写入冻结启动器 `<root>/code/run_campaign.py`；之后所有动作都经由冻结启动器运行冻结代码。`verify` 只以 `code/` 与 `inputs/` 的摘要为准；源码仓库的 Git 状态在 prepare 时记录，漂移只在 `check` 中报告，不再导致校验失败。

修复只允许两种形式：在新的运行目录中重建，或通过 `--resume` / `--allow-code-change` 留下可审计的记录。

## 12. 异常处理与断点恢复

失败按类别处置：基础设施、模型输出、provider 拒绝、审校、配置、代码 bug。分类、处置表、重试与修复策略、单元失败记录统一在 `reliability/` 中定义。基础设施故障和模型输出修复用尽属于可重试失败；审校和配置问题会停止当前阶段；代码 bug 会立即停止整个流程。详见 [ERROR_HANDLING.md](ERROR_HANDLING.md)，其中包括新增异常类型时的扩展方法。

