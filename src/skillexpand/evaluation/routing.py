"""Freeze selector assignments once, independently for validation and test."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from omegaconf import OmegaConf

from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from skillexpand import schema as S
from skillexpand.persistence.io import freeze
from skillexpand.l1.adapters import resolve
from skillexpand.persistence.io import save
from skillexpand.evaluation.selector import SkillSelector
from skillexpand.reliability.errors import JournalConflict
from skillexpand.reliability.units import FailureCollector, guard


@dataclass(frozen=True)
class RouteSpec:
    requires_native_environment: ClassVar[bool] = False
    benchmark: str
    task_id: int
    descriptions: tuple
    usage_path: str


def route_task(spec):
    """Routing constructs no environment and receives no answers or Skill bodies."""
    from types import SimpleNamespace

    def select():
        cfg = PL._config(spec.benchmark)
        host = F.build_reasoning_host(cfg, spec.usage_path, role='selector')
        skills = [SimpleNamespace(**s) for s in spec.descriptions]
        return SkillSelector(host, resolve(cfg)).select(F.task_text_of(cfg, spec.task_id), skills)

    choice, failure = guard(select, unit_id=spec.task_id, stage="routing")
    if failure is not None:
        return {"task_id": spec.task_id, "failure": failure}
    return {"task_id": spec.task_id, "selection": asdict(choice), "failure": None}


class FrozenRoutes:
    def __init__(self, cfg, plan, library, root, split, test_workers=4):
        if split not in (S.SPLIT_VAL, S.SPLIT_TEST, S.SPLIT_TRAIN):
            raise ValueError("Only val, test or (progressive closed-set) train tasks are routed")
        self.cfg, self.plan, self.split = cfg, plan, split
        route_root = Path(root) / split
        self.root, self.test_workers = route_root, test_workers
        self.ids = tuple(sorted(plan.tasks_in(split)))
        self.descriptions = tuple(
            {"skill_id": s.skill_id, "description": s.description}
            for s in sorted(library, key=lambda s: s.skill_id)
        )
        if not self.descriptions or len(
            {s["skill_id"] for s in self.descriptions}
        ) != len(self.descriptions):
            raise ValueError(
                "Routing requires a nonempty library with unique Skill IDs"
            )
        identity = {
            "protocol": "fixed-description-routes-v1",
            "split": split,
            "config": OmegaConf.to_container(cfg, resolve=True),
            "descriptions": list(self.descriptions),
            "tasks": {str(t): F.task_text_of(cfg, t) for t in self.ids},
            "prompt": resolve(cfg).selector_prompt(plan.benchmark),
        }
        self.fingerprint = S.content_hash(identity)
        freeze(self.root / "manifest.json", identity)
        self.records = {}

    @classmethod
    def load_existing(cls, cfg, plan, library, root, split):
        """Load a completed frozen route without revalidating its provider hash.

        This is intentionally read-only.  It is used for post-hoc evaluation when
        the selector service version has changed since the route was measured; the
        task-to-Skill assignment is the frozen input we want to reuse, while a new
        selector call would silently change the evaluation panel.
        """
        if split not in (S.SPLIT_VAL, S.SPLIT_TEST):
            raise ValueError("Only val or test tasks are routed")
        route_root = Path(root) / split
        manifest_path = route_root / "manifest.json"
        complete_path = route_root / "complete.json"
        if not manifest_path.exists() or not complete_path.exists():
            raise FileNotFoundError(f"incomplete frozen route at {route_root}")
        manifest = json.loads(manifest_path.read_text())
        complete = json.loads(complete_path.read_text())
        obj = cls.__new__(cls)
        obj.cfg, obj.plan, obj.split = cfg, plan, split
        obj.root, obj.test_workers = route_root, 1
        obj.ids = tuple(sorted(plan.tasks_in(split)))
        obj.descriptions = tuple(
            {"skill_id": s.skill_id, "description": s.description}
            for s in sorted(library, key=lambda s: s.skill_id)
        )
        if manifest.get("descriptions") != list(obj.descriptions):
            raise JournalConflict("Frozen route descriptions do not match supplied Skills")
        obj.fingerprint = complete.get("fingerprint") or S.content_hash(manifest)
        obj.records = {}
        for task_id in obj.ids:
            path = route_root / "tasks" / f"{task_id}.json"
            if not path.exists():
                raise JournalConflict(f"Frozen route is missing task {task_id}")
            obj._add(json.loads(path.read_text()))
        if set(obj.records) != set(obj.ids):
            raise JournalConflict("Frozen route does not cover the requested split")
        recorded_groups = {
            str(skill_id): tuple(int(task_id) for task_id in task_ids)
            for skill_id, task_ids in (complete.get("groups") or {}).items()
        }
        if recorded_groups != obj.groups:
            raise JournalConflict("Frozen route complete.json disagrees with task records")
        return obj

    def run(self):
        for task_id in self.ids:
            path = self.root / "tasks" / f"{task_id}.json"
            if path.exists():
                self._add(json.loads(path.read_text()))
        pending = [
            RouteSpec(
                self.plan.benchmark,
                t,
                self.descriptions,
                str(self.root / "usage" / f"{t}.json"),
            )
            for t in self.ids
            if t not in self.records
        ]
        collector = FailureCollector(f"routing-{self.split}", self.root / "errors")

        def sink(record):
            task_id = record["task_id"]
            if record["failure"]:
                collector.record(record["failure"], str(task_id))
                return
            self._add(record)
            save(self.root / "tasks" / f"{task_id}.json", record)

        PL.run_generic(pending, route_task, workers=self.test_workers, on_result=sink)
        collector.raise_if_incomplete("Routing incomplete")
        if set(self.records) != set(self.ids):
            raise JournalConflict("Routing returned no record for some tasks")
        save(
            self.root / "complete.json",
            {
                "fingerprint": self.fingerprint,
                "tasks": len(self.ids),
                "groups": self.groups,
                "failed_task_ids": self.failed_task_ids,
            },
        )
        return self

    def _add(self, record):
        task_id = record["task_id"]
        choice = record.get("selection", {})
        if task_id not in self.ids or record["failure"] or "ok" not in choice:
            raise JournalConflict("Invalid frozen routing record")
        if choice["ok"] and choice.get("skill_id") not in {
            s["skill_id"] for s in self.descriptions
        }:
            raise JournalConflict("Frozen route references unknown Skill")
        if task_id in self.records and self.records[task_id] != record:
            raise JournalConflict("Conflicting frozen routing records")
        self.records[task_id] = record

    @property
    def groups(self):
        return {
            s["skill_id"]: tuple(
                t
                for t in self.ids
                if t in self.records
                and self.records[t]["selection"]["ok"]
                and self.records[t]["selection"]["skill_id"] == s["skill_id"]
            )
            for s in self.descriptions
        }

    @property
    def failed_task_ids(self):
        return tuple(
            t
            for t in self.ids
            if t in self.records and not self.records[t]["selection"]["ok"]
        )
