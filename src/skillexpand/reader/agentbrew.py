"""Read-only adapter for AgentBrew phase trials and versioned memory notes."""

from pathlib import Path

from .data import Card, Run, Trace, mapping, pretty, sequence


def note_skill(note, version=None, provenance=None):
    return {"skill_id": note.get("note_key", "?"), "name": note.get("note_key", "?"),
            "version": version, "description": note.get("trigger_pattern", ""),
            "body": "\n\n".join([note.get("trigger_pattern", ""), note.get("corrective_rule", ""),
                                    "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sequence(note.get("minimal_steps"))))]),
            "provenance": provenance or {}, "note": note}


def index(reader, root, label=None):
    base = root / "run" if (root / "run").is_dir() else root
    if not (base / "contract.json").exists():
        return
    contract = mapping(reader.optional(base / "contract.json", {}))
    method = mapping(contract.get("method"))
    label = label or f"AgentBrew · {base.parent.name}"
    for phase in ["vanilla", "initial", "ralph-r01", "ralph-r02", "ralph-r03", "serve"]:
        if phase in {"serve", "vanilla"}:
            units = sorted((base / phase).glob("rep*/*/request.json"))
        else:
            suffix = "initial" if phase == "initial" else "ralph/" + phase.split("-")[1]
            units = sorted((base / "brew").glob(f"*/{suffix}/request.json"))
        if not units:
            continue
        key = f"{base}#{phase}"
        run = Run(key, f"{label} · {phase}", base,
                  {"format": "agentbrew", "stage": phase, "contract": contract,
                   "executor_model": mapping(method.get("student")).get("model", ""),
                   "method_model": mapping(method.get("teacher")).get("model", "")},
                  mapping(reader.optional(base / "phases" / f"{phase}.json", {})))
        for path in units:
            request = mapping(reader.optional(path, {}))
            name = request.get("task") or path.parent.name
            run.tasks[name] = {"name": name, "text": ""}
            adopted = mapping(reader.optional(path.parent / "rollout.json", {}))
            # The progress ledger is authoritative when a recovery replaces rollout.json.
            for record in sequence(reader.optional(base / phase / "progress.json", [])):
                if record.get("task") == name and record.get("replicate", 0) == int(path.parent.parent.name.removeprefix("rep") or 0):
                    adopted = record
            slot = str(path)
            run.slots[slot] = {"task_id": name, "status": "valid", "reward": None,
                               "unit": str(path.parent), "record": adopted}
            result = adopted.get("raw_result_path")
            if result:
                data = mapping(reader.optional(Path(result), {}, "result"))
                run.slots[slot]["reward"] = mapping(mapping(data.get("verifier_result")).get("rewards")).get("reward")
            if not run.raw_model and adopted.get("trajectory_path"):
                try:
                    import ijson
                    with Path(adopted["trajectory_path"]).open("rb") as stream:
                        agent = next(ijson.items(stream, "agent"), {})
                    run.raw_model = agent.get("model_name", "")
                except (ImportError, OSError, ValueError):
                    pass
        run.manifest["expected_slots"] = len(units)
        for path in sorted((base / "brew").glob("*/draft/*/candidate.json")):
            note = mapping(reader.optional(path, {}))
            name = path.parents[2].name.split("-", 1)[-1]
            if name not in run.tasks:
                continue
            round_name = path.parent.name
            validation = mapping(reader.optional(path.parents[2] / "ralph" / round_name / "rollout.json", {}))
            state = mapping(reader.optional(path.parents[2] / "state.json", {}))
            raw = {"note": note, "task": {"name": name}, "execution": validation,
                   "claims": [note], "state": state, "candidate_path": str(path)}
            run.cards.append(Card(str(path), f"{name}/{round_name}/{note.get('note_key', '?')}",
                                  "AgentBrew 候选记忆", name, name, path, raw))
        frozen_path = base / "frozen/manifest.json"
        frozen = mapping(mapping(mapping(reader.optional(frozen_path, {})).get("payload")).get("memory"))
        for identity, entry in mapping(frozen.get("notes")).items():
            entry = mapping(entry)
            provenance = mapping(entry.get("provenance"))
            name = provenance.get("task", "")
            raw = {**entry, "task": {"name": name}, "execution": provenance.get("validation", {}),
                   "claims": [entry.get("note", {})], "memory_generation": frozen.get("generation")}
            run.cards.append(Card(f"{frozen_path}:{identity}", identity, "AgentBrew 冻结记忆", name, name, frozen_path, raw))
        reader.runs[key] = run


def traces(reader, run, tid):
    rows = []
    for slot in run.slots.values():
        if slot["task_id"] != tid:
            continue
        unit = Path(slot["unit"])
        adopted = slot["record"]
        request = mapping(reader.optional(unit / "request.json", {}))
        retrieval = mapping(request.get("retrieval"))
        selection = {**retrieval, "request_path": str(unit / "request.json"),
                     "why": pretty({**{k: retrieval.get(k) for k in ("scopes", "scope_scores", "memory_generation")},
                                    "selected_keys": [mapping(n).get("key") for n in sequence(retrieval.get("selected"))]})}
        paths = set(unit.glob("physical*/jobs/*/*/agent/trajectory.json"))
        if adopted.get("trajectory_path"):
            paths.add(Path(adopted["trajectory_path"]))
        for path in sorted(paths):
            rows.append(Trace(str(path), tid, tid, unit.parent.name,
                              f"{path.parents[4].name}/{path.parents[1].name}", path,
                              path.parent.parent / "result.json", model=run.raw_model,
                              accepted=str(path) == adopted.get("trajectory_path"),
                              origin=f"AgentBrew {run.manifest['stage']}", selection=selection,
                              exception=adopted.get("exception"), request_dir=unit))
    return rows


def notes(reader, run):
    base = run.path
    paths = [(p, "记忆历史") for p in sorted((base / "memory/versions").glob("*.json"))]
    paths += [(base / "frozen/manifest.json", "冻结记忆")]
    rows = []
    for path, kind in paths:
        data = mapping(reader.optional(path, {}))
        if kind == "冻结记忆":
            data = mapping(mapping(data.get("payload")).get("memory"))
        for key, entry in mapping(data.get("notes")).items():
            entry = mapping(entry)
            note = mapping(entry.get("note"))
            generation = data.get("generation")
            skill = note_skill(note, entry.get("version"), entry.get("provenance"))
            skill["provenance"] = {**skill["provenance"], "source_experience_ids": [key]}
            skill.update({"generation": generation, "raw_entry": entry})
            rows.append({"id": f"{path}:{key}", "key": f"{key}@v{entry.get('version')} / generation {generation}",
                         "kind": kind, "path": str(path), "skill": skill})
    return rows


def batches(reader, run):
    rows = []
    for card in run.cards:
        if card.kind != "AgentBrew 候选记忆":
            continue
        raw = card.data
        validation = mapping(raw.get("execution"))
        state = mapping(raw.get("state"))
        curator_path = card.path.parents[2] / "curator/decision.json"
        curator = reader.optional(curator_path, {})
        candidate = {"candidate_id": card.identity, "skill": note_skill(raw["note"])}
        data = {"batch_id": card.identity, "round": card.path.parent.name, "skill_id": raw["note"].get("note_key"),
                "experience_ids": [card.identity], "task_ids": [card.task_id],
                "proposals": [{"edit": {"candidate": candidate}}],
                "outcome": state.get("disposition", "未知"), "acceptance_mode": "real_validation",
                "acceptance": {"validation": validation, "task_state": state,
                               "curator_path": str(curator_path), "curator": curator},
                "reason": "AgentBrew ralph 验证记录；采纳状态见 task_state 和冻结记忆 provenance。",
                "candidate_path": str(card.path)}
        rows.append({"id": card.key, "path": str(card.path), "root": str(run.path), "data": data})
    return rows
