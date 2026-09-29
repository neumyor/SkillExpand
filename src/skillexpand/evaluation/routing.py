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
        route_root = Path(root) / split
        persisted_split = split
        # Runs created before the train/val/test vocabulary used ``admission`` for
        # the validation directory.  Reusing such a frozen route is safe because
        # the route records themselves carry task ids and Skill ids; only the
        # on-disk directory name changed.
        if split == S.SPLIT_VAL and not route_root.exists():
            legacy_root = Path(root) / "admission"
            if legacy_root.exists():
                route_root = legacy_root
                persisted_split = "admission"
        self.root, self.final_workers = route_root, final_workers
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
            # Preserve the spelling used by a reused legacy manifest so its
            # fingerprint remains the identity of the frozen route.
            "split": persisted_split,
            "config": OmegaConf.to_container(cfg, resolve=True),
            "provider": provider_signature(),
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

        This is intentionally read-only.  It is used for post-hoc calibration when
        the selector service version has changed since the route was measured; the
        task-to-Skill assignment is the frozen input we want to reuse, while a new
        selector call would silently change the evaluation panel.
        """
        if split not in (S.SPLIT_ADMISSION, S.SPLIT_FINAL):
            raise ValueError("Only held-out tasks are routed")
        route_root = Path(root) / split
        if not route_root.exists() and split == S.SPLIT_VAL:
            legacy_root = Path(root) / "admission"
            if legacy_root.exists():
                route_root = legacy_root
        manifest_path = route_root / "manifest.json"
        complete_path = route_root / "complete.json"
        if not manifest_path.exists() or not complete_path.exists():
            raise FileNotFoundError(f"incomplete frozen route at {route_root}")
        manifest = json.loads(manifest_path.read_text())
        complete = json.loads(complete_path.read_text())
        obj = cls.__new__(cls)
        obj.cfg, obj.plan, obj.split = cfg, plan, split
        obj.root, obj.final_workers = route_root, 1
        obj.ids = tuple(sorted(plan.tasks_in(split)))
        obj.descriptions = tuple(
            {"skill_id": s.skill_id, "description": s.description}
            for s in sorted(library, key=lambda s: s.skill_id)
        )
        if manifest.get("descriptions") != list(obj.descriptions):
            raise ValueError("Frozen route descriptions do not match supplied Skills")
        obj.fingerprint = complete.get("fingerprint") or S.content_hash(manifest)
        obj.records = {}
        for task_id in obj.ids:
            path = route_root / "tasks" / f"{task_id}.json"
            if not path.exists():
                raise ValueError(f"Frozen route is missing task {task_id}")
            obj._add(json.loads(path.read_text()))
        if set(obj.records) != set(obj.ids):
            raise ValueError("Frozen route does not cover the requested split")
        recorded_groups = {
            str(skill_id): tuple(int(task_id) for task_id in task_ids)
            for skill_id, task_ids in (complete.get("groups") or {}).items()
        }
        if recorded_groups != obj.groups:
            raise ValueError("Frozen route complete.json disagrees with task records")
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
