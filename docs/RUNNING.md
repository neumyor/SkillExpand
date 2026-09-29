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
  --acceptance-mode predicted \
  --predicted-review-scope val
```

切换回基于 train 经验卡的 predicted Reviewer：

```bash
.venv/bin/python -m skillexpand \
  --benchmark searchqa --run-dir runs/searchqa-example \
  --phase evolve --evolve-rounds 2 --resume \
  --acceptance-mode predicted --predicted-review-scope train_cards
```

`--acceptance-mode empirical` 使用 val 真实执行；`--acceptance-mode jev` 使用 val 上的 JEV 预测。structured Skill 编辑可在冷启动和 Evolve 中保持一致地启用：

```bash
--skill-edit-mode structured
```

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

## 恢复与审计

每个 task、模型响应、经验卡、候选、acceptance 和 batch 事务都逐单元落盘；恢复只补缺失单元。更改代码、prompt、模型、split 或协议必须新建运行目录。

每轮 Evolve 完成后运行：

```bash
.venv/bin/python -m skillexpand.l2.audit \
  --run-dir runs/searchqa-example --round 1 \
  --output runs/searchqa-example/evolution/round-1/audit.json
```

审计检查 train 卡覆盖、checkpoint、卡片 hash、候选重放、acceptance scope、Skill 版本链和提交事务。predicted-val 审计不会重新调用模型；它读取批次中保存的 paired 预测。

## campaign launcher

`python scripts/run_campaign.py prepare` 会冻结两个 benchmark 的输入、模型角色和并发参数；默认 `predicted_review_scope` 为 `val`，也可以在 prepare 时显式传 `train_cards`。`stage_args()` 会把冻结的 scope 传给每个 cold-start/evolve 阶段，避免恢复时意外切换验收口径。

长任务应通过独立 session 启动，并使用 pidfile、job lock 和产物文件判断进度；不要用模糊进程名判断存活。启动前先做 1–2 个 task 的全链路 smoke、真实 LLM 健康请求和离线审计。
