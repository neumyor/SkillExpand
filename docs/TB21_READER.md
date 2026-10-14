# TB2.1 Gradio 阅读器

只读展示实验产物：轨迹、来源卡、执行经验卡、Skill 版本、L2 批次及同题对比。
中文界面保留原始英文文本。正文和日志不会发送给模型。

## jinan40 安装与启动

仓库：`/data2/liyishan/SkillExpand-tb21/SkillExpand`。
服务运行目录：`/data2/liyishan/SkillExpand-tb21/reader-runtime`。
使用单独的虚拟环境，不安装 SkillExpand 的评测依赖。

```bash
cd /data2/liyishan/SkillExpand-tb21/SkillExpand
python3 -m venv ../reader-runtime/venv
../reader-runtime/venv/bin/pip install -r requirements-reader.txt
python3 scripts/reader_service.py start
python3 scripts/reader_service.py status
```

默认仅监听服务器 `127.0.0.1:7860`，Gradio `share=False`。
在 Mac 终端建立转发：

```bash
ssh -N -L 7860:127.0.0.1:7860 jinan40
```

浏览器访问 <http://127.0.0.1:7860>。端口被占用时，可在本机改用
`ssh -N -L 7861:127.0.0.1:7860 jinan40`，访问 <http://127.0.0.1:7861>。

停止服务和查看日志：

```bash
cd /data2/liyishan/SkillExpand-tb21/SkillExpand
python3 scripts/reader_service.py stop
tail -n 80 ../reader-runtime/reader.log
```

停止脚本核验 PID 对应的命令及独立虚拟环境，仅终止阅读器。
SSH 转发可用 Ctrl-C 停止；转发与服务器阅读器的生命周期相互独立。

## 其他目录 / 前台运行

```bash
../reader-runtime/venv/bin/python scripts/read_tb21.py --root /path/to/runs --host 127.0.0.1 --port 7860
```

也可在已安装项目的环境中使用 `skillexpand-reader`。
默认根目录为本仓库 `runs/`；支持 runs 根目录或单个 run。
默认目录使用 E1–E7 正式面板清单，E7 父目录和 `revisions/178-cards-v1` 独立索引。
其他目录通过 manifest 发现实验及修订。原始来源从 manifest / ledger / 卡片引用注册。

## 数据含义

- 总览的有效/成功槽位来自 `status=valid` 且包含 0/1 reward 的 slot record；
  不把文件数、预测 review 或错误请求当成有效评测。原始来源没有槽位审计时显示待核验。
- 轨迹详情直接读取关联 `result.json` 的 `verifier_result.rewards.reward`。
  未写入或无法读取时显示未解决；保留 `exception_info`，包括真实 reward 伴随的 AgentTimeoutError。
- 同题匹配依据任务名称，不假设不同实验的 task_id 相同。
- 来源卡、发现卡及演化卡分开显示；来源命名空间和 original_experience_id 保留。
  E7 修正版的 178 张来源卡与其执行后经验卡是不同集合。
- Skill 页面优先展示当前请求目录内实际挂载的 SKILL.md；版本库、初始库及 library.json 冻结库分别标识。
  FINAL 页面可通过 source_run 浏览来源卡及 L2；不存在历史正文时显示缺失。
- Evidence 编号不是 ATIF step_id。只有显式 step_id 或唯一、完全匹配的消息内容可定位步骤，
  其他引用显示无法核验的原因；不会按编号猜测位置。
- 每页展示十个步骤，箭头翻页，步骤下拉框可直接跳转；思考和长工具输出可展开。
  同题对比两侧各有独立步骤选择器。原始 JSON 点击加载后保留未知字段。
- 点击刷新重新读取索引；正文缓存随文件 mtime 和 size 变化失效。
  写入中的不完整 JSON 显示暂不可读，其他对象继续可用。

## 定向验证

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src ../reader-runtime/venv/bin/python -m pytest tests/test_reader.py tests/test_reader_agentbrew.py -q
```

覆盖 ATIF 内容、HTML 转义、缓存刷新、不完整文件、重试与槽位采用、reward/timeout、
178 张来源卡身份、冻结版本、L2 来源、同题异 ID，以及 E1 旧恢复目录引用。
## AgentBrew 数据源

在数据根目录的 `external_roots.json` 中填写额外产物目录的 JSON 数组。
元素可以是路径字符串，也可以是包含 `path` 和 `label` 的对象。
阅读器还读取 `runs/*/manifest.json` 的 `output_root`；若同目录 `status.json`
指向不同的 `output_root`，原实验和重跑实验分别保留，并使用不同名称。
阅读器支持 AgentBrew `run/contract.json`、baseline、vanilla、initial、ralph-r01/r02/r03 和 serve，
其中 ACP baseline 使用 `baseline/<task>/request.json`，serve/vanilla 兼容带或不带 rep 目录的布局。
阶段仅在已有请求产物时出现；运行中生成新产物后点击刷新重新索引。
通过 progress / rollout 明确标识采用的 physical trial，真实评分读取 Harbor result。
“Skill / 记忆版本”展示 AgentBrew note 的历史 generation、note version 和冻结 manifest，
执行正文取 request.retrieval.rendered_system，候选与验证记录在记忆卡和演化页阅读。
当前记忆不会替代冻结记忆；新增文件需要数据所有者授予阅读器用户读取权限后手动刷新。
管理员可使用 `scripts/reader_artifact_acl.py ROOT UID` 为选定 JSON 产物设置用户 ACL。
