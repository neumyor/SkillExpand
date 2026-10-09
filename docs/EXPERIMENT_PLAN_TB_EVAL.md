# TB-eval：TerminalBench 2.1 的 progressive library 实验（E3/E4）

> 怎么在远程服务器上跑：见 [TerminalBench 测试指南](TERMINALBENCH_TESTING.md)；实测 smoke 见第 8 节。

本文是分支 `TB-eval` 的实验规格（建立在精简后的 main 之上），复刻 `codex/tb21-progressive-relay-recovery` 的 TB2.1 实验，
但用**一个开关**实现，且开关关闭时 main 的所有执行路径逐字节不变（旧 run 的卡片哈希、`l2_manifest`
身份、searchqa/alfworld 行为均不受影响）。

## 1. 开关与复刻的实验条件

唯一的新开关：`--progressive-library`（`EvolutionConfig.progressive_library`，默认关）。

| 条件 | main（关） | TB-eval（开） |
|---|---|---|
| L1 如何选 Skill | family 固定映射，`SELECTION_FIXED` | 运行时 catalog → select → load，`SELECTION_AGENT` |
| 批分组 | 按 `plan.families` | 按卡上 `selected_skill_id` |
| predicted 验收面板 | 冻结 val route | 全部 train 题（闭集），route 落在 `routes/train/` |
| 打分缓存 | `val/predicted_scores.jsonl` | `train/predicted_scores.jsonl` |

冻结 config 的 `benchmark.progressive_library`（由导入脚本写入，不是 CLI 参数）是冷启动加载与离线审计的识别载体。
`_initialize` 做单向一致性守卫：冻结为真则开关必须开；开关开启则冻结必须为真，且 benchmark 为
terminalbench、`rollout.mode=harbor_rollout`；`__post_init__` 要求 `acceptance_mode=predicted`。开关关闭时 `progressive_library` 从 l2 身份中弹出，
旧 run 可照常 resume。

### 阶段规格（E3 / E4）

每个阶段一个独立 run 目录，原地 `--phase evolve --resume`，不使用 `--cold-start-dir`。

| 阶段 | 轨迹来源 | 区别 |
|---|---|---|
| E3 | 单源 rollouts（一个 source model） | method model A |
| E4 | 单源 rollouts（另一个 source model） | method model B |

固定参数（由 `scripts/tb_eval_stage.py launch` 写死，测试逐项核对）：`--progressive-library --acceptance-mode predicted
--skill-edit-mode rewrite --evolve-rounds 1 --candidate-count 3
--batch-size 50 --autonomous-attempts 3 --supervised-attempts 0`，worker 数取 `TB21_WORKERS`（默认 100），
`--l1-model` 为执行模型，其余角色模型与 `--selector-model` 均为 method model，加 `--llm-relay`；环境
`TBENCH_PERSIST_SANDBOXES=0`，`Popen(start_new_session=True)` 脱离进程组并写 pidfile。
`--skill-edit-mode rewrite` 是 codex 基线的默认值，main 的默认值是 `structured`，所以必须显式给出。
`--predicted-review-scope` 与 `--reviewer-update-mode`（codex 基线里的开关）在精简后的 main 中已不存在，启动器不再传。

### M0 链路（模型生成的初始库）

1. `propose_terminalbench_library.py`：模型读 89 张冷启动卡，提议多 Skill 库（不分配任务）；
2. `materialize_terminalbench_library.py`：把提议冻结为 v0 库，写 `library_manifest.json`；
3. 各阶段以 `--skill-source` 复用同一 M0（复制 `initial_skills.json`/`skills.jsonl`/`cold_start_complete.json`/`library_manifest.json`）。

准备阶段（`tb_eval_stage.py prepare`）先校验 89 题 × 3 次尝试的 raw 覆盖：基础设施异常只标注，不转成 reward=0；
覆盖不全时 `launch` 拒绝启动。

## 2. 按节点的数据流

| 节点 | 开关开启时的行为 |
|---|---|
| 0 冷启动 | `load_cold_start` 识别 progressive 标记：`protocol='progressive-library'`、卡 skill-free 且 schema-5、不要求 clusters/task_skill_map；冷启动卡保留导入时的 `family_id`，不调用 `plan.family_of` |
| 1 L1 | `evaluation/progressive.execute_progressive_experience`：目录（仅 `skill_id`+`description`，断言无 body）→ 选一个 → 只加载其 body → 共享 harbor 转换执行；卡的 `family_id`/`initial_skill_key` 写选中 Skill 的值 |
| 2 批分组 | 按 `selected_skill_id` 分组；`_round_input` 跳过 family 覆盖检查 |
| 3 验收 | 面板为 `SPLIT_TRAIN`，route 落在 `routes/train/`，与 val 一样用普通 `FrozenRoutes(...).run()`（resume 时重建并核对冻结身份） |
| 4 审计 | `audit_round` 读冻结 config 标记：`SELECTION_AGENT`、选中 Skill 属于当轮 heads 且 family/key 匹配、批按选中 Skill 分组；侧车与 manifest `routes` 交叉核对 |

溯源持久化：selector 的全部证据（含 raw 输出、完整目录）只写
`evolution/round-N/selection/<task>.json`（侧车，先于卡片写盘）；轮 manifest 的 `routes` 块只从侧车派生，
只含 `skill_id`/`skill_key`/`load_stage`/目录指纹，所以半轮崩溃后 resume 得到逐字节相同的 manifest。
`TaskExperience` 与 schema 零改动，侧车不放进 `cards/`（该目录文件集必须恰好等于任务集）。

## 3. 与 main 方法结果不可比

- **闭集选择偏差**：验收面板就是用来演化 Skill 的同一批 train 题，且 route 把它们分到各 Skill 后只在对应子集上打分，
  结果乐观；没有独立 val/test，不能报告泛化。
- 判官是 predicted reviewer（不执行），其口径与 main 的 val 面板不同。panel_key 与 `acceptance.scope`
  字符串仍写 `"val:…"`，实际面板是 train（codex 未改 update.py，保持原样以求忠实）。
- 因此 E3/E4 只能在彼此之间比较，不得与 main 的 searchqa/alfworld 或 val 面板结果并列。

## 4. 不触发的分支

sampled / empirical 验收；val/test split 与 `execute_routed`；监督式修复（`supervised-attempts=0`）；`structured` 编辑模式；
campaign launcher；多于 1 轮的演化（`--evolve-rounds 1`，代码路径支持但未在该实验中验证）。

## 5. 有意偏差（相对 codex）

1. progressive worker 放在 `evaluation/`，通过分层测试，不再绕过；fixed 与 progressive 共用一个 harbor 转换函数；
2. selection 溯源单一来源（侧车）+ manifest 聚合视图 + 审计交叉核对，不存卡字段，不写 `load_stage:'fixed'`；
3. manifest 不冻结 raw selector 输出；
4. 不再需要 route 复用：精简后的 main 里 relay 不改写冻结 config，route 身份不含 provider/端点，
   因此 relay 端口变化后 `FrozenRoutes` 的冻结身份逐字节相同，不再有 `load_existing` 的 progressive 分支，
   也没有 freeze 的 transport 容忍代码；`tests/test_tb_eval_loop.py` 的 relay 续跑测试在普通 write-once `freeze` 下验证；
5. 守卫环境 `step` 直接 raise，而非返回提示并 `reward=False`；
6. 不移植 `frozen_selector_result`：半轮崩溃后未出卡任务重新调用 selector，是已接受的 resume 成本；
7. 启动器/导入脚本参数化（保留 89/3 默认值），一次性恢复脚本、运行日志、uv.lock 不进仓库；
8. thinking 策略的第二实现（codex 的 `PredictedSkillScorer` 自解析环境变量）延后，不在本分支改。

## 6. 范围外

**E5（双源混合）不在本次范围**：codex 的 `prepare-mixed` 只写混合清单，分支中没有把两源 rollouts 组合成卡片的代码，
构建方式未知。启动器只提供 `prepare`/`launch`，待 E5 的卡片构建方式确认后另行计划。

## 7. 启动前检查

遵循 AGENTS.md：先在 1–2 个任务上干跑全链路并跑 `audit_round`；真实 LLM 健康请求；从新 shell 确认 supervisor 存活；
每个阶段完成后先做完整性审计再使用其数字。

## 8. 实测 smoke 记录（2026-10-09）

在远程节点 node29 上用本分支（`4d20bfa` 起，导入脚本修复后）端到端跑通一次最小 E3：2 个任务
（`fix-git`、`dna-insert`），所有角色（执行、selector、Planner、Editor、Reviewer、M0 提议）均为
`qwen3.6-plus-distill`，经 `--llm-relay`；`TB21_WORKERS=4`。操作步骤见
[TERMINALBENCH_TESTING.md](TERMINALBENCH_TESTING.md)。

| 项 | 结果 |
|---|---|
| E1 冷 rollouts（2 任务 × 3 次） | 11.4 分钟；6 trial，0 error；fix-git 3/3，dna-insert 1/3 |
| prepare/导入 | `--expected-tasks 2 --attempts 3`，6 trial、4 个 reward=1 |
| M0 | 约 1.2 分钟；2 个 Skill（`family-p001`、`family-p002`） |
| E3（1 轮） | 约 52 分钟（L1 约 43 分钟，其后 L2 约 8 分钟）；fix-git 3/3，dna-insert 2/3 |
| L2 | 2 批：family-p001 hold，family-p002 `review_approved`（v0→v1）；predicted Reviewer 请求 5 次，`val_executions=0` |
| 审计 | `audit_round`（`audit.json` 与离线 `audit_offline.json`）通过，所有冷启动与 round-1 卡的 `audit_harbor_experience` 通过 |

说明：

- 这是链路验证，不是方法结论。2 个任务、闭集验收，`review_approved` 不是提升证据；dna-insert 的
  1/3 → 2/3 在 3 次尝试的噪声内；
- provider 不返回 token usage，`usage/` 里 token 为 0（请求数可信）；Harbor 的 `result.json` 有每次
  rollout 的 token；
- 过程中暴露并修掉两个导入问题：冻结 config 缺 `l2_verifier` 角色（`4d20bfa`）、任务表未排序导致
  `Cold-start task data changed`（导入脚本改为按任务文件顺序编号）；另有一次在 M0 之前 launch 的失败 run
  （当时 `launch` 不检查 M0；现已改为缺 M0 时拒绝启动）。两个失败 run 目录均按“冻结目录不可原地修复”的规则归档，没有复用；
- 全量 89 × 3 的耗时没有实测，墙钟由最慢任务的 3 次串行尝试与沙箱配额决定。
