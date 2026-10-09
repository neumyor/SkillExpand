# 异常处理与断点恢复

所有失败处理集中在 `src/skillexpand/reliability/`：

| 模块 | 职责 |
|---|---|
| `errors.py` | 异常分类、各类别的处置方式（一张表）、第三方异常的翻译注册 |
| `policies.py` | 所有重试与修复预算（一张表） |
| `retry.py` | 仅有的两个重试循环：`retry_transient`（基础设施）、`call_with_repair`（模型输出） |
| `units.py` | 单元边界：`unit-error-v1` 失败记录、`guard`、`FailureCollector`、`map_units` |

## 1. 原则

1. 测量只来自正常完成的单元；异常永远不会变成得分。
2. 基础设施故障不消耗任务预算，不写入结果缓存；续跑时补齐。
3. 模型输出不合格：按修复策略重试；预算用完后视为**可重试**的单元失败，续跑时重新采样。
4. 审校失败停止当前阶段，交给人处理。
5. 未归类的异常就是代码 bug：**立即停止整个流程**，包括 campaign 中另一个 benchmark 的任务。

## 2. 类别与处置

处置只在 `errors.DISPOSITIONS` 里定义，抛出异常的地方不做决定。

| 类别 | 典型异常 | 可重试 | 停止范围 |
|---|---|---|---|
| `infrastructure` | `ProviderUnavailable`、`EnvironmentFailure`/`EnvironmentTimeout`、`WorkerLost`、`StageIncomplete`（失败全部来自 `response` 时，类别也取 `response`） | 是 | 当前批次跑完后，阶段以 `StageIncomplete` 结束 |
| `response` | `JsonExtractionError`、`SchemaViolation`、`ReferenceViolation`、`RepairExhausted` | 是 | 同上 |
| `provider_rejected` | `ProviderRejected`（认证、权限、模型不存在、请求非法或超长） | 否 | 立即停止当前阶段 |
| `integrity` | `FrozenProtocolChanged`、`AuditFailure`、`JournalConflict`、`StoreError`/`LedgerCorrupt`、`IsolationViolation` | 否 | 立即停止当前阶段 |
| `configuration` | `InvalidInput`、`RunLocked` | 否 | 立即停止当前阶段 |
| `bug` | 其他任何异常 | 否 | **立即停止整个流程** |

`ResponseFormatError`、`IntegrityError`、`ConfigurationError` 继承 `ValueError`；`StageIncomplete`、`RunLocked` 继承 `RuntimeError`；`EnvironmentTimeout`、`WorkerLost` 继承 `TimeoutError`。原有的 `except` 写法因此仍然有效。

## 3. 边界翻译

第三方异常只在调用它的那一层翻译，上层只看到本体系内的类型。

| 边界 | 翻译 |
|---|---|
| `runtime/models/llm.py` | openai 的 `Timeout`、`APIConnectionError`、`RateLimitError`、`ServiceUnavailableError`、`TryAgain` → `ProviderUnavailable`；`APIError` 按 HTTP 状态区分：408、409、429、5xx 或无状态 → `ProviderUnavailable`，其余 4xx → `ProviderRejected`；`AuthenticationError`、`PermissionError`、`InvalidRequestError`、`InvalidAPIType`、`SignatureVerificationError` → `ProviderRejected` |
| `benchmarks/base.py` | 原生环境调用超时 → `EnvironmentTimeout`；`BrokenPipeError`、`ConnectionResetError`、`EOFError` → `EnvironmentFailure` |
| `runtime/parallel.py` | 进程池无进展 → `WorkerLost` |
| `persistence/io.py` | 写锁冲突 → `RunLocked`；冻结身份不一致 → `FrozenProtocolChanged`；JSONL 中间行损坏 → `LedgerCorrupt`；`require()` 失败 → `AuditFailure` |
| 各响应解析器 | 解析时出现的 `ValueError/KeyError/TypeError/AttributeError` 由 `call_with_repair` 统一转为 `SchemaViolation`；请求阶段的异常不做这种转换。同一个校验函数（例如 `DiscoveryError`）用于已落盘产物时属于 `integrity`，用于模型输出时属于 `response` |

## 4. 重试与修复策略

`PROVIDER`：模型与服务端点被视为永不下线，拥堵时**无限重试**，退避 1→2→4…→60s。

`CAMPAIGN_STAGE`：阶段失败且可重试时，按 15→60→180→300s 退避后重新拉起；次数上限由 `EXPE_STAGE_ATTEMPTS` 设定，0 表示不限。

修复策略（`policies.REPAIR`；预算是"新发出的模型调用次数"，从缓存回放的响应不计入）：

| 名称 | 次数 | 用完之后 |
|---|---:|---|
| `reviewer.predicted_val`、`reviewer.delta_review` | 32（可用 `EXPE_REVIEWER_ATTEMPTS` 覆盖） | 可重试 |
| `verifier.claim` | 8 | 可重试 |
| `planner.hypotheses` | 2（第 2 次附带 correction） | 可重试 |
| `discovery.tags`、`discovery.proposals`、`discovery.assignment`、`discovery.initial_skill` | 3（附固定后缀） | 可重试 |
| `editor.candidate`、`patterns.batch`、`selector.route` | 1 | 降级，记为协议内的结果（无候选、无 pattern、路由失败） |

修复预算决定模型调用次数，属于实验协议；修改后应使用新的运行目录。

`STAGE_ATTEMPTS_BY_CATEGORY`：同一阶段连续 5 次因 `response` 类失败结束时，阶段转为 needs_attention。修复用尽本身仍可重试，这个上限只是为了防止解析器自身有缺陷时无限重新采样。

## 5. 单元边界与失败记录

worker 不向外抛异常，而是用 `guard` 或 `failure_record` 把异常转成 `unit-error-v1` 记录，这样记录可以跨进程传回：

```json
{"schema": "unit-error-v1", "unit_id": 7, "stage": "evolution-1/l1",
 "category": "infrastructure", "type": "EnvironmentTimeout", "retryable": true,
 "message": "...", "cause_chain": ["EnvironmentTimeout"], "time": 0.0}
```

L1、路由和 fixed-Skill 执行的 worker 结果都带 `failure` 字段：成功时为 `null`，失败时为上面的记录。

阶段内由 `FailureCollector` 写入 `errors/<unit>.json` 或 `evaluation_errors/<key>.json`（fixed-Skill 执行还会附带已有的事件作为 `evidence`），并按类别处置：

- 可重试的失败先收集，阶段结束时统一抛出 `StageIncomplete`；
- 不可重试的失败立即抛出 `UnitFailed`。此时线程池会取消排队中的单元，进程池会被终止。线程无法被强行中断，正在执行的单元会继续跑完；因此 campaign 阶段进程和 `python -m skillexpand` 在记录不可重试的失败后，用 `exit_now` 直接退出，不等这些线程结束。

记录中的 `stage` 字段由 collector 写入（如 `evolution-1/l1`、`routing-test`、`fixed-execution`）。

接入位置：冷启动和 evolution 的 L1、val/test 路由、fixed-Skill 执行、predicted-val、sampled 配对 Δ Reviewer（stage `delta-review`）。sampled 判定者不经过 collector，修复用尽的 `RepairExhausted` 直接中止当前 batch（可重试，`--resume` 时从缓存续跑）。L1 checkpoint 的 `errors[]` 和被中断 trial 的 `failure_category` 也会记录类别。

## 6. 阶段与 campaign

- `l2/loop`：失败时 `summary.json` 记录 `status=needs_attention`、失败所在轮次和 `failure_category`。冷启动和 test 阶段不写 summary，失败信息在 `errors/`、`evaluation_errors/` 和 campaign 的 attempt 记录中。
- `campaign.execute_stage`：在 `attempts/<stage>-<n>.json` 中写入 `category / retryable / halt`。是否重试**只看类别**，不再匹配字符串。
- `campaign.run_job`：可重试时退避后重跑；不可重试时把 `failure_category` 和 `halt` 写进 `status.json` 并停止。
- `campaign.supervise`：任一 job 的 `halt` 为 `all` 时，立即终止其余 job，并在 `status.json` 写入 `halted_by`。
- 阶段子进程被信号杀死（返回码 < 0，如 OOM、人为终止）视为基础设施故障，可重试；以正返回码退出却没有写 attempt 记录时，按 `bug` 处理。

## 7. 断点恢复

续跑依次经过以下几层：

1. **互斥**：`RunLock` / `exclusive_lock`，冲突时抛 `RunLocked`。
2. **协议冻结**：`freeze` 只冻结方法输入，写一次、之后必须相等。
3. **单元缓存**：已完成的单元跳过（冷启动 `results/`、evolution `cards/`、路由 `tasks/`、`ScoreCache`、L2 `l2_proposals/`）；失败的单元不写入缓存。
4. **修复回放**：L2 的每次修复尝试都按 `hypotheses-<n>`（`n` 从 0 开始）落盘。续跑时先回放这些尝试且不计入预算，然后继续新的请求。
5. **事务日志**：先写 L2 batch journal，再写 Skill 版本库；续跑时只核对提交一致性（候选已批准/选中、日志与版本库一致）并补写缺失的版本；批次决策的完整重放只在每轮结束的 `audit_round` 做一次。
6. **账本自愈**：中断的最后一行移到 `.interrupted-tail`，补上缺失的换行；没有结束事件的请求记为 abandoned。abandoned 请求不再导致 L1 checkpoint 审计失败：被中断的 trial 已记为 interrupted 并重跑，checkpoint 不会使用这些响应，而它实际用到的每个响应都与账本逐一核对。它们的 token 成本未知，由 `tokens_complete=false` 和 `abandoned_requests` 计数反映。

## 8. 扩展指南

运行中发现新的异常时，按以下三种情况处理：

1. **已有含义的第三方异常**（例如某个网关抛出新的超时类型）：在调用该库的边界模块里加一行
   `register_translation(NewLibraryError, ProviderUnavailable)`。重试循环、单元记录和 campaign 判断会自动生效。
2. **新的模型输出调用点**：在 `policies.REPAIR` 里加一行策略，调用处使用
   `call_with_repair(repair_policy('<name>'), request, parse)`。`parse` 只负责校验，抛出 `ValueError` 等即可。
3. **新的失败含义**：在 `errors.py` 中从最接近的类派生。只有需要不同处置时才新增 `Category`，并在 `DISPOSITIONS` 里加一行。

不要在调用点用 `except Exception` 吞掉异常；需要转成数据时，用 `guard` 或 `FailureCollector`。

## 9. 不兼容历史 run

当前代码只读取自己产生的工件格式，不对旧 run 做任何兼容：失败记录没有 `error` 字符串，修复尝试的缓存名、predicted reviewer 的协议哈希和 l2_manifest 都已变化，`discovery/card_hashes.json` 与 L1 checkpoint 的 `identity` 为必需项。旧 run（包括含 `invalid_hypotheses` / `invalid_review` hold 的 batch）只能用产生它的代码续跑或审计。

- 尚未实现：把冷启动、evolution、路由、L2 等各自的"存在即跳过"统一抽象成 `UnitStore`，以及在每个阶段开始时写 `resume.json`。目前这几处缓存仍各自实现，但语义一致，失败单元都不会写入缓存。
