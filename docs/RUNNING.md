# 运行、导入与恢复

命令从仓库根目录执行。先从 `.env.example` 创建被 Git 忽略的 `.env`，填写模型连接信息及所需外部路径，然后运行 `bash scripts/setup_env.sh` 安装 `skillexpand` 包。
只使用 SearchQA 时可在自己的虚拟环境中 `pip install -e ".[dev]"`；ALFWorld 需额外安装 `.[alfworld]`。
数据不随 Git 分发。`python scripts/prepare_data.py alfworld` 从固定上游版本准备 ALFWorld 数据，
不再依赖本仓库历史。SearchQA 用 `--task-file` 提供包含 question、answer(s)、context 的任务文件。
环境按 `scripts/env.sh` 和 benchmark 文档配置。

```bash
source scripts/env.sh
```

## 直接复用已完成的冷启动

先只读检查，不调用模型、不创建输出目录：

```bash
$ALFWORLD_PYTHON -m skillexpand \
  --benchmark alfworld \
  --cold-start-dir /absolute/path/completed-alfworld-cold-start \
  --run-dir runs/alfworld-evolve --phase evolve --show-plan
```

新建 L2 输出目录并导入：

```bash
$ALFWORLD_PYTHON -m skillexpand \
  --benchmark alfworld \
  --cold-start-dir /absolute/path/completed-alfworld-cold-start \
  --run-dir runs/alfworld-evolve --phase evolve --evolve-rounds 2

.venv/bin/python -m skillexpand \
  --benchmark searchqa \
  --cold-start-dir /absolute/path/completed-searchqa-cold-start \
  --run-dir runs/searchqa-evolve --phase evolve --evolve-rounds 2
```

以上为运行示例，不表示已经启动真实实验。ALFWorld 执行必须用 `.env` 中的
`ALFWORLD_PYTHON`，且该环境还需要安装本仓库执行链依赖；
运行前检查 `ALFWORLD_DATA`、`ALFWORLD_CONFIG`、`ALFWORLD_BENCH_SRC`。
若仓库本地 venv 不满足这些条件，不要用它替代正式 ALFWorld 评测环境。

默认使用导入配置中冻结的模型和任务文件，环境变量配置 endpoint/key。
修改后的实现不要求与历史冷启动代码相同，但新的 L2 一旦开始，代码、模型配置、
证据和批次设置必须保持一致。改变实现或实验协议请导入到新目录。

恢复只需指定新输出目录，不必再次提供原目录：

```bash
.venv/bin/python -m skillexpand \
  --benchmark searchqa --run-dir runs/searchqa-evolve --phase evolve --resume \
  --evolve-rounds 2 --batch-size 50 --candidate-count 3
```

`--phase evolve` 执行一次完整的 Skill-aware 闭环：当前 Skill 注入 source L1，
生成本轮经验卡，再由 L2 基于本轮卡更新 Skill Bank。用 `--evolve-rounds N`
连续执行 N 轮；每轮只读取本轮新卡，旧卡保留在 `evolution/round-*` 供审计。
任务到 family 的路由沿用冷启动映射，admission/final 不会生成演化经验卡。
`--phase l2` 与 `evolve` 等价，均先运行带 Skill 的 L1，再运行 L2。
`--evolve-rounds N` 是累计目标轮数；已完成一轮后可用 `--resume --evolve-rounds 2`
续跑第二轮。恢复不允许缩短到已开始轮次之前。

每个 round 完成后先做离线完整性审计，再使用结果：

```bash
.venv/bin/python -m skillexpand.l2.audit \
  --run-dir runs/searchqa-evolve --round 1 \
  --output runs/searchqa-evolve/evolution/round-1/audit.json
```

该审计核对 L1 原始检查点与卡片、上一轮输出与本轮输入、任务覆盖及卡片 hash，
并从原始评审重算候选选择、检查 Skill 版本链；它不把 reviewer 预测当成实测成功率。
已有异步演化产物的目录必须导入新目录，不能混合协议。

## 从头运行

先冻结划分。显式 JSON 示例：

```json
{"assignment":{"0":"source","1":"source","2":"admission","3":"final"}}
```

```bash
.venv/bin/python -m skillexpand \
  --benchmark searchqa --task-file /absolute/path/tasks.json \
  --split-file /absolute/path/splits.json --run-dir runs/new-searchqa \
  --phase cold-start --cold-start-workers 8 --family-discovery-workers 8
```

未提供划分时，使用 seed=42 的全局 50/25/25。官方数据来源划分须显式提供。
默认 phase 是 cold-start；`all` 串行运行完整冷启动和 L2，不自动运行 final。
已完成冷启动不会因进入 L2 而重新执行、重聚类或重写初始 Skill。

## 参数变化

- `--batch-size`：每批 L2 的 source 卡数，默认 50；小组和尾批也执行。
- `--candidate-count`：每批独立候选数量，默认 3；先生成不同修改假设，当前批次卡上独立评审，证据不足或无法分出优劣则保留原版。
- `--skill-edit-mode`：`rewrite`（默认）保留完整正文候选；`structured` 将 Skill 分为 Procedure、Conditions、Completion checks，候选只提交一条新增或替换操作。程序保存其他规则及其稳定 ID。可在冷启动时启用，也可在导入纯文本冷启动后启用；L2 开始后续跑须使用同一值。正式 campaign 在 `prepare` 时传入此参数并冻结到 manifest。
- `--cold-start-workers`：冷启动 source 任务 L1 的并发数，默认 8。
- `--family-discovery-workers`：能力标签提取与 family 指派请求的并发数，默认 8。
- `--evolve-l1-workers`：每轮 Skill-aware source L1 的并发数，默认 8。
- `--l2-review-workers`：单个 L2 batch 内逐卡 reviewer 请求的并发数，默认 8；
  batch 与 round 仍按顺序处理。
- `--final-workers`：final 路由与执行的并发数，默认 4。
- 以上并发参数只影响新启动的相应阶段；导入已完成冷启动不会改变其冻结的 L1 预算。

移除了在线阈值/新增经验重试/池容量、`--l2-jobs`、`--wave-size`、`--max-waves`、
`--panel-size`。L2 不使用 admission；只评审当前批次经验卡。

## Final

```bash
.venv/bin/python -m skillexpand \
  --benchmark searchqa --run-dir runs/searchqa-evolve --phase final --resume \
  --final-workers 4
```

使用初始 description 冻结 final 路由，使用最终 body 执行。
结果在 `final/<library_hash>/summary.json`，同时报告每 Skill 和整体成功率。
路由失败计入整体分母，无题分组的成功率为 null。
程序在 final 完成后自动复算审计，写入同目录的 `audit.json`。
存在未完成的 evolve 时不能运行 final。网络错误保留错误记录、停止出分，恢复只重跑缺失结果。

## 超时与恢复边界

- 模型请求默认 300 秒超时、最多额外重试 2 次；配置为 `EXPE_LLM_TIMEOUT_SECONDS` 和 `EXPE_LLM_RETRIES`。客户端内部重试关闭，避免重试次数相乘。
- ALFWorld reset/step/close 默认 120 秒，配置为 `EXPE_ENV_TIMEOUT_SECONDS`。
- ALFWorld 始终使用独立进程。连续 3600 秒没有任何单元完成时，进程池退出并保留已有检查点；配置为 `EXPE_WORKER_TIMEOUT_SECONDS`。这是进度超时，不是每题总时限。
- 中断的 episode 保留记录，恢复时从该 episode 的环境初态重跑，不恢复到某个动作；它不消耗已完成自主尝试的预算。
- 总结请求失败时保留完整轨迹，恢复仅补总结。已有总结响应即使格式无效也不重新抽样。
- 已完成任务、候选、逐卡评审和提交事务复用落盘结果。未知请求 token 不推算，成本审计会标记不完整。

这些参数属于冻结协议；开始后改变参数须新建实验目录。审计范围及局限见
[执行链审计](EVOLUTION_EXECUTION_AUDIT.md)。

## 验证与长任务

```bash
bash scripts/run_tests.sh
```

离线测试覆盖冷启动、真实本地 SearchQA 单次执行、串行 L2、固定路由隔离、导入、
评分缓存、provider 错误、批次/策略事务恢复及 final 分母。脚本模型测试不代表真实 LLM 效果。

长实验前必须完成工作区 AGENTS.md 的多次独立检查、真实生成健康请求、1–2 单元全链路、
预注册指标及恢复审计。长任务使用独立进程会话并验证 PID，不用模糊进程名判活：

```bash
.venv/bin/python scripts/detach.py \
  --pidfile runs/launch/l2.pid --log runs/launch/l2.log -- \
  .venv/bin/python -u -m skillexpand \
  --benchmark searchqa --run-dir runs/searchqa-evolve --phase evolve \
  --evolve-rounds 2 --resume
```

当前协议为 `serial-card-id-review-v6-relative-outcomes`。Reviewer 对每个候选分别输出
`old_outcome` 与 `new_outcome`，程序再推导 `improve/regress/unchanged/unknown`；从本协议的全新运行目录开始。
每批文件中的 `review_approved` 为预测评审通过，不能报告为实测提升。
L3 与 description 已冻结，旧拒绝缓冲和 L3 阈值参数已移除。
完整假设、候选、diff、逐卡证据和评审原始响应分别保存在 `l2_proposals/` 与 `l2_batches/`。
每次评审只处理一张卡和所有候选，使用证据/规则 ID 引用。每卡独立缓存，恢复只继续尚未完成的请求。
任何卡在一次格式修正后仍非法则整批 hold；合法评审由程序汇总，不让模型计算总数。

## 包结构迁移

新入口为 `python -m skillexpand` 或安装后的 `skillexpand` 命令。
本次 fresh start 不支持旧版卡片或运行日志；恢复仅复用当前协议的执行缓存。
完整的运行时代码、prompt 和环境适配器均纳入新代码指纹。
原始实验 task file 和审计引用路径需要继续可访问；数据准备脚本不会重建自定义划分。
