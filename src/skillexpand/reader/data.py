"""File-backed index for experiment, card, execution and Skill provenance."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import difflib
import html
import json
from pathlib import Path
import re
from typing import Any


PRESETS = {
    "tb21-e1-progressive-formal-20261006-repair9-stream-concurrent": "E1 · 初始库 / L2",
    "tb21-e1-empirical-final-library-20261008": "E1_FINAL · 冻结库评测",
    "tb21-e3-e1aligned-free-library-20261009": "E3 · 初始库 / L2",
    "tb21-e3-final-library-empirical-20261010": "E3_FINAL · 冻结库评测",
    "tb21-e4-e3-final-library-empirical-20261010": "E4 · E3 冻结库 / Qwen 执行",
    "tb21-e5-e1aligned-free-library-20261009": "E5 · 双来源合并卡 / L2",
    "tb21-e5-final-library-empirical-20261010": "E5_FINAL · 已停止的历史面板",
    "tb21-e6-e1aligned-qwen-experience-deepseek-evolver-20261009": "E6 · 初始库 / L2",
    "tb21-e6-final-library-empirical-20261010": "E6_FINAL · 冻结库评测",
    "tb21-qwen-deepseek-library-qwen-selector-20261011": "E7 · 历史合并卡面板",
    "tb21-qwen-deepseek-library-qwen-selector-20261011/revisions/178-cards-v1": "E7 · 178 张独立卡修正版",
}


def pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def sequence(value: Any) -> list:
    return value if isinstance(value, list) else []


def mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def outcome(reward: Any) -> str:
    if reward is True or reward == 1:
        return "成功"
    if reward is False or reward == 0:
        return "失败"
    return "未解决"


def skill_key(skill: dict) -> str:
    return skill.get("key") or f"{skill.get('skill_id', '?')}@v{skill.get('version', '?')}"


class FileCache:
    """Bounded cache invalidated by nanosecond mtime and size; never repairs files."""

    def __init__(self, limit: int = 48):
        self.limit = limit
        self.values: OrderedDict = OrderedDict()

    def read(self, path: Path, kind: str = "json") -> tuple[Any, str]:
        try:
            before = path.stat()
            key = (str(path), before.st_mtime_ns, before.st_size, kind)
            if key in self.values:
                self.values.move_to_end(key)
                return self.values[key], ""
            if kind == "result":
                try:
                    import ijson
                except ImportError:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    value = {k: raw.get(k) for k in ("verifier_result", "exception_info")}
                else:
                    from ijson.common import JSONError
                    try:
                        with path.open("rb") as stream:
                            rewards = list(ijson.items(stream, "verifier_result.rewards.reward", use_float=True))
                            reward = rewards[0] if rewards else None
                            stream.seek(0)
                            exceptions = list(ijson.items(stream, "exception_info", use_float=True))
                            exception = exceptions[0] if exceptions else None
                    except JSONError as exc:
                        raise ValueError("Incomplete result") from exc
                    value = {"verifier_result": {"rewards": {"reward": reward}}, "exception_info": exception}
                text = ""
            else:
                text = path.read_text(encoding="utf-8")
            if kind == "result":
                pass
            elif kind == "text":
                value = text
            elif kind == "jsonl":
                value = [json.loads(line) for line in text.splitlines() if line.strip()]
            else:
                value = json.loads(text)
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                return None, f"暂不可读，可刷新（文件正在变化）：{path}"
            for old in list(self.values):
                if old[0] == str(path):
                    del self.values[old]
            # Very large raw trajectories remain readable without occupying the cache.
            if before.st_size <= 8_000_000 or kind == "result":
                self.values[key] = value
            while len(self.values) > self.limit:
                self.values.popitem(last=False)
            return value, ""
        except FileNotFoundError:
            return None, f"缺失文件：{path}"
        except (OSError, UnicodeError, ValueError) as exc:
            return None, f"暂不可读，可刷新：{path}（{type(exc).__name__}）"


@dataclass
class Card:
    key: str
    identity: str
    kind: str
    task_id: str
    task_name: str
    path: Path
    data: dict
    source_model: str = ""

    @property
    def content(self) -> dict:
        return mapping(self.data.get("experience_card")) or self.data


@dataclass
class Trace:
    key: str
    task_name: str
    task_id: str
    attempt: str
    request: str
    path: Path
    result_path: Path | None = None
    model: str = ""
    reward: Any = None
    accepted: bool = False
    origin: str = ""
    selection: dict = field(default_factory=dict)
    skill_file: Path | None = None
    exception: Any = None
    request_dir: Path | None = None

    @property
    def label(self) -> str:
        adopted = " · 槽位采用" if self.accepted else ""
        return f"attempt {self.attempt} / {self.request} · {self.model or '模型待核验'}{adopted}"


@dataclass
class Run:
    key: str
    label: str
    path: Path
    manifest: dict
    status: dict
    tasks: dict = field(default_factory=dict)
    slots: dict = field(default_factory=dict)
    raw: list = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    source: str = ""
    raw_model: str = ""
    warnings: list[str] = field(default_factory=list)


class Reader:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.cache = FileCache()
        self.runs: dict[str, Run] = {}
        self.refresh()

    def optional(self, path: Path, default=None, kind="json"):
        if not path.exists():
            return default
        value, _ = self.cache.read(path, kind)
        return default if value is None else value

    def resolve(self, path: Any, base: Path) -> Path | None:
        if not isinstance(path, str) or not path:
            return None
        path = path.removeprefix("file://")
        value = Path(path).expanduser()
        return value if value.is_absolute() else base / value

    def refresh(self):
        self.cache.values.clear()
        self.runs = {}
        candidates = [(self.root / name, label) for name, label in PRESETS.items()]
        if not any(p.is_dir() for p, _ in candidates):
            if (self.root / "manifest.json").exists():
                candidates = [(self.root, self.root.name)]
            else:
                candidates = [(p.parent, p.parent.name) for p in sorted(self.root.glob("*/manifest.json"))]
                candidates += [(p.parent, str(p.parent.relative_to(self.root)))
                               for p in sorted(self.root.glob("*/revisions/*/manifest.json"))]
        for path, label in candidates:
            if not path.is_dir():
                continue
            manifest, error = self.cache.read(path / "manifest.json")
            key = str(path)
            run = Run(key, label, path, mapping(manifest),
                      mapping(self.optional(path / "status.json", {})))
            if error:
                run.warnings.append(error)
            run.source = str(run.manifest.get("source_run") or "")
            self._index(run)
            self.runs[key] = run
        self._raw_runs()
        from . import agentbrew
        roots = self.optional(self.root / "external_roots.json", [])
        if (self.root / "run/contract.json").exists():
            roots = [str(self.root)] + sequence(roots)
        for external in sequence(roots):
            agentbrew.index(self, Path(external))

    def _index(self, run: Run):
        table = sequence(self.optional(run.path / "tasks.json", []))
        if not table:
            benchmark = mapping(mapping(run.manifest.get("config")).get("benchmark"))
            path = self.resolve(run.manifest.get("task_file") or benchmark.get("task_file"), run.path)
            table = sequence(self.optional(path, [])) if path else []
        for i, row in enumerate(table):
            if not isinstance(row, dict):
                continue
            tid = str(row.get("task_id", i))
            env = mapping(row.get("env_kwargs"))
            run.tasks[tid] = {"name": row.get("task_name", env.get("task_name", tid)),
                              "text": row.get("instruction", row.get("task", ""))}
        if not table:
            for i, name in enumerate(sequence(run.manifest.get("task_names"))):
                run.tasks[str(i)] = {"name": name, "text": ""}
        raw = self.optional(run.path / "raw_inputs.json", [])
        run.raw = sequence(raw)
        imported = mapping(run.manifest.get("trajectory_import"))
        if not run.raw:
            run.raw = sequence(imported.get("ledger"))
        source_cards, error = (self.cache.read(run.path / "source_cards.json")
                               if (run.path / "source_cards.json").exists() else ([], ""))
        if error:
            run.warnings.append(error)
        for i, row in enumerate(sequence(source_cards)):
            if isinstance(row, dict):
                self._card(run, row, run.path / "source_cards.json", f"source:{i}", "来源卡")
        for pat, kind in [("discovery/results/*.json", "发现卡"),
                          ("evolution/round-*/cards/*.json", "演化卡")]:
            for path in sorted(run.path.glob(pat)):
                value, error = self.cache.read(path)
                if error:
                    run.warnings.append(error)
                if isinstance(value, dict):
                    self._card(run, value, path, str(path), kind)
        for row in run.raw:
            if not isinstance(row, dict):
                continue
            tid = str(row.get("task_id", "?"))
            run.tasks.setdefault(tid, {"name": row.get("task_name", tid), "text": ""})
        for path in sorted(run.path.glob("slots/*/record.json")):
            record, error = self.cache.read(path)
            if error:
                run.warnings.append(error)
                continue
            if not isinstance(record, dict):
                continue
            slot = path.parent.name
            tid = str(record.get("task_id", slot.split("-")[0]))
            tid = str(int(tid)) if tid.isdigit() else tid
            run.slots[slot] = record
            run.tasks.setdefault(tid, {"name": record.get("task_name", tid), "text": ""})
        for path in sorted(run.path.glob("slots/*")):
            if not path.is_dir() or path.name in run.slots or "-" not in path.name:
                continue
            tid, attempt = path.name.rsplit("-", 1)
            tid = str(int(tid)) if tid.isdigit() else tid
            run.slots[path.name] = {"task_id": tid, "attempt_index": attempt, "status": "pending"}
            run.tasks.setdefault(tid, {"name": tid, "text": ""})

    def _card(self, run, row, path, key, kind):
        content = mapping(row.get("experience_card"))
        task = mapping(content.get("task"))
        tid = str(row.get("task_id", task.get("task_id", "?")))
        env = mapping(row.get("env_kwargs"))
        name = row.get("task_name") or env.get("task_name") or run.tasks.get(tid, {}).get("name", tid)
        identity = str(row.get("source_card_id") or row.get("experience_id") or content.get("card_id") or key)
        card = Card(key, identity, kind, tid, name, path, row, str(row.get("source_model", "")))
        run.cards.append(card)
        text = task.get("text", row.get("task", ""))
        run.tasks.setdefault(tid, {"name": name, "text": text if isinstance(text, str) else pretty(text)})

    def _raw_runs(self):
        sources = {}
        for run in list(self.runs.values()):
            for row in run.raw:
                if not isinstance(row, dict) or not row.get("trajectory_path"):
                    continue
                p = self.resolve(row["trajectory_path"], run.path)
                if p and len(p.parents) >= 3:
                    sources.setdefault(str(p.parent.parent.parent), {})[str(p)] = row
            for card in run.cards:
                for row in sequence(card.data.get("l1_trials")):
                    if not isinstance(row, dict) or not row.get("result_path"):
                        continue
                    p = self.resolve(row["result_path"], run.path)
                    # Only imported no-Skill trials become baseline experiments.
                    if card.kind != "发现卡" or card.data.get("selected_skill_id"):
                        continue
                    tp = self.resolve(row.get("trajectory"), run.path)
                    if tp and p:
                        if tp.is_dir() or tp.name != "trajectory.json":
                            tp = tp / "agent" / "trajectory.json"
                        root = p.parent.parent
                        sources.setdefault(str(root), {})[str(tp)] = {
                            "task_id": card.task_id, "task_name": card.task_name,
                            "attempt_index": row.get("source_attempt_index", row.get("index")),
                            "trajectory_path": str(tp), "result_path": str(p),
                            "source_model": row.get("source_model", ""),
                            "reward": row.get("verifier_reward"),
                        }
        e8 = self.root / "tb21-e8-deepseek-max-1m-20261012/full_config.json"
        if e8.exists():
            config = mapping(self.optional(e8, {}))
            job = Path(config.get("jobs_dir", "")) / config.get("job_name", "")
            rows = sources.setdefault(str(job), {})
            attempts = {}
            audited = mapping(self.optional(e8.parent / 'execution_audit.json', {}))
            accepted_paths = {row['trial']: Path(row['result'])
                              for row in audited.get('rows', [])
                              if row.get('reward') in (0, 1)}
            for result_path in sorted(job.glob("*/result.json")):
                result_path = accepted_paths.get(result_path.parent.name, result_path)
                result = mapping(self.optional(result_path, {}))
                task = result.get("task_name", result_path.parent.name.split("__")[0])
                attempts[task] = attempts.get(task, 0) + 1
                trajectory = result_path.parent / "agent/trajectory.json"
                rows[str(trajectory)] = {
                    "task_id": task, "task_name": task, "attempt_index": attempts[task],
                    "trajectory_path": str(trajectory), "result_path": str(result_path),
                    "source_model": "deepseek-v4-flash-0731",
                    "reward": mapping(mapping(result.get("verifier_result")).get("rewards")).get("reward"),
                }
        for key, rows in sources.items():
            values = list(rows.values())
            models = sorted({str(v.get("source_model", "")) for v in values})
            model = " / ".join(models)
            label = ("E2 · DeepSeek 原始来源" if "deepseek" in model.lower()
                     else "Qwen 原始来源") + f" · {Path(key).name}"
            if Path(key).name == "tb21_e8_deepseek_max_1m_raw_20261012":
                label = "E8 · DeepSeek max / 1M 无 Skill"
            run = Run(key, label, Path(key), {}, {}, raw=values, raw_model=model)
            for row in values:
                tid = str(row.get("task_id", "?"))
                run.tasks[tid] = {"name": row.get("task_name", tid), "text": ""}
            self.runs.setdefault(key, run)

    def roles(self, run: Run) -> dict:
        models = mapping(mapping(run.manifest.get("config")).get("models"))
        return {"selector": run.manifest.get("selector_model") or models.get("selector", ""),
                "executor": run.manifest.get("executor_model") or models.get("l1_executor", run.raw_model),
                "evolver": run.manifest.get("method_model") or models.get("l2_editor", "")}

    def run_choices(self):
        return [(run.label, key) for key, run in self.runs.items()]

    def task_choices(self, run_key: str, query="", model="", status="全部"):
        run = self.runs.get(run_key)
        if not run:
            return []
        query = query.strip().casefold()
        choices = []
        for tid, task in sorted(run.tasks.items(), key=lambda p: (int(p[0]) if p[0].isdigit() else 9999, p[0])):
            if query and query not in f"{tid} {task['name']} {task.get('text', '')}".casefold():
                continue
            records = [v for v in run.slots.values() if str(v.get("task_id")) == tid]
            if model and model not in " ".join(str(x) for x in self.roles(run).values()) and not any(
                    r.get("source_model") == model for r in run.raw if str(r.get("task_id")) == tid):
                continue
            states = [outcome(v.get("reward")) if v.get("status") == "valid" else "未解决" for v in records]
            if not records:
                states = [outcome(v.get("reward")) for v in run.raw if str(v.get("task_id")) == tid]
            if status != "全部" and status not in (states or ["未解决"]):
                continue
            choices.append((f"{tid} · {task['name']}", tid))
        return choices

    def overview(self):
        rows = []
        for run in self.runs.values():
            valid = [v for v in run.slots.values() if v.get("status") == "valid" and outcome(v.get("reward")) != "未解决"]
            passed = sum(outcome(v.get("reward")) == "成功" for v in valid)
            roles = self.roles(run)
            audit = self.optional(run.path / "audit.json", {})
            expected = mapping(audit).get("expected_slots", run.manifest.get("expected_slots", len(run.tasks) * 3))
            rows.append([run.label, run.status.get("phase") or run.status.get("status") or run.manifest.get("stage", "原始来源"),
                         roles["selector"], roles["executor"], roles["evolver"], len(run.tasks),
                         f"{len(valid)}/{expected}" if run.slots else "待按请求核验",
                         f"{passed}/{len(valid)}" if valid else "—",
                         str(mapping(audit).get("complete", "未知")), str(run.path)])
        return rows

    def task_cards(self, run_key, tid):
        run = self.runs.get(run_key)
        return [c for c in run.cards if c.task_id == str(tid)] if run else []

    def cards(self, run_key, include_source=True):
        run = self.runs.get(run_key)
        if not run:
            return []
        cards = list(run.cards)
        source = self.runs.get(run.source)
        if source and include_source:
            cards += source.cards
        return cards

    def traces(self, run_key: str, tid: str) -> list[Trace]:
        run = self.runs.get(run_key)
        if not run or not tid:
            return []
        if run.manifest.get("format") == "agentbrew":
            from .agentbrew import traces
            return traces(self, run, str(tid))
        traces: dict[str, Trace] = {}
        name = run.tasks.get(str(tid), {}).get("name", str(tid))

        def add(path, attempt, request, **kwargs):
            p = self.resolve(path, run.path)
            if not p:
                return
            if p.is_dir() or p.name != "trajectory.json":
                p = p / "agent" / "trajectory.json"
            if not kwargs.get("result_path"):
                kwargs["result_path"] = p.parent.parent / "result.json"
            key = str(p)
            if key not in traces:
                traces[key] = Trace(key, name, str(tid), str(attempt), request, p, **kwargs)
            elif kwargs.get("accepted"):
                traces[key].accepted = True

        for slot, record in run.slots.items():
            if str(record.get("task_id")) != str(tid):
                continue
            accepted = str(self.resolve(record.get("trajectory_path"), run.path) or "")
            attempt = record.get("attempt_index", slot.rsplit("-", 1)[-1])
            for req in sorted((run.path / "slots" / slot / "requests").glob("*")):
                if not req.is_dir():
                    continue
                sel = mapping(self.optional(req / "selection.json", {}))
                mounted = sorted((req / "skills").glob("**/SKILL.md"))
                files = list(req.glob("jobs/*/*/agent/trajectory.json"))
                for p in sorted(files):
                    is_accepted = str(p) == accepted
                    add(str(p), attempt, f"request {req.name}",
                        result_path=p.parent.parent / "result.json", model=str(self.roles(run)["executor"]),
                        reward=record.get("reward") if is_accepted else None,
                        accepted=is_accepted, selection=sel, request_dir=req,
                        skill_file=mounted[0] if len(mounted) == 1 else None,
                        exception=record.get("exception_info") if is_accepted else None,
                        origin="槽位执行")
                if not files:
                    p = req / "jobs" / "trajectory.json"
                    add(str(p), attempt, f"request {req.name} · 未完成", request_dir=req,
                        selection=sel, model=str(self.roles(run)["executor"]), origin="槽位执行")
            if accepted:
                p = Path(accepted)
                add(accepted, attempt, self._request_name(p),
                    result_path=self.resolve(record.get("result_path"), run.path),
                    model=str(self.roles(run)["executor"]), reward=record.get("reward"),
                    accepted=True, selection=mapping(record.get("selection")),
                    skill_file=self.resolve(record.get("skill_file"), run.path),
                    exception=record.get("exception_info"), origin="槽位采用记录")

        for row in run.raw:
            if not isinstance(row, dict) or str(row.get("task_id")) != str(tid):
                continue
            add(row.get("trajectory_path"), row.get("attempt_index", "?"), "原始来源",
                result_path=self.resolve(row.get("result_path"), run.path),
                model=str(row.get("source_model", "")), reward=row.get("reward"), origin="来源 ledger")
        for card in self.task_cards(run_key, tid):
            trial_paths = []
            for trial in sequence(card.data.get("l1_trials")) + sequence(card.data.get("attempts")):
                if not isinstance(trial, dict):
                    continue
                path = trial.get("trajectory_path") or trial.get("trajectory")
                if path:
                    trial_paths.append(path)
                    add(path, trial.get("source_attempt_index", trial.get("index", trial.get("attempt_index", "?"))),
                        f"{card.kind}引用", result_path=self.resolve(trial.get("result_path"), run.path),
                        model=str(trial.get("source_model", card.source_model)),
                        reward=trial.get("verifier_reward", trial.get("reward")), origin=card.identity)
            other = sequence(card.data.get("failed_trajectories"))
            if card.data.get("final_trajectory"):
                other += [card.data["final_trajectory"]]
            for i, path in enumerate(other, 1):
                if path not in trial_paths:
                    p = self.resolve(path, run.path)
                    result = p.parent.parent / "result.json" if p and p.name == "trajectory.json" else None
                    add(path, "?", f"{card.kind}引用 {i}", result_path=result, origin=card.identity)
        return sorted(traces.values(), key=lambda t: (int(t.attempt) if t.attempt.isdigit() else 999, t.request, t.key))

    @staticmethod
    def _request_name(path):
        parts = path.parts
        return "request " + parts[parts.index("requests") + 1] if "requests" in parts else "外部引用"

    def trace_detail(self, trace: Trace | None) -> dict:
        if not trace:
            return {"error": "未选择轨迹", "steps": []}
        value, error = self.cache.read(trace.path)
        result, result_error = self.cache.read(trace.result_path, "result") if trace.result_path else ({}, "缺少 result 路径")
        result = mapping(result)
        reward = mapping(mapping(result.get("verifier_result")).get("rewards")).get("reward")
        exception = result.get("exception_info", trace.exception)
        return {"path": str(trace.path), "result_path": str(trace.result_path or ""),
                "error": error, "result_error": result_error,
                "reward": reward, "outcome": outcome(reward),
                "exception": exception, "accepted": trace.accepted,
                "selection": trace.selection, "origin": trace.origin,
                "model": mapping(mapping(value).get("agent")).get("model_name", trace.model),
                "agent": mapping(value).get("agent"), "final_metrics": mapping(value).get("final_metrics"),
                "schema_version": mapping(value).get("schema_version"),
                "raw": value, "steps": sequence(mapping(value).get("steps")),
                "request_error": self.optional(trace.request_dir / "error.json") if trace.request_dir else None}

    def skills(self, run_key: str) -> list[dict]:
        run = self.runs.get(run_key)
        if not run:
            return []
        if run.manifest.get("format") == "agentbrew":
            from .agentbrew import notes
            return notes(self, run)
        result = []
        paths = [(run.path / "initial_skills.json", "初始库", "json"),
                 (run.path / "skills.jsonl", "版本库", "jsonl"),
                 (run.path / "library.json", "冻结库", "json")]
        if run.source and run.source in self.runs:
            paths += [(Path(run.source) / "skills.jsonl", "来源版本库", "jsonl")]
        for path, kind, fmt in paths:
            for i, skill in enumerate(sequence(self.optional(path, [], fmt))):
                if not isinstance(skill, dict) or not skill.get("skill_id"):
                    continue
                result.append({"id": f"{path}:{i}", "key": skill_key(skill), "kind": kind,
                               "path": str(path), "skill": skill})
        return result

    def batches(self, run_key: str) -> list[dict]:
        run = self.runs.get(run_key)
        if not run:
            return []
        if run.manifest.get("format") == "agentbrew":
            from .agentbrew import batches
            return batches(self, run)
        roots = [run.path]
        if run.source and run.source in self.runs:
            roots += [Path(run.source)]
        rows = {}
        for root in roots:
            for path in sorted(root.glob("evolution/round-*/batches.json")):
                for row in sequence(self.optional(path, [])):
                    if isinstance(row, dict) and row.get("batch_id"):
                        journal = root / "l2_batches" / f"{row['batch_id']}.json"
                        data = self.optional(journal, row)
                        rows[f"{root}:{row['batch_id']}"] = {"id": f"{root}:{row['batch_id']}",
                            "path": str(journal if journal.exists() else path), "root": str(root), "data": data}
            for path in sorted(root.glob("l2_batches/*.json")):
                row = self.optional(path, {})
                if isinstance(row, dict) and row.get("batch_id"):
                    rows[f"{root}:{row['batch_id']}"] = {"id": f"{root}:{row['batch_id']}",
                        "path": str(path), "root": str(root), "data": row}
        return list(rows.values())

    def same_task(self, run_key: str, name: str) -> str | None:
        run = self.runs.get(run_key)
        if not run:
            return None
        return next((tid for tid, task in run.tasks.items() if task["name"] == name), None)


def step_text(step: dict) -> str:
    return "\n".join(pretty(step.get(k)) if not isinstance(step.get(k), str) else step[k]
                     for k in ("message", "reasoning_content", "tool_calls", "observation") if step.get(k))


def matching_steps(steps: list, query: str = "") -> list[int]:
    needle = query.strip().casefold()
    return [i for i, step in enumerate(steps) if isinstance(step, dict) and
            (not needle or needle in step_text(step).casefold() or needle == str(step.get("step_id")))]


def evidence_step(evidence: dict, steps: list) -> int | None:
    explicit = evidence.get("step_id", mapping(evidence.get("source")).get("step_id"))
    if explicit is not None:
        return next((i for i, s in enumerate(steps) if str(s.get("step_id")) == str(explicit)), None)
    # Evidence event numbers are not ATIF step IDs. Only a unique content match is safe.
    text = evidence.get("observation")
    if not isinstance(text, str) or not text:
        return None
    matches = [i for i, s in enumerate(steps) if s.get("message") == text]
    return matches[0] if len(matches) == 1 else None


def trajectory_html(steps: list, start=0, query="", page_size=10) -> str:
    matches = matching_steps(steps, query)
    if not matches:
        return '<div class="empty">没有匹配步骤</div>'
    start = int(start or 0)
    offset = matches.index(start) if start in matches else 0
    parts = []
    for index in matches[offset:offset + page_size]:
        s = steps[index]
        header = f"步骤 {s.get('step_id', index + 1)} · {s.get('source', '?')} · {s.get('timestamp', '')}"
        body = []
        for key, label in [("message", "消息"), ("reasoning_content", "已记录思考"),
                           ("tool_calls", "工具调用"), ("observation", "工具输出"), ("metrics", "用量")]:
            value = s.get(key)
            if value is None or value == "" or value == [] or value == {}:
                continue
            content = value if isinstance(value, str) else pretty(value)
            escaped = html.escape(content)
            if key in {"observation", "reasoning_content", "metrics"} or len(content) > 1600:
                body.append(f'<details><summary>{label} · {len(content):,} 字符</summary><pre>{escaped}</pre></details>')
            else:
                body.append(f'<div class="field-label">{label}</div><pre>{escaped}</pre>')
        parts.append(f'<article class="trace-step" id="step-{index}"><h3>{html.escape(header)}</h3>{"".join(body)}</article>')
    return "".join(parts)


def skill_diff(left: dict, right: dict) -> str:
    return "".join(difflib.unified_diff(str(left.get("body", "")).splitlines(True),
                                      str(right.get("body", "")).splitlines(True),
                                      fromfile=skill_key(left), tofile=skill_key(right))) or "正文无差异"


def evidence_trial(evidence: dict) -> str:
    trial = evidence.get("trial")
    match = re.match(r"t(\d+):", str(evidence.get("id", "")))
    return str(trial if trial is not None else match.group(1) if match else "")
