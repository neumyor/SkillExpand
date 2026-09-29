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
3. 按 family、task ID 和固定 batch size 生成 `batches.json`。默认每批最多 50 张卡、最多 3 个候选。
4. L2 Planner 读取当前 Skill、本批经验卡和 pattern 候选，提出不同机制的修改假设。
5. `rewrite` 模式由 Editor 生成完整候选 body；`structured` 模式由 Planner 直接输出 schema 约束的单个 edit，程序依据真实 section/rule ID 应用它。description 不可修改。
6. 相同 body 去重后进入 acceptance。每个 batch 事务写入 `l2_batches/<batch-id>.json`，只有选中的 candidate 才追加到 `skills.jsonl`。
7. round summary 和离线 audit 完成后，下一轮才允许开始。

## 4. L2 acceptance

`EvolutionConfig.acceptance_mode` 支持 `predicted`、`empirical`、`jev`。

### predicted + val（默认）

`PredictedSkillScorer` 使用冻结的 val route。对同一 Skill family 的旧 Skill 和每个候选 Skill，逐 task 请求独立的 `l2_reviewer` 模型。prompt 只包含 task 与 Skill，要求预测“一次自主尝试是否成功”，不提供经验卡、执行轨迹、答案或重试假设。程序缓存每个 `(panel, task, Skill body, protocol)` 结果，并以候选平均 `probability_true` 严格高于旧 Skill 为接受条件。

这条路径不执行 benchmark 环境，`acceptance.executions` 必须为 0；LLM 请求数记录为 `predicted_requests`。

### predicted + train_cards

这是兼容路径。`CardReviewer` 每次只读取一张 train 经验卡、当前 Skill 和匿名候选。reviewer 输出每个候选的 `old_outcome` 与 `new_outcome`，程序推导相对 effect 并聚合选择。它是预测性验收，不是实测成功率。

### empirical / jev

两者使用同一套冻结 val route 和 paired task IDs。`empirical` 运行真实 executor；`jev` 调用 JEV endpoint。每个候选都与当前 head 在同一 task panel 上比较，严格提高才接受。

## 5. test 评测

显式 `--phase test` 时，selector 只根据初始 Skill description 固定 test task→Skill 路由，随后每道 test task 使用当前 Skill body 独立执行一次。test 不提供 train 卡、反思、答案或指导，也不写回 L2。每题结果、轨迹、route、score cache 和 `audit.json` 都落盘；路由失败保留在总体分母中。

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
- `skills.jsonl`：Skill 版本链；`test/<library-hash>/`：独立 test 评测。
