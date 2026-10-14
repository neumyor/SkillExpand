# 小库整体演化结果

固定协议已完成，远端及本地归档离线审计均通过：199 项冻结绑定一致，62/62 个执行槽、6/6 次演化调用，无审计错误或待完成项。61 个执行有效，官方成功 44 次，1 个环境初始化未决。完整库按开发结果选中候选 1，final 不参与选择或修改。

## 结果

| 任务 | 面板 | 原始 E6 库 baseline | 选定整库 final |
| --- | --- | --- | --- |
| cancel-async-tasks | 开发 | 0 0 0 | 0 1 1 |
| headless-terminal | 开发 | 0 1 0 | 1 1 0 |
| log-summary-date-ranges | 开发 | 1 1 1 | 1 ? 1 |
| multi-source-data-merger | 开发 | 1 1 1 | 1 1 1 |
| kv-store-grpc | 开发 | 1 1 0 | 1 1 1 |
| constraints-scheduling | 独立回归 | 1 1 1 | 1 1 1 |
| nginx-request-logging | 独立回归 | 1 1 0 | 1 1 1 |

Baseline 为 14/21 成功，final 为 18 个成功、2 个有效失败、1 个未决。final 的未决不能计零，也不能用有效结果分母替换原定 21 槽。两项独立回归的比较结果均为 passed：全部比较结果有效，成功数分别保持 3/3、从 2/3 到 3/3。

`?` 是 `final/slots/log-summary-date-ranges/02`：环境启动时 `apt-get update` 的 120 秒请求触发 E2B `TimeoutException`，尚未进入模型执行。保留 reward=null，没有补跑。最后结束的 `cancel-async-tasks/03` 为有效成功，槽耗时 4554.25 秒。

## 实际方法

完整保留原 E6 p003/p004/p006 的 seed 正文及 provenance。每槽先把三个 skill_id/description 全目录交给原生 `select_and_load`，一次真实模型选择后只加载选定包。选择器与执行器为独立会话；执行中没有动态多技能切换，也没有人工 task-to-skill 映射。名称、description、ID、family 与路由拓扑保持固定。

三个 skill 均可编辑，只使用本轮开发任务中实际选中该 skill 的证据。第一轮读取 baseline 开发轨迹，第二轮读取 baseline 加候选 1 开发轨迹。回归及 final 反馈不进入生成；没有注入之前研究产生的最终 skill、checker 或实现。调用原生 `SkillEditor.propose_package`，未按结果修改提示或解析器。

Baseline 和 final 各 7 题 x 3 槽；两个候选各 5 道开发题 x 2 槽，共 62 槽。授权绝对上限 66，额外 4 个开发槽未分配。每个 skill 每轮最多一次生成，共 6 次；失败、无效提案和未决均消耗槽，不修复响应、不补调用。Harbor attempt=1、retry=0；原 provider HTTP 与 snapshot 内部重试机制保留，不把 62 槽解释成底层请求次数。

## 无截断输入

这轮直接投影完整合规工具观测，不经过通用 memory 的单条 2000 字符和总量预算，也没有旧协议 8192 字符首尾截断。受限 verifier/test/reward 步骤仍过滤。未自动缩减超长请求。

| 轮次/skill | 来源槽数 | 合规观测数 | 合规观测字符总量 | 完整提示字符数 |
| --- | --- | --- | --- | --- |
| 1 / p003 | 3 | 24 | 21422 | 43121 |
| 1 / p004 | 7 | 53 | 113841 | 172073 |
| 1 / p006 | 5 | 105 | 257017 | 321187 |
| 2 / p003 | 5 | 44 | 39798 | 76262 |
| 2 / p004 | 11 | 90 | 197590 | 294869 |
| 2 / p006 | 9 | 169 | 393715 | 523954 |

字符数不是 token 数。全部合规结果 `truncated=false`，最长单条 10068 字符。离线审计逐项对照原轨迹、生成输入及原生提示重建，六次真实请求都通过；本轮没有 provider 上下文溢出。该结论针对本轮演化路径，不声称通用 memory 模块的默认配置已全局修改。

## 候选与资源

候选 1 的三个提案均有效，整库为 p003@v2、p004@v2、p006@v2，开发 7/10。候选 2 只有 p003 产生新有效提案（v3），整库开发 5/10。p004 返回多余 `no_change` 字段，p006 缺少 `resources/rationale`，均由原生 exact-fields 校验判为 invalid_proposal，并沿用候选 1 的对应包。按官方开发成功数最大、并列选较早者，最终选候选 1。

候选 1 的 p006 包含 `verify_sigint.py`；p003/p004 无资源。候选 2 的 p003 新增 `check_contract_fields.py`，未见实际引用，且该库未被选中。挂载资源正文、源路径及实际 entrypoint 均通过审计。

独立逐步读轨迹发现 `verify_sigint.py` 的真实挂载读取 6 次：候选 1/2 各两个 headless 槽、final 两个 headless 槽。明确执行挂载脚本仅 1 次：候选 1 `headless-terminal/02` step 13，观测显示 `SIGINT verification passed`，但该槽官方 reward=0。因此自检标记不能证明任务正确或资源带来收益。候选 2 cancel 槽的 `/app/verify_sigint.py` 是同名自写脚本，不能算挂载资源执行。自动审计中的文件名引用保持保守标签，不能直接当作实际调用数。

## 共同设置与开销

执行器和选择器为 `qwen3.6-flash-distill`，provider 为 E10 实际配置的 `https://llm-center.modelbest.co/v1`。所有执行阶段共同使用输入上下文 262144、输出 65536、max_tokens=65536、top_p=0.95、top_k=20、thinking/preserve/interleaved=true、summarize=true、threshold=8000、agent timeout=7200 秒；实际有效上下文和挂载均已核查。生成模型为 `DEEPSEEK_up5zdj`，stream=true、max_tokens=65536、thinking=true。262144 是共同条件，不能归因为演化收益。

执行轨迹可观测 token 合计 18966055：输入 18265946、输出 700109，61 个记录已知、1 个初始化失败记录未知。六次生成响应未提供可用 token usage，不能估算为零；费用未知。62 槽耗时之和 24405.41 秒，六次生成耗时之和 623.81 秒；并发耗时和不是墙钟。首槽预留至最后结果约 8003 秒（UTC 12:16:28 至 14:29:51）。

## 解释与复核

这次证明了小型完整库通过真实选择、按实际证据分组及原生演化后，可以在该闭集面板得到更多官方成功；不证明未见任务泛化、统计显著性、资源因果收益或多技能动态使用。seed 有历史学习来源，面板仅七题且每题三次。没有截断开关的对照臂，因此不能把本轮改善单独归因于关闭截断。

原始记录保存在远端 `/data2/liyishan/SkillExpand-tb21/SkillExpand/runs/library-pilot-20261014-262144`，本地完整归档位于 `/Users/a1/Documents/New project/library_pilot_final_archive/library-pilot-20261014-262144`。目录含冻结源/设置、实际 catalog/selection、载入包及挂载清单、全部模型请求/SSE/响应、Harbor 官方结果、轨迹与快照；缓存不参与交付，已绑定文件完整保留。

远端 `completion_audit.json` 与本地 `library_pilot_archive_completion_audit.json` 均为 passed=true、protocol_complete=true、errors=[]、pending=[]。审计只读取保存产物，不调用模型或 verifier。运行代码发布于 `2ba52c2`；研究会话请求的模型切换没有已验证证据，不将其写作本轮实验模型事实。
