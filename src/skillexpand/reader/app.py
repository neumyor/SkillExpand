"""Chinese Gradio UI for the read-only TB2.1 artifact reader."""

from __future__ import annotations

import argparse
from pathlib import Path

from .data import (Reader, evidence_step, evidence_trial, mapping, matching_steps,
                   pretty, sequence, skill_diff, trajectory_html)


CSS = """
.gradio-container { max-width: 1500px !important; }
.trace-step { border-bottom: 1px solid #d5dde0; padding: 12px 0 18px; }
.trace-step h3 { font-size: 15px; color: #166552; margin: 0 0 10px; }
.trace-step pre { font: 13px/1.65 ui-monospace, monospace; white-space: pre-wrap;
  overflow-wrap: anywhere; padding: 10px; background: #f4f6f7; color: #1f2933; }
.trace-step details { margin: 8px 0; }
.trace-step summary { cursor: pointer; font-size: 13px; color: #47545c; }
.field-label { font-size: 12px; color: #47545c; margin-top: 8px; }
.trace-pane { max-height: 760px; overflow: auto; padding: 0 10px; }
.empty { padding: 24px; color: #68747b; }
footer { display: none !important; }
"""


def build_demo(root: Path):
    import gradio as gr

    reader = Reader(root)

    def dropdown(choices, value=None):
        values = [x[1] if isinstance(x, tuple) else x for x in choices]
        return gr.update(choices=choices, value=value if value in values else values[0] if values else None)

    def traces(run, tid):
        return reader.traces(run, tid)

    def trace_by_key(run, tid, key):
        return next((t for t in traces(run, tid) if t.key == key), None)

    def detail(run, tid, key):
        return reader.trace_detail(trace_by_key(run, tid, key))

    def step_options(data, query=""):
        return [(f"步骤 {data['steps'][i].get('step_id', i + 1)} · {data['steps'][i].get('source', '?')}", str(i))
                for i in matching_steps(data.get("steps", []), query)]

    def trace_payload(run, tid, key):
        d = detail(run, tid, key)
        errors = "\n\n".join(str(d.get(k) or "") for k in ("error", "result_error") if d.get(k))
        summary = (f"**真实 verifier：{d.get('outcome', '未解决')} · reward={d.get('reward')}**"
                   f" · {'槽位采用' if d.get('accepted') else '来源 / 历史请求'}\n\n"
                   f"模型：`{d.get('model', '')}`\n\n轨迹：`{d.get('path', '')}`\n\n"
                   f"结果：`{d.get('result_path', '')}`\n\n{errors}")
        meta = {k: v for k, v in d.items() if k not in {"raw", "steps"}}
        return summary, dropdown(step_options(d)), trajectory_html(d.get("steps", [])), meta, "", ""

    def render_steps(run, tid, key, step, query):
        d = detail(run, tid, key)
        return trajectory_html(d.get("steps", []), int(step or 0), query)

    def search_steps(run, tid, key, query):
        d = detail(run, tid, key)
        opts = step_options(d, query)
        return dropdown(opts), trajectory_html(d.get("steps", []), int(opts[0][1]) if opts else 0, query)

    def move_steps(delta, run, tid, key, step, query):
        d = detail(run, tid, key)
        indexes = matching_steps(d.get("steps", []), query)
        if not indexes:
            return gr.update(value=None), trajectory_html([])
        current = int(step or 0)
        position = indexes.index(current) if current in indexes else 0
        index = indexes[max(0, min(len(indexes) - 1, position + delta))]
        return gr.update(value=str(index)), trajectory_html(d["steps"], index, query)

    def card_by_key(run, key):
        return next((c for c in reader.cards(run) if c.key == key), None)

    def card_payload(run, key):
        c = card_by_key(run, key)
        if not c:
            return "未选择记忆卡", {}, {}, [], [], {}, dropdown([]), dropdown([])
        content = c.content
        evidence = sequence(content.get("evidence"))
        trace_choices = []
        linked_runs = [run, reader.runs[run].source]
        if reader.runs[run].manifest.get("format") == "agentbrew":
            linked_runs += [k for k, r in reader.runs.items() if r.path == reader.runs[run].path]
        for rk in dict.fromkeys(linked_runs):
            if rk not in reader.runs:
                continue
            for t in reader.traces(rk, c.task_id):
                refs = pretty(c.data)
                if str(t.path) in refs:
                    trace_choices.append((f"{reader.runs[rk].label} · {t.label}", f"{rk}\n{t.key}"))
        trace_choices = list(dict.fromkeys(trace_choices))
        summary = (f"**{c.kind} · {c.identity}**\n\n模型：`{c.source_model or '见 execution / 原始记录'}`"
                   f"\n\n来源：`{c.path}`\n\n原始身份：`{c.data.get('original_experience_id', c.identity)}`")
        options = [(f"{e.get('id', i)} · {e.get('action', '')}", str(i))
                   for i, e in enumerate(evidence) if isinstance(e, dict)]
        return (summary, content.get("task", c.data.get("task", {})), content.get("execution", {}),
                evidence, content.get("claims", []), c.data, dropdown(trace_choices), dropdown(options))

    def skills_payload(run, selected):
        items = reader.skills(run)
        t = selected
        summary = "未选择执行请求"
        body = ""
        raw = {}
        if t:
            selected_keys = ", ".join(str(mapping(n).get("key", "?")) for n in sequence(t.selection.get("selected")))
            selected_name = selected_keys or ("无记忆" if "rendered_system" in t.selection else "未知")
            summary = f"选择 / 加载：`{t.selection.get('loaded_skill_key') or t.selection.get('skill_id', selected_name)}`\n\n"
            summary += str(t.selection.get("why") or t.selection.get("reason") or "")
            raw = t.selection
            if "rendered_system" in t.selection:
                body = t.selection["rendered_system"] or "（此请求未注入记忆）"
                summary += f"\n\n记忆 generation={t.selection.get('memory_generation')}；请求：`{t.selection.get('request_path')}`"
            elif t.skill_file:
                content, error = reader.cache.read(t.skill_file, "text")
                body = content or error
                summary += f"\n\n执行时挂载文件：`{t.skill_file}`"
            else:
                body = "缺少执行时挂载正文；请查看版本库及请求原始记录。"
        opts = [(f"{i['kind']} · {i['key']} · {i['skill'].get('name', '')}", i["id"]) for i in items]
        sa, sb = default_skills(items, t)
        return summary, body, raw, dropdown(opts, sa), dropdown(opts, sb)

    def default_skills(items, trace=None):
        if not items:
            return None, None
        selected_id = trace.selection.get("loaded_skill_id") or trace.selection.get("skill_id") if trace else None
        selected_id = selected_id or items[0]["skill"].get("skill_id")
        same = [i for i in items if i["skill"].get("skill_id") == selected_id] or items[:1]
        ordered = sorted(same, key=lambda i: (i["skill"].get("version", -1), i["kind"] == "冻结库"))
        return ordered[0]["id"], ordered[-1]["id"]

    def skill_by_key(run, key):
        return next((i for i in reader.skills(run) if i["id"] == key), None)

    def compare_skills(run, left, right):
        a, b = skill_by_key(run, left), skill_by_key(run, right)
        cards = reader.cards(run)
        if not a or not b:
            return "未选择 Skill 版本", "", "", "", {}, dropdown([])
        ids = set(sequence(mapping(b["skill"].get("provenance")).get("source_experience_ids")))
        linked = [(f"{c.kind} · {c.identity} · task {c.task_id}", c.key) for c in cards if c.identity in ids]
        paths = f"**A：{a['kind']} / {a['key']}**\n\n`{a['path']}`\n\n**B：{b['kind']} / {b['key']}**\n\n`{b['path']}`"
        return paths, a["skill"].get("body", ""), b["skill"].get("body", ""), skill_diff(a["skill"], b["skill"]), {"A": a["skill"], "B": b["skill"]}, dropdown(linked)

    def batch_by_key(run, key):
        return next((b for b in reader.batches(run) if b["id"] == key), None)

    def batch_payload(run, key):
        b = batch_by_key(run, key)
        if not b:
            return "尚无 L2 批次", {}, dropdown([]), dropdown([]), {}, "", "", ""
        v = b["data"]
        refs = set(sequence(v.get("experience_ids")))
        linked = [(f"{c.kind} · {c.identity}", c.key) for c in reader.cards(run) if c.identity in refs]
        candidates = []
        for proposal in sequence(v.get("proposals")):
            candidate = mapping(mapping(proposal.get("edit")).get("candidate")) if isinstance(proposal, dict) else {}
            if candidate:
                candidates.append(candidate)
        if not candidates and v.get("proposal_id"):
            for p in sorted((Path(b["root"]) / "l2_proposals" / str(v["proposal_id"])).glob("candidate-*.json")):
                c = mapping(mapping(reader.optional(p, {})).get("candidate"))
                if c:
                    candidates.append(c)
        opts = [(f"{c.get('candidate_id')} · {'已采纳' if c.get('candidate_id') == v.get('selected_candidate_id') else '候选'}", c.get("candidate_id")) for c in candidates]
        predicted = v.get("acceptance_mode") == "predicted" or mapping(v.get("acceptance")).get("mode") == "predicted"
        summary = (f"**round {v.get('round', '?')} · {v.get('skill_id', '')} · {v.get('outcome', '待处理')}**"
                   f"\n\n{'预测 review' if predicted else '见 acceptance 原始协议'} · "
                   f"empirically_validated={v.get('empirically_validated', '未知')}"
                   f"\n\n{v.get('reason', '')}\n\n`{b['path']}`")
        reviews = {k: v.get(k) for k in ("reviews", "acceptance", "review_errors", "candidate_aliases", "batch_patterns", "hypotheses")}
        first = candidates[0] if candidates else {}
        base = next((i["skill"] for i in reader.skills(run) if i["key"] == v.get("base_skill_key")), {})
        proposed = mapping(first.get("skill"))
        return summary, v, dropdown(linked), dropdown(opts), reviews, base.get("body", ""), proposed.get("body", ""), skill_diff(base, proposed) if proposed else ""

    def candidate_payload(run, batch, candidate):
        b = batch_by_key(run, batch)
        if not b:
            return "", "", ""
        v = b["data"]
        rows = [mapping(mapping(p.get("edit")).get("candidate")) for p in sequence(v.get("proposals")) if isinstance(p, dict)]
        if not rows and v.get("proposal_id"):
            rows = [mapping(mapping(reader.optional(p, {})).get("candidate"))
                    for p in sorted((Path(b["root"]) / "l2_proposals" / str(v["proposal_id"])).glob("candidate-*.json"))]
        selected = next((r for r in rows if r.get("candidate_id") == candidate), {})
        base = next((i["skill"] for i in reader.skills(run) if i["key"] == v.get("base_skill_key")), {})
        proposed = mapping(selected.get("skill"))
        return base.get("body", ""), proposed.get("body", ""), skill_diff(base, proposed) if proposed else ""

    def compare_payload(run, tid, other):
        name = reader.runs.get(run).tasks.get(str(tid), {}).get("name", "") if run in reader.runs else ""
        other_tid = reader.same_task(other, name)
        rows = reader.traces(other, other_tid) if other_tid is not None else []
        return dropdown([(t.label, t.key) for t in rows]), f"同题：**{name}** · {'已匹配' if other_tid is not None else '该实验无同名任务'}"

    def render_compare(run, tid, left, other, right):
        current = reader.runs.get(run)
        name = current.tasks.get(str(tid), {}).get("name", "") if current else ""
        rt = reader.same_task(other, name)
        a, b = detail(run, tid, left), detail(other, rt, right)
        summary = (f"**{name}**\n\nA：{a.get('outcome', '未解决')} / reward={a.get('reward')} · `{a.get('path', '')}`"
                   f"\n\nB：{b.get('outcome', '未解决')} / reward={b.get('reward')} · `{b.get('path', '')}`")
        return summary, trajectory_html(a.get("steps", [])), trajectory_html(b.get("steps", [])), dropdown(step_options(a)), dropdown(step_options(b))

    def render_compare_right(run, tid, other, right, step):
        current = reader.runs.get(run)
        name = current.tasks.get(str(tid), {}).get("name", "") if current else ""
        rt = reader.same_task(other, name)
        return trajectory_html(detail(other, rt, right).get("steps", []), int(step or 0))

    runs = reader.run_choices()
    initial_run = runs[0][1] if runs else None
    tasks = reader.task_choices(initial_run)
    initial_task = tasks[0][1] if tasks else None
    with gr.Blocks(title="TB2.1 轨迹阅读器", css=CSS, analytics_enabled=False, theme=gr.themes.Default(primary_hue="emerald", neutral_hue="gray")) as demo:
        gr.Markdown("## TB2.1 轨迹阅读器")
        with gr.Row():
            run_select = gr.Dropdown(runs, value=initial_run, label="实验 / 修订", scale=4)
            task_select = gr.Dropdown(tasks, value=initial_task, label="任务", scale=3)
            refresh = gr.Button("刷新", scale=1)
        with gr.Row():
            query = gr.Textbox(label="任务搜索", scale=3)
            models = sorted({str(v) for r in reader.runs.values() for v in reader.roles(r).values() if v} |
                            {str(x.get("source_model")) for r in reader.runs.values() for x in r.raw if x.get("source_model")})
            model_filter = gr.Dropdown([("全部模型", "")] + [(x, x) for x in models], value="", label="模型", scale=2)
            status_filter = gr.Radio(["全部", "成功", "失败", "未解决"], value="全部", label="包含的执行结果", scale=2)
        run_summary = gr.Markdown()
        with gr.Tabs() as tabs:
            with gr.Tab("轨迹", id="trajectory"):
                trace_select = gr.Dropdown(label="attempt / request / 来源")
                trace_summary = gr.Markdown()
                with gr.Row():
                    step_query = gr.Textbox(label="步骤搜索", scale=3)
                    step_select = gr.Dropdown(label="跳转步骤", scale=3)
                    previous = gr.Button("←", scale=0, min_width=42)
                    following = gr.Button("→", scale=0, min_width=42)
                timeline = gr.HTML(elem_classes=["trace-pane"])
                with gr.Accordion("轨迹元信息 / 用量 / 选择理由", open=False):
                    trace_meta = gr.JSON()
                with gr.Accordion("原始 ATIF JSON", open=False):
                    load_raw = gr.Button("加载原始 JSON")
                    trace_raw = gr.Code(language="json", interactive=False, lines=15, max_lines=30, wrap_lines=True)
            with gr.Tab("记忆卡", id="cards"):
                card_select = gr.Dropdown(label="来源卡 / 发现卡 / 演化卡")
                card_summary = gr.Markdown()
                with gr.Row():
                    card_trace = gr.Dropdown(label="关联轨迹", scale=3)
                    evidence_select = gr.Dropdown(label="Evidence", scale=3)
                    jump = gr.Button("打开轨迹", scale=1)
                jump_status = gr.Markdown()
                with gr.Row():
                    card_task = gr.JSON(label="Task")
                    card_execution = gr.JSON(label="Execution")
                card_evidence = gr.JSON(label="Evidence")
                card_claims = gr.JSON(label="Claims")
                with gr.Accordion("原始卡片 JSON", open=False):
                    card_raw = gr.JSON()
            with gr.Tab("Skill / 记忆版本", id="skills"):
                loaded_summary = gr.Markdown()
                loaded_body = gr.Code(label="执行时挂载的正文", interactive=False, language="markdown", wrap_lines=True)
                with gr.Accordion("Selector 原始记录", open=False):
                    selection_raw = gr.JSON()
                with gr.Row():
                    skill_a = gr.Dropdown(label="版本 A")
                    skill_b = gr.Dropdown(label="版本 B")
                skill_paths = gr.Markdown()
                with gr.Row():
                    skill_body_a = gr.Code(label="A 正文", interactive=False, language="markdown", wrap_lines=True)
                    skill_body_b = gr.Code(label="B 正文", interactive=False, language="markdown", wrap_lines=True)
                skill_changes = gr.Code(label="正文差异", interactive=False, wrap_lines=True)
                provenance_cards = gr.Dropdown(label="B 版本关联经验卡")
                open_provenance = gr.Button("打开经验卡")
                with gr.Accordion("名称 / 描述 / provenance / 原始版本", open=False):
                    skill_meta = gr.JSON()
            with gr.Tab("L2 / 记忆演化", id="evolution"):
                batch_select = gr.Dropdown(label="批次")
                batch_summary = gr.Markdown()
                with gr.Row():
                    batch_cards = gr.Dropdown(label="批次经验卡", scale=3)
                    open_batch_card = gr.Button("打开经验卡", scale=1)
                    candidate_select = gr.Dropdown(label="候选 / 采纳状态", scale=3)
                with gr.Row():
                    candidate_base = gr.Code(label="原版本", interactive=False, language="markdown", wrap_lines=True)
                    candidate_body = gr.Code(label="候选正文", interactive=False, language="markdown", wrap_lines=True)
                candidate_diff = gr.Code(label="候选差异", interactive=False, wrap_lines=True)
                review_raw = gr.JSON(label="Review / Acceptance / Hypotheses")
                with gr.Accordion("原始 batch journal", open=False):
                    batch_raw = gr.JSON()
            with gr.Tab("同题对比", id="compare"):
                with gr.Row():
                    compare_run = gr.Dropdown(runs, value=initial_run, label="对比实验 B")
                    compare_trace = gr.Dropdown(label="B attempt / request")
                    compare_button = gr.Button("比较", scale=0)
                compare_match = gr.Markdown()
                compare_summary = gr.Markdown()
                with gr.Row():
                    with gr.Column():
                        gr.Markdown("### A · 当前轨迹")
                        compare_step_a = gr.Dropdown(label="A 跳转步骤")
                        compare_left = gr.HTML(elem_classes=["trace-pane"])
                    with gr.Column():
                        gr.Markdown("### B · 对比轨迹")
                        compare_step_b = gr.Dropdown(label="B 跳转步骤")
                        compare_right = gr.HTML(elem_classes=["trace-pane"])
            with gr.Tab("实验总览", id="overview"):
                overview = gr.Dataframe(headers=["实验", "阶段 / 状态", "Selector", "Executor", "Evolver", "任务数", "有效槽位", "成功 / 有效", "Audit complete", "数据目录"],
                                        value=reader.overview(), interactive=False, wrap=True)
                run_raw = gr.JSON(label="Manifest / Status / Audits")

        trace_outputs = [trace_summary, step_select, timeline, trace_meta, trace_raw, step_query]
        card_outputs = [card_summary, card_task, card_execution, card_evidence, card_claims, card_raw, card_trace, evidence_select]
        skill_outputs = [skill_paths, skill_body_a, skill_body_b, skill_changes, skill_meta, provenance_cards]
        batch_outputs = [batch_summary, batch_raw, batch_cards, candidate_select, review_raw, candidate_base, candidate_body, candidate_diff]

        def populate(run, tid, other, chosen_trace=None, chosen_card=None):
            r = reader.runs.get(run)
            if not r:
                summary, raw = "没有可读取的实验", {}
            else:
                source = reader.runs.get(r.source)
                summary = f"**{r.label}** · task {tid}\n\n`{r.path}`"
                counts = {kind: sum(c.kind == kind for c in r.cards) for kind in dict.fromkeys(c.kind for c in r.cards)}
                summary += "\n\n" + " · ".join(f"{kind} {count}" for kind, count in counts.items() if count)
                if source:
                    summary += f"\n\n冻结库来源：**{source.label}**"
                if r.warnings:
                    summary += "\n\n" + "\n\n".join(r.warnings[:5])
                raw = {"manifest": r.manifest, "status": r.status,
                       "audit": reader.optional(r.path / "audit.json"),
                       "generation_source_audit": reader.optional(r.path / "generation_source_audit.json"),
                       "evolution_audits": {str(p): reader.optional(p) for p in r.path.glob("evolution/round-*/audit.json")}}
            ts = traces(run, tid)
            first = chosen_trace if chosen_trace in [t.key for t in ts] else ts[0].key if ts else None
            cs = [c for c in reader.cards(run) if c.task_id == str(tid) or c.kind == "AgentBrew 冻结记忆"]
            card = chosen_card if chosen_card in [c.key for c in cs] else cs[0].key if cs else None
            sp = skills_payload(run, trace_by_key(run, tid, first))
            skills = reader.skills(run)
            sa, sb = default_skills(skills, trace_by_key(run, tid, first))
            batches = reader.batches(run)
            preferred = next((b for b in batches if any(str(i) == str(tid) for i in sequence(b["data"].get("task_ids")))), batches[0] if batches else None)
            batch = preferred["id"] if preferred else None
            bp = batch_payload(run, batch)
            cmp = compare_payload(run, tid, other)
            return (summary, raw, dropdown([(t.label, t.key) for t in ts], first), *trace_payload(run, tid, first),
                    dropdown([(f"{c.kind} · {c.identity}", c.key) for c in cs], card), *card_payload(run, card),
                    *sp, *compare_skills(run, sa, sb),
                    dropdown([(f"round {b['data'].get('round')} · {b['data'].get('skill_id')} · {b['data'].get('outcome', '待处理')}", b["id"]) for b in batches], batch),
                    *bp, *cmp, "", "", "", "", dropdown([]), dropdown([]))

        populate_outputs = ([run_summary, run_raw, trace_select] + trace_outputs + [card_select] + card_outputs +
                            [loaded_summary, loaded_body, selection_raw, skill_a, skill_b] + skill_outputs +
                            [batch_select] + batch_outputs + [compare_trace, compare_match, compare_summary, compare_left, compare_right, jump_status, compare_step_a, compare_step_b])

        def filter_tasks(run, text, model, status):
            return dropdown(reader.task_choices(run, text, model, status))

        def refresh_all(current, text, model, status):
            nonlocal reader
            reader = Reader(root)
            choices = reader.run_choices()
            keys = [x[1] for x in choices]
            rk = current if current in keys else keys[0] if keys else None
            return dropdown(choices, rk), dropdown(reader.task_choices(rk, text, model, status)), reader.overview(), dropdown(choices, rk)

        for control in [run_select, query, model_filter, status_filter]:
            control.input(filter_tasks, [run_select, query, model_filter, status_filter], task_select).then(
                populate, [run_select, task_select, compare_run], populate_outputs)
        task_select.input(populate, [run_select, task_select, compare_run], populate_outputs)
        refresh.click(refresh_all, [run_select, query, model_filter, status_filter],
                      [run_select, task_select, overview, compare_run]).then(populate, [run_select, task_select, compare_run], populate_outputs)
        def trace_skills(run, tid, key):
            t = trace_by_key(run, tid, key)
            sp = skills_payload(run, t)
            sa, sb = default_skills(reader.skills(run), t)
            return *sp, *compare_skills(run, sa, sb)

        trace_select.input(trace_payload, [run_select, task_select, trace_select], trace_outputs).then(
            trace_skills, [run_select, task_select, trace_select], [loaded_summary, loaded_body, selection_raw, skill_a, skill_b] + skill_outputs)
        step_query.input(search_steps, [run_select, task_select, trace_select, step_query], [step_select, timeline])
        step_select.input(render_steps, [run_select, task_select, trace_select, step_select, step_query], timeline)
        load_raw.click(lambda r, t, k: pretty(detail(r, t, k).get("raw")), [run_select, task_select, trace_select], trace_raw)
        for button, delta in [(previous, -10), (following, 10)]:
            button.click(lambda r, t, k, s, q, d=delta: move_steps(d, r, t, k, s, q),
                         [run_select, task_select, trace_select, step_select, step_query], [step_select, timeline])
        card_select.input(card_payload, [run_select, card_select], card_outputs)
        for control in [skill_a, skill_b]:
            control.input(compare_skills, [run_select, skill_a, skill_b], skill_outputs)
        batch_select.input(batch_payload, [run_select, batch_select], batch_outputs)
        candidate_select.input(candidate_payload, [run_select, batch_select, candidate_select], [candidate_base, candidate_body, candidate_diff])

        def open_card(run, key, other):
            c = card_by_key(run, key)
            if not c:
                return gr.update(), gr.update(), *populate(run, None, other)
            tid = c.task_id if c.task_id in reader.runs[run].tasks else next(iter(reader.runs[run].tasks), None)
            return gr.update(selected="cards"), dropdown(reader.task_choices(run), tid), *populate(run, tid, other, chosen_card=c.key)

        for button, selector in [(open_provenance, provenance_cards), (open_batch_card, batch_cards)]:
            button.click(open_card, [run_select, selector, compare_run], [tabs, task_select] + populate_outputs)

        def jump_evidence(run, card_key, reference, evidence_index, other):
            c = card_by_key(run, card_key)
            if not c or not reference:
                return (gr.update(), gr.update(), gr.update(), *populate(run, None, other))
            target_run, trace_key = reference.split("\n", 1)
            target_task = reader.same_task(target_run, c.task_name) or c.task_id
            ts = traces(target_run, target_task)
            d = detail(target_run, target_task, trace_key)
            evidence = sequence(c.content.get("evidence"))
            e = evidence[int(evidence_index)] if evidence_index is not None and int(evidence_index) < len(evidence) else {}
            trial = evidence_trial(mapping(e))
            t = trace_by_key(target_run, target_task, trace_key)
            matched = evidence_step(mapping(e), d.get("steps", [])) if not trial or t and t.attempt == trial else None
            payload = list(populate(target_run, target_task, other, chosen_trace=trace_key))
            trace_offset = populate_outputs.index(trace_summary)
            if matched is not None:
                payload[trace_offset + 1] = dropdown(step_options(d), str(matched))
                payload[trace_offset + 2] = trajectory_html(d["steps"], matched)
            message = "已定位 Evidence 对应步骤" if matched is not None else "已打开轨迹；该 Evidence 无可核验的步骤映射，或不属于所选 attempt。"
            payload[trace_offset] += "\n\n" + message
            return (gr.update(selected="trajectory"), dropdown(reader.run_choices(), target_run),
                    dropdown(reader.task_choices(target_run), target_task), *payload)

        jump.click(jump_evidence, [run_select, card_select, card_trace, evidence_select, compare_run],
                   [tabs, run_select, task_select] + populate_outputs)
        compare_run.input(compare_payload, [run_select, task_select, compare_run], [compare_trace, compare_match])
        compare_button.click(render_compare, [run_select, task_select, trace_select, compare_run, compare_trace], [compare_summary, compare_left, compare_right, compare_step_a, compare_step_b])
        compare_step_a.input(lambda r, t, k, s: render_steps(r, t, k, s, ""), [run_select, task_select, trace_select, compare_step_a], compare_left)
        compare_step_b.input(render_compare_right, [run_select, task_select, compare_run, compare_trace, compare_step_b], compare_right)
        demo.load(populate, [run_select, task_select, compare_run], populate_outputs)
    return demo


def main():
    parser = argparse.ArgumentParser(description="Read-only Gradio reader for TB2.1 SkillExpand runs")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3] / "runs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    build_demo(args.root).queue(default_concurrency_limit=2).launch(server_name=args.host, server_port=args.port, share=False)


if __name__ == "__main__":
    main()
