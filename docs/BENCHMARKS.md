# Benchmark 与 L1 接口

L1 面向 `train` task 生成 task-local 经验卡；`val` 和 `test` 的任务不会进入经验提取或 Skill 编辑。每次尝试结束后，程序先确定性保存动作和环境反馈，再决定反思、可选指导或结束。

`src/skillexpand/l1/adapters.py::Adapter` 定义 benchmark 接口：

| 接口 | 责任 |
|---|---|
| `tool_semantics` | 工具和环境语义；也进入反思与经验提取上下文 |
| `configure(agent)` | 安装执行指令和工具 |
| `build_feedback(agent, trial)` | 生成可序列化的 benchmark 反馈 |
| `prepare_guidance(agent, trials)` | train 自主预算耗尽后的可选指导 |
| `reflection_prompt(guided, extraction)` | 反思及结构化经验提取契约 |
| `selector_prompt(benchmark)` | 只根据 task 和 Skill description 路由 val/test |
| `repeated_action_is_stalled(events, action)` | 判断重复动作是否无进展 |
| `evidence_event(event)` | 把完整观察标注为 method、observed、rejected 或 score |

SearchQA 使用 Search、Lookup、Finish；自主预算耗尽后可以使用 benchmark 提供的 train 指导。ALFWorld 使用环境动作和反馈，没有答案指导。环境评分不等于下一步动作的 ground truth，不能把参考轨迹同序号动作当成当前状态的答案。

## 经验卡

卡片由程序保存 task、split、Skill provenance、尝试结果、动作/观察证据、claim、claim 状态和审计路径。LLM 只能从实际证据中提出 task-local claim；程序检查 evidence ID、phase、effect、method 和 success/failure 约束。失败、重试和 supervised repair 都保留，后续尝试不会自动变成可复用规则。

L2 的 train-card Reviewer 读取卡片的 `projection()`：它保留 task、execution、claims 和带 evidence ID 的证据，同时隐藏未请求的运行目录细节。predicted-val Reviewer 不读取 projection，只读取 task 文本和 Skill。

## Skill 角色配置

冷启动、L1 执行器、L2 Planner、rewrite Editor、L2 Reviewer、L2 Verifier（sampled 协议的判定者）、selector 可以在配置中分别指定模型：

```yaml
models:
  l1_executor: executor-model
  cold_start: synthesis-model
  l2_planner: planner-model
  l2_editor: editor-model
  l2_reviewer: reviewer-model
  l2_verifier: verifier-model
  selector: selector-model
```

对应 CLI 参数是 `--l1-model`、`--cold-start-model`、`--l2-planner-model`、`--l2-editor-model`、`--l2-reviewer-model`、`--l2-verifier-model` 和 `--selector-model`。

## L1 预算

冷启动和每轮 Evolve 的 autonomous/supervised 次数均可配置：`--autonomous-attempts`、`--supervised-attempts`。Evolve 会把当前 Skill 注入 train L1，再将该轮新卡交给 L2；每一轮卡片独立保存。
