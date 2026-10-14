# 三题历史输入消融结果

固定协议已完成，独立离线审计通过。磁盘故障造成的未决结果与来源缺失全部保留；协议完成不等于所有测量证据完整，也不要求达到某个成功率。

实际消耗 63/81 个执行槽位、12/12 个生成槽位；有效执行 51，官方成功 45，已结束但未决 12。`?` 表示未决，不能记作零分。

| 任务 | sanitized-original baseline | A 最终 | B 最终 | A/B 选中候选 |
| --- | --- | --- | --- | --- |
| cancel-async-tasks | ? 0 ? 0 ? | 1 1 1 1 1 | 1 0 0 0 1 | 1 / 2 |
| multi-source-data-merger | 1 ? ? 1 ? | 1 1 1 1 1 | 1 1 1 1 1 | 2 / 2 |
| log-summary-date-ranges | 1 1 1 ? 1 | 1 1 1 1 1 | 1 ? 1 1 1 | 2 / 2 |

## 固定方法与精确输入

共同对照采用 sanitized-original：保留原 E6 所选 Skill 的通用路由身份、名称和 description，统一移除历史学习 body/resources/provenance，仅保留 `Follow the public task requirements.`。它不是未经改动的旧 Skill。该选择、历史来源审计及全部输入在实测前冻结。

A 保留预选 E1/E4/E6 各三次历史执行、原已加载 Skill 文本与存在的原始记忆卡；工具观察按固定顺序做 8192 字符首尾投影。B 只保留数值且非 bool 的 reward=0 行结构，统一移除全部记忆卡、历史已加载 Skill 文本以及命令、结果、叙述文本。两臂新开发反馈采用同一过滤器，先整体屏蔽受限评测步骤，再截断。

| 任务 | 原始 Skill | A 历史行/记忆卡 | B 失败结构行/记忆卡 | 精确输入与来源 |
| --- | --- | --- | --- | --- |
| cancel-async-tasks | terminalbench.family-p006@v1 | 9/2 | 8/0 | `tasks/cancel-async-tasks/frozen/input.json`, `source_manifest.json`, `evidence-A.json`, `evidence-B.json` |
| multi-source-data-merger | terminalbench.family-p004@v1 | 9/2 | 2/0 | `tasks/multi-source-data-merger/frozen/input.json`, `source_manifest.json`, `evidence-A.json`, `evidence-B.json` |
| log-summary-date-ranges | terminalbench.family-p004@v1 | 9/2 | 2/0 | `tasks/log-summary-date-ranges/frozen/input.json`, `source_manifest.json`, `evidence-A.json`, `evidence-B.json` |

原始面板分别是 `tb21-e1-empirical-final-library-20261008`、`tb21-e4-e3-final-library-empirical-20261010`、`tb21-e6-final-library-empirical-20261010`。E4 记忆实际来自 E3 DeepSeek discovery，E6 记忆来自 E6 discovery；E1 缺少记忆，未替换补入。历史执行原件在各任务 `frozen/sources/`，记忆原件在 `frozen/memory-E4.json`、`frozen/memory-E6.json` 留存。

原生模块为 `SkillEditor.propose_package`，来源提交 `255a67c4b71c24767265a142757443ec0b91dcda`；独立审计核对 337 个冻结文件并用原生 prompt/proposal 方法重放记录的 completion。系统提示在任务及两臂间相同，没有按最终反馈编辑模块。

每臂最多两个候选，每个可用候选恰有三个开发槽位；第二候选只读取本臂前一候选及其三个开发反馈。首候选无可用包时，第二候选从共同 seed 出发、没有新开发反馈。按有效官方开发成功数选最大者，并列选较早候选；随后冻结完整包并执行五个独立新环境。最终反馈未回流。

执行模型为 `openai/qwen3.6-flash-distill`，生成请求模型为 `DEEPSEEK_up5zdj`。Qwen 参数、任务环境与官方 verifier 取自各任务 `frozen/executor.json`；7200 秒预算、top_p=0.95、max_tokens=65536、top_k=20、thinking/preserve/interleaved 保持冻结值。Harbor retry=0；原 provider HTTP 重试仍在，81 上限计执行 trial 而非底层 API 请求数。

## 候选生成与开发选择

| 任务/臂/候选 | 生成状态 | 开发奖励 | 选中 | 生成秒数 | 精确请求与响应 |
| --- | --- | --- | --- | --- | --- |
| cancel-async-tasks/A/1 | generated | ? ? ? | True | 100.99100422859192 | `tasks/cancel-async-tasks/arms/A/candidate-001/request.json`, `response.sse`, `completion.json`（若完整） |
| cancel-async-tasks/A/2 | invalid_proposal | 未执行 | False | 203.11942434310913 | `tasks/cancel-async-tasks/arms/A/candidate-002/request.json`, `response.sse`, `completion.json`（若完整） |
| cancel-async-tasks/B/1 | failed | 未执行 | False | 185.18949627876282 | `tasks/cancel-async-tasks/arms/B/candidate-001/request.json`, `response.sse`, `completion.json`（若完整） |
| cancel-async-tasks/B/2 | generated | 1 ? 0 | True | 80.41944909095764 | `tasks/cancel-async-tasks/arms/B/candidate-002/request.json`, `response.sse`, `completion.json`（若完整） |
| multi-source-data-merger/A/1 | interrupted_unresolved | 未执行 | False | 未知 | `tasks/multi-source-data-merger/arms/A/candidate-001/request.json`, `response.sse`, `completion.json`（若完整） |
| multi-source-data-merger/A/2 | generated | 1 1 1 | True | 86.19747424125671 | `tasks/multi-source-data-merger/arms/A/candidate-002/request.json`, `response.sse`, `completion.json`（若完整） |
| multi-source-data-merger/B/1 | interrupted_unresolved | 未执行 | False | 未知 | `tasks/multi-source-data-merger/arms/B/candidate-001/request.json`, `response.sse`, `completion.json`（若完整） |
| multi-source-data-merger/B/2 | generated | 1 1 1 | True | 98.71801400184631 | `tasks/multi-source-data-merger/arms/B/candidate-002/request.json`, `response.sse`, `completion.json`（若完整） |
| log-summary-date-ranges/A/1 | interrupted_unresolved | 未执行 | False | 未知 | `tasks/log-summary-date-ranges/arms/A/candidate-001/request.json`, `response.sse`, `completion.json`（若完整） |
| log-summary-date-ranges/A/2 | generated | 1 1 1 | True | 36.02791380882263 | `tasks/log-summary-date-ranges/arms/A/candidate-002/request.json`, `response.sse`, `completion.json`（若完整） |
| log-summary-date-ranges/B/1 | interrupted_unresolved | 未执行 | False | 未知 | `tasks/log-summary-date-ranges/arms/B/candidate-001/request.json`, `response.sse`, `completion.json`（若完整） |
| log-summary-date-ranges/B/2 | generated | 1 1 1 | True | 97.2675986289978 | `tasks/log-summary-date-ranges/arms/B/candidate-002/request.json`, `response.sse`, `completion.json`（若完整） |

## 基础设施故障与证据限制

远端 `/data2` 耗尽使部分 JSON/JSONL 变为空文件或截断，并中断四个首候选生成流。原驱动和相关进程均确认退出后，先完整备份，再离线恢复 wrapper。有完整官方 result、配置、完整挂载包、轨迹及新且唯一 snapshot 身份的结果才可恢复为有效；缺少任一必需证据则维持 unresolved/null。缺失官方源文件始终未改写。

受损原字节保存在槽位 `infrastructure_originals/*.raw`，恢复说明在 `infra_recovery.json`。不完整生成流没有获得候选包、completion 或奖励。已消耗的执行/生成槽位没有重跑或替换。外部清理恢复空间后，`resume_dispatch.json` 继续原冻结 driver 的未用槽位；预算未重置。

独立审计列出 61 条来源限制，详见 `completion_audit.json:source_artifact_limitations`。这些是来源限制条目数，不等于失败 trial 数。完整轨迹中的测量值可以保留；损坏 provider 流的模型身份、重试计数和用量完整性仍为未知。

所有未决并非同一原因。`InfrastructureWriteFailure` 记录是磁盘写入丢失；其他 ValueError/RuntimeError 按每次官方记录留存。例如 log B final/02 的官方异常为 `failed to send non-blocking keys: no server running on /tmp/tmux-0/default`，这是独立运行故障，未归入磁盘丢失或补跑。未决类型计数见 `final_results.json:unresolved_error_types`。

## 用量与执行观察

| 来源 | 计数 | 观测总量 | 已知记录 | 未知记录 | 完整 |
| --- | --- | --- | --- | --- | --- |
| 生成 | input tokens | 179313 | 7 | 5 | False |
| 生成 | output tokens | 91961 | 7 | 5 | False |
| 生成 | cached tokens | None | 0 | 12 | False |
| 生成 | total tokens | 271274 | 7 | 5 | False |
| 执行 | input tokens | 2772570 | 53 | 10 | False |
| 执行 | output tokens | 361640 | 53 | 10 | False |
| 执行 | cached tokens | 0 | 53 | 10 | False |
| 执行 | total tokens | 3134210 | 53 | 10 | False |
| 执行 | duration_seconds | 13364.854361772537 | 52 | 11 | False |
| 执行 | rounds | 460 | 53 | 10 | False |
| 执行 | tool_calls | 802 | 53 | 10 | False |

以上总量为已观察部分，未知值未填零，不推算货币成本。duration_seconds 是各 trial 时长之和，包含并发重叠，不是研究墙钟耗时。每次执行的时长、轮数、请求/响应记录、返回模型身份与资源引用详见 `completion_audit.json:executor_slots`。

`resource_analysis.json` 和 `resource_analysis.md` 独立列出资源读取、复制、调用命令、观察关联及公开工具错误步骤。损坏轨迹仅提取完整的步骤对象，明确标为部分证据，不用于恢复奖励或生成反馈。命令中的资源引用、调用或无错误输出均不能单独证明资源正确或因果收益。

## 解释边界

这是联合历史信息删除：B 同时丢弃有用的失败诊断，A/B 差异不能单独归因于成功轨迹、代码回显或泄漏。磁盘故障与大量未决进一步限制比较，不能据此给出可靠的精确增益。若 B 有有效官方成功，只支持该次执行无需被删除材料仍可完成任务；若只有 A 有成功，只能说明本协议下的信息差异敏感性。

`multi-source-data-merger` 和 `log-summary-date-ranges` 曾用于此前诊断，三题及同题新环境都不构成盲测新任务泛化。未增加额外验收批次以追求成功。

## 每次执行的真实结果

| 槽位 | 有效 | 官方奖励 | 时长秒 | 轮数 |
| --- | --- | --- | --- | --- |
| `tasks/cancel-async-tasks/baseline/01` | False | ? | 未知 | 未知 |
| `tasks/cancel-async-tasks/baseline/02` | True | 0 | 297.41401505470276 | 7 |
| `tasks/cancel-async-tasks/baseline/03` | False | ? | 128.5293960571289 | 3 |
| `tasks/cancel-async-tasks/baseline/04` | True | 0 | 371.86543679237366 | 9 |
| `tasks/cancel-async-tasks/baseline/05` | False | ? | 未知 | 未知 |
| `tasks/cancel-async-tasks/arms/A/candidate-001/dev/01` | False | ? | 未知 | 未知 |
| `tasks/cancel-async-tasks/arms/A/candidate-001/dev/02` | False | ? | 未知 | 未知 |
| `tasks/cancel-async-tasks/arms/A/candidate-001/dev/03` | False | ? | 未知 | 未知 |
| `tasks/cancel-async-tasks/arms/A/final/01` | True | 1 | 257.5075035095215 | 11 |
| `tasks/cancel-async-tasks/arms/A/final/02` | True | 1 | 620.7732532024384 | 17 |
| `tasks/cancel-async-tasks/arms/A/final/03` | True | 1 | 235.0371584892273 | 11 |
| `tasks/cancel-async-tasks/arms/A/final/04` | True | 1 | 238.80839490890503 | 13 |
| `tasks/cancel-async-tasks/arms/A/final/05` | True | 1 | 418.826500415802 | 9 |
| `tasks/cancel-async-tasks/arms/B/candidate-002/dev/01` | True | 1 | 179.1623923778534 | 8 |
| `tasks/cancel-async-tasks/arms/B/candidate-002/dev/02` | False | ? | 未知 | 未知 |
| `tasks/cancel-async-tasks/arms/B/candidate-002/dev/03` | True | 0 | 224.91533756256104 | 11 |
| `tasks/cancel-async-tasks/arms/B/final/01` | True | 1 | 278.4168610572815 | 13 |
| `tasks/cancel-async-tasks/arms/B/final/02` | True | 0 | 373.42896127700806 | 16 |
| `tasks/cancel-async-tasks/arms/B/final/03` | True | 0 | 577.9391279220581 | 11 |
| `tasks/cancel-async-tasks/arms/B/final/04` | True | 0 | 299.8974173069 | 6 |
| `tasks/cancel-async-tasks/arms/B/final/05` | True | 1 | 234.96305680274963 | 13 |
| `tasks/multi-source-data-merger/baseline/01` | True | 1 | 306.92166471481323 | 9 |
| `tasks/multi-source-data-merger/baseline/02` | False | ? | 未知 | 未知 |
| `tasks/multi-source-data-merger/baseline/03` | False | ? | 未知 | 未知 |
| `tasks/multi-source-data-merger/baseline/04` | True | 1 | 407.4315514564514 | 6 |
| `tasks/multi-source-data-merger/baseline/05` | False | ? | 未知 | 未知 |
| `tasks/multi-source-data-merger/arms/A/candidate-002/dev/01` | True | 1 | 131.45954251289368 | 8 |
| `tasks/multi-source-data-merger/arms/A/candidate-002/dev/02` | True | 1 | 154.35972905158997 | 10 |
| `tasks/multi-source-data-merger/arms/A/candidate-002/dev/03` | True | 1 | 141.614022731781 | 7 |
| `tasks/multi-source-data-merger/arms/A/final/01` | True | 1 | 166.79356455802917 | 8 |
| `tasks/multi-source-data-merger/arms/A/final/02` | True | 1 | 157.5342779159546 | 9 |
| `tasks/multi-source-data-merger/arms/A/final/03` | True | 1 | 262.4505741596222 | 7 |
| `tasks/multi-source-data-merger/arms/A/final/04` | True | 1 | 165.1180911064148 | 8 |
| `tasks/multi-source-data-merger/arms/A/final/05` | True | 1 | 128.20053887367249 | 7 |
| `tasks/multi-source-data-merger/arms/B/candidate-002/dev/01` | True | 1 | 196.76364493370056 | 9 |
| `tasks/multi-source-data-merger/arms/B/candidate-002/dev/02` | True | 1 | 435.5188271999359 | 9 |
| `tasks/multi-source-data-merger/arms/B/candidate-002/dev/03` | True | 1 | 162.12421345710754 | 8 |
| `tasks/multi-source-data-merger/arms/B/final/01` | True | 1 | 312.9316940307617 | 10 |
| `tasks/multi-source-data-merger/arms/B/final/02` | True | 1 | 136.94699597358704 | 8 |
| `tasks/multi-source-data-merger/arms/B/final/03` | True | 1 | 139.43292379379272 | 6 |
| `tasks/multi-source-data-merger/arms/B/final/04` | True | 1 | 164.9841914176941 | 8 |
| `tasks/multi-source-data-merger/arms/B/final/05` | True | 1 | 119.45746684074402 | 6 |
| `tasks/log-summary-date-ranges/baseline/01` | True | 1 | 未知 | 6 |
| `tasks/log-summary-date-ranges/baseline/02` | True | 1 | 253.32834839820862 | 6 |
| `tasks/log-summary-date-ranges/baseline/03` | True | 1 | 272.1648290157318 | 5 |
| `tasks/log-summary-date-ranges/baseline/04` | False | ? | 未知 | 未知 |
| `tasks/log-summary-date-ranges/baseline/05` | True | 1 | 396.1005585193634 | 7 |
| `tasks/log-summary-date-ranges/arms/A/candidate-002/dev/01` | True | 1 | 412.6262216567993 | 8 |
| `tasks/log-summary-date-ranges/arms/A/candidate-002/dev/02` | True | 1 | 133.5801978111267 | 7 |
| `tasks/log-summary-date-ranges/arms/A/candidate-002/dev/03` | True | 1 | 124.1783435344696 | 10 |
| `tasks/log-summary-date-ranges/arms/A/final/01` | True | 1 | 416.18244981765747 | 10 |
| `tasks/log-summary-date-ranges/arms/A/final/02` | True | 1 | 254.63711309432983 | 7 |
| `tasks/log-summary-date-ranges/arms/A/final/03` | True | 1 | 101.74143528938293 | 6 |
| `tasks/log-summary-date-ranges/arms/A/final/04` | True | 1 | 290.44137811660767 | 9 |
| `tasks/log-summary-date-ranges/arms/A/final/05` | True | 1 | 97.53514575958252 | 7 |
| `tasks/log-summary-date-ranges/arms/B/candidate-002/dev/01` | True | 1 | 383.1015727519989 | 8 |
| `tasks/log-summary-date-ranges/arms/B/candidate-002/dev/02` | True | 1 | 360.56922364234924 | 15 |
| `tasks/log-summary-date-ranges/arms/B/candidate-002/dev/03` | True | 1 | 388.60894560813904 | 6 |
| `tasks/log-summary-date-ranges/arms/B/final/01` | True | 1 | 111.84649157524109 | 6 |
| `tasks/log-summary-date-ranges/arms/B/final/02` | False | ? | 310.270060300827 | 12 |
| `tasks/log-summary-date-ranges/arms/B/final/03` | True | 1 | 253.17031335830688 | 8 |
| `tasks/log-summary-date-ranges/arms/B/final/04` | True | 1 | 120.84694480895996 | 8 |
| `tasks/log-summary-date-ranges/arms/B/final/05` | True | 1 | 256.3964612483978 | 8 |
