# SkillExpand

SkillExpand 从任务执行轨迹中提取有证据的经验卡，归纳初始 Skill，再通过逐卡独立评审演化 Skill 规则。它维护自然语言 Skill 库，不训练模型权重。目前支持 SearchQA 和 ALFWorld；执行器与部分环境组件改编自 [ExpeL](https://github.com/LeapLabTHU/ExpeL)。

项目区分三种信息：**执行事实**由程序从动作和环境反馈记录；**经验 claim 与跨卡 pattern** 是模型提出、程序检查引用的候选知识；**Skill 改进**由 reviewer 预测，只有另行执行评测才能测得实际效果。程序验证引用存在，并不证明自然语言结论的因果正确性。

## 工作流程

```mermaid
flowchart LR
  A[固定 source / admission / final 划分] --> B[冷启动：source 无 Skill 执行]
  B --> C[逐任务经验卡]
  C --> D[能力家族与跨卡 pattern 候选]
  D --> E[初始 Skill Bank]
  E --> F[第 1 轮：带 Skill 执行 source]
  F --> G[本轮新卡 → L2 编辑与逐卡评审]
  G --> H[第 2 轮：用更新后的 Skill 再执行 source]
  H --> I[本轮新卡 → L2 编辑与逐卡评审]
  I --> J[显式启动 final：留出任务真实执行]
```

划分在执行前固定，默认比例为 50/25/25。source 用于建库和演化；admission 在当前协议中预留，但不参与 L2；final 只在显式调用时执行，结果不回流到 Skill 编辑。`--phase all` 包含冷启动和指定轮数的演化，**不包含 final**。

### 冷启动：从执行到初始 Skill

每个 source 任务先不带 Skill 执行。默认最多四次自主尝试；失败可触发反思、重置环境和重试，benchmark 有可用指导时还可做一次辅助尝试。反思中的计划是下一次尝试的假设，不直接写成经验。

任务结束后，程序从已完成尝试构造 schema-5 经验卡：任务与来源、各次尝试的结果、实际动作及观察证据、模型提出的 claim、claim 状态和审计 ID。成功和失败任务都写卡，零条 claim 合法。模型最多提出两条任务局部 claim，并引用证据 ID；程序逐条校验，保留合格项，对被拒项最多做一次定向修复。正向 procedure 必须有同一次成功尝试的方法证据，失败动作或单纯评分不能充当成功方法。原始输出、拒绝原因和修复结果留在检查点，便于恢复与复核。

单卡不声称包含跨任务事实。冷启动先依据卡片发现能力家族，并把每个 source 任务分到唯一家族；多卡批次再生成带不同卡片与证据 ID 的 pattern **候选**，用于合成初始 Skill 的 `description`（适用场景）和 `body`（行动规则）。初始 Skill 不表示已经通过留出集验证。

### 演化：带 Skill 的 L1 与串行 L2

每轮 Evolve 先用当前 Skill Bank 在同一批 source 任务上重新执行 L1，生成**本轮独立的经验卡**。任务到家族的映射沿用冷启动；L2 只读取本轮卡、当前 Skill 和该批次的 pattern 候选，不混用旧轮卡或 held-out 数据。

L2 按 Skill 和任务的固定顺序处理，每批最多 50 张卡。编辑器先检查现有规则是否覆盖证据，再提出至多 3 种有证据引用、机制不同的修改假设并生成候选 body；`description` 冻结。相同 body 去重后，独立 reviewer 每次只看**一张卡、当前 body 和全部匿名候选**，对每个候选给出 improve、regress、unchanged 或 unknown，并引用该卡证据及变化的规则 ID。程序检查覆盖、ID 和格式；单卡评审格式错误最多修正一次，仍无效则整批保留原 Skill。

对每个候选累计预测改善 `I`、回退 `R` 和未知 `U`。只有 `I-R-U > 0`，且胜出者下界严格超过其他候选上界 `I-R+U` 时，才提交新版本；否则保留当前版本。这是保守的**反事实选择规则**，不是统计置信区间，也不是实测成功率。当前协议冻结 description，暂停自动 L3 更新。

### Final：独立真实评测

显式启动 final 后，系统用**初始 Skill description** 固定留出任务路由，再用最新 body 对每题独立执行一次；不提供 source 卡、反思或指导。输出逐题轨迹、分 Skill 与总体成功率及审计结果。路由失败仍计入总体分母。曾用于诊断的 final 数据不能再当作全新确认集。

## 安装与配置

要求 Python 3.9–3.11。仓库根目录执行：

```bash
git clone git@github.com:neumyor/SkillExpand.git
cd SkillExpand
cp .env.example .env
# 填好 .env 中的模型连接信息和所需路径后继续。
bash scripts/setup_env.sh
source scripts/env.sh
```

安装脚本需要 `uv`，默认安装 `.[alfworld,dev]`。只运行 SearchQA 时，可在自己的虚拟环境中执行 `pip install -e '.[dev]'`。运行前先复制 [`.env.example`](.env.example) 为 `.env`，填写 `EXPE_LLM_BASE_URL`（OpenAI 兼容服务地址）、`EXPE_LLM_MODEL`（服务中的模型 ID）和 `OPENAI_API_KEY`，再执行 `source scripts/env.sh`。本地 `.env` 被 Git 忽略；不要将真实凭据写入 example、命令行或提交的运行产物。

ALFWorld 还需在 `.env` 中填写 `ALFWORLD_PYTHON`（含模拟器依赖的解释器）、`ALFWORLD_DATA`（数据目录）、`ALFWORLD_CONFIG`（TextWorld 配置文件）与 `ALFWORLD_BENCH_SRC`（评测环境源码目录）。只用实验专用的 `scripts/run_campaign.py` 时，还需提供 `EXPE_CAMPAIGN_OVERLAY`（额外依赖目录）。该脚本从环境读取凭据，不把 API key 写入冻结 manifest；任务、划分和本地运行产物也不随仓库分发。

SearchQA 从 `--task-file` 指定的 JSON/JSONL 读取 question、answer(s) 和 context，在提供的 context 内本地检索。ALFWorld 数据可用 `.venv/bin/python scripts/prepare_data.py alfworld` 准备；正式评测必须使用装有 `alfworld`/`textworld` 的环境。详见 [Benchmark 配置](docs/BENCHMARKS.md)。

## 运行示例

以下以 SearchQA 为例。任务文件必须覆盖本次划分；显式 split JSON 例如 `{"assignment":{"0":"source","1":"source","2":"admission","3":"final"}}`。省略 `--split-file` 则使用固定 seed 的默认划分。

```bash
# 1. 冷启动：完成无 Skill 执行、经验卡和初始 Skill Bank。
.venv/bin/python -m skillexpand \
  --benchmark searchqa --task-file /absolute/path/tasks.json \
  --split-file /absolute/path/splits.json \
  --run-dir runs/searchqa-example --phase cold-start \
  --workers 8 --discovery-workers 8

# 2. 以同一目录续跑两轮 Skill-aware L1 → L2。
.venv/bin/python -m skillexpand \
  --benchmark searchqa --run-dir runs/searchqa-example \
  --phase evolve --evolve-rounds 2 --resume

# 3. 演化完成并通过审计后，单独执行 final。
.venv/bin/python -m skillexpand \
  --benchmark searchqa --run-dir runs/searchqa-example \
  --phase final --resume
```

`--evolve-rounds` 是累计目标轮数：完成一轮后用 `--resume --evolve-rounds 2` 可续跑第二轮。`--phase l2` 与 `evolve` 等价，都会先运行带 Skill 的 L1。`--cold-start-dir` 可以把**本协议**已完成的冷启动导入新 run 目录；不支持旧卡或旧运行日志。`--show-plan` 可只读检查任务划分。完整参数和恢复语义见 [运行指南](docs/RUNNING.md)。

运行目录逐单元保存 L1 检查点、经验卡和模型原文；`evolution/round-*/` 隔离各轮数据，`l2_patterns/`、`l2_proposals/`、`l2_batches/` 保存候选与逐卡评审，`skills.jsonl` 保存版本链，`final/<library-hash>/` 保存实测结果。每轮和 final 都有完整性审计。恢复复用已落盘的有效响应；改变协议、代码或输入应使用新目录。

## 验证与研究边界

```bash
bash scripts/run_tests.sh
```

离线测试检查卡片引用、家族归属、批次证据、候选评审、恢复和审计等实现行为；脚本模型测试不能证明真实模型提取或 Skill 效果。启动昂贵实验前，应先以 1–2 个任务做端到端干跑，独立检查划分与指标、真实请求验证服务、确认逐单元落盘及恢复，并在读取结果前完成产物审计。不要把 reviewer 通过数当成实测提升。

更多设计和边界见 [架构](docs/ARCHITECTURE.md)、[L1 与经验卡](docs/BENCHMARKS.md)、[执行审计](docs/EVOLUTION_EXECUTION_AUDIT.md) 和 [L2 评审审计](docs/L2_CARD_REVIEW_AUDIT.md)。项目沿用上游 [Apache 2.0 许可](LICENSE)，具体来源见 [NOTICE](NOTICE)。
