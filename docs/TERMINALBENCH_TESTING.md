# TerminalBench 远程测试指南

本文是在远程服务器上跑 TerminalBench（TB2.1）测试的实操手册，依据一次真实跑通的 smoke
（2 个任务 `fix-git` + `dna-insert`，所有角色都用 `qwen3.6-plus-distill`，2026-10-09，
节点 node29）写成。命令都可以直接复制，`<尖括号>` 是必须替换的占位符；文中只出现环境变量名，
不出现任何密钥值。

实验规格（开关、阶段、数据流、与 main 不可比的原因）见
[EXPERIMENT_PLAN_TB_EVAL.md](EXPERIMENT_PLAN_TB_EVAL.md)；本文只讲怎么跑、怎么看、怎么排错。

## 1. “TB 测试”在这里指什么

TerminalBench 的任务不在 SkillExpand 进程内执行。每一次 rollout 都是：Harbor 在 Tencent E2B
任务沙箱里起一个 Terminus agent，agent 在沙箱里敲命令，最后由任务自带的 verifier 打分。
SkillExpand 只负责提交任务、回收 trajectory 与 verifier 结果，并在宿主机上做 Skill 的演化。

```
 宿主机（node29）                               Tencent E2B（远端）
┌──────────────────────────────────┐
│ SkillExpand                      │
│  L1 / selector / L2 Planner      │   提交任务    ┌──────────────────────────┐
│  Editor / Reviewer / M0 提议     │ ───────────▶ │ 任务沙箱（每个 trial 一个）│
│        │                         │  (Harbor +   │  Terminus agent 敲命令    │
│        │ 方法侧 LLM 请求          │  runner 脚本) │  verifier 打分            │
│        ▼                         │ ◀─────────── │  agent 的 LLM 请求直连    │──┐
│  127.0.0.1:<port>  (LLM relay)   │ trajectory + │  provider (MODEL_API_BASE)│  │
│        │                         │ verifier 结果 └──────────────────────────┘  │
└────────┼─────────────────────────┘                                             │
         │ E2B API                    ┌──────────────────────────┐               │
         └──────────────────────────▶ │ 常驻中继沙箱：在沙箱内    │               │
                                      │ 向 provider 发 HTTP 请求  │ ──┐           │
                                      └──────────────────────────┘   ▼           ▼
                                                              LLM provider (<MODEL>)
```

要点：

- **宿主机不能直连 provider**，所以宿主机上的所有方法侧 LLM 调用（Planner、Editor、Reviewer、
  selector、M0 提议）都经 `--llm-relay` 的常驻 E2B 中继沙箱转发；任务沙箱内的 Terminus 则直接请求
  provider（`TBENCH_RELAY_PROVIDER_BASE`，由 `EXPE_LLM_RELAY_REQUIRED` 触发改写 `MODEL_API_BASE`）。
- **执行 agent 的模型与其他角色一样取自冻结的角色表**（`l1_executor`，由 `--l1-model`，即 launch 的
  `--executor-model` 设置）。`harbor_rollout` 用它设置 runner 的 `MODEL_NAME`（去掉 `openai/` 前缀，runner
  自己会加），不继承外部环境——runner 在 `MODEL_NAME` 缺省时会静默回落到自己的默认模型。唯一的例外是
  步骤 2 的 E1：它直接调用 runner 脚本，不经过 SkillExpand，所以要在命令里显式给 `MODEL_NAME`。
- 一个任务的多次尝试在一次 runner 调用内**顺序**执行（`TBENCH_N_CONCURRENT=1`）；并行发生在任务之间
  （`TB21_WORKERS`）。所以墙钟时间由最慢的任务决定。
- Harbor 数据集、runner 脚本、适配器只读地来自 `/data2/liyishan/...`，**不要写 `/data2`**。

## 2. 前置条件与一次性准备

### 2.1 目录布局

```
~/<WORKSPACE>/                      # smoke 用的是 ~/niuyiming/SkillExpand-tbeval
├── repo/        # 本仓库（TB-eval 分支）的拷贝；远程不需要 .git
├── .venv/       # python 3.10 虚拟环境，装 skillexpand
├── env.sh       # 环境变量（含对密钥文件的引用，本身不含密钥；chmod 600）
├── data/tb2.json            # 任务表
├── work/                    # 全部产物：e1/ run/ retry_archives/ ...
└── logs/                    # 启动脚本的 stdout 与 pidfile
```

### 2.2 同步代码并安装

```bash
# 本机 -> 远程（排除虚拟环境与运行产物；也可以用 git clone 后 checkout TB-eval）
rsync -a --delete --exclude .venv --exclude .git --exclude runs --exclude '.uv-*' \
  <LOCAL_REPO>/ <USER>@<HOST>:~/<WORKSPACE>/repo/

# 远程
python3 -m venv ~/<WORKSPACE>/.venv
cd ~/<WORKSPACE>/repo
~/<WORKSPACE>/.venv/bin/pip install -e '.[tencent-relay,dev]' \
  -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple   # 直连 PyPI 不通时
```

`tencent-relay` extra 提供 `e2b`，`dev` 提供 pytest 与 ruff。smoke 环境是 Python 3.10。

### 2.3 Harbor 与密钥文件

宿主机还需要一个可执行的 Harbor 环境（`harbor` 可执行文件与带 Harbor 依赖的 python）。smoke 用的是
`~/agentbrew-tb21/.venv`（xinhaidong 自己的）；`/data2/liyishan/.../.venv/bin/python` 是指向
`/home/liyishan` 的符号链接，其他用户**不可执行**，不要用默认值。

另需一个 `.env`（smoke 用 `~/agentbrew-tb21/.env`），其中定义：

| 变量名 | 用途 |
|---|---|
| `E2B_API_KEY` | Tencent E2B（任务沙箱与中继沙箱） |
| `E2B_DOMAIN` | E2B 域名（runner 默认 `ap-beijing.tencentags.com`） |
| `TBENCH_LLM_KEY` | LLM provider 的 key；下面映射为 `OPENAI_API_KEY` |

### 2.4 env.sh 模板

```bash
# ~/<WORKSPACE>/env.sh —— 用 `. ./env.sh` 引入；不要 cat/echo 它展开后的环境。
_R=$HOME/<WORKSPACE>
set -a
. "<PATH_TO_DOTENV>"                          # 定义 E2B_API_KEY / E2B_DOMAIN / TBENCH_LLM_KEY
set +a
export OPENAI_API_KEY="$TBENCH_LLM_KEY"       # relay 与 Harbor 读这个名字
export TBENCH_E2B_RELAY_TEMPLATE=<RELAY_TEMPLATE>   # smoke: code-agent-cfs2-new
export TBENCH_TENCENT_ENV_FILE=/nonexistent/.env    # 让 runner 不再去加载另一份 .env
export RETRY_ARCHIVE_ROOT=$_R/work/retry_archives   # 默认会写 /data2，改到自己的目录
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=$_R/repo/src
export TBENCH_PERSIST_SANDBOXES=0             # 跑完回收沙箱，别留在共享配额里
export PYTHON=$_R/.venv/bin/python
export HARBOR_BIN=<YOUR_HARBOR_VENV>/bin/harbor
export PYTHON_BIN=<YOUR_HARBOR_VENV>/bin/python
```

### 2.5 任务表 `data/tb2.json`

格式：JSON 数组，每行 `{"task_name": ..., "instruction": ...}`；`instruction` 取数据集中
`<DATASET_PATH>/<task>/instruction.md` 去掉首尾空白后的全文。

```python
import json, pathlib
D = pathlib.Path("/data2/liyishan/tbench2-openclaw-min/sources/terminal-bench-2-1-modelbest/tasks")
names = ["fix-git", "dna-insert"]          # 全量：sorted(p.name for p in D.iterdir() if p.is_dir())
rows = [{"task_name": n, "instruction": (D / n / "instruction.md").read_text().strip()} for n in names]
pathlib.Path("data/tb2.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
```

task id 就是任务在该文件里的**行号**（运行时 `load_tasks` 与导入脚本都按文件顺序编号）。冻结后
**不能改动这个文件**（增删、改序、改 instruction 都会触发 `Cold-start task data changed`）。
旧版导入脚本会先按 `task_name` 排序，导致未排序的文件在启动时报这个错；现已改为按文件顺序编号
（提交 `fix: sort the TerminalBench task table on import`），已排序的文件结果不变，重复的
`task_name` 会被直接拒绝。要和旧 run 保持一致就继续用排序后的文件。

## 3. 逐步流程

下面所有命令都在远程执行，先 `cd ~/<WORKSPACE> && . ./env.sh && cd repo`。
`<RUN>` 记作 `../work/run`。**每一步都先在 1–2 个任务上通过，再放大**（AGENTS.md 第一节）。

### 步骤 0：离线测试

```bash
bash scripts/run_tests.sh          # 需要 $PYTHON 指向 venv（env.sh 已设置）
$PYTHON -m ruff check src tests
```

不依赖网络与沙箱，全部通过才继续。

### 步骤 1：健康检查（必须发真实生成请求）

只看进程或 `/v1/models` 不能证明可用。这个脚本启动中继沙箱、发两次真实 chat 请求并关闭中继：

```bash
cat > ../work/health.py <<'EOF'
import json, time, urllib.request
from skillexpand.runtime.llm_relay import relay_from_env
relay = relay_from_env()
try:
    base = relay.start()
    print("relay sandbox:", relay.transport.sandbox_id, flush=True)
    for extra in ({}, {"chat_template_kwargs": {"enable_thinking": False}, "enable_thinking": False}):
        body = {"model": "<MODEL>", "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
                "max_tokens": 64, "temperature": 0, **extra}
        t = time.time()
        req = urllib.request.Request(base + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                msg = json.loads(r.read())["choices"][0]["message"]
            print(list(extra), "%.1fs" % (time.time() - t), repr((msg.get("content") or "")[:80]), flush=True)
        except Exception as e:
            print(list(extra), "FAILED", type(e).__name__, getattr(e, "read", lambda: b"")()[:300], flush=True)
finally:
    relay.close()
EOF
$PYTHON ../work/health.py
```

通过标准：两次请求都在秒级返回非空内容。不通过就不要往下走。

### 步骤 2：E1 冷启动 rollouts（先 dry run，再正式）

E1 是“无 Skill 的 bare rollouts”，产出后续导入用的原始 Harbor trial 目录。

```bash
export MODEL_NAME=<MODEL> \
       TBENCH_RUN_MODE=selected TBENCH_TASK_NAMES="<TASK_A> <TASK_B>" \
       TBENCH_EVAL_MODE=bare TBENCH_N_ATTEMPTS=3 TBENCH_N_CONCURRENT=6 \
       TBENCH_MAX_TRIAL_RETRIES=0 RUN_ID=e1_cold \
       JOBS_DIR=$HOME/<WORKSPACE>/work/e1/jobs
RUNNER=/data2/liyishan/tb21-tencent-skill/scripts/run_tencent_tb21_smoke.sh

DRY_RUN=1 timeout 280 bash $RUNNER            # 只检查配置与路径，不起沙箱

# 正式运行：脱离本 shell 的进程组，记录子进程 pid（不要用 nohup+disown，见第 6 节）
DRY_RUN=0 $PYTHON - <<'EOF'
import os, subprocess
p = subprocess.Popen(["bash", "/data2/liyishan/tb21-tencent-skill/scripts/run_tencent_tb21_smoke.sh"],
                     env=os.environ.copy(), stdout=open("../logs/e1.log", "ab"),
                     stderr=subprocess.STDOUT, start_new_session=True)
open("../logs/e1.pid", "w").write(f"{p.pid}\n")
EOF
kill -0 "$(cat ../logs/e1.pid)" && echo alive      # 从新 shell 再确认一次
```

完成标志：`logs/e1.pid` 的进程已退出，`logs/e1.log` 末尾有 Harbor 的结果表（`Errors 0`），且
`work/e1/jobs/e1_cold/result.json` 存在。
smoke 结果：6 个 trial，0 error，fix-git 3/3，dna-insert 1/3（平均 0.667）。

可选的输入预检（全量 89×3 时推荐）：
`$PYTHON scripts/validate_tb21_rollouts.py --source-root ../work/e1/jobs/e1_cold --task-file ../data/tb2.json --out <OUT_JSON> --attempts 3`。

### 步骤 3：prepare / 导入

`tb_eval_stage.py prepare` 先核对“任务数 × 尝试数”的 raw 覆盖（基础设施异常只标注，不会转成
reward=0），不全则写 `blocked_before_rollout` 并以退出码 2 结束；完整才调用
`import_terminalbench_batch.py` 冻结冷启动。

```bash
$PYTHON scripts/tb_eval_stage.py prepare --stage E3 \
  --source-root ../work/e1/jobs/e1_cold --task-file ../data/tb2.json \
  --run-dir ../work/run \
  --source-model openai/<MODEL> --method-model <MODEL> \
  --expected-tasks 2 --attempts 3
```

- `--expected-tasks`/`--attempts` 默认 89/3，小规模必须显式给；
- `--source-model` 是 rollouts 的模型名，带 `openai/` 前缀（来自 Harbor result 里的 `model_name`）；
- 结果：`<RUN>/batch_import_summary.json`（smoke：2 任务、6 trial、4 个 reward=1）、`input_coverage.json`。

### 步骤 4：M0 —— 模型生成的初始 Skill 库

**必须在 launch 之前完成。** 导入脚本只放了一个手写的 bootstrap Skill，演化它不是要做的实验。
`launch` 在 `library_manifest.json` 不存在、或 `initial_skills.json` 不是它列出的那组 Skill 时拒绝启动
（退出码 2，`status.json` 写 `blocked_before_rollout` 与原因）。

```bash
$PYTHON scripts/propose_terminalbench_library.py --run-dir ../work/run \
  --model <MODEL> --llm-relay --expected-cards 2 --request-timeout 300
$PYTHON scripts/materialize_terminalbench_library.py --run-dir ../work/run \
  --proposal ../work/run/library_proposal/library_proposals.json
```

- `--expected-cards` 必须等于任务数（全量 89）；
- 先人工看一眼 `library_proposal/library_proposals.json`：skill 的 `description` 应只描述能力，不含任务名；
- 约 1–2 分钟；smoke 产出 2 个 Skill（`family-p001`、`family-p002`），`library_manifest.json` 里
  `task_assignment` 为 `None`（路由留给运行时 selector）。

### 步骤 5：launch

```bash
TB21_WORKERS=4 $PYTHON scripts/tb_eval_stage.py launch --stage E3 \
  --run-dir ../work/run --method-model <MODEL> --executor-model <MODEL> --python $PYTHON
```

launch 写死整套协议开关（`--progressive-library --acceptance-mode predicted --skill-edit-mode rewrite
--evolve-rounds 1 --candidate-count 3 --batch-size 50 --autonomous-attempts 3 --supervised-attempts 0
--llm-relay`），用 `Popen(start_new_session=True)` 脱离调用者进程组，把子进程 pid 写进
`<RUN>/stage.pid`，日志写 `<RUN>/stage.log`。`TB21_WORKERS` 默认 100，共享配额下小规模先用 4。

### 步骤 6：监控（新 shell，不要用 `pgrep -f`）

```bash
cd ~/<WORKSPACE>/work/run
kill -0 "$(cat stage.pid)" 2>/dev/null && echo alive || echo exited   # 从新 shell 验证
cat run.pid                      # 训练器持有的 flock，内容是同一个 pid
tail -n 20 stage.log
ls evolution/round-1/cards/ | wc -l                    # L1 完成的任务数
ls evolution/round-1/harbor/jobs/*/*/result.json | wc -l   # 已完成的 Harbor trial 数
ls l2_batches/ ; cat summary.json 2>/dev/null          # L2 批次与完成标志
```

- **完成判据：`summary.json` 存在（`"status": "complete"`）且 `stage.pid` 的进程已退出。**
  `status.json` 在 launch 时写成 `running`，之后**不会**自动改为完成，不能用它判完成。
- 第二个进程对同一 run 目录会因 `run.pid` 的 flock 直接抛 `RunLocked`；这是保护，不是故障。

### 步骤 7：审计

训练器已经写了 `evolution/round-1/audit.json`。再做两项独立复核（不发模型请求）：

```bash
$PYTHON -m skillexpand.l2.audit --run-dir ../work/run --round 1 \
  --output ../work/run/evolution/round-1/audit_offline.json          # 期望 integrity: passed

$PYTHON - <<'EOF'
import glob, json
from skillexpand import schema as S
from skillexpand.benchmarks.terminalbench import audit_harbor_experience
R = "../work/run"
for label, pat in (("cold", R + "/discovery/results/*.json"), ("round1", R + "/evolution/round-1/cards/*.json")):
    for f in sorted(glob.glob(pat)):
        exp = S.from_dict(S.TaskExperience, json.load(open(f)))
        print(label, f.rsplit("/", 1)[-1], audit_harbor_experience(exp), exp.selected_skill_id, exp.trial_rewards)
EOF
```

还要核对：每张卡都有 `selected_skill_id`；`evolution/round-1/selection/<task>.json` 与 manifest 的
`routes` 一致（offline audit 会做）；`routes/train/` 与 `l2_batches/` 数量与任务/批次数相符。
带任何问题的 run 作废归档，不进入结果表。

### 步骤 8：收集结果

结果都在 `<RUN>` 下：

- Skill 的演化：`summary.json` 的 `skills`（`...@v0` 未改动，`...@v1` 是被接受的更新）、`l2_batches/*.json`
  的 `outcome`（`review_approved` / `hold`）；
- 每次 rollout 的真实 reward：`evolution/round-1/harbor/jobs/*/<task>__*/result.json` 里
  `verifier_result.rewards.reward`；token 用量取其 `agent_result.n_input_tokens/n_output_tokens`；
- 把整个 `<RUN>` 与 `work/e1` 拷回本机留档（smoke 分别约 8 MB / 4 MB）。

## 4. 产物目录说明

| 路径（`<RUN>` 下） | 内容 |
|---|---|
| `config.json` `split.json` `manifest.json` `initial_skills.json` `skills.jsonl` | 导入时冻结的方法输入；改动即 `FrozenProtocolChanged`，只能换新 run 目录 |
| `discovery/results/<i>.json` | 冷启动经验卡（每任务一张，含 3 次尝试的 reward 与轨迹路径） |
| `trial_manifest.jsonl` | 每个原始 trial 的 task、attempt、reward、轨迹路径 |
| `input_coverage.json` `batch_import_summary.json` | 输入覆盖与导入汇总 |
| `library_proposal/` `library_manifest.json` | M0 提议与冻结后的 v0 库 |
| `evolution/round-1/harbor/jobs/skillexpand_r1_task<i>_<pid>/` | 该轮 L1 的 Harbor trial（`result.json`、`agent/trajectory.json`、verifier 输出） |
| `evolution/round-1/selection/<i>.json` | selector 的全部证据（选了哪个 Skill、raw 输出） |
| `evolution/round-1/cards/<i>.json` | 带 Skill 的 L1 新经验卡 |
| `evolution/round-1/{manifest,batches,summary,audit,audit_offline}.json` | 本轮冻结身份、分批、汇总、审计 |
| `l2_proposals/` `l2_batches/` `l2_patterns/` | Planner 假设与候选、每批的验收记录 |
| `routes/train/` `train/predicted_scores.jsonl` | 闭集验收面板的路由与 predicted Reviewer 的逐题打分 |
| `usage/` | 各角色请求记录；token 字段为 0（provider 不返回 usage），请求数可信 |
| `stage.pid` `run.pid` `stage.log` `status.json` `summary.json` | 启动器与训练器的状态文件 |
| `relay_manifest.json` | 本次中继的端口与沙箱 id（仅信息性，不进冻结身份） |

## 5. 如何读结果

- **先看审计，再看数字**。审计没通过的 run 不使用。
- **闭集验收不是提升证据**：progressive 模式的验收面板就是用来演化 Skill 的同一批 train 题，且判官是
  predicted Reviewer（不执行环境）。`review_approved` 只说明“Reviewer 预测候选更好”，
  `val_executions=0` 也说明没有任何真实验证。不得把它报成成功率提升，也不得与 main 的 val/test
  结果并列。
- smoke 读数（仅用于确认链路通畅，样本量 2 任务，不能下任何方法结论）：

  | 指标 | 值 |
  |---|---|
  | E1 冷 rollouts | fix-git 3/3，dna-insert 1/3 |
  | E3 L1 rollouts（带 Skill） | fix-git 3/3，dna-insert 2/3 |
  | L2 批次 | 2 批；family-p001 hold，family-p002 `review_approved`（v0→v1） |
  | predicted Reviewer 请求 | 5 次；`val_executions` = 0 |

  dna-insert 的 1/3 → 2/3 在 3 次尝试的噪声内，**不是**改进证据。
- 要看真实收益必须在**独立 split 上实测**（第 8 节的 val/test），不能用本模式的数字代替。

## 6. 运行方式与安全规则（共享主机）

- 这台主机上有别人的长任务。**只管自己 pidfile 里记录的 pid**；不要 kill、不要用 `pgrep -f`/`pkill -f`
  判断或终止任何进程（它会匹配到命令行里含相同字符串的 launcher 或别人的作业）。判活用
  `kill -0 $(cat <pidfile>)`，等待完成用轮询 `summary.json` 或 pidfile。
- `nohup ... & disown` 只脱离 shell 的 job table，**不脱离进程组**；调用方一结束，同组进程可能一起被
  杀。全量或长任务一律用 `launch`（内置 `start_new_session=True`），其它脚本用 Python 的
  `subprocess.Popen(..., start_new_session=True)` 或 `os.setsid()`（macOS 无 `setsid(1)`，Linux 宿主机有）。
- 磁盘：`/` 约剩 29 GB，`/data2` 已 95%。**不要写 `/data2`**；`RETRY_ARCHIVE_ROOT` 与 `JOBS_DIR`
  必须在自己的工作目录下。产物很小（smoke 共约 12 MB），但全量 89×3 的 Harbor trial 目录会大得多，
  先 `df -h /` 再启动。
- 沙箱配额共享：`TB21_WORKERS` 从小开始（smoke 用 4）；`TBENCH_PERSIST_SANDBOXES=0`；
  中断（含上面的 kill）之后确认没有遗留的任务沙箱/中继沙箱。
- 密钥只存在于 `.env` 与进程环境里。不要 `cat env.sh` 后展开、不要 `env`/`printenv` 贴日志、
  不要把 `.env` 拷进仓库或产物目录；env.sh 设 `chmod 600`。
- 冻结目录不可原地修复：任何导入/配置级 bug 修复后，都要在**新的 run 目录**重新 prepare。
  失败的 run 目录改名归档（smoke 里保留了 `run_v0_unlaunchable`、`run_v1_aborted_no_m0`），不删除。
- 每个条件跑完先审计（第 3 步骤 7）再使用其数字。

## 7. 常见故障与处理（均来自 smoke 过程）

| 现象 | 原因 | 处理 |
|---|---|---|
| 首次 launch 立刻抛 `FrozenProtocolChanged: Frozen inputs changed: <run>/config.json` | 旧导入脚本冻结的 config 缺 `l2_verifier` 角色，launch 重新解析角色映射后与冻结内容不同 | 已在 `4d20bfa` 修复；更新 `repo/`，在**新 run 目录**重新 prepare（旧目录不能原地改） |
| 启动时 `JournalConflict: Cold-start task data changed` | 任务表未排序，旧导入脚本按名排序编号而运行时按文件顺序 | 已修复（按文件顺序编号）；旧 run 目录作废并重新导入 |
| `launch` 退出码 2，原因 `M0 library is missing` | 没做 M0 就 launch（smoke 时 `launch` 还不检查，曾因此启动了只有 bootstrap Skill 的 run） | 在同一目录跑完步骤 4 再 launch；没有启动任何进程，目录不用作废 |
| 需要中止一个已启动的 stage | — | `kill -- -"$(cat stage.pid)"`（`launch` 用独立 session，进程组号等于该 pid，只会杀到自己的这一组），run 目录归档作废；`stage.log` 里随后的 `BrokenPipeError`/leaked semaphore 是杀进程后的噪音 |
| `harbor`/python 报 Permission denied，路径在 `/home/liyishan` | 默认 Harbor venv 是不可执行的符号链接 | env.sh 里显式设 `HARBOR_BIN`、`PYTHON_BIN` 指向自己的 Harbor venv |
| runner 去读另一份 `.env` 或往 `/data2` 写 | 默认 `TBENCH_TENCENT_ENV_FILE`、`RETRY_ARCHIVE_ROOT`、`JOBS_DIR` | env.sh 里覆盖（见 2.4）；`JOBS_DIR` 在命令里显式给 |
| M0 提议请求超时 | 默认 `--request-timeout 180` 对长 prompt 偏短 | 用 `--request-timeout 300` |
| `prepare` 退出码 2，`blocked_before_rollout` | raw 覆盖不是任务数×尝试数，或缺 trajectory | 看 `input_coverage.json` 的 `incomplete_tasks`；补跑 E1，不要手改 reward |
| predicted Reviewer 报 HTTP 400 / 400006 | Tencent provider 偶发拒绝严格 `response_format` | 导出 `EXPE_REVIEWER_RESPONSE_FORMAT=omit`（协议哈希会区分两种模式，换新 run 目录） |
| `usage/*.json` 里 token 全是 0 | provider 响应不带 usage | 预期行为；请求数可信，token 取 Harbor `result.json` |
| `RunLocked: ... is already being written by another process` | 同一 run 目录有第二个写入者 | 查 `run.pid` 里的 pid 是否是自己的存活进程；不要强行启动第二个 |

`--resume` 只补缺失的单元；改 prompt、模型、split、协议必须新目录。重启后进程内统计会清零，
所以 `usage/` 逐请求落盘，汇总以磁盘文件为准。

## 8. 两种实验模式：progressive 与 main 方法

### 模式 A：progressive（TB-eval，复刻 codex 的 E3/E4）——本文第 3 节

- 入口：`scripts/tb_eval_stage.py prepare|launch --stage E3|E4`；一个冻结的初始库（M0），
  L1 运行时从 catalog 里选 Skill 再加载 body；
- 验收：`predicted`，**面板是全部 train 题（闭集）**，没有 val/test；
- 已被 smoke 端到端验证。

### 模式 B：main 方法（`sampled` 验收）在 TB 上

诚实的现状：**代码路径是接通的，但从未端到端跑过，也没有测试覆盖，应视为实验性。**

- 接通的部分：冷启动 L1（`l1/workers.py::harbor_experience`）、val/test 单元执行
  （`evaluation/workers.py::_harbor_fixed`，注释写明 sampled verifier 读取缓存的 trajectory）、
  selector 路由、sampled 验收本身（对 benchmark 无关）；
- 缺口：没有 TB 的 sampled/predicted-val 端到端测试；`campaign` 启动器与 `prepare_data.py` 只支持
  searchqa/alfworld，TB 要手写 split 文件并直接用 `python -m skillexpand`；
- **必须有 val 任务**。progressive 的冷启动导入把全部任务都标成 train（`assignment` 全为 `train`），
  没有 val，所以 sampled 无法在 progressive 的 run 目录里运行，也不能 `--cold-start-dir` 复用它。
  main 方法要自己写 `train/val/test` 的 split 文件（默认比例 50/25/25），并让每个参与演化的 family
  在 val 里至少有 2 题（只有 1 题时一律拒绝并记 `insufficient_sample`，为空则整批 hold）；
- sampled 要求 `--skill-edit-mode structured`、`--llm-relay`，`--acceptance-sample-size`（上限，至少 2）
  每个被抽中的 val 题要跑**两臂**真实沙箱，每个 TB 沙箱最长可达 2 小时，应按预算调小。

起点（占位符自行替换；**先用 2 个 train + 2 个 val 的极小集合 dry run，并通过审计后再放大**）：

```bash
# split.json: {"assignment": {"0":"train","1":"train","2":"val","3":"val","4":"test", ...}}
$PYTHON -m skillexpand --benchmark terminalbench \
  --task-file ../data/tb2.json --split-file ../data/tb_splits.json \
  --run-dir ../work/main_cold --phase cold-start --llm-relay \
  --cold-start-workers 4 --family-discovery-workers 4
$PYTHON -m skillexpand --benchmark terminalbench --cold-start-dir ../work/main_cold \
  --run-dir ../work/main_sampled --phase evolve --evolve-rounds 1 --resume --llm-relay \
  --acceptance-mode sampled --skill-edit-mode structured \
  --candidate-count 1 --single-candidate \
  --acceptance-sample-size 2 --acceptance-confidence 0.9 \
  --evolve-l1-workers 4 --l2-review-workers 4 --test-workers 4
```

env 与 progressive 模式相同（执行模型由 `--l1-model` 决定，不需要 `MODEL_NAME`）。这两个模式的结果**互不可比**。

## 9. 时间与成本参照（smoke，2 任务 × 3 次尝试，`TB21_WORKERS=4`）

| 阶段 | 墙钟 | 说明 |
|---|---|---|
| 健康检查 | 秒级 | 一次请求 |
| E1 冷 rollouts | 11.4 分钟 | 6 trial 并发；fix-git 每 trial 约 2–3 分钟，dna-insert 约 10 分钟 |
| 导入 prepare | 秒级 | 纯本地 |
| M0 提议 + 冻结 | 约 1.2 分钟 | 2 张卡 |
| E3 launch（1 轮） | 约 52 分钟 | L1 带 Skill rollouts 约 43 分钟（dna-insert 的 3 次尝试串行，是关键路径），其后 L2 Planner/Editor/Reviewer 约 8 分钟 |
| 审计与收集 | 分钟级 | 离线 |

- 墙钟由**最慢任务的 3 次串行尝试**决定，不随任务数线性增长；全量 89 任务的耗时取决于最慢任务
  与 `TB21_WORKERS`/沙箱配额，没有实测数据，不要用 smoke 外推。
- LLM 方法侧请求很少（smoke 里 predicted Reviewer 仅 5 次）；成本主要在沙箱时长和执行模型的
  Harbor token（一次 dna-insert 尝试约 20–33 万 input、2–4.6 万 output token）。
- provider 不返回 token usage，`usage/` 里的 token 为 0。
