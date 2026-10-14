"""Render the completed fixed experiment from its independent offline audit."""
import argparse
from collections import Counter
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def sequence(values):
    return ' '.join('?' if x is None else str(int(x)) for x in values) or '未执行'


def display(value):
    return '未知' if value is None else str(value)


def measured(rows, key):
    values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
    return {'observed_sum': sum(values) if values else None,
            'known': len(values), 'unknown': len(rows)-len(values)}


def render(root):
    audit = read(root/'completion_audit.json')
    if not audit.get('passed') or not audit.get('protocol_complete'):
        raise ValueError('The independent completion audit has not passed')
    manifest = read(root/'manifest.json')
    baseline = {r['task']: r for r in audit['baseline']}
    arms = {(r['task'], r['arm']): r for r in audit['arms']}
    rows = audit['executor_slots']
    slots = audit['generation_slots']
    limits = audit['source_artifact_limitations']
    unresolved_causes = dict(Counter(r.get('error_type') or 'unknown' for r in rows if not r['valid']))
    results = {'format': 'three-task-final-report-v1', 'audit': 'completion_audit.json',
               'protocol_complete': True, 'protocol_passed': True,
               'source_artifacts_complete': not limits,
               'reward_measurement_complete': audit['unresolved_finished_trials'] == 0,
               'baseline': audit['baseline'], 'arms': audit['arms'],
               'executor_reserved': audit['reservation_audit']['executor_reserved'],
               'generation_reserved': audit['reservation_audit']['generation_reserved'],
               'generation_tokens': audit['generation_tokens'],
               'executor_tokens': audit['executor_tokens'],
               'duration_seconds': measured(rows, 'duration_seconds'),
               'rounds': measured(rows, 'rounds'),
               'tool_calls': measured(rows, 'tool_calls'),
               'valid_trials': audit['valid_trials'],
               'successful_trials': audit['successful_trials'],
               'unresolved_finished_trials': audit['unresolved_finished_trials'],
               'unresolved_error_types': unresolved_causes,
               'source_artifact_limitations': limits,
               'generation_statuses': dict(Counter(s['status'] for s in slots)),
               'model_calls': 0, 'evaluator_executions': 0}
    lines = ['# 三题历史输入消融结果', '',
        '固定协议已完成，独立离线审计通过。磁盘故障造成的未决结果与来源缺失全部保留；'
        '协议完成不等于所有测量证据完整，也不要求达到某个成功率。', '',
        f"实际消耗 {results['executor_reserved']}/81 个执行槽位、"
        f"{results['generation_reserved']}/12 个生成槽位；"
        f"有效执行 {audit['valid_trials']}，官方成功 {audit['successful_trials']}，"
        f"已结束但未决 {audit['unresolved_finished_trials']}。`?` 表示未决，不能记作零分。", '',
        '| 任务 | sanitized-original baseline | A 最终 | B 最终 | A/B 选中候选 |',
        '| --- | --- | --- | --- | --- |']
    for task in manifest['tasks']:
        a, b = arms[(task, 'A')], arms[(task, 'B')]
        lines.append(f"| {task} | {sequence(baseline[task]['rewards'])} | "
                     f"{sequence(a['final'].get('rewards', []))} | {sequence(b['final'].get('rewards', []))} | "
                     f"{a['selected']} / {b['selected']} |")
    lines += ['', '## 固定方法与精确输入', '',
        '共同对照采用 sanitized-original：保留原 E6 所选 Skill 的通用路由身份、名称和 description，'
        '统一移除历史学习 body/resources/provenance，仅保留 `Follow the public task requirements.`。'
        '它不是未经改动的旧 Skill。该选择、历史来源审计及全部输入在实测前冻结。', '',
        'A 保留预选 E1/E4/E6 各三次历史执行、原已加载 Skill 文本与存在的原始记忆卡；'
        '工具观察按固定顺序做 8192 字符首尾投影。B 只保留数值且非 bool 的 reward=0 行结构，'
        '统一移除全部记忆卡、历史已加载 Skill 文本以及命令、结果、叙述文本。'
        '两臂新开发反馈采用同一过滤器，先整体屏蔽受限评测步骤，再截断。', '',
        '| 任务 | 原始 Skill | A 历史行/记忆卡 | B 失败结构行/记忆卡 | 精确输入与来源 |',
        '| --- | --- | --- | --- | --- |']
    for task in manifest['tasks']:
        folder = root/'tasks'/task/'frozen'
        raw = read(folder/'input.json')
        a, b = read(folder/'evidence-A.json'), read(folder/'evidence-B.json')
        skill = raw['original_skill']
        lines.append(f"| {task} | {skill['skill_id']}@v{skill['version']} | "
                     f"{len(a['historical_evidence'])}/{len(a['frozen_memory'])} | "
                     f"{len(b['historical_evidence'])}/{len(b['frozen_memory'])} | "
                     f"`tasks/{task}/frozen/input.json`, `source_manifest.json`, `evidence-A.json`, `evidence-B.json` |")
    lines += ['',
        '原始面板分别是 `tb21-e1-empirical-final-library-20261008`、'
        '`tb21-e4-e3-final-library-empirical-20261010`、'
        '`tb21-e6-final-library-empirical-20261010`。E4 记忆实际来自 E3 DeepSeek discovery，'
        'E6 记忆来自 E6 discovery；E1 缺少记忆，未替换补入。历史执行原件在各任务 `frozen/sources/`，'
        '记忆原件在 `frozen/memory-E4.json`、`frozen/memory-E6.json` 留存。', '',
        f"原生模块为 `SkillEditor.propose_package`，来源提交 `{manifest['native_source_commit']}`；"
        f"独立审计核对 {audit['binding_files_checked']} 个冻结文件并用原生 prompt/proposal 方法重放记录的 completion。"
        '系统提示在任务及两臂间相同，没有按最终反馈编辑模块。', '',
        '每臂最多两个候选，每个可用候选恰有三个开发槽位；第二候选只读取本臂前一候选及其三个开发反馈。'
        '首候选无可用包时，第二候选从共同 seed 出发、没有新开发反馈。按有效官方开发成功数选最大者，'
        '并列选较早候选；随后冻结完整包并执行五个独立新环境。最终反馈未回流。', '',
        '执行模型为 `openai/qwen3.6-flash-distill`，生成请求模型为 `DEEPSEEK_up5zdj`。'
        'Qwen 参数、任务环境与官方 verifier 取自各任务 `frozen/executor.json`；'
        '7200 秒预算、top_p=0.95、max_tokens=65536、top_k=20、thinking/preserve/interleaved 保持冻结值。'
        'Harbor retry=0；原 provider HTTP 重试仍在，81 上限计执行 trial 而非底层 API 请求数。', '',
        '## 候选生成与开发选择', '',
        '| 任务/臂/候选 | 生成状态 | 开发奖励 | 选中 | 生成秒数 | 精确请求与响应 |',
        '| --- | --- | --- | --- | --- | --- |']
    for g in slots:
        arm = arms[(g['task'], g['arm'])]
        c = next(c for c in arm['candidates'] if c['number'] == g['number'])
        lines.append(f"| {g['task']}/{g['arm']}/{g['number']} | {g['status']} | "
                     f"{sequence(c['rewards'])} | {arm['selected'] == g['number']} | "
                     f"{display(g.get('seconds'))} | `{g['path']}/request.json`, `response.sse`, `completion.json`（若完整） |")
    lines += ['', '## 基础设施故障与证据限制', '',
        '远端 `/data2` 耗尽使部分 JSON/JSONL 变为空文件或截断，并中断四个首候选生成流。'
        '原驱动和相关进程均确认退出后，先完整备份，再离线恢复 wrapper。'
        '有完整官方 result、配置、完整挂载包、轨迹及新且唯一 snapshot 身份的结果才可恢复为有效；'
        '缺少任一必需证据则维持 unresolved/null。缺失官方源文件始终未改写。', '',
        '受损原字节保存在槽位 `infrastructure_originals/*.raw`，恢复说明在 `infra_recovery.json`。'
        '不完整生成流没有获得候选包、completion 或奖励。已消耗的执行/生成槽位没有重跑或替换。'
        '外部清理恢复空间后，`resume_dispatch.json` 继续原冻结 driver 的未用槽位；预算未重置。', '',
        f"独立审计列出 {len(limits)} 条来源限制，详见 `completion_audit.json:source_artifact_limitations`。"
        '这些是来源限制条目数，不等于失败 trial 数。完整轨迹中的测量值可以保留；'
        '损坏 provider 流的模型身份、重试计数和用量完整性仍为未知。', '',
        '所有未决并非同一原因。`InfrastructureWriteFailure` 记录是磁盘写入丢失；'
        '其他 ValueError/RuntimeError 按每次官方记录留存。例如 log B final/02 的官方异常为 '
        '`failed to send non-blocking keys: no server running on /tmp/tmux-0/default`，'
        '这是独立运行故障，未归入磁盘丢失或补跑。未决类型计数见 `final_results.json:unresolved_error_types`。', '',
        '## 用量与执行观察', '',
        '| 来源 | 计数 | 观测总量 | 已知记录 | 未知记录 | 完整 |',
        '| --- | --- | --- | --- | --- | --- |']
    for label, totals in (('生成', audit['generation_tokens']), ('执行', audit['executor_tokens'])):
        for key, value in totals.items():
            lines.append(f"| {label} | {key} tokens | {value['observed_sum']} | "
                         f"{value['known_records']} | {value['unknown_records']} | {value['complete']} |")
    for key in ('duration_seconds', 'rounds', 'tool_calls'):
        value = results[key]
        lines.append(f"| 执行 | {key} | {value['observed_sum']} | {value['known']} | "
                     f"{value['unknown']} | {value['unknown'] == 0} |")
    lines += ['',
        '以上总量为已观察部分，未知值未填零，不推算货币成本。duration_seconds 是各 trial 时长之和，'
        '包含并发重叠，不是研究墙钟耗时。每次执行的时长、轮数、'
        '请求/响应记录、返回模型身份与资源引用详见 `completion_audit.json:executor_slots`。', '',
        '`resource_analysis.json` 和 `resource_analysis.md` 独立列出资源读取、复制、调用命令、'
        '观察关联及公开工具错误步骤。损坏轨迹仅提取完整的步骤对象，明确标为部分证据，'
        '不用于恢复奖励或生成反馈。命令中的资源引用、调用或无错误输出均不能单独证明资源正确或因果收益。', '',
        '## 解释边界', '',
        '这是联合历史信息删除：B 同时丢弃有用的失败诊断，A/B 差异不能单独归因于成功轨迹、'
        '代码回显或泄漏。磁盘故障与大量未决进一步限制比较，不能据此给出可靠的精确增益。'
        '若 B 有有效官方成功，只支持该次执行无需被删除材料仍可完成任务；'
        '若只有 A 有成功，只能说明本协议下的信息差异敏感性。', '',
        '`multi-source-data-merger` 和 `log-summary-date-ranges` 曾用于此前诊断，'
        '三题及同题新环境都不构成盲测新任务泛化。未增加额外验收批次以追求成功。', '',
        '## 每次执行的真实结果', '',
        '| 槽位 | 有效 | 官方奖励 | 时长秒 | 轮数 |',
        '| --- | --- | --- | --- | --- |']
    for row in rows:
        lines.append(f"| `{row['path']}` | {row['valid']} | "
                     f"{'?' if row['reward'] is None else row['reward']} | "
                     f"{display(row.get('duration_seconds'))} | {display(row.get('rounds'))} |")
    lines.append('')
    (root/'final_results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2)+'\n')
    (root/'final_report.md').write_text('\n'.join(lines))
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    result = render(parser.parse_args().root)
    print(json.dumps({k: result[k] for k in ('protocol_complete', 'executor_reserved',
                     'generation_reserved', 'valid_trials', 'unresolved_finished_trials')}))
