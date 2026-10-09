# L2 设计变更记录

这是一份历史审计索引。当前实现以 [ARCHITECTURE.md](ARCHITECTURE.md) 和 [L2_CARD_REVIEW_AUDIT.md](L2_CARD_REVIEW_AUDIT.md) 为准；本文不再描述已废弃的 split 或旧 acceptance 协议。

当前保留的设计约束是：

- train 经验卡与 val/test 严格隔离；
- 每个 family 的 train 卡固定分批，尾批也执行；
- Planner、Editor、Reviewer、Verifier（以及 L1 执行器、冷启动、selector）的模型角色可以独立配置；
- structured 模式使用程序可验证的 section/rule ID edit；
- predicted、empirical 和 sampled 都使用冻结 val route，并和旧 Skill 做 paired comparison；
- test 只做独立评测，不参与候选选择；
- 每个 task、请求、候选、acceptance 和提交事务逐单元落盘，可断点恢复；
- round audit 在下一轮或结果分析前执行。

过去的实验目录和历史报告可能仍含旧协议名称。它们不是当前代码可继续写入的输入；需要复现时，从当前代码新建运行目录并使用 `train/val/test` split。
