# 串行、离线的 Skill 演化

## 数据边界

任何模型执行之前，固定互斥的 source、admission、final 集合。默认全局 50/25/25；
可以提供显式划分。原生任务类别不决定 Skill。

source 提供经验和 Skill 内容；admission 保留历史划分，但当前 L2 不读取或执行它；
final 只在明确启动 final 阶段时运行。held-out 的题目、答案和执行轨迹不作为 L2/L3 经验输入。

## L1 与冷启动建库

1. 所有 source task 不带 Skill 运行 L1：默认至多 4 次自主尝试（包含首次），
   失败后反思并从重置后的环境重试；有可用指导时至多再辅助尝试一次。
2. 每题结束后，程序从已完成尝试的实际动作/反馈组装 schema 5 经验卡；模型只给至多两条任务局部 claim，可返回空。逐条校验并保留合格 claim；对被拒部分至多发起一次定向修复。首次成功和最终失败都保存；拒绝动作、方法观察及终止反馈由程序保留。正向 procedure 须引用同一次成功尝试中的方法证据。重试假设不晋升为经验。初稿、拒绝原因、修复稿和最终结果分别落盘，恢复不重采样已保存的响应。
3. 从所有经验卡提取能力标签，提出簇，再逐题审核唯一归属。每题恰好属于一个非空簇。
4. 每簇卡片固定分批。多卡批次先产生引用至少两张不同卡及真实方法或拒绝证据 ID 的 pattern 候选；纯评分反馈不足以支持 pattern。单卡批次不伪造重复支持。候选连同卡片用于合成非空 description/body，并冻结 source task→Skill 映射。归纳仅覆盖同一批次，不声称发现跨批次的重复机制。
5. 发布初始技能库及冷启动完成标记。初始 Skill 不声称已经通过 admission 验收。

这些步骤全部完成后才能进入 L2。冷启动执行可以并发，但不会与 L2 同时运行。

每个 evolve round 都先读取当前 Skill head，沿用冷启动的固定 task→family 路由，
只在 source 上重新运行一次 Skill-aware L1。该轮生成的卡片单独保存在
`evolution/round-N/cards`，旧轮卡片只用于审计；随后 L2 只消费这一轮卡片并提交新的
Skill version。`--evolve-rounds N` 串行重复该闭环。

## 已有结果作为输入

`artifacts.load_cold_start` 检查 split、实际任务数据、完成标记、映射、初始库 hash、
逐题卡片覆盖、唯一经验 ID 和初始 Skill 的来源是否一致。

`--cold-start-dir` 可将本协议已完成的冷启动输入导入新目录，保存原路径和输入 hash，
复制配置、划分、聚类、初始 Skill 和卡片。它不支持旧版经验卡或运行日志。
原始无 Skill 执行的 `initial_skill_key` 和 `selected_skill_id` 仍为 null；
L2 在内存中依据冻结映射补充 family 归属，不伪造成带 Skill 的执行。
完整原始审计仍由卡片的原始审计路径指向原运行。

L2 从第一次运行开始冻结代码、配置与证据；之后恢复不得混用不同实现。

## L2：不同修改机制与独立经验卡评审

按 Skill ID、source task_id 顺序处理，每批最多 50 张卡，K 默认 3。
每批只使用当前轮的 Skill-aware 卡、当前 Skill 与当前批次卡，不引入其他轮卡、拒绝反馈或
admission/final 信息。冷启动卡只用于初始 Skill 合成；它们不再作为 evolve 的 L2 输入。

1. 多卡批次先归纳最多三条跨卡 pattern 候选，引用至少两张不同卡的真实证据 ID；它们是编辑线索，不是已证实的规则。编辑器检查完整现有规则的覆盖、条件、顺序及 AND/OR 分支，排除已覆盖的需求；再提出至多 K 个不同的行为修改假设，引用卡片 evidence ID，说明修改机制及目标规则。
2. 逐个生成 body，后续生成看到前面已生成的修改机制与完整 diff，避免重复。
   生成前复核假设；若原规则已覆盖则返回 no_change，每项修改须符合该假设，保留无关规则。
   description 字节级冻结。无支持可少于 K 个或不修改，不无限重试。
3. 对相同 body 去重；每个原始响应、假设和完整 diff 持久化。
4. 新建独立 reviewer host。每次仅发送当前规则、按内容哈希重排的所有匿名候选规则与本批的一张卡。
   不发送编辑策略、生成理由、候选编号含义或历史评审结果。
5. 程序遍历本批所有卡；reviewer 每次对所有候选输出 improve / regress / unchanged / unknown。
   卡片直接使用其 evidence ID，当前规则使用 B1...，候选规则使用 C1R1...；保留动作、反馈与完整规则。
   改善与回退必须引用本卡证据 ID 及有变化的规则 ID，并解释机制。程序拒绝漏候选、重复 ID、
   越界 ID、跨候选规则和仅引用未变规则的方向性判断。删除规则可以引用当前版本的规则 ID。
   仅换措辞应判 unchanged；不再输出未经验证的全局行为等价标记。合法 ID 不证明因果推断正确。
6. 目标是这道题的预期 benchmark 成功：改善指当前规则预计失败、候选预计成功，回退反之；
   两者预计均成功或均失败则不变，证据不足则未知。不把未触发的备用规则或单纯省步骤计为改善。
   汇总预计改善 I、回退 R、无法判断 U；净收益 N=I-R。相同 body 已在评审前去重。
   采用保守选择：N-U>0 才可入选；最高 N 候选的下界 N-U 必须严格超过其他候选的
   上界 N+U。并列或区间重叠保留当前版本。这是保守决策口径，不是统计置信区间。
7. 每张卡的原始评审立即落盘；全部卡合法后才汇总选择，再写整批事务，最后提交唯一获选版本。后续批次基于最新版本。

`review_approved` 只表示评审通过，`empirically_validated=false`。
这些计数是反事实预测，不是成功率、真实修复数或 admission 分数。
无修改、无不同候选、证据不足和不合法模型输出均记录为 hold。
假设格式不合法时最多修正一次；每张卡评审不合法时也最多修正一次。原响应与修正响应分别保留。
任意卡修正后仍不合法，整批 hold，不使用部分计数选择。每卡最多两次请求，不重抽合法判断。
服务错误中止并可恢复；恢复复用已落盘假设、候选和评审，不重新抽样。

## 冻结描述与 L3

本协议冻结 description，L3 自动更新暂停，编辑策略固定为 M@v0。
不使用过往 admission 评分优化编辑或评审，避免反馈循环迎合 reviewer。
保留历史记录 schema 和评分读取能力；停用的在线 L3 updater 与 pool 已删除。

## 独立 final

仅显式 final 阶段运行；all 只运行冷启动与 L2；初始 description 与 selector 冻结 final 分组。
评测只使用 Skill body，单次独立执行，没有 source 卡、反思和指导。
逐题保存结果、完整 trajectory 和 events；相同 body 复用缓存。
未路由成功题保留在整体分母，空 Skill 分组分数为 null。final 不反馈到 L2。
先前已用于诊断的 final 不再是全新确认集，后续机制有效性应在新的留出数据上检查。

## 落盘与恢复

| 文件 | 内容 |
|---|---|
| l2_manifest.json | 协议、输入、模型配置与代码指纹；旧协议必须新建目录 |
| l2_proposals/<hash>/hypotheses.json | 修改假设原始输出 |
| l2_proposals/<hash>/candidate-N.json | 每个候选完整内容与原始输出 |
| l2_proposals/<hash>/review-<card-hash>.json | 单卡、全部候选的原始评审；修正另存 -repair.json |
| l2_batches/<hash>.json | 假设、diff、匿名映射、逐卡判断、预测计数、选择理由和提交事务 |
| skills.jsonl | 初始及评审通过后的 Skill 历史 |
| meta_skills.jsonl | 固定 M@v0 |
| summary.json | 完成批次、候选数、评审通过数、异常输出计数；不伪装成实测指标 |
| usage/ | 分开的 editor/reviewer 请求与累计成本 |
| final/、routes/final/ | 独立真实评测与固定路由 |

不再生成当前 L2 的 admission 路由、panel_scores、patch_attempts 或 meta_decisions。

当前 prompt 要求 reviewer 先给出条件状态、动作差异、成功差异，再输出标签。
这仍是模型判断，并非程序证明；针对性验证及剩余局限见 [规则核对 prompt](RULE_REVIEW_PROMPTS.md)。
