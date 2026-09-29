"""Serial source-batch editing with selectable predicted or empirical acceptance."""

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from skillexpand.runtime import agent_factory as F
from skillexpand import schema as S
from skillexpand.persistence import store as ST
from skillexpand.l2 import editor as ED
from skillexpand.l2 import update as UP
from skillexpand.l2 import patterns as BP
from skillexpand.l2.card_review import CardReviewer
from skillexpand.l2.card_review import PROTOCOL
from skillexpand.persistence.artifacts import load_cold_start
from skillexpand.persistence.artifacts import code_signature
from skillexpand.persistence.artifacts import provider_signature
from skillexpand.l1.cold_start import freeze
from skillexpand.l1.runner import save
from skillexpand.runtime import parallel as PL
from skillexpand.evaluation.routing import FrozenRoutes
from skillexpand.evaluation import validation as VA


@dataclass
class EvolutionConfig:
    batch_size: int = 50
    candidate_count: int = 3
    evolve_l1_workers: int = 8
    l2_review_workers: int = 8
    autonomous_attempts: int = 4
    supervised_attempts: int = 1
    evolve_rounds: int = 1
    skill_edit_mode: str = "rewrite"
    acceptance_mode: str = "predicted"

    def __post_init__(self):
        if min(self.batch_size, self.candidate_count, self.evolve_l1_workers,
               self.l2_review_workers, self.evolve_rounds, self.autonomous_attempts) < 1:
            raise ValueError("Evolution budgets must be positive")
        if self.supervised_attempts < 0:
            raise ValueError("supervised_attempts must be nonnegative")
        if self.skill_edit_mode not in ("rewrite", "structured"):
            raise ValueError("Unknown Skill edit mode")
        if self.acceptance_mode not in ("predicted", "empirical"):
            raise ValueError("Unknown acceptance mode")

    def to_dict(self):
        return S.to_dict(self)


class RunLock:
    """An OS-held exclusive lock, released automatically if the process exits."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle = None

    def acquire(self) -> None:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError(
                f"{self.path.parent} is already being written by another process"
            )
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


@dataclass
class LoopPaths:
    """One directory holds the whole run."""

    root: Path

    def _p(self, name: str) -> Path:
        return self.root / name

    @property
    def skills(self) -> Path:
        return self._p("skills.jsonl")

    @property
    def lock(self) -> Path:
        return self._p("run.pid")

    @property
    def meta_skills(self) -> Path:
        return self._p("meta_skills.jsonl")

    @property
    def summary(self) -> Path:
        return self._p("summary.json")


class SerialEvolutionLoop:
    """Persist every batch decision before committing a reviewed Skill."""

    def __init__(self, cfg, plan, paths, config=None):
        with RunLock(paths.lock):
            self._initialize(cfg, plan, paths, config)

    def _initialize(self, cfg, plan, paths, config):
        self.cfg, self.plan, self.paths = cfg, plan, paths
        self.config = config or EvolutionConfig()
        input_cfg, checked_plan, self.initial, cold_cards = load_cold_start(paths.root)
        if input_cfg != cfg:
            raise ValueError(
                "L2 runtime config differs from imported cold-start config"
            )
        if checked_plan != plan:
            raise ValueError("L2 plan differs from completed cold start")
        self.cards = {
            t: replace(e, family_id=plan.family_of(t)) for t, e in cold_cards.items()
        }
        manifest = json.loads((paths.root / "manifest.json").read_text())
        self.l1_attempts = int(manifest.get("k", self.config.autonomous_attempts))
        self.l1_supervised = bool(manifest.get("supervised", True))
        self.l1_supervised_attempts = int(manifest.get("supervised_attempts", self.config.supervised_attempts))
        # Assignment is derived from the audited source map. selected_skill_id and
        # initial_skill_key stay None: these tasks were executed WITHOUT a Skill.
        protocol_config = self.config.to_dict()
        # The requested horizon is resumable metadata, not a compatibility
        # property: a one-round run may be extended to round two later. Batch,
        # candidate and L1 execution settings remain frozen.
        protocol_config.pop('evolve_rounds', None)
        identity = {
            "protocol": PROTOCOL,
            "execution_protocol": "skill-aware-rounds-v2",
            "acceptance_mode": self.config.acceptance_mode,
            "l1": {"attempts": self.l1_attempts, "supervised": self.l1_supervised},
            "config": protocol_config,
            "cards": {
                str(t): S.content_hash(S.to_dict(e)) for t, e in cold_cards.items()
            },
            "initial": [S.to_dict(s) for s in self.initial],
            "runtime": json.loads((paths.root / "config.json").read_text()),
            "provider": provider_signature(),
            "code": code_signature(),
        }
        if not (paths.root / "l2_manifest.json").exists() and (
            (paths.root / "experiences.jsonl").exists()
            or (paths.root / "patch_attempts.jsonl").exists()
        ):
            raise ValueError(
                "Legacy evolution output: import the cold start into a new run directory"
            )
        new_run = not (paths.root / "l2_manifest.json").exists()
        self.skills = ST.SkillLibrary(paths.skills, benchmark=plan.benchmark)
        for skill in self.initial:
            if skill.family_id not in self.skills.families:
                self.skills._append_new(skill)
            elif self.skills.history(skill.family_id)[0] != skill:
                raise ValueError("Initial Skill library changed")
        if set(self.skills.families) != {s.family_id for s in self.initial}:
            raise ValueError("Skill library contains unknown families")
        if new_run and any(
            self.skills.head(s.family_id).version != 0 for s in self.initial
        ):
            raise ValueError("Import initial cold-start Skills into a new L2 run")
        freeze(paths.root / "l2_manifest.json", identity)
        self.meta = ST.MetaSkillStore(paths.meta_skills)
        self.meta.ensure_initial()
        if self.meta.head().version != 0:
            raise ValueError("L3 is frozen; use a fresh cold-start import")
        self._recover_transactions()
        self.admission_routes = None
        self.admission_scorer = None

    def _ensure_admission_scorer(self):
        if self.config.acceptance_mode != "empirical":
            return None
        if self.admission_scorer is not None:
            return self.admission_scorer
        self.admission_routes = FrozenRoutes(
            self.cfg, self.plan, self.initial, self.paths.root / "routes",
            S.SPLIT_ADMISSION, self.config.l2_review_workers
        ).run()
        self.admission_scorer = VA.FixedSkillScorer(
            self.cfg,
            VA.ScoreCache(self.paths.root / "val" / "scores.jsonl"),
            self.admission_routes,
            self.config.l2_review_workers,
        )
        return self.admission_scorer

    def _reasoning_host(self, role, usage_path):
        """Build a role-specific host while keeping the two-argument API usable.

        A few offline integrations replace ``build_reasoning_host`` with a small
        factory accepting only ``(cfg, path)``.  Passing a role through a cloned
        config preserves that compatibility and still makes the selected model
        explicit in the host's config and usage artifact.
        """
        from omegaconf import OmegaConf
        role_cfg = OmegaConf.create(OmegaConf.to_container(self.cfg, resolve=True))
        role_cfg.agent.llm = F.role_model(self.cfg, role)
        role_cfg.models = OmegaConf.create(
            {**dict(OmegaConf.to_container(self.cfg.get('models', {}), resolve=True)),
             role: role_cfg.agent.llm}
        )
        return F.build_reasoning_host(role_cfg, usage_path)

    def skill_heads(self):
        return [self.skills.head(f) for f in sorted(self.skills.families)]

    def batches(self):
        batches = []
        for skill in sorted(self.initial, key=lambda s: s.skill_id):
            ids = sorted(self.plan.families[skill.family_id])
            for offset in range(0, len(ids), self.config.batch_size):
                batch_ids = ids[offset : offset + self.config.batch_size]
                batches.append(
                    {
                        "skill_id": skill.skill_id,
                        "family_id": skill.family_id,
                        "task_ids": batch_ids,
                        "batch_id": S.content_hash(
                            {"skill": skill.skill_id, "tasks": batch_ids}
                        ),
                    }
                )
        return batches

    def _restore(self, value):
        from skillexpand.l2.audit import audit_batch
        directory = self.paths.root / 'evolution' / f'round-{value["round"]}' / 'cards'
        cards = [S.from_dict(S.TaskExperience, json.loads((directory / f'{t}.json').read_text()))
                 for t in value['task_ids']]
        audit_batch(self.paths.root, value, self.skills.get(value['base_skill_key']), cards)
        raw = value.get("candidate")
        if raw:
            if value["outcome"] != "review_approved":
                raise ST.StoreError("Only review-approved candidates may be installed")
            candidate = S.from_dict(S.CandidateSkill, raw)
            if candidate.candidate_id != value["selected_candidate_id"]:
                raise ST.StoreError("Journal candidate differs from selected candidate")
            try:
                installed = self.skills.get(candidate.skill.key)
            except KeyError:
                base = self.skills.get(candidate.base_skill_key)
                if base.description != candidate.skill.description:
                    raise ST.StoreError("Routing description is frozen")
                installed = self.skills.commit(candidate)
            if installed != candidate.skill:
                raise ST.StoreError("Journal conflicts with Skill history")

    def _recover_transactions(self):
        directory = self.paths.root / "l2_batches"
        if not directory.exists():
            return
        # Replay in round and task order, never hash-filename order.
        records = [json.loads(p.read_text()) for p in directory.glob('*.json')]
        for value in sorted(records, key=lambda r: (r.get('round', 0), r['skill_id'],
                                                   r['task_ids'][0])):
            round_index = value.get('round', 0)
            if round_index < 1:
                raise ValueError('Legacy card-only journals require a separate run directory')
            plans = json.loads((self.paths.root / 'evolution' / f'round-{round_index}' /
                                'batches.json').read_text())
            plan = next((b for b in plans if b['batch_id'] == value['batch_id']), None)
            if plan is None or any(value.get(k) != v for k, v in plan.items()):
                raise ValueError('Journal differs from frozen evolution plan')
            self._restore(value)

    def _run_batch(self, batch):
        path = self.paths.root / "l2_batches" / (batch["batch_id"] + ".json")
        if path.exists():
            value = json.loads(path.read_text())
            if any(value.get(k) != v for k, v in batch.items()):
                raise ValueError('Batch journal differs from frozen batch')
            self._restore(value)
            return
        skill = self.skills.head(batch["family_id"])
        evidence = [self.cards[t] for t in batch["task_ids"]]
        planner_host = self._reasoning_host(
            'l2_planner', self.paths.root / "usage" / f"planner-{skill.skill_id}.json")
        editor_host = None
        if self.config.skill_edit_mode != 'structured':
            editor_host = self._reasoning_host(
                'l2_editor', self.paths.root / "usage" / f"editor-{skill.skill_id}.json")
        def reviewer_factory(card):
            card_key = S.content_hash(card)
            host = self._reasoning_host(
                'l2_reviewer',
                self.paths.root / "usage" / f"reviewer-{skill.skill_id}-{card_key}.json")
            return CardReviewer(host)

        runner = UP.SkillPatchRunner(
            ED.SkillEditor(planner_host, self.meta.head(), self.config.skill_edit_mode,
                           editor_host=editor_host),
            None,
            self.paths.root / "l2_proposals",
            reviewer_factory=reviewer_factory,
            acceptance_mode=self.config.acceptance_mode,
            admission_scorer=self._ensure_admission_scorer(),
        )
        pattern_path = self.paths.root / 'l2_patterns' / (batch['batch_id'] + '.json')
        if pattern_path.exists():
            patterns = json.loads(pattern_path.read_text())
            BP.validate_cache(patterns,evidence)
        else:
            patterns = BP.generate(planner_host, evidence) if len(evidence) > 1 else {
                'raw': None, 'patterns': [], 'status': 'insufficient_cards'}
            save(pattern_path, patterns)
        result = runner.run(skill, evidence, self.config.candidate_count,
                            batch_patterns=patterns['patterns'],
                            l2_review_workers=self.config.l2_review_workers)
        value = dict(
            batch,
            **result.record,
            batch_patterns=patterns,
            candidate=S.to_dict(result.candidate) if result.candidate else None,
        )
        save(path, value)
        self._restore(value)

    def _evolution_batches(self, round_index, cards):
        """Build fixed family batches from one and only one L1 evolution round."""
        batches = []
        for skill in sorted(self.skill_heads(), key=lambda s: s.skill_id):
            ids = sorted(self.plan.families[skill.family_id])
            for offset in range(0, len(ids), self.config.batch_size):
                task_ids = ids[offset:offset + self.config.batch_size]
                batches.append(self._make_evolution_batch(round_index, skill.family_id, task_ids, cards))
        return batches

    def _make_evolution_batch(self, round_index, family_id, task_ids, cards):
        """Stable evidence identity; the journal separately records the current head."""
        skill = self.skills.head(family_id)
        hashes = {str(t): S.content_hash(S.to_dict(cards[t])) for t in task_ids}
        return {
            'round': round_index, 'skill_id': skill.skill_id,
            'family_id': family_id, 'task_ids': list(task_ids),
            'card_hashes': hashes,
            'batch_id': S.content_hash({'round': round_index, 'skill': skill.skill_id,
                                        'tasks': list(task_ids), 'cards': hashes}),
        }

    def _round_input(self, round_index):
        directory = self.paths.root / 'evolution' / f'round-{round_index}'
        path = directory / 'input.json'
        expected = {s.skill_id: s.key for s in self.initial} if round_index == 1 else json.loads(
            (self.paths.root / 'evolution' / f'round-{round_index-1}' / 'summary.json').read_text())['skills']
        if path.exists():
            value = json.loads(path.read_text())
            skills = [S.from_dict(S.Skill, s) for s in value['skills']]
            if value['round'] != round_index or value['task_ids'] != sorted(self.plan.tasks_in(S.SPLIT_SOURCE)):
                raise ValueError('Round input identity mismatch')
            if {s.family_id for s in skills} != set(self.plan.families):
                raise ValueError('Round input family coverage mismatch')
            if {s.skill_id: s.key for s in skills} != expected:
                raise ValueError('Round input differs from previous output')
            for skill in skills:
                if self.skills.get(skill.key) != skill:
                    raise ValueError('Round input Skill differs from version history')
            return skills
        skills = self.skill_heads()
        if {s.skill_id: s.key for s in skills} != expected:
            raise ValueError('Skill heads differ from previous round output')
        freeze(path, {'round': round_index, 'skills': [S.to_dict(s) for s in skills],
                      'task_ids': sorted(self.plan.tasks_in(S.SPLIT_SOURCE))})
        return skills

    def _check_card(self, exp, task_id, round_index, skills):
        skill = next(s for s in skills if s.family_id == self.plan.family_of(task_id))
        if (exp.task_id != task_id or exp.benchmark != self.plan.benchmark or
                exp.split != S.SPLIT_SOURCE or exp.evolution_round != round_index or
                exp.family_id != skill.family_id or exp.initial_skill_key != skill.key or
                exp.selected_skill_id != skill.skill_id or exp.selection_source != S.SELECTION_FIXED or
                exp.experience_card is None or exp.experience_card.get('task', {}).get('task_id') != task_id):
            raise ValueError(f'Evolution card identity/provenance mismatch: {task_id}')

    def _collect_evolution_cards(self, round_index):
        """Run L1 with the current Skill heads and persist every task result."""
        directory = self.paths.root / "evolution" / f"round-{round_index}"
        results_dir = directory / "cards"
        results_dir.mkdir(parents=True, exist_ok=True)
        specs = []
        skills = self._round_input(round_index)
        for skill in skills:
            for task_id in sorted(self.plan.families[skill.family_id]):
                path = results_dir / f"{task_id}.json"
                if path.exists():
                    continue
                specs.append(PL.ExperienceSpec(
                    unit_id=f"evolution:{round_index}:{task_id}",
                    benchmark=self.plan.benchmark, task_id=task_id,
                    family_id=skill.family_id, split=S.SPLIT_SOURCE,
                    skill_key=skill.key, skill_body=skill.body,
                    skill_description=skill.description, skill_aware=True,
                    selected_skill_id=skill.skill_id,
                    selection_source=S.SELECTION_FIXED,
                    max_trials=self.l1_attempts,
                    supervised_repair=self.l1_supervised,
                    supervised_attempts=self.l1_supervised_attempts,
                    evolution_round=round_index,
                    l1_checkpoint_path=str(directory / "trials" / f"{task_id}.json")))
        errors = []
        pending = {s.task_id for s in specs}
        received = set()
        def sink(record):
            task_id = record['task_id']
            if task_id not in pending or task_id in received:
                raise ValueError('Unexpected or duplicate L1 result')
            received.add(task_id)
            if not record.get('ok'):
                errors.append(record)
                save(directory / 'errors' / f"{record['task_id']}.json", record)
                return
            exp = S.from_dict(S.TaskExperience, record['experience'])
            self._check_card(exp, task_id, round_index, skills)
            save(results_dir / f"{exp.task_id}.json", record['experience'])
        if specs:
            PL.run_generic(specs, PL.execute_experience, workers=self.config.evolve_l1_workers,
                           on_result=sink)
        if errors:
            raise RuntimeError(f"Evolution L1 interrupted on {len(errors)} tasks")
        cards = self._read_evolution_cards(round_index)
        freeze(directory / 'manifest.json', {
            'round': round_index,
            'skill_keys': {s.family_id: s.key for s in skills},
            'cards': {str(t): S.content_hash(S.to_dict(c)) for t, c in cards.items()},
            'task_ids': sorted(cards),
        })
        return cards

    def _read_evolution_cards(self, round_index):
        directory = self.paths.root / "evolution" / f"round-{round_index}"
        results_dir = directory / "cards"
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
        skills = self._round_input(round_index)
        expected = sorted(t for ids in self.plan.families.values() for t in ids)
        if {p.name for p in results_dir.glob('*.json')} != {f'{t}.json' for t in expected}:
            raise ValueError('Round card coverage differs from source tasks')
        cards = {}
        for task_id in expected:
            path = results_dir / f"{task_id}.json"
            if not path.exists():
                raise RuntimeError(f"Missing evolution card for task {task_id}")
            exp = S.from_dict(S.TaskExperience, json.loads(path.read_text()))
            self._check_card(exp, task_id, round_index, skills)
            from skillexpand.l1.audit import audit_checkpoint
            from skillexpand.l1.adapters import resolve
            checkpoint = json.loads((directory / 'trials' / f'{task_id}.json').read_text())
            if checkpoint['experience'] != S.to_dict(exp):
                raise ValueError(f'Evolution card/checkpoint mismatch: {task_id}')
            audit_checkpoint(checkpoint, resolve(self.cfg))
            if manifest is not None:
                expected_skill = manifest.get('skill_keys', {}).get(exp.family_id)
                if expected_skill and exp.initial_skill_key != expected_skill:
                    raise ValueError(f"Evolution card {task_id} came from a different Skill head")
                if manifest['cards'][str(task_id)] != S.content_hash(S.to_dict(exp)):
                    raise ValueError(f'Evolution card hash mismatch: {task_id}')
            cards[task_id] = exp
        return cards

    def run_evolutions(self, rounds=None):
        """Execute the explicit closed loop: Skill-aware L1, then serial L2."""
        rounds = self.config.evolve_rounds if rounds is None else int(rounds)
        if rounds < 1:
            raise ValueError('At least one evolve round is required')
        lock = RunLock(self.paths.lock)
        lock.acquire()
        try:
            self.skills = ST.SkillLibrary(self.paths.skills, benchmark=self.plan.benchmark)
            self._recover_transactions()
            last_result = None
            started_rounds = [int(p.parent.name.split('-')[1]) for p in
                             (self.paths.root / 'evolution').glob('round-*/input.json')]
            if started_rounds and rounds < max(started_rounds):
                raise ValueError('Requested horizon precedes an existing round')
            for round_index in range(1, rounds + 1):
                round_summary = self.paths.root / 'evolution' / f'round-{round_index}' / 'summary.json'
                if round_summary.exists() and json.loads(round_summary.read_text()).get('status') == 'complete':
                    self.cards = self._read_evolution_cards(round_index)
                    from skillexpand.l2.audit import audit_round
                    audit_round(self.paths.root, round_index)
                    last_result = json.loads(round_summary.read_text())
                    continue
                cards = self._collect_evolution_cards(round_index)
                self.cards = cards
                batches = self._evolution_batches(round_index, cards)
                freeze(round_summary.parent / 'batches.json', batches)
                for batch in batches:
                    self._run_batch(batch)
                save(self.paths.root / 'evolution' / f'round-{round_index}' / 'summary.json',
                     self.summary(round_index, batches))
                last_result = self.summary(round_index, batches)
                from skillexpand.l2.audit import audit_round
                save(round_summary.parent / 'audit.json', audit_round(self.paths.root, round_index))
            result = last_result or self.summary()
            result.update({'evolve_rounds': rounds, 'latest_evolution_round': rounds,
                           'l1_cards_are_skill_aware': True})
            save(self.paths.summary, result)
            return result
        except Exception as exc:
            save(self.paths.summary, dict(self.summary(), status='needs_attention',
                                          error=f'{type(exc).__name__}: {exc}'))
            raise
        finally:
            lock.release()

    def run(self):
        return self.run_evolutions()

    def summary(self, round_index=None, batches_override=None):
        batches = batches_override or (self.batches() if round_index is None else self._evolution_batches(
            round_index, self.cards))
        records = [
            json.loads(p.read_text())
            for b in batches
            if (
                p := self.paths.root / "l2_batches" / (b["batch_id"] + ".json")
            ).exists()
        ]
        return {
            "status": "complete" if len(records) == len(batches) else "partial",
            "protocol": PROTOCOL,
            "benchmark": self.plan.benchmark,
            "source_cards": len(self.cards),
            "batches": len(batches),
            "completed_batches": len(records),
            "hypotheses": sum(len(r["hypotheses"]) for r in records),
            "reviewed_candidates": sum(len(r["reviews"]) for r in records),
            "review_approved_updates": sum(
                r["outcome"] == "review_approved" for r in records
            ),
            "invalid_batches": sum(
                r["reason"].startswith(("invalid_review:", "invalid_hypotheses:"))
                for r in records
            ),
            "skills": {s.skill_id: s.key for s in self.skill_heads()},
            "acceptance_mode": self.config.acceptance_mode,
            "empirically_validated": bool(records) and self.config.acceptance_mode == "empirical" and all(
                r.get("empirically_validated") is True for r in records
            ),
            "admission_executions": sum(
                int(r.get("acceptance", {}).get("executions", 0)) for r in records
            ),
            "description_frozen": True,
            "meta_skill": self.meta.head().key,
            "l3_enabled": False,
        }
