"""Serial train-batch editing with selectable predictive, empirical, or JEV acceptance."""

import json
from typing import Optional
from dataclasses import dataclass, replace
from pathlib import Path
from skillexpand.runtime import agent_factory as F
from skillexpand import schema as S
from skillexpand.persistence import io as IO
from skillexpand.persistence import store as ST
from skillexpand.l2 import editor as ED
from skillexpand.l2 import update as UP
from skillexpand.l1 import patterns as BP
from skillexpand.l2.card_review import CardReviewer
from skillexpand.l2.card_review import PROTOCOL
from skillexpand.l1.artifacts import is_progressive, load_cold_start
from skillexpand.persistence.io import code_signature
from skillexpand.runtime.models.llm import provider_signature
from skillexpand.persistence.io import RunLock, freeze, save
from skillexpand.reliability.errors import (
    FrozenProtocolChanged, InvalidInput, JournalConflict, StoreError, classify,
)
from skillexpand.reliability.units import FailureCollector
from skillexpand.runtime import parallel as PL
from skillexpand.l1 import workers as LW
from skillexpand.evaluation.routing import FrozenRoutes
from skillexpand.evaluation import progressive as PG
from skillexpand.l2 import audit as AU
from skillexpand.evaluation import validation as VA
from skillexpand.evaluation.jev import JevSkillScorer
from skillexpand.l2 import reviewer_coevolution as RC
from skillexpand.l2 import sampled as SM
from skillexpand.evaluation.claim_check import TrajectoryVerifier
from skillexpand.evaluation.delta_review import PairedDeltaReviewer
from skillexpand.evaluation.sampled_validation import SampledDeltaValidator


@dataclass
class EvolutionConfig:
    batch_size: int = 50
    candidate_count: int = 1
    evolve_l1_workers: int = 8
    l2_review_workers: int = 8
    autonomous_attempts: int = 4
    supervised_attempts: int = 1
    evolve_rounds: int = 1
    skill_edit_mode: str = "structured"
    acceptance_mode: str = "predicted"
    predicted_review_scope: str = "val"
    single_candidate: bool = False
    #: ``None`` resolves to the protocol's default: the older calibration for the
    #: older protocols, none for sampled (which has its own Reviewer memory).
    reviewer_update_mode: Optional[str] = None
    reviewer_feedback_size: int = 0
    acceptance_sample_size: int = SM.DEFAULTS["acceptance_sample_size"]
    acceptance_confidence: float = SM.DEFAULTS["acceptance_confidence"]
    claim_verification: str = SM.DEFAULTS["claim_verification"]
    planner_memory_mode: str = SM.DEFAULTS["planner_memory_mode"]
    reviewer_memory_mode: str = SM.DEFAULTS["reviewer_memory_mode"]
    #: TB-eval: run L1 through a progressive catalog->select->load Skill library
    #: and accept against the closed train panel.  Off keeps every main path.
    progressive_library: bool = False

    def __post_init__(self):
        if min(self.batch_size, self.candidate_count, self.evolve_l1_workers,
               self.l2_review_workers, self.evolve_rounds, self.autonomous_attempts) < 1:
            raise InvalidInput("Evolution budgets must be positive")
        if self.supervised_attempts < 0:
            raise InvalidInput("supervised_attempts must be nonnegative")
        if self.skill_edit_mode not in ("rewrite", "structured"):
            raise InvalidInput("Unknown Skill edit mode")
        if self.acceptance_mode not in ("predicted", "empirical", "jev", "sampled"):
            raise InvalidInput("Unknown acceptance mode")
        if self.predicted_review_scope not in ("val", "train_cards"):
            raise InvalidInput("Unknown predicted review scope")
        if self.single_candidate and self.candidate_count != 1:
            raise InvalidInput("single_candidate protocol requires candidate_count=1")
        if self.reviewer_update_mode is None:
            self.reviewer_update_mode = SM.default_reviewer_update_mode(self.acceptance_mode)
        if self.reviewer_update_mode not in ("none", "summary", "rules"):
            raise InvalidInput("Unknown reviewer update mode")
        if self.reviewer_feedback_size < 0:
            raise InvalidInput("reviewer_feedback_size must be nonnegative")
        SM.validate_options(self.to_dict())
        if self.progressive_library and (
                self.acceptance_mode != "predicted" or self.predicted_review_scope != "val"):
            raise InvalidInput(
                "progressive_library requires acceptance_mode='predicted' and "
                "predicted_review_scope='val' (the closed-set train panel)")

    def to_dict(self):
        return S.to_dict(self)


def family_task_batches(task_ids, batch_size):
    """Fixed-order batches of one family's train tasks; the tail batch is kept."""
    ids = sorted(task_ids)
    return [ids[offset:offset + batch_size] for offset in range(0, len(ids), batch_size)]


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
    def summary(self) -> Path:
        return self._p("summary.json")


class SerialEvolutionLoop:
    """Persist every batch decision before committing a reviewed Skill."""

    def __init__(self, cfg, plan, paths, config=None, allow_code_change=False):
        with RunLock(paths.lock):
            self._initialize(cfg, plan, paths, config, allow_code_change)

    def _initialize(self, cfg, plan, paths, config, allow_code_change=False):
        self.cfg, self.plan, self.paths = cfg, plan, paths
        self.config = config or EvolutionConfig()
        self._check_progressive_switch(cfg, paths.root)
        input_cfg, checked_plan, self.initial, cold_cards = load_cold_start(paths.root)
        if input_cfg != cfg:
            raise FrozenProtocolChanged(
                "L2 runtime config differs from imported cold-start config"
            )
        if checked_plan != plan:
            raise FrozenProtocolChanged("L2 plan differs from completed cold start")
        if self.config.progressive_library:
            # No task->family map exists; cold cards keep their imported family_id
            # and only feed the identity hash below.
            self.cards = dict(cold_cards)
        else:
            self.cards = {
                t: replace(e, family_id=plan.family_of(t)) for t, e in cold_cards.items()
            }
        manifest = json.loads((paths.root / "manifest.json").read_text())
        self.l1_attempts = int(manifest["k"])
        self.l1_supervised = bool(manifest["supervised"])
        self.l1_supervised_attempts = int(manifest["supervised_attempts"])
        # Assignment is derived from the audited train map. selected_skill_id and
        # initial_skill_key stay None: these tasks were executed WITHOUT a Skill.
        protocol_config = self.config.to_dict()
        # The requested horizon is resumable metadata, not a compatibility
        # property: a one-round run may be extended to round two later. Batch,
        # candidate and L1 execution settings remain frozen.
        protocol_config.pop('evolve_rounds', None)
        if not self.config.progressive_library:
            # Same device: a default-valued switch must not change the identity
            # that every existing run froze.
            protocol_config.pop('progressive_library', None)
        identity = {
            "protocol": PROTOCOL,
            "execution_protocol": "skill-aware-rounds-v2",
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
        new_run = not (paths.root / "l2_manifest.json").exists()
        self.skills = ST.SkillLibrary(paths.skills, benchmark=plan.benchmark)
        for skill in self.initial:
            if skill.family_id not in self.skills.families:
                self.skills._append_new(skill)
            elif self.skills.history(skill.family_id)[0] != skill:
                raise JournalConflict("Initial Skill library changed")
        if set(self.skills.families) != {s.family_id for s in self.initial}:
            raise JournalConflict("Skill library contains unknown families")
        if new_run and any(
            self.skills.head(s.family_id).version != 0 for s in self.initial
        ):
            raise JournalConflict("Import initial cold-start Skills into a new L2 run")
        freeze(paths.root / "l2_manifest.json", identity, allow_code_change=allow_code_change)
        self._recover_transactions()
        self.val_routes = None
        self.val_scorer = None
        self.jev_routes = None
        self.jev_scorer = None
        self.predicted_routes = None
        self.predicted_scorer = None
        self.sampled_validator = None
        self.sampled_validator_round = None
        self.reviewer_update = self._load_reviewer_update()

    def _check_progressive_switch(self, cfg, root):
        """One-directional guard between ``--progressive-library`` and the frozen config."""
        config_path = Path(root) / "config.json"
        frozen = is_progressive(root) if config_path.exists() else False
        if not self.config.progressive_library:
            if frozen:
                raise InvalidInput(
                    "the frozen config marks this run as a progressive library "
                    "(benchmark.progressive_library); rerun with --progressive-library")
            return
        rollout = cfg.benchmark.get("rollout", {})
        if cfg.benchmark.name != "terminalbench" or rollout.get("mode") != "harbor_rollout":
            raise InvalidInput(
                "--progressive-library requires benchmark terminalbench with "
                "rollout.mode=harbor_rollout")
        if not frozen:
            raise InvalidInput(
                "--progressive-library requires a progressive cold start "
                "(benchmark.progressive_library in the frozen config); this run's "
                "frozen config is not progressive")
        if self.config.reviewer_update_mode != "none":
            raise InvalidInput(
                "--progressive-library requires reviewer_update_mode=none: train-panel "
                "Reviewer calibration is defined per task family, and a progressive "
                "library has no task->family map")

    def _load_reviewer_update(self):
        path = self.paths.root / "reviewer_updates.jsonl"
        rows = IO.read_jsonl(path, repair_tail=False)
        if not rows:
            return None
        update = S.from_dict(RC.ReviewerUpdate, rows[-1])
        if update.protocol != RC.PROTOCOL:
            raise JournalConflict("Reviewer update protocol mismatch")
        return update

    def _calibration_block(self):
        update = self.reviewer_update
        # C2 is the fixed-prompt feedback control.  It still writes feedback and
        # updates for audit, but no feedback-derived text may reach its Reviewer.
        if (update is None or self.config.reviewer_update_mode in ("none", "summary")):
            return "", 0
        rules = update.rules if self.config.reviewer_update_mode == "rules" else ()
        return RC.render_calibration_block(update.summary, rules), update.reviewer_prompt_version


    def _ensure_sampled_validator(self, round_index, reviewer_memory):
        """Paired delta predictions corrected by a random val sample.

        Rebuilt when the round changes, because the Reviewer's memory is drawn
        from the rounds that finished before this one.
        """
        if self.config.acceptance_mode != SM.MODE:
            return None
        if (self.sampled_validator is not None
                and self.sampled_validator_round == round_index):
            return self.sampled_validator
        routes = FrozenRoutes(
            self.cfg, self.plan, self.initial, self.paths.root / "routes",
            S.SPLIT_VAL, self.config.l2_review_workers
        ).run()

        def host_factory(task_id, usage_path):
            # Ordinary configured L2 reviewer model, same as the older protocols.
            return self._reasoning_host("l2_reviewer", usage_path)

        reviewer = PairedDeltaReviewer(
            self.cfg,
            routes,
            VA.ScoreCache(self.paths.root / "val" / "delta_predictions.jsonl"),
            self.config.l2_review_workers,
            host_factory=host_factory,
        )
        executor = VA.FixedSkillScorer(
            self.cfg,
            VA.ScoreCache(self.paths.root / "val" / "sampled_scores.jsonl"),
            routes,
            self.config.l2_review_workers,
        )
        verifier = None
        if self.config.claim_verification == "on":
            def verifier_factory(task_id, usage_path):
                return self._reasoning_host("l2_verifier", usage_path)

            verifier = TrajectoryVerifier(
                self.cfg,
                VA.ScoreCache(self.paths.root / "val" / "verifications.jsonl"),
                self.config.l2_review_workers,
                host_factory=verifier_factory,
            )
        self.sampled_validator = SampledDeltaValidator(
            self.cfg, routes, reviewer, executor,
            sample_size=self.config.acceptance_sample_size,
            confidence=self.config.acceptance_confidence,
            verifier=verifier,
            reviewer_memory=reviewer_memory,
        )
        self.sampled_validator_round = round_index
        return self.sampled_validator

    def _ensure_val_scorer(self):
        if self.config.acceptance_mode != "empirical":
            return None
        if self.val_scorer is not None:
            return self.val_scorer
        self.val_routes = FrozenRoutes(
            self.cfg, self.plan, self.initial, self.paths.root / "routes",
            S.SPLIT_VAL, self.config.l2_review_workers
        ).run()
        self.val_scorer = VA.FixedSkillScorer(
            self.cfg,
            VA.ScoreCache(self.paths.root / "val" / "scores.jsonl"),
            self.val_routes,
            self.config.l2_review_workers,
        )
        return self.val_scorer

    def _ensure_jev_scorer(self):
        if self.config.acceptance_mode != "jev":
            return None
        if self.jev_scorer is not None:
            return self.jev_scorer
        self.jev_routes = FrozenRoutes(
            self.cfg, self.plan, self.initial, self.paths.root / "routes",
            S.SPLIT_VAL, self.config.l2_review_workers
        ).run()
        self.jev_scorer = JevSkillScorer(
            self.cfg, self.jev_routes,
            VA.ScoreCache(self.paths.root / "val" / "jev_scores.jsonl"),
            self.config.l2_review_workers,
        )
        return self.jev_scorer

    def _ensure_predicted_scorer(self):
        if (self.config.acceptance_mode != "predicted" or
                self.config.predicted_review_scope != "val"):
            return None
        if self.predicted_scorer is not None:
            return self.predicted_scorer
        progressive = self.config.progressive_library
        panel = S.SPLIT_TRAIN if progressive else S.SPLIT_VAL
        if progressive and (self.paths.root / "routes" / panel / "complete.json").exists():
            # A completed closed-set route is frozen input.  Its identity embeds the
            # relay's per-run port, so a resumed run cannot rebuild it; reload it
            # (descriptions, tasks and groups are still checked).
            self.predicted_routes = FrozenRoutes.load_existing(
                self.cfg, self.plan, self.initial, self.paths.root / "routes", panel)
        else:
            self.predicted_routes = FrozenRoutes(
                self.cfg, self.plan, self.initial, self.paths.root / "routes",
                panel, self.config.l2_review_workers
            ).run()

        def judge_factory(task_id, skill, usage_path):
            # The predicted reviewer is an ordinary configured L2 reviewer model;
            # it is independent from the JEV endpoint and receives no trajectory.
            return self._reasoning_host("l2_reviewer", usage_path)

        self.predicted_scorer = VA.PredictedSkillScorer(
            self.cfg,
            self.predicted_routes,
            VA.ScoreCache(self.paths.root / panel / "predicted_scores.jsonl"),
            self.config.l2_review_workers,
            judge_factory=judge_factory,
            calibration_block=self._calibration_block()[0],
            reviewer_prompt_version=self._calibration_block()[1],
        )
        return self.predicted_scorer

    def _reasoning_host(self, role, usage_path):
        return F.build_reasoning_host(self.cfg, usage_path, role=role)

    def skill_heads(self):
        return [self.skills.head(f) for f in sorted(self.skills.families)]

    def _restore(self, value):
        from skillexpand.l2.audit import audit_batch
        directory = self.paths.root / 'evolution' / f'round-{value["round"]}' / 'cards'
        cards = [S.from_dict(S.TaskExperience, json.loads((directory / f'{t}.json').read_text()))
                 for t in value['task_ids']]
        audit_batch(self.paths.root, value, self.skills.get(value['base_skill_key']), cards)
        raw = value.get("candidate")
        if raw:
            if value["outcome"] != "review_approved":
                raise StoreError("Only review-approved candidates may be installed")
            candidate = S.from_dict(S.CandidateSkill, raw)
            if candidate.candidate_id != value["selected_candidate_id"]:
                raise StoreError("Journal candidate differs from selected candidate")
            try:
                installed = self.skills.get(candidate.skill.key)
            except KeyError:
                base = self.skills.get(candidate.base_skill_key)
                if base.description != candidate.skill.description:
                    raise StoreError("Routing description is frozen")
                installed = self.skills.commit(candidate)
            if installed != candidate.skill:
                raise StoreError("Journal conflicts with Skill history")

    def _recover_transactions(self):
        directory = self.paths.root / "l2_batches"
        if not directory.exists():
            return
        # Replay in round and task order, never hash-filename order.
        records = [json.loads(p.read_text()) for p in directory.glob('*.json')]
        for value in sorted(records, key=lambda r: (r['round'], r['skill_id'], r['task_ids'][0])):
            round_index = value['round']
            plans = json.loads((self.paths.root / 'evolution' / f'round-{round_index}' /
                                'batches.json').read_text())
            plan = next((b for b in plans if b['batch_id'] == value['batch_id']), None)
            if plan is None or any(value.get(k) != v for k, v in plan.items()):
                raise JournalConflict('Journal differs from frozen evolution plan')
            self._restore(value)

    def _run_batch(self, batch):
        path = self.paths.root / "l2_batches" / (batch["batch_id"] + ".json")
        if path.exists():
            value = json.loads(path.read_text())
            if any(value.get(k) != v for k, v in batch.items()):
                raise JournalConflict('Batch journal differs from frozen batch')
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
        reviewer_factory = None
        # Only the card-review protocols read per-card judgments.  The predicted
        # and sampled val protocols never call this factory, and building it
        # anyway would spend a reviewer call on every train card for nothing.
        uses_card_review = (
            self.config.acceptance_mode in ("empirical", "jev")
            or (self.config.acceptance_mode == "predicted"
                and self.config.predicted_review_scope == "train_cards"))
        if uses_card_review:
            def reviewer_factory(card):
                card_key = S.content_hash(card)
                host = self._reasoning_host(
                    'l2_reviewer',
                    self.paths.root / "usage" / f"reviewer-{skill.skill_id}-{card_key}.json")
                return CardReviewer(host)

        planner_memory, reviewer_memory = "", None
        if self.config.acceptance_mode == SM.MODE:
            planner_memory, reviewer_memory = SM.round_memories(
                self.paths.root, batch["round"], self.config,
                task_text=lambda task_id: F.task_text_of(self.cfg, task_id))
        runner = UP.SkillPatchRunner(
            ED.SkillEditor(planner_host, self.config.skill_edit_mode,
                           editor_host=editor_host),
            None,
            self.paths.root / "l2_proposals",
            reviewer_factory=reviewer_factory,
            acceptance_mode=self.config.acceptance_mode,
            predicted_review_scope=self.config.predicted_review_scope,
            val_scorer=self._ensure_val_scorer(),
            jev_scorer=self._ensure_jev_scorer(),
            predicted_scorer=self._ensure_predicted_scorer(),
            sampled_validator=self._ensure_sampled_validator(batch["round"],
                                                             reviewer_memory),
            single_candidate=self.config.single_candidate,
            planner_memory=planner_memory,
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
            # Recorded so the audit can recompute what each memory held rather
            # than take its absence of leaked task text on trust.
            **(SM.journal_fields(planner_memory, reviewer_memory)
               if self.config.acceptance_mode == SM.MODE else {}),
        )
        save(path, value)
        self._restore(value)

    def _evolution_batches(self, round_index, cards):
        """Build fixed family batches from one and only one L1 evolution round."""
        if self.config.progressive_library:
            # Batches follow the Skill each card actually loaded.
            return [self._make_evolution_batch(round_index, skill.family_id, task_ids, cards)
                    for skill in sorted(self.skill_heads(), key=lambda s: s.skill_id)
                    for task_ids in family_task_batches(
                        [t for t, c in cards.items() if c.selected_skill_id == skill.skill_id],
                        self.config.batch_size)]
        return [self._make_evolution_batch(round_index, skill.family_id, task_ids, cards)
                for skill in sorted(self.skill_heads(), key=lambda s: s.skill_id)
                for task_ids in family_task_batches(self.plan.families[skill.family_id],
                                                    self.config.batch_size)]

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
            if value['round'] != round_index or value['task_ids'] != sorted(self.plan.tasks_in(S.SPLIT_TRAIN)):
                raise JournalConflict('Round input identity mismatch')
            if (not self.config.progressive_library and
                    {s.family_id for s in skills} != set(self.plan.families)):
                raise JournalConflict('Round input family coverage mismatch')
            if {s.skill_id: s.key for s in skills} != expected:
                raise JournalConflict('Round input differs from previous output')
            for skill in skills:
                if self.skills.get(skill.key) != skill:
                    raise JournalConflict('Round input Skill differs from version history')
            return skills
        skills = self.skill_heads()
        if {s.skill_id: s.key for s in skills} != expected:
            raise JournalConflict('Skill heads differ from previous round output')
        freeze(path, {'round': round_index, 'skills': [S.to_dict(s) for s in skills],
                      'task_ids': sorted(self.plan.tasks_in(S.SPLIT_TRAIN))})
        return skills

    def _check_card(self, exp, task_id, round_index, skills):
        if self.config.progressive_library:
            skill = next((s for s in skills if s.skill_id == exp.selected_skill_id), None)
            if (skill is None or exp.task_id != task_id or exp.benchmark != self.plan.benchmark or
                    exp.split != S.SPLIT_TRAIN or exp.evolution_round != round_index or
                    exp.family_id != skill.family_id or exp.initial_skill_key != skill.key or
                    exp.selection_source != S.SELECTION_AGENT or
                    exp.experience_card is None or
                    exp.experience_card.get('task', {}).get('task_id') != task_id):
                raise JournalConflict(f'Evolution card identity/provenance mismatch: {task_id}')
            return
        skill = next(s for s in skills if s.family_id == self.plan.family_of(task_id))
        if (exp.task_id != task_id or exp.benchmark != self.plan.benchmark or
                exp.split != S.SPLIT_TRAIN or exp.evolution_round != round_index or
                exp.family_id != skill.family_id or exp.initial_skill_key != skill.key or
                exp.selected_skill_id != skill.skill_id or exp.selection_source != S.SELECTION_FIXED or
                exp.experience_card is None or exp.experience_card.get('task', {}).get('task_id') != task_id):
            raise JournalConflict(f'Evolution card identity/provenance mismatch: {task_id}')

    def _collect_evolution_cards(self, round_index):
        """Run L1 with the current Skill heads and persist every task result."""
        directory = self.paths.root / "evolution" / f"round-{round_index}"
        results_dir = directory / "cards"
        results_dir.mkdir(parents=True, exist_ok=True)
        specs = []
        skills = self._round_input(round_index)
        progressive = self.config.progressive_library
        if progressive:
            # Every train task selects from the full current-head library at run time.
            library = tuple(S.to_dict(s) for s in skills)
            for task_id in sorted(self.plan.tasks_in(S.SPLIT_TRAIN)):
                if (results_dir / f"{task_id}.json").exists():
                    continue
                specs.append(PG.ProgressiveSpec(
                    unit_id=f"evolution:{round_index}:{task_id}",
                    benchmark=self.plan.benchmark, task_id=task_id,
                    skill_library=library, split=S.SPLIT_TRAIN,
                    max_trials=self.l1_attempts, evolution_round=round_index,
                    l1_checkpoint_path=str(directory / "trials" / f"{task_id}.json")))
        for skill in (() if progressive else skills):
            for task_id in sorted(self.plan.families[skill.family_id]):
                path = results_dir / f"{task_id}.json"
                if path.exists():
                    continue
                specs.append(LW.ExperienceSpec(
                    unit_id=f"evolution:{round_index}:{task_id}",
                    benchmark=self.plan.benchmark, task_id=task_id,
                    family_id=skill.family_id, split=S.SPLIT_TRAIN,
                    skill_key=skill.key, skill_body=skill.body,
                    skill_description=skill.description, skill_aware=True,
                    selected_skill_id=skill.skill_id,
                    selection_source=S.SELECTION_FIXED,
                    max_trials=self.l1_attempts,
                    supervised_repair=self.l1_supervised,
                    supervised_attempts=self.l1_supervised_attempts,
                    evolution_round=round_index,
                    l1_checkpoint_path=str(directory / "trials" / f"{task_id}.json")))
        collector = FailureCollector(f'evolution-{round_index}/l1', directory / 'errors')
        pending = {s.task_id for s in specs}
        received = set()
        def sink(record):
            task_id = record['task_id']
            if task_id not in pending or task_id in received:
                raise JournalConflict('Unexpected or duplicate L1 result')
            received.add(task_id)
            if not record.get('ok'):
                collector.record(record['failure'], str(task_id))
                return
            exp = S.from_dict(S.TaskExperience, record['experience'])
            self._check_card(exp, task_id, round_index, skills)
            if progressive:
                # Provenance goes to its own directory (cards/ must equal the task
                # set).  Written before the card: a card without its sidecar could
                # never be skipped safely on resume.
                save(directory / "selection" / f"{task_id}.json", {
                    'task_id': task_id, 'selection': record['selection'],
                    'skill_load': record['skill_load']})
            save(results_dir / f"{exp.task_id}.json", record['experience'])
        if specs:
            if progressive:
                PL.run_generic(specs, PG.execute_progressive_experience,
                               workers=self.config.evolve_l1_workers, on_result=sink)
            else:
                PL.run_generic(specs, LW.execute_experience,
                               workers=self.config.evolve_l1_workers, on_result=sink)
        collector.raise_if_incomplete("Evolution L1 interrupted")
        cards = self._read_evolution_cards(round_index)
        manifest = {
            'round': round_index,
            'skill_keys': {s.family_id: s.key for s in skills},
            'cards': {str(t): S.content_hash(S.to_dict(c)) for t, c in cards.items()},
            'task_ids': sorted(cards),
        }
        if progressive:
            # Derived only from the sidecars, so a resume after a mid-round crash
            # rebuilds exactly what an uninterrupted round froze.
            manifest['routes'] = AU.progressive_routes(directory, sorted(cards))
        freeze(directory / 'manifest.json', manifest)
        return cards

    def _read_evolution_cards(self, round_index):
        directory = self.paths.root / "evolution" / f"round-{round_index}"
        results_dir = directory / "cards"
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
        skills = self._round_input(round_index)
        expected = (sorted(self.plan.tasks_in(S.SPLIT_TRAIN)) if self.config.progressive_library
                    else sorted(t for ids in self.plan.families.values() for t in ids))
        if {p.name for p in results_dir.glob('*.json')} != {f'{t}.json' for t in expected}:
            raise JournalConflict('Round card coverage differs from train tasks')
        cards = {}
        for task_id in expected:
            path = results_dir / f"{task_id}.json"
            if not path.exists():
                raise JournalConflict(f"Missing evolution card for task {task_id}")
            exp = S.from_dict(S.TaskExperience, json.loads(path.read_text()))
            self._check_card(exp, task_id, round_index, skills)
            harbor = (self.cfg.benchmark.name == 'terminalbench'
                      and self.cfg.benchmark.get('rollout', {}).get('mode') == 'harbor_rollout')
            if harbor:
                # TerminalBench units have no in-process L1 checkpoint; the
                # benchmark audits the Harbor rollout and its verdict is kept
                # where the checkpoint would be.
                from skillexpand.benchmarks.terminalbench import audit_harbor_experience
                save(directory / 'trials' / f'{task_id}.json', audit_harbor_experience(exp))
            else:
                from skillexpand.l1.audit import audit_checkpoint
                from skillexpand.l1.adapters import resolve
                checkpoint = json.loads((directory / 'trials' / f'{task_id}.json').read_text())
                if checkpoint['experience'] != S.to_dict(exp):
                    raise JournalConflict(f'Evolution card/checkpoint mismatch: {task_id}')
                audit_checkpoint(checkpoint, resolve(self.cfg))
            if self.config.progressive_library:
                route = AU.progressive_route(directory, task_id)
                if (route['skill_id'], route['skill_key']) != (exp.selected_skill_id,
                                                               exp.initial_skill_key):
                    raise JournalConflict(f'Evolution card/selection mismatch: {task_id}')
                if manifest is not None and manifest.get('routes', {}).get(str(task_id)) != route:
                    raise JournalConflict(f'Evolution route differs from manifest: {task_id}')
            if manifest is not None:
                expected_skill = manifest.get('skill_keys', {}).get(exp.family_id)
                if expected_skill and exp.initial_skill_key != expected_skill:
                    raise JournalConflict(f"Evolution card {task_id} came from a different Skill head")
                if manifest['cards'][str(task_id)] != S.content_hash(S.to_dict(exp)):
                    raise JournalConflict(f'Evolution card hash mismatch: {task_id}')
            cards[task_id] = exp
        return cards

    def _collect_reviewer_feedback(self, round_index):
        """Measure old/candidate pairs on a fixed train panel and update memory.

        This is deliberately after all L2 journals are complete and before the
        round is marked complete.  A partial feedback panel therefore cannot be
        mistaken for a usable Reviewer update on resume.
        """
        if self.config.reviewer_update_mode == "none":
            return self.reviewer_update
        if (self.reviewer_update is not None and
                self.reviewer_update.generation_round >= round_index):
            return self.reviewer_update

        route = RC.FixedTrainRoutes.from_plan(self.plan)
        feedback_root = self.paths.root / "reviewer_feedback"
        feedback_root.mkdir(parents=True, exist_ok=True)
        feedback_path = self.paths.root / "reviewer_feedback.jsonl"
        existing_rows = IO.read_jsonl(feedback_path, repair_tail=False)
        existing_ids = {row.get("feedback_id") for row in existing_rows}
        cache = VA.ScoreCache(feedback_root / "scores.jsonl")
        scorer = VA.FixedSkillScorer(
            self.cfg, cache, route, self.config.l2_review_workers
        )

        def judge_factory(task_id, skill, usage_path):
            return self._reasoning_host("l2_reviewer", usage_path)

        predictor = VA.PredictedSkillScorer(
            self.cfg,
            route,
            VA.ScoreCache(feedback_root / "predicted_scores.jsonl"),
            self.config.l2_review_workers,
            judge_factory=judge_factory,
            calibration_block=self._calibration_block()[0],
            reviewer_prompt_version=self._calibration_block()[1],
        )
        new_records = []
        round_batches = sorted(
            (json.loads(path.read_text()) for path in
             (self.paths.root / "l2_batches").glob("*.json")
             if json.loads(path.read_text()).get("round") == round_index),
            key=lambda value: (value["family_id"], value["task_ids"][0]),
        )
        seen_candidates = set()
        for batch in round_batches:
            base = self.skills.get(batch["base_skill_key"])
            task_ids = tuple(route.groups.get(base.skill_id, ()))
            if self.config.reviewer_feedback_size:
                task_ids = task_ids[: self.config.reviewer_feedback_size]
            if not task_ids:
                raise InvalidInput(f"Reviewer feedback panel is empty for {base.skill_id}")
            candidates = []
            for proposal in batch.get("proposals", ()):
                raw = proposal.get("edit", {}).get("candidate")
                if not raw:
                    continue
                candidate = S.from_dict(S.CandidateSkill, raw)
                if candidate.candidate_id in seen_candidates:
                    continue
                seen_candidates.add(candidate.candidate_id)
                candidates.append(candidate)
            for candidate in candidates:
                panel_key = f"train-feedback:{route.fingerprint}:{base.skill_id}"
                prediction = predictor.validate(
                    base.skill_id, base, candidate.skill, task_ids, panel_key
                )
                old = scorer.score(base, task_ids, panel_key, role=S.ROLE_EVAL)
                new = scorer.score(candidate.skill, task_ids, panel_key, role=S.ROLE_EVAL)
                prompt_version = self._calibration_block()[1]
                rows = RC.make_feedback_records(
                    round_index=round_index,
                    batch_id=batch["batch_id"],
                    family_id=batch["family_id"],
                    base_skill_key=base.key,
                    candidate_skill_key=candidate.skill.key,
                    base_outcomes=old.outcomes,
                    candidate_outcomes=new.outcomes,
                    prediction_rows=prediction.prediction_rows,
                    route_fingerprint=route.fingerprint,
                    panel_key=panel_key,
                    executor_protocol=scorer.protocol_hash,
                    reviewer_prompt_version=prompt_version,
                    reviewer_protocol_hash=(
                        str(prediction.prediction_rows[0].get('reviewer_protocol_hash', ''))
                        if prediction.prediction_rows else ''
                    ),
                )
                for row in rows:
                    if row.feedback_id not in existing_ids:
                        IO.append_jsonl(feedback_path, row)
                        existing_ids.add(row.feedback_id)
                    new_records.append(row)
        # A round with no materialized candidate has no factual calibration
        # evidence.  Keep the previous prompt version and let the next round use
        # the initial prompt (or the last valid update) rather than fabricating a
        # zero-sample update.
        if not new_records:
            return self.reviewer_update
        parent_version = (self.reviewer_update.reviewer_prompt_version
                          if self.reviewer_update else None)
        if self.config.reviewer_update_mode == "rules":
            update = RC.generate_reviewer_update(
                new_records, generation_round=round_index,
                parent_version=parent_version,
                host_factory=lambda: self._reasoning_host(
                    "l2_reviewer",
                    self.paths.root / "usage" / f"reviewer-update-{round_index}.json",
                ),
            )
        else:
            update = RC.build_reviewer_update(
                new_records,
                generation_round=round_index,
                parent_version=parent_version,
            )
        IO.append_jsonl(self.paths.root / "reviewer_updates.jsonl", update)
        self.reviewer_update = update
        # A new update must be picked up by the next round's scorer, while a
        # resumed current round must never silently use a stale prompt object.
        self.predicted_scorer = None
        return update

    def run_evolutions(self, rounds=None):
        """Execute the explicit closed loop: Skill-aware L1, then serial L2."""
        rounds = self.config.evolve_rounds if rounds is None else int(rounds)
        if rounds < 1:
            raise InvalidInput('At least one evolve round is required')
        lock = RunLock(self.paths.lock)
        lock.acquire()
        in_flight = None  # (round_index, batches) of the round being executed
        try:
            self.skills = ST.SkillLibrary(self.paths.skills, benchmark=self.plan.benchmark)
            self._recover_transactions()
            last_result = None
            started_rounds = [int(p.parent.name.split('-')[1]) for p in
                             (self.paths.root / 'evolution').glob('round-*/input.json')]
            if started_rounds and rounds < max(started_rounds):
                raise InvalidInput('Requested horizon precedes an existing round')
            for round_index in range(1, rounds + 1):
                in_flight = (round_index, None)
                round_summary = self.paths.root / 'evolution' / f'round-{round_index}' / 'summary.json'
                if round_summary.exists() and json.loads(round_summary.read_text()).get('status') == 'complete':
                    self.cards = self._read_evolution_cards(round_index)
                    from skillexpand.l2.audit import audit_round
                    audit_round(self.paths.root, round_index)
                    self._collect_reviewer_feedback(round_index)
                    last_result = json.loads(round_summary.read_text())
                    continue
                cards = self._collect_evolution_cards(round_index)
                self.cards = cards
                batches = self._evolution_batches(round_index, cards)
                in_flight = (round_index, batches)
                freeze(round_summary.parent / 'batches.json', batches)
                for batch in batches:
                    self._run_batch(batch)
                self._collect_reviewer_feedback(round_index)
                last_result = self.summary(batches)
                last_result.update({
                    'reviewer_prompt_version': (
                        self.reviewer_update.reviewer_prompt_version
                        if self.reviewer_update else 0
                    ),
                    'reviewer_feedback_count': len(IO.read_jsonl(
                        self.paths.root / 'reviewer_feedback.jsonl', repair_tail=False)),
                })
                save(self.paths.root / 'evolution' / f'round-{round_index}' / 'summary.json',
                     last_result)
                from skillexpand.l2.audit import audit_round
                save(round_summary.parent / 'audit.json', audit_round(self.paths.root, round_index))
            result = dict(last_result)
            result.update({'evolve_rounds': rounds, 'latest_evolution_round': rounds,
                           'l1_cards_are_skill_aware': True})
            save(self.paths.summary, result)
            return result
        except Exception as exc:
            # Report the round that failed, not a horizon-wide count: batch IDs
            # are round-specific, so only that round's batches can be counted.
            progress = {'protocol': PROTOCOL, 'benchmark': self.plan.benchmark}
            if in_flight is not None:
                round_index, batches = in_flight
                progress = dict(self.summary(batches) if batches is not None else progress,
                                evolution_round=round_index)
            save(self.paths.summary, dict(progress, status='needs_attention',
                                          error=f'{type(exc).__name__}: {exc}',
                                          failure_category=classify(exc).value))
            raise
        finally:
            lock.release()

    def run(self):
        return self.run_evolutions()

    def summary(self, batches):
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
            "train_cards": len(self.cards),
            "batches": len(batches),
            "completed_batches": len(records),
            "hypotheses": sum(len(r["hypotheses"]) for r in records),
            "reviewed_candidates": sum(len(r["reviews"]) for r in records),
            "predicted_val_candidates": sum(
                len(r.get("acceptance", {}).get("candidates", ()))
                for r in records
                if r.get("acceptance", {}).get("scope") == "val"
            ),
            "review_approved_updates": sum(
                r["outcome"] == "review_approved" for r in records
            ),
            "skills": {s.skill_id: s.key for s in self.skill_heads()},
            "acceptance_mode": self.config.acceptance_mode,
            "predicted_review_scope": self.config.predicted_review_scope,
            "single_candidate": self.config.single_candidate,
            "reviewer_update_mode": self.config.reviewer_update_mode,
            "reviewer_prompt_version": (
                self.reviewer_update.reviewer_prompt_version
                if self.reviewer_update else 0
            ),
            "reviewer_feedback_count": len(IO.read_jsonl(
                self.paths.root / "reviewer_feedback.jsonl", repair_tail=False)),
            "empirically_validated": bool(records) and self.config.acceptance_mode == "empirical" and all(
                r.get("empirically_validated") is True for r in records
            ),
            "jev_validated": bool(records) and self.config.acceptance_mode == "jev" and all(
                r.get("jev_validated") is True for r in records
            ),
            "val_executions": sum(
                int(r.get("acceptance", {}).get("executions", 0)) for r in records
            ),
            "jev_requests": sum(
                int(r.get("acceptance", {}).get("jev_requests", 0)) for r in records
            ),
            "predicted_val_requests": sum(
                int(r.get("acceptance", {}).get("predicted_requests", 0))
                for r in records
            ),
            "description_frozen": True,
            "l3_enabled": False,
            **(SM.summary_fields(self.config, records)
               if self.config.acceptance_mode == SM.MODE else {}),
        }
