"""Freeze selector assignments once, independently for admission and final."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import ClassVar

from omegaconf import OmegaConf

from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from skillexpand import schema as S
from skillexpand.l1.cold_start import freeze
from skillexpand.persistence.artifacts import provider_signature
from skillexpand.l1.adapters import resolve
from skillexpand.l1.runner import save
from skillexpand.evaluation.selector import SkillSelector
from skillexpand.evaluation.selector import REASON_SELECTOR_ERROR


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

    try:
        cfg = PL._config(spec.benchmark)
        host = F.build_reasoning_host(cfg, spec.usage_path, role='selector')
        skills = [SimpleNamespace(**s) for s in spec.descriptions]
        choice = SkillSelector(host).select(F.task_text_of(cfg, spec.task_id), skills)
        return {
            "task_id": spec.task_id,
            "selection": asdict(choice),
            "error": "selector provider error"
            if choice.reason == REASON_SELECTOR_ERROR
            else None,
        }
    except Exception as exc:
        return {"task_id": spec.task_id, "error": f"{type(exc).__name__}: {exc}"}


class FrozenRoutes:
    def __init__(self, cfg, plan, library, root, split, final_workers=4):
        if split not in (S.SPLIT_ADMISSION, S.SPLIT_FINAL):
            raise ValueError("Only held-out tasks are routed")
        self.cfg, self.plan, self.split = cfg, plan, split
        self.root, self.final_workers = Path(root) / split, final_workers
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
            "provider": provider_signature(),
            "descriptions": list(self.descriptions),
            "tasks": {str(t): F.task_text_of(cfg, t) for t in self.ids},
            "prompt": resolve(cfg).selector_prompt(plan.benchmark),
        }
        self.fingerprint = S.content_hash(identity)
        freeze(self.root / "manifest.json", identity)
        self.records = {}

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
        errors = []

        def sink(record):
            task_id = record["task_id"]
            if record.get("error"):
                save(self.root / "errors" / f"{task_id}.json", record)
                errors.append(record)
                return
            self._add(record)
            save(self.root / "tasks" / f"{task_id}.json", record)

        PL.run_generic(pending, route_task, workers=self.final_workers, on_result=sink)
        if errors or set(self.records) != set(self.ids):
            raise RuntimeError(
                "Routing incomplete; resume retries only provider failures or missing tasks"
            )
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
        if task_id not in self.ids or record.get("error") or "ok" not in choice:
            raise ValueError("Invalid frozen routing record")
        if choice["ok"] and choice.get("skill_id") not in {
            s["skill_id"] for s in self.descriptions
        }:
            raise ValueError("Frozen route references unknown Skill")
        if task_id in self.records and self.records[task_id] != record:
            raise ValueError("Conflicting frozen routing records")
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
