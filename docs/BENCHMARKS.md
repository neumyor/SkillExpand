# Benchmark 与 L1 接口

L1 在当前任务预算内尽可能完成任务，输出 task-specific 经验卡；L2 在全部冷启动任务完成后，直接读取卡片进行跨任务归纳。
每次尝试结束先确定性刷新实际反馈，再决定自主反思、可选指导或结束。

`src/skillexpand/l1/adapters.py::Adapter` 定义以下接口：

| 接口 | 责任 |
|---|---|
| tool_semantics | 冷启动工具语义；ALFWorld 同一份运行说明也进入执行、反思、最终提取 |
| configure(agent) | 安装执行指令及工具 |
| build_feedback(agent, trial) | benchmark 专属、可 JSON 序列化的反馈；不统一内容字段 |
| prepare_guidance(agent, trials) | source 自主预算耗尽后返回可用指导，或 None |
| render_guidance(payload) / guidance_source(payload) | 渲染任意指导 payload，并记录来源 |
| reflection_prompt(guided, extraction) | benchmark 指令与统一结构化输出契约 |
| selector_prompt(benchmark) | 仅根据描述和问题进行 Skill 路由 |
| repeated_action_is_stalled(events, action) | 判断重复动作是否真的无进展 |
| evidence_event(event) | 返回完整观察 text、effect（rejected/observed/score）与 method；默认不认可正向方法证据 |

SearchQA 使用本地 Search、Lookup、Finish，在自主预算耗尽后可提供 source 参考答案。
ALFWorld 使用环境动作、反馈和可用动作，当前没有可选指导；环境能评分不等于存在 GT
下一步动作。不能把参考轨迹的同序号动作当作当前状态的正确动作。

## 配置方式

每个 benchmark 的 `src/skillexpand/configs/benchmark/<name>.yaml` 可设置独立 `l1` 块：

```yaml
l1:
  adapter: skillexpand.l1.adapters:SearchQAAdapter
  execution_instructions: null
  execution_instructions_file: null
  reflection_instructions: null
  reflection_instructions_file: null
  guidance_instructions: null
  guidance_instructions_file: null
  selector_instructions: null
  selector_instructions_file: null
  action_recovery_instructions: null
  action_recovery_instructions_file: null
  extraction_instructions: null
  extraction_instructions_file: null
  action_format_attempts: 4
  action_only_attempts: 2
  card_target_tokens: 400
  card_limit_tokens: 800
```

每组 prompt 只设置 inline 文本或 `_file` 中的一项；null 使用适配器默认。
文件支持绝对路径或相对仓库根目录的 UTF-8 路径。内容参与恢复校验，不能在同一 run
中悄悄修改。不同 benchmark 可导入自己的 `package.module:AdapterClass`；新 benchmark
还须实现任务加载与环境，不能仅修改 prompt 就获得执行支持。

selector 返回 `SKILL: <精确 skill_id>` 与 `WHY: <简短依据>`。
当前协议仅为 final 在初始 description 下冻结分组；后续执行直接使用已选 Skill，不重新路由。
admission 划分保留，但不进入当前 L2 评审或执行。
覆盖 selector prompt 不会开放 body/GT 输入，候选列表的程序结构始终只包含 ID 和 description。
执行与 held-out 评测共享同一适配器；评测不调用反思或指导提供者。

## 修复状态和经验卡

每步默认 4 次普通输出 + 2 次 action-only 格式恢复，仅解析成功的动作能调用环境。
反思采用紧凑 diagnosis、evidence_refs、next_change、uncertainty；有指导才加 guidance_delta。
latest_attempt、实际失败动作和反馈由程序维护，模型不能覆盖。

L1 v7 将重试假设与最终学习分开。每个结束的任务（首次成功、重试成功、辅助完成或
最终失败）都执行一次最终综合，不再根据计划动作是否出现来跳过提取。
输入仅包含成对的动作/观察、实际尝试结果；模型 Thought 不作为事实。ALFWorld 删除
学习输入中重复的可行动作菜单，保留完整观察和否定信息；执行器仍能看到菜单。

最终模型只返回零到两条任务局部 claim（procedure/constraint/comparison、text、
evidence_refs）。程序检查引用的是已完成尝试中的实际执行观察；正向步骤须引用同一次
成功尝试中的方法证据，不能以 rejected 动作、纯评分/参考答案提交或全失败任务为支持。
引用检查不证明自由文本的因果真实性。
无可靠经验时 claims 为空；诊断只留在修复检查点，不能作为 Skill 规则。

新卡 schema_version=5：task、execution（各完成尝试的阶段、结果、终止原因）、
evidence（实际动作/反馈及效果）、claims、claim_status 和审计标识。单卡不能表达跨卡
重复事实；冷启动合成和每轮 L2 编辑前，批次单独生成可回溯的 pattern 候选。
最终输出原文、输入和逐条解析结果单独存入 checkpoint.synthesis。被拒 claim
有具体原因；程序保留已通过的 claim，只对被拒部分至多调用一次定向修复。
修复输入、原文、逐条结果和最终接受列表均落盘，恢复不重复生成已保存的响应。
结果区分有 claim、模型主动返回空、修复后为空、部分接受和仍然无效；请求错误
另记在 checkpoint.errors，不覆盖真实任务执行结果。
这是 fresh-start 协议，不导入旧卡或续跑旧版 L1 日志。

400 tokens 是软目标，800 是通常上限；核心超限标记并保留，不直接丢任务，不截断完整
关键证据。较大卡片可能使聚类或 L2 请求增大，需在小样本中检查实际上下文大小。
完整指导和反思只在审计中，卡片仍可能包含实际提交答案，不声称完全屏蔽 source GT。

每张经验卡的 execution 字段确定性保留所有已完成尝试的阶段、结果和终止原因。
evidence 保留被引用的动作/反馈，以及每次拒绝、末尾反馈和最多四条方法观察；单条
反馈最多 400 字符并显式标记截断。不包含 Thought 或完整监督 payload。
首次成功仍可 claims 为空；执行事实不自动升级为已验证的泛化经验。

## ALFWorld 专属边界

`l1/alfworld_contract.py` 的运行说明仅由 AlfworldAdapter 和 ALFWorld prompt 引用；
两段固定演示使用本 TextWorld 配置的 move 放置命令。持物容量、对象不可替换、空容器、
reset、预算及重复动作规则都属于 ALFWorld 说明，不进入 SearchQA prompt。
连续重复动作的终止规则保留，但记录为 repeated_action；reset 清除该原因及截断标志。
SearchQA 的执行指令、本地检索、评分与格式恢复路径保持原有行为；共享变化仅发生在
L1 证据组织及最终学习阶段。
