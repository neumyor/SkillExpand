# 运行、导入与恢复

从仓库根目录执行。先配置 `.env` 并运行：

```bash
bash scripts/setup_env.sh
source scripts/env.sh
```

SearchQA 需要 `--task-file`；ALFWorld 正式执行必须使用 `.env` 中的 `ALFWORLD_PYTHON`，并设置 `ALFWORLD_DATA`、`ALFWORLD_CONFIG`、`ALFWORLD_BENCH_SRC`。数据任务的 split JSON 必须使用 `train`、`val`、`test`：

```json
{"assignment":{"0":"train","1":"train","2":"val","3":"test"}}
```

## 从头运行

```bash
.venv/bin/python -m skillexpand \
  --benchmark searchqa \
  --task-file /absolute/path/tasks.json \
  --split-file /absolute/path/splits.json \
  --run-dir runs/searchqa-example \
  --phase cold-start \
  --cold-start-workers 8 \
  --family-discovery-workers 8
```

冷启动完成后运行两轮 Evolve。默认 predicted acceptance 在 val 上独立预测：

```bash
.venv/bin/python -m skillexpand \
  --benchmark searchqa \
  --run-dir runs/searchqa-example \
  --phase evolve --evolve-rounds 2 --resume \
  --acceptance-mode predicted
```

`--acceptance-mode empirical` 使用 val 真实执行。`--acceptance-mode sampled` 使用配对增量预测，并用随机 val 抽检修正（见 [协同进化实验计划](EXPERIMENT_PLAN_PLANNER_REVIEWER_COEVOLVE.md)）。验收协议冻结在运行目录中，因此 sampled 需新建运行目录并导入已完成的冷启动：

```bash
.venv/bin/python -m skillexpand \
  --benchmark searchqa --cold-start-dir runs/searchqa-example \
  --run-dir runs/searchqa-sampled \
  --phase evolve --evolve-rounds 2 --resume \
  --acceptance-mode sampled --skill-edit-mode structured \
  --candidate-count 1 --single-candidate \
  --acceptance-sample-size 16 --acceptance-confidence 0.9 \
  --claim-verification on \
  --planner-memory-mode aggregate --reviewer-memory-mode cases
```

`--claim-verification off`、`--planner-memory-mode off`、`--reviewer-memory-mode off` 分别关闭判定者、Planner 记忆与 Reviewer 记忆，用于单因素消融。`--acceptance-sample-size` 是按 family 自适应的上限，小于 2 时启动即被拒绝：panel 更小的 family 全量执行；panel 只有 1 题时无法给出置信区间，候选一律拒绝并记为 `insufficient_sample`；panel 为空时整批 hold。

structured Skill 编辑可在冷启动和 Evolve 中保持一致地启用：

```bash
--skill-edit-mode structured
```

各角色模型可单独指定，例如 `--l2-reviewer-model <model>`；其余角色见 README。

## 复用冷启动

```bash
.venv/bin/python -m skillexpand \
  --benchmark alfworld \
  --cold-start-dir /absolute/path/completed-alfworld-cold-start \
  --run-dir runs/alfworld-evolve \
  --phase evolve --evolve-rounds 2 --resume
```

`--cold-start-dir` 只接受当前协议生成的完整冷启动；旧版本经验卡和旧运行日志不导入。导入前可加 `--show-plan` 做只读检查。导入后 L2 的代码、模型、split、证据和 batch 设置会冻结到新目录。

## test 评测

CLI 的 `test` 阶段使用 `test` split：

```bash
.venv/bin/python -m skillexpand \
  --benchmark searchqa --run-dir runs/searchqa-example \
  --phase test --resume --test-workers 128
```

系统先用初始 Skill description 固定 `routes/test/`，再用当前 Skill body 对每个 test task 独立执行一次。结果写入 `test/<library-hash>/`，并由 `audit.json` 复算。test 不进入 L1/L2，也不回流到 Skill。

在同一组冻结路由上评测某一轮结束时的快照（`--round 0` 为冷启动库；需要先运行过 `--phase test` 以冻结路由）：

```bash
.venv/bin/python scripts/evaluate_snapshot.py \
  --run-dir runs/searchqa-example --round 1 \
  --output runs/searchqa-example/test-snapshots/round-1 --test-workers 128
```

`--smoke` 每个路由组只测一题且不做覆盖审计，用于启动前的全链路检查。

## 恢复与审计

每个 task、模型响应、经验卡、候选、acceptance 和 batch 事务都逐单元落盘；恢复只补缺失单元。更改 prompt、模型、split、配置或协议必须新建运行目录。

冻结身份（`manifest.json`、`l2_manifest.json`、`test/<hash>/protocol.json`）只包含方法输入，不含源码版本、端点 URL 或超时；因此换端点或改用 `--llm-relay` 续跑都可以直接 `--resume`。协议输入（模型名、候选数等）变化会报 `FrozenProtocolChanged`。

**注意**：L2 的 Planner/Editor/Reviewer prompt 与初始 Skill 合成 prompt 不在 manifest 中，改动它们属于协议变更，必须新建运行目录。

每轮 Evolve 完成后运行：

```bash
.venv/bin/python -m skillexpand.l2.audit \
  --run-dir runs/searchqa-example --round 1 \
  --output runs/searchqa-example/evolution/round-1/audit.json
```

审计检查 train 卡覆盖、checkpoint、卡片 hash、候选重放、acceptance scope、Skill 版本链和提交事务。predicted-val 审计不会重新调用模型；它读取批次中保存的 paired 预测。

## TerminalBench（远程 Harbor 执行）

TerminalBench 任务不在本进程内执行。`--benchmark terminalbench` 时，L1、val/test 单元都调用外部
Harbor/Tencent runner（`benchmarks/terminalbench.harbor_rollout`）：真实 rollout 发生在远程任务沙箱内，
worker 只消费它保存的 trajectory 与 verifier 结果。任务表来自 `--task-file`（默认
`data/terminalbench/tb21.json`，每行含 `task_name` 与 `instruction`）。

前置条件：

- `configs/benchmark/terminalbench.yaml` 中 `rollout.runner_script` 指向 TB2.1 的 Tencent 启动脚本；
- 远程机器若不能直连 provider，加 `--llm-relay`：全部 LLM 调用经一个常驻 Tencent E2B 中继沙箱转发
  （`runtime/llm_relay.py`；需安装 `.[tencent-relay]` extra 并设置 `E2B_API_KEY` 与
  `TBENCH_E2B_RELAY_TEMPLATE`）。中继不改写冻结 config，只设置环境变量并写信息性的
  `relay_manifest.json`；中继下 `GPTWrapper` 去掉模型名的 `openai/` 前缀，TB 沙箱的 `MODEL_API_BASE`
  由 `EXPE_LLM_RELAY_REQUIRED` 与 `TBENCH_RELAY_PROVIDER_BASE` 决定；
- TB 单元最长可运行 2 小时，worker 进度超时默认已放宽到 7500s。

成本提示：sampled 验收的每道抽检题都是一次真实沙箱执行（两臂 × 抽样题数，默认上限 16 题），在 TB 上
应按预算调小 `--acceptance-sample-size`。

Tencent provider 会偶发拒绝严格的 wire `response_format`（HTTP 400 / 400006）。predicted Reviewer 可用
`EXPE_REVIEWER_RESPONSE_FORMAT=omit` 省去该字段（prompt 与解析不变，协议哈希会区分两种模式）。

### TB-eval（progressive library，E3/E4）

`--progressive-library` 让 TerminalBench 的 L1 在运行时从 Skill 目录里选一个 Skill 再加载其 body，并把 predicted 验收的面板换成全部 train 题（闭集）。它要求冻结 config 的 `benchmark.progressive_library`（由 `scripts/import_terminalbench_batch.py` 写入）、`--acceptance-mode predicted`，且只用于 `terminalbench` + Harbor；与冻结 config 不一致会在启动时直接报错。每个阶段用 `scripts/tb_eval_stage.py prepare|launch --stage E3|E4` 在独立 run 目录中原地运行（launch 写死完整开关集）。selector 溯源写入 `evolution/round-N/selection/`，`audit_round` 会与 manifest 的 `routes` 交叉核对。relay 端口在 resume 时变化无需任何容忍代码（relay 不改写冻结 config，身份里没有端点）。闭集验收不能与 main 的 val 面板结果比较，详见 `docs/EXPERIMENT_PLAN_TB_EVAL.md`。

## campaign launcher

`python scripts/run_campaign.py prepare --root <campaign> --inputs <dir>` 冻结两个 benchmark 的输入、模型角色和并发参数，并把当前 `src/` 复制到 `<campaign>/code/src`，同时写入冻结启动器 `<campaign>/code/run_campaign.py`。之后的所有动作都用冻结启动器运行，确保只执行冻结代码（对已 prepare 的 campaign，`scripts/run_campaign.py` 也会把非 prepare 动作转交给冻结启动器）：

```bash
python <campaign>/code/run_campaign.py check --root <campaign>
python <campaign>/code/run_campaign.py start --root <campaign> --mode preflight
python scripts/check_fresh_campaign.py --root <campaign>   # 等价于 independent-check
python <campaign>/code/run_campaign.py start --root <campaign> --mode full
python <campaign>/code/run_campaign.py test --root <campaign> --benchmark searchqa
```

单候选以及 sampled 的五个抽样与记忆开关都在 prepare 时冻结并校验。`stage_args()` 把冻结参数传给每个 cold-start/evolve 阶段（sampled 开关只在 sampled 下传），避免恢复时意外切换验收口径。sampled campaign 的 `independent-check` 还会在 4 道 preflight 题上真实跑一次 sampled 验收（配对 Δ 预测、两臂执行、判定者、PPI 下界），并用离线审计重放，弥补 preflight 只有 1 道 val 题、跑不到接受路径的缺口。`check` 以 `code/` 与 `inputs/` 的摘要为准，源码仓库的 Git 漂移只在输出的 `source_drift` 中报告。

长任务应通过独立 session 启动，并使用 supervisor flock 和产物文件判断进度；不要用模糊进程名判断存活。启动前先做 1–2 个 task 的全链路 smoke、真实 LLM 健康请求和离线审计。
