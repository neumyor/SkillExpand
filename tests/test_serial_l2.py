from pathlib import Path
"""Offline integration checks of serial L2, real local SearchQA execution and resume."""

import json
import os
import unittest
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from omegaconf import OmegaConf
from tests import test_experience_first as fixtures
from tests.test_l1_repair import Model
from skillexpand.l1 import artifacts as A
from skillexpand.l1 import cold_start as C
from skillexpand import cli as evolve
from skillexpand.l2 import loop as L
from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from skillexpand.l1 import workers as LW
from skillexpand.evaluation import workers as EW
from skillexpand.reliability.errors import (
    EnvironmentTimeout, ProviderUnavailable, StageIncomplete, UnitFailed,
)
from skillexpand.reliability.units import failure_record
from skillexpand import schema as S
from skillexpand.evaluation import validation as V
from skillexpand.evaluation import routing as R


from skillexpand.l2 import editor as ED


class SerialL2Tests(unittest.TestCase):
    setUp = fixtures.ExperienceFirstTests.setUp
    tearDown = fixtures.ExperienceFirstTests.tearDown
    ask = fixtures.ExperienceFirstTests.ask

    def units(self, specs, worker, workers, on_result, **kw):
        """Run L1 offline; the first Skill-aware attempt fails, so the cards show a gap."""
        output = []
        for spec in specs:
            self.executed.append(spec)
            answers = (["Action 1: Finish[Honda]"] if spec.skill_aware else []) + \
                ["Action 1: Finish[Toyota]"] * 8
            with patch.object(PL, "_config", return_value=self.cfg), patch.object(
                F, "LLM_CLS", side_effect=lambda **kw: Model(answers)
            ):
                item = worker(spec)
            output.append(item)
            on_result(item)
        return output
    cold = fixtures.ExperienceFirstTests.cold

    def prepared(self, batch_size=1, **config):
        plan = self.cold().run()
        C.freeze(
            self.root / "config.json", OmegaConf.to_container(self.cfg, resolve=True)
        )
        # The scripted Planner/Editor speak the rewrite protocol with three
        # candidates; pin it instead of the product defaults.
        config.setdefault("candidate_count", 3)
        config.setdefault("skill_edit_mode", "rewrite")
        return L.SerialEvolutionLoop(
            self.cfg,
            plan,
            L.LoopPaths(self.root),
            L.EvolutionConfig(batch_size=batch_size, **config),
        )

    def hosts(self, driver, fail_review=False, identical=False, tie=False):
        """Scripted Planner/Editor host and predicted-val judge host."""
        self.editor_prompts, self.judge_prompts = [], []

        def editor_llm(messages, **kw):
            self.editor_prompts.append(messages)
            payload = json.loads(messages[-1].content)
            if isinstance(payload, list):
                return json.dumps({"patterns": []})
            if "K" in payload:
                card = payload["cards"][0]
                return json.dumps(
                    {
                        "hypotheses": [
                            {
                                "mechanism": name,
                                "change": name,
                                "evidence": [
                                    {
                                        "card_id": card["card_id"],
                                        "evidence_id": card["evidence"][0]["id"],
                                    }
                                ],
                            }
                            for name in ["inspect evidence", "remove fallback"][
                                : payload["K"]
                            ]
                        ]
                    }
                )
            hyp = payload["selected_hypothesis"]
            self.assertEqual(
                {(e["card_id"], e["id"]) for e in payload["hypothesis_evidence"]},
                {(e["card_id"], e["evidence_id"]) for e in hyp["evidence"]},
            )
            self.assertTrue(
                all(
                    "value" in e and "path" in e for e in payload["hypothesis_evidence"]
                )
            )
            current = json.loads(messages[-2].content)["current_skill"]
            return json.dumps(
                {
                    "description": current["description"],
                    "body": (
                        "NEW inspect evidence"
                        if identical
                        else "NEW " + hyp["mechanism"]
                    ),
                }
            )

        def judge_llm(messages, **kw):
            self.judge_prompts.append(messages)
            if fail_review:
                raise RuntimeError("review service unavailable")
            body = json.loads(messages[-1].content)["skill"]["body"]
            # A Skill that checks the evidence is forecast to succeed; with
            # ``tie`` every Skill gets the same forecast.
            good = tie or "inspect" in body
            return json.dumps({
                "probability_true": 0.8 if good else 0.2,
                "predicted_success": good,
                "reason": "controlled test forecast",
            })

        return (
            SimpleNamespace(token_counter=len, llm=editor_llm),
            SimpleNamespace(token_counter=len, llm=judge_llm),
        )

    @contextmanager
    def models(self, driver, **kwargs):
        """Route Planner/Editor and predicted-judge hosts; val routes are fixed."""
        editor, judge = self.hosts(driver, **kwargs)

        class Routes:
            fingerprint = "test-val-routes"
            groups = {skill.skill_id: (2,) for skill in driver.initial}

        def factory(cfg, path, role=None):
            return judge if "predicted-" in str(path) else editor

        with patch.object(F, "build_reasoning_host", side_effect=factory), patch.object(
            L.FrozenRoutes, "run", return_value=Routes()
        ):
            yield editor, judge

    def run_offline(self, driver, **kwargs):
        with self.models(driver, **kwargs), patch.object(
            PL, "run_generic", side_effect=self.units
        ):
            return driver.run()

    def test_predicted_val_round_isolation_frozen_description_resume_and_audit(self):
        from skillexpand.l2.audit import audit_round
        driver = self.prepared()
        summary = self.run_offline(driver)
        self.assertEqual(summary["completed_batches"], 2)
        self.assertEqual(summary["review_approved_updates"], 1)
        self.assertFalse(summary["empirically_validated"])
        self.assertEqual(summary["val_executions"], 0)
        self.assertGreater(summary["predicted_val_requests"], 0)
        self.assertEqual(
            driver.skill_heads()[0].description, driver.initial[0].description
        )
        self.assertFalse((self.root / "panel_scores.jsonl").exists())
        self.assertFalse((self.root / "meta_decisions.jsonl").exists())
        self.assertTrue(self.judge_prompts)
        for prompt in self.judge_prompts:
            payload = json.loads(prompt[-1].content)
            # The forecast sees only the Skill and a task: no card or trajectory.
            self.assertEqual(set(payload), {"task", "skill", "instructions", "output_schema"})
        for path in (self.root / "l2_batches").glob("*.json"):
            batch = json.loads(path.read_text())
            self.assertEqual(batch["acceptance"]["mode"], "predicted")
            self.assertEqual(batch["acceptance"]["executions"], 0)
            self.assertTrue(batch["acceptance"]["panel"].startswith("val:"))
        self.assertEqual(audit_round(self.root, 1)["review_approved"], 1)
        restored = L.SerialEvolutionLoop(
            self.cfg, driver.plan, L.LoopPaths(self.root), driver.config
        )
        with patch.object(
            F, "build_reasoning_host", side_effect=AssertionError("resume model")
        ):
            self.assertEqual(restored.run(), summary)

    def test_empirical_round_decides_from_the_paired_val_measurement(self):
        from skillexpand.l2.audit import audit_round
        driver = self.prepared(batch_size=50, acceptance_mode="empirical")
        self.assertEqual(driver.config.acceptance_mode, "empirical")

        class Routes:
            fingerprint = "test-val-routes"
            groups = {skill.skill_id: (2,) for skill in driver.initial}

        def units(specs, worker, workers, on_result, **kw):
            if worker is LW.execute_experience:
                return self.units(specs, worker, workers, on_result, **kw)
            self.assertIs(worker, EW.execute_fixed)
            output = []
            for spec in specs:
                helped = spec.skill_body.startswith("NEW inspect")
                actions = ["Finish[Toyota]"] if helped else ["Finish[Honda]"]
                item = {"task_id": spec.task_id, "success": helped, "steps": len(actions),
                        "events": [{"model_text": a, "action": a, "observation": "seen"}
                                   for a in actions],
                        "skill_key": spec.skill_key, "failure": None}
                output.append(item)
                on_result(item)
            return output

        editor, _ = self.hosts(driver)
        with patch.object(PL, "run_generic", side_effect=units), patch.object(
            F, "build_reasoning_host",
            side_effect=lambda cfg, path, role=None: editor
        ), patch.object(L.FrozenRoutes, "run", return_value=Routes()):
            summary = driver.run()
        self.assertTrue(summary["empirically_validated"])
        self.assertEqual(summary["review_approved_updates"], 1)
        self.assertGreater(summary["val_executions"], 0)
        self.assertEqual(driver.skill_heads()[0].version, 1)
        self.assertEqual(driver.skill_heads()[0].body, "NEW inspect evidence")
        batch = json.loads(next((self.root / "l2_batches").glob("*.json")).read_text())
        self.assertEqual(batch["acceptance"]["mode"], "empirical")
        self.assertEqual(batch["acceptance"]["predicted_requests"], 0)
        self.assertEqual(audit_round(self.root, 1)["review_approved"], 1)
        restored = L.SerialEvolutionLoop(
            self.cfg, driver.plan, L.LoopPaths(self.root), driver.config
        )
        with patch.object(
            F, "build_reasoning_host", side_effect=AssertionError("resume model")
        ), patch.object(PL, "run_generic", side_effect=AssertionError("resume execution")):
            self.assertEqual(restored.run(), summary)

    def test_review_failure_resumes_saved_generation(self):
        driver = self.prepared(batch_size=50)
        with self.assertRaises(RuntimeError):
            self.run_offline(driver, fail_review=True)
        self.assertEqual(driver.skill_heads()[0].version, 0)
        with patch.object(
            ED.SkillEditor, "plan", side_effect=AssertionError("replan")
        ), patch.object(
            ED.SkillEditor, "propose", side_effect=AssertionError("regenerate")
        ):
            self.assertEqual(self.run_offline(driver)["review_approved_updates"], 1)

    def test_corrupt_cached_pattern_is_rejected_before_editing(self):
        driver=self.prepared(batch_size=50)
        with self.assertRaises(RuntimeError):
            self.run_offline(driver,fail_review=True)
        path=next((self.root/'l2_patterns').glob('*.json'))
        data=json.loads(path.read_text())
        data['patterns']=[{'id':'P1','text':'invented','support':[],
                           'counter_card_ids':[]}]
        path.write_text(json.dumps(data))
        with patch.object(ED.SkillEditor,'plan',side_effect=AssertionError('editor called')):
            with self.assertRaisesRegex(ValueError,'Batch pattern evidence changed'):
                self.run_offline(driver)

    def test_supported_pattern_reaches_l2_editor_and_round_audit(self):
        driver=self.prepared(batch_size=50)
        observed=[]

        def units(specs,worker,workers,on_result,**kw):
            for spec in specs:
                with (patch.object(PL,'_config',return_value=self.cfg),
                      patch.object(F,'LLM_CLS',side_effect=lambda **kw:
                          Model(['Action 1: Search[Prius]','Action 2: Finish[Toyota]']))):
                    on_result(worker(spec))

        with self.models(driver) as (editor, _), patch.object(
                PL,'run_generic',side_effect=units):
            original=editor.llm

            def patterned(messages,**kw):
                payload=json.loads(messages[-1].content)
                if isinstance(payload,list):
                    return json.dumps({'patterns':[{'text':'Search for the product before answering',
                        'support':[{'card_id':item['card_id'],'evidence_id':'t1:e1'}
                                   for item in payload], 'counter_card_ids':[]}]})
                if 'K' in payload:
                    observed.extend(payload['batch_patterns'])
                return original(messages,**kw)

            editor.llm=patterned
            driver.run_evolutions(1)
        self.assertEqual(len(observed),1)
        self.assertEqual(len(observed[0]['support']),2)
        from skillexpand.l2.audit import audit_round
        self.assertEqual(audit_round(self.root,1)['tasks'],2)

    def test_commit_journal_recovers_before_library_write(self):
        driver = self.prepared(batch_size=50)
        with patch.object(
            L.ST.SkillLibrary, "commit", side_effect=RuntimeError("interruption")
        ):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                self.run_offline(driver)
        restored = L.SerialEvolutionLoop(
            self.cfg, driver.plan, L.LoopPaths(self.root), driver.config
        )
        self.assertEqual(restored.skill_heads()[0].version, 1)
        with patch.object(
            F, "build_reasoning_host", side_effect=AssertionError("model")
        ):
            self.assertEqual(restored.run()["review_approved_updates"], 1)

    def test_duplicate_bodies_are_scored_once(self):
        driver = self.prepared(batch_size=50)
        self.assertEqual(
            self.run_offline(driver, identical=True)["predicted_val_candidates"], 1
        )

    def test_equal_predictions_hold_current_skill(self):
        driver = self.prepared(batch_size=50)
        self.assertEqual(
            self.run_offline(driver, tie=True)["review_approved_updates"], 0
        )
        self.assertEqual(driver.skill_heads()[0].version, 0)

    def test_card_payload_preserves_evidence_context_and_is_deterministic(self):
        card = {"schema_version": 5, "card_id": "one", "task": {"text": "q"},
                "execution": {"success": True}, "claims": [], "claim_status": "valid",
                "evidence": [{"id": "t1:e2", "trial": 1, "phase": "autonomous",
                              "action": "Search[x]", "observation": "observed",
                              "effect": "observed", "method": True,
                              "observation_truncated": True}]}
        exp = SimpleNamespace(experience_id="one", experience_card=card)
        self.assertEqual(ED.card_payload([exp]), ED.card_payload([exp]))
        self.assertEqual(ED.card_payload([exp])[0]["evidence"][0]["id"], "t1:e2")
        self.assertEqual(ED.card_payload([exp])[0]["evidence"][0]["path"], "/evidence/0")
        self.assertTrue(ED.card_payload([exp])[0]["evidence"][0]["value"]["observation_truncated"])

    def test_editor_enforces_frozen_description(self):
        driver = self.prepared()
        host = SimpleNamespace(
            token_counter=len,
            llm=lambda *a, **k: json.dumps({"description": "changed", "body": "new"}),
        )
        editor = ED.SkillEditor(host)
        edit = editor.propose(driver.initial[0], list(driver.cards.values()))
        self.assertIsNone(edit.candidate)

    def test_structured_mode_applies_single_edit_and_replays_audit(self):
        from skillexpand import structured_skill as SS
        from skillexpand.l2.audit import audit_round
        driver = self.prepared(batch_size=50, skill_edit_mode='structured')

        planner_calls = []
        def planner_llm(messages, **kw):
            payload = json.loads(messages[-1].content)
            if isinstance(payload, list):
                return json.dumps({'patterns': []})
            if 'K' in payload:
                planner_calls.append(payload)
                card = payload['cards'][0]
                return json.dumps({'hypotheses': [{
                    'mechanism': 'inspect evidence', 'change': 'Add a source check',
                    'evidence': [{'card_id': card['card_id'],
                                  'evidence_id': card['evidence'][0]['id']}],
                    'edit': {'op': 'add', 'section': 'completion_checks',
                             'target_id': None,
                             'text': 'inspect the supporting source before Finish.'},
                }]})

        with self.models(driver) as (editor, _), patch.object(
            PL, 'run_generic', side_effect=self.units
        ):
            editor.llm = planner_llm
            result = driver.run_evolutions(1)
        self.assertTrue(planner_calls)
        self.assertEqual(result['review_approved_updates'], 1)
        old = SS.from_legacy(driver.initial[0].body)
        new = SS.parse(driver.skill_heads()[0].body)
        self.assertEqual(new['procedure'], old['procedure'])
        self.assertEqual(new['conditions'], old['conditions'])
        self.assertEqual([r['id'] for r in new['completion_checks']], ['V1'])
        self.assertEqual(audit_round(self.root, 1)['review_approved'], 1)
        restored = L.SerialEvolutionLoop(self.cfg, driver.plan, L.LoopPaths(self.root), driver.config)
        with patch.object(F, 'build_reasoning_host', side_effect=AssertionError('resume model')):
            self.assertEqual(restored.run_evolutions(1), result)
        with self.assertRaisesRegex(ValueError, 'Frozen inputs changed'):
            L.SerialEvolutionLoop(self.cfg, driver.plan, L.LoopPaths(self.root),
                                  L.EvolutionConfig(batch_size=50, skill_edit_mode='rewrite'))

    def test_sampled_round_runs_end_to_end_resumes_and_audits_offline(self):
        """One full sampled round through the real loop, journal and round audit.

        Every unit test of the sampled modules passes with stubs; this is the
        test that proves the pieces are actually wired together.  A one-task
        val panel cannot bound the estimate and must hold; a two-task panel
        whose sample agrees must install the candidate.
        """
        for val_tasks in ((2,), (1, 2)):
            with self.subTest(val_tasks=val_tasks):
                self.tearDown()
                self.setUp()
                assignment = {0: "train", 1: "train", 2: "val", 3: "test"}
                assignment.update({t: "val" for t in val_tasks})
                self.plan = S.SplitPlan.make(assignment, "searchqa", 42)
                self._sampled_round(val_tasks)

    def test_sampled_second_round_shows_both_memories_and_audits(self):
        """Round two is the first round in which both memories hold anything."""
        from skillexpand.l2.audit import audit_round
        self.plan = S.SplitPlan.make({0: "train", 1: "val", 2: "val", 3: "test"},
                                     "searchqa", 42)
        driver, calls = self._sampled_round((1, 2), rounds=2)
        self.assertEqual([audit_round(self.root, r)["review_approved"] for r in (1, 2)],
                         [1, 0])
        second = next(b for b in (json.loads(p.read_text())
                                  for p in (self.root / "l2_batches").glob("*.json"))
                      if b["round"] == 2)
        self.assertEqual(second["reviewer_memory_version"], 1)
        self.assertIn("completion_checks/add: 1 proposal(s) -> 1 effective",
                      second["planner_memory"])
        self.assertNotIn("Prius", second["planner_memory"])
        reviewer_prompt = json.loads(calls["l2_reviewer"][-1][-1].content)
        self.assertIn("Prius", reviewer_prompt["reviewer_memory"])

    def _sampled_round(self, val_tasks, rounds=1):
        from skillexpand.l2.audit import audit_round
        driver = self.prepared(batch_size=50, skill_edit_mode="structured",
                               acceptance_mode="sampled", candidate_count=1,
                               acceptance_sample_size=2)
        calls = {"l2_reviewer": [], "l2_verifier": []}
        rule = "inspect the supporting source before Finish."

        def planner(messages, **kw):
            payload = json.loads(messages[-1].content)
            if isinstance(payload, list):
                return json.dumps({"patterns": []})
            card = payload["cards"][0]
            return json.dumps({"hypotheses": [{
                "mechanism": "inspect evidence", "change": "Add a source check",
                "claim": {"trigger": "an answer is about to be submitted",
                          "action_change": "search the source before Finish"},
                "evidence": [{"card_id": card["card_id"],
                              "evidence_id": card["evidence"][0]["id"]}],
                "edit": {"op": "add", "section": "completion_checks",
                         "target_id": None, "text": rule}}]})

        replies = {
            "l2_reviewer": {"trigger_probability": 0.9, "delta_probability": 0.5,
                            "reason": "fires before every answer"},
            "l2_verifier": {"category": "claim_confirmed", "first_difference_step": 1,
                            "reason": "the extra search precedes Finish"},
        }

        def factory(cfg, path, role=None):
            if role in replies:
                def llm(messages, **kw):
                    calls[role].append(messages)
                    return json.dumps(replies[role])
                return SimpleNamespace(token_counter=len, llm=llm)
            return SimpleNamespace(token_counter=len, llm=planner)

        class Routes:
            fingerprint = "val-routes"
            groups = {skill.skill_id: val_tasks for skill in driver.initial}

        def units(specs, worker, workers, on_result, **kw):
            if worker is LW.execute_experience:
                return self.units(specs, worker, workers, on_result, **kw)
            self.assertIs(worker, EW.execute_fixed)
            output = []
            for spec in specs:
                helped = rule in spec.skill_body
                actions = ["Search[source]", "Finish[Toyota]"] if helped else ["Finish[Honda]"]
                item = {"task_id": spec.task_id, "success": helped, "steps": len(actions),
                        "events": [{"model_text": a, "action": a, "observation": "seen"}
                                   for a in actions],
                        "skill_key": spec.skill_key, "failure": None}
                output.append(item)
                on_result(item)
            return output

        with patch.object(PL, "run_generic", side_effect=units), patch.object(
                F, "build_reasoning_host", side_effect=factory), patch.object(
                L.FrozenRoutes, "run", return_value=Routes()):
            result = driver.run_evolutions(rounds)
        if rounds > 1:
            return driver, calls

        n = len(val_tasks)
        accepted = int(n >= 2)
        self.assertEqual(result["review_approved_updates"], accepted)
        self.assertEqual(driver.skill_heads()[0].version, accepted)
        self.assertEqual(result["val_executions"], 2 * n)
        self.assertEqual((len(calls["l2_reviewer"]), len(calls["l2_verifier"])), (n, n))
        self.assertEqual(result["reviewer_metrics"]["sampled_pairs"], n)
        batch = json.loads(next((self.root / "l2_batches").glob("*.json")).read_text())
        decision = batch["acceptance"]["candidates"][0]["result"]["decision"]
        self.assertEqual(decision["reason"], "accepted" if accepted else "insufficient_sample")
        self.assertEqual((batch["planner_memory"], batch["reviewer_memory_version"]), ("", 0))
        self.assertEqual(audit_round(self.root, 1)["review_approved"], accepted)
        restored = L.SerialEvolutionLoop(self.cfg, driver.plan, L.LoopPaths(self.root),
                                         driver.config)
        with patch.object(F, "build_reasoning_host", side_effect=AssertionError("resume model")), \
                patch.object(PL, "run_generic", side_effect=AssertionError("resume execution")), \
                patch.object(L.FrozenRoutes, "run", return_value=Routes()):
            self.assertEqual(restored.run_evolutions(1), result)

    def test_structured_editor_rejects_whole_body_response(self):
        driver = self.prepared(skill_edit_mode='structured')
        host = SimpleNamespace(token_counter=len,
                               llm=lambda *a, **kw: json.dumps({'body': 'rewrite everything'}))
        editor = ED.SkillEditor(host, skill_edit_mode='structured')
        outcome = editor.propose(driver.initial[0], list(driver.cards.values()))
        self.assertIsNone(outcome.candidate)
        self.assertEqual(outcome.reason, ED.REASON_NO_OPERATIONS)

    def test_cli_defaults_and_tail_batches(self):
        # The product defaults are the single-candidate structured protocol with
        # predicted val acceptance; this fixture overrides them above.
        defaults = L.EvolutionConfig()
        self.assertEqual((defaults.candidate_count, defaults.batch_size), (1, 50))
        self.assertEqual((defaults.skill_edit_mode, defaults.acceptance_mode),
                         ("structured", "predicted"))
        self.assertEqual([len(b) for b in L.family_task_batches(range(123), 50)], [50, 50, 23])
        parsed = evolve.build_parser().parse_args(["--run-dir", "x"])
        self.assertEqual((parsed.candidate_count, parsed.skill_edit_mode), (1, "structured"))
        self.assertEqual(parsed.acceptance_mode, "predicted")

    def test_evolve_round_runs_skill_aware_l1_then_l2_and_resumes(self):
        driver = self.prepared(batch_size=1)
        with self.models(driver), patch.object(PL, "run_generic", side_effect=self.units):
            result = driver.run_evolutions(1)
        self.assertEqual(result["latest_evolution_round"], 1)
        self.assertEqual(driver.skill_heads()[0].version, 1)
        card = json.loads((self.root / "evolution/round-1/cards/0.json").read_text())
        self.assertEqual(card["evolution_round"], 1)
        self.assertEqual(card["initial_skill_key"], "searchqa.family-p001@v0")

        resumed = L.SerialEvolutionLoop(
            driver.cfg, driver.plan, L.LoopPaths(self.root),
            L.EvolutionConfig(batch_size=1, candidate_count=3, skill_edit_mode="rewrite"),
        )
        with patch.object(PL, "run_generic", side_effect=AssertionError("resampled")), patch.object(
            F, "build_reasoning_host", side_effect=AssertionError("resampled")
        ):
            resumed_result = resumed.run_evolutions(1)
        self.assertEqual(resumed_result["latest_evolution_round"], 1)
        self.assertEqual(resumed.skill_heads()[0].key, driver.skill_heads()[0].key)

    def test_partial_round_resume_and_extend_use_real_l1(self):
        driver = self.prepared(batch_size=1)
        original = driver._run_batch
        calls = []
        def crash(batch):
            calls.append(batch['task_ids'])
            if len(calls) == 2:
                raise TimeoutError('between batches')
            original(batch)
        with self.models(driver), patch.object(
                PL, 'run_generic', side_effect=self.units), patch.object(
                driver, '_run_batch', side_effect=crash):
            with self.assertRaises(TimeoutError):
                driver.run_evolutions(1)
        failed = json.loads((self.root/'summary.json').read_text())
        self.assertEqual(failed['status'], 'needs_attention')
        self.assertEqual(failed['evolution_round'], 1)
        self.assertEqual(failed['batches'], len(json.loads(
            (self.root/'evolution/round-1/batches.json').read_text())))
        self.assertEqual(failed['completed_batches'], 1)
        frozen = (self.root/'evolution/round-1/input.json').read_bytes()
        resumed = L.SerialEvolutionLoop(driver.cfg, driver.plan, L.LoopPaths(self.root), driver.config)
        with self.models(resumed), patch.object(
                PL, 'run_generic', side_effect=AssertionError('L1 repeated')):
            resumed.run_evolutions(1)
        self.assertEqual((self.root/'evolution/round-1/input.json').read_bytes(), frozen)
        with self.models(resumed), patch.object(PL, 'run_generic', side_effect=self.units):
            resumed.run_evolutions(2)
        from skillexpand.l2.audit import audit_round
        for index in (1, 2):
            self.assertEqual(audit_round(self.root, index)['tasks'], 2)
        second = json.loads((self.root/'evolution/round-2/cards/0.json').read_text())
        self.assertEqual(second['initial_skill_key'], 'searchqa.family-p001@v1')
        first = json.loads((self.root/'evolution/round-1/cards/0.json').read_text())
        self.assertNotEqual(first['experience_id'], second['experience_id'])
        from skillexpand.l1.audit import audit_checkpoint
        from skillexpand.l1.adapters import resolve
        checkpoint = json.loads((self.root/'evolution/round-2/trials/0.json').read_text())
        audit_checkpoint(checkpoint, resolve(self.cfg))
        second['experience_card']['claims'] = [{'text': 'tampered'}]
        (self.root/'evolution/round-2/cards/0.json').write_text(json.dumps(second))
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            audit_round(self.root, 2)

    def test_partial_l1_resumes_missing_task_before_any_l2(self):
        driver = self.prepared(batch_size=50)
        def interrupted(specs, worker, workers, on_result, **kw):
            self.units(specs[:1], worker, workers, on_result, **kw)
            on_result({'task_id': specs[1].task_id, 'ok': False,
                       'failure': failure_record(EnvironmentTimeout('timeout'),
                                                 unit_id=specs[1].task_id, stage='l1')})
        with patch.object(PL, 'run_generic', side_effect=interrupted), patch.object(
                F, 'build_reasoning_host', side_effect=AssertionError('L2 before complete L1')):
            with self.assertRaisesRegex(RuntimeError, 'L1 interrupted'):
                driver.run()
        self.assertFalse((self.root/'l2_batches').exists())
        saved = (self.root/'evolution/round-1/cards/0.json').read_bytes()
        def remaining(specs, *args, **kw):
            self.assertEqual([s.task_id for s in specs], [1])
            return self.units(specs, *args, **kw)
        with self.models(driver), patch.object(PL, 'run_generic', side_effect=remaining):
            driver.run()
        self.assertEqual((self.root/'evolution/round-1/cards/0.json').read_bytes(), saved)

    def test_round_audit_rejects_checkpoint_and_raw_response_corruption(self):
        from skillexpand.l2.audit import audit_round
        driver = self.prepared(batch_size=50)
        self.run_offline(driver)
        checkpoint = self.root/'evolution/round-1/trials/0.json'
        saved = checkpoint.read_text()
        data = json.loads(saved)
        data['trials'][0]['success'] = not data['trials'][0]['success']
        checkpoint.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            audit_round(self.root, 1)
        checkpoint.write_text(saved)
        response = next((self.root/'l2_proposals').glob('*/hypotheses-*.json'))
        original = response.read_text()
        response.write_text(json.dumps(dict(json.loads(original), raw='{}')))
        with self.assertRaises(ValueError):
            audit_round(self.root, 1)
        response.write_text(original)
        # The batch decision journal is replayed by the round audit alone.
        journal = next((self.root/'l2_batches').glob('*.json'))
        batch = json.loads(journal.read_text())
        journal.write_text(json.dumps(dict(batch, reason='tampered')))
        with self.assertRaises(ValueError):
            audit_round(self.root, 1)

    def test_cli_import_and_test_remain_separate(self):
        driver = self.prepared()
        target = self.root.parent / "cli-import"
        with self.models(driver), patch.object(
            PL, "run_generic", side_effect=self.units
        ):
            evolve.main(
                [
                    "--benchmark",
                    "searchqa",
                    "--cold-start-dir",
                    str(self.root),
                    "--run-dir",
                    str(target),
                    "--phase",
                    "l2",
                    "--candidate-count", "3",
                    "--skill-edit-mode", "rewrite",
                ]
            )
        summary = json.loads((target / "summary.json").read_text())
        self.assertEqual(summary["review_approved_updates"], 1)
        self.assertEqual(len(list((target/'discovery/initial_skills').glob('*-patterns.json'))),2)
        self.assertEqual(summary["val_executions"], 0)
        cfg, plan, _, _ = A.load_cold_start(target)
        with patch.object(PL, "run_generic", side_effect=self.fake_units):
            final = evolve.test_evaluate(cfg, plan, target, 1)
        self.assertEqual(final["tasks"], 1)
        self.assertEqual(final["successes"], 1)
        from skillexpand.evaluation.audit import audit_test
        final_dir = target/'test'/final['library_hash']
        self.assertEqual(audit_test(target, final_dir)['measured'], 1)
        summary = json.loads((final_dir/'summary.json').read_text())
        summary['successes'] = 0
        (final_dir/'summary.json').write_text(json.dumps(summary))
        with self.assertRaisesRegex(ValueError, 'summary differs'):
            audit_test(target, final_dir)

    def _cli_l2(self, driver, target, *extra):
        with self.models(driver), patch.object(PL, "run_generic", side_effect=self.units):
            evolve.main(["--benchmark", "searchqa", "--cold-start-dir", str(self.root),
                         "--run-dir", str(target), "--phase", "l2", "--candidate-count", "3",
                         "--skill-edit-mode", "rewrite", *extra])

    def test_relay_start_leaves_frozen_config_byte_identical(self):
        driver = self.prepared()
        plain, relayed = self.root.parent / "plain", self.root.parent / "relayed"
        self._cli_l2(driver, plain)

        class Relay:
            transport = SimpleNamespace(sandbox_id="sandbox")
            start = staticmethod(lambda: "http://127.0.0.1:1/v1")
            close = staticmethod(lambda: None)

        with patch("skillexpand.runtime.llm_relay.relay_from_env", return_value=Relay()), \
                patch.dict(os.environ, OPENAI_API_KEY="k"):
            self._cli_l2(driver, relayed, "--llm-relay")
        self.assertEqual((plain / "config.json").read_bytes(), (relayed / "config.json").read_bytes())
        self.assertTrue((relayed / "relay_manifest.json").exists())
        self.assertNotIn("EXPE_LLM_RELAY_REQUIRED", os.environ)

    def test_resume_with_a_different_endpoint_is_accepted(self):
        driver = self.prepared()
        target = self.root.parent / "endpoint"
        with patch.dict(os.environ, EXPE_LLM_BASE_URL="http://one.invalid/v1", OPENAI_API_KEY="k"):
            self._cli_l2(driver, target)
        before = (target / "l2_manifest.json").read_bytes()
        with patch.dict(os.environ, EXPE_LLM_BASE_URL="http://two.invalid/v1", OPENAI_API_KEY="k"):
            self._cli_l2(driver, target, "--resume")
        self.assertEqual((target / "l2_manifest.json").read_bytes(), before)

    def test_changed_candidate_count_or_model_name_is_a_protocol_change(self):
        from skillexpand.reliability.errors import FrozenProtocolChanged
        driver = self.prepared()
        with self.assertRaises(FrozenProtocolChanged):
            L.SerialEvolutionLoop(self.cfg, driver.plan, L.LoopPaths(self.root),
                                  L.EvolutionConfig(batch_size=1, candidate_count=2,
                                                    skill_edit_mode="rewrite"))
        target = self.root.parent / "model"
        self._cli_l2(driver, target)
        with self.assertRaises(FrozenProtocolChanged):
            self._cli_l2(driver, target, "--resume", "--l2-planner-model", "other-model")

    def test_changed_method_prompt_is_a_protocol_change(self):
        from skillexpand.l1 import family_discovery as FD
        from skillexpand.reliability.errors import FrozenProtocolChanged
        driver = self.prepared()
        for module, name in ((ED, "EDITING_STRATEGY"), (V.PredictedSkillScorer, "INSTRUCTIONS")):
            with patch.object(module, name, getattr(module, name) + " changed"):
                with self.assertRaises(FrozenProtocolChanged):
                    L.SerialEvolutionLoop(self.cfg, driver.plan, L.LoopPaths(self.root),
                                          driver.config)
        with patch.object(FD, "FAMILY_CONTRACT", FD.FAMILY_CONTRACT + " changed"):
            with self.assertRaises(FrozenProtocolChanged):
                self.cold()

    def test_snapshot_reuses_frozen_routes(self):
        import importlib.util
        from skillexpand.evaluation.routing import FrozenRoutes
        from skillexpand.evaluation.snapshots import evaluate_library

        spec = importlib.util.spec_from_file_location(
            "evaluate_snapshot", Path(__file__).resolve().parents[1] / "scripts/evaluate_snapshot.py")
        ES = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ES)
        driver = self.prepared()
        with patch.object(PL, "run_generic", side_effect=self.fake_units):
            canonical = evolve.test_evaluate(self.cfg, driver.plan, self.root, 1)
        skills = ES.snapshot(self.root, driver.initial, driver.plan, 0)
        routes = FrozenRoutes(self.cfg, driver.plan, driver.initial, self.root / "routes",
                              S.SPLIT_TEST, 1).run()
        with patch.object(PL, "run_generic", side_effect=self.fake_units):
            result = evaluate_library(self.cfg, driver.plan, self.root, skills, driver.initial,
                                      routes, self.root.parent / "snapshot-0", 1,
                                      summary_extra={"evolution_round": 0})
        self.assertEqual(result["successes"], canonical["successes"])
        self.assertEqual(result["evolution_round"], 0)

    def test_snapshot_evaluation_continues_after_one_group_fails(self):
        from types import SimpleNamespace
        from skillexpand.evaluation import snapshots as SN

        skills = [S.Skill(f"searchqa.{f}", f, 0, f, "d", "b", S.Provenance(rationale="t"))
                  for f in ("family-a", "family-b")]
        routes = SimpleNamespace(groups={skills[0].skill_id: (1,), skills[1].skill_id: (2,)},
                                 fingerprint="f", failed_task_ids=())
        calls = []

        class Scorer:
            protocol_hash = "h"

            def __init__(self, *args):
                pass

            def score(self, skill, task_ids, panel):
                calls.append(skill.skill_id)
                if skill.skill_id == skills[0].skill_id:
                    raise StageIncomplete("provider down", [failure_record(
                        ProviderUnavailable("down"), unit_id=1, stage="fixed-execution")])
                return SimpleNamespace(n=1, successes=1, score=1.0)

        target = self.root / "snapshot-flaky"
        with patch.object(SN, "FixedSkillScorer", Scorer):
            with self.assertRaisesRegex(RuntimeError, "failed groups"):
                SN.evaluate_library(self.cfg, None, self.root, skills, skills, routes, target, 1)
        self.assertEqual(calls, [s.skill_id for s in skills])
        progress = json.loads((target / "progress.json").read_text())
        self.assertEqual([f["skill_id"] for f in progress["failed_skills"]], [skills[0].skill_id])
        self.assertIn(skills[1].skill_id, progress["completed_skills"])
        self.assertTrue((target / "skills" / f"{skills[1].skill_id}.json").exists())
        self.assertFalse((target / "summary.json").exists())

        calls.clear()

        class Buggy(Scorer):
            def score(self, skill, task_ids, panel):
                calls.append(skill.skill_id)
                raise UnitFailed(failure_record(KeyError("bug"), unit_id=1, stage="x"))

        with patch.object(SN, "FixedSkillScorer", Buggy):
            with self.assertRaises(UnitFailed):
                SN.evaluate_library(self.cfg, None, self.root, skills, skills, routes,
                                    self.root / "snapshot-bug", 1)
        self.assertEqual(calls, [skills[0].skill_id])

    def test_failed_executor_keeps_partial_events(self):
        driver = self.prepared()
        skill = driver.initial[0]
        spec = EW.FixedSpec("partial", "searchqa", 2, skill_key=skill.key,
            skill_body=skill.body,
        )

        def execute(adapter, state, supervision, on_event):
            on_event(
                [
                    {
                        "model_text": "Action 1: Search[Toyota]",
                        "action": "Search[Toyota]",
                        "observation": "Toyota evidence",
                    }
                ]
            )
            raise RuntimeError("provider disconnected")

        agent = SimpleNamespace(execute_trial=execute)
        adapter = SimpleNamespace(configure=lambda _: None)
        with patch.object(PL, "_config", return_value=self.cfg), patch.object(
            F, "build_agent", return_value=agent
        ), patch("skillexpand.l1.adapters.resolve", return_value=adapter):
            result = EW.execute_fixed(spec)
        self.assertIn("provider disconnected", result["failure"]["message"])
        self.assertEqual(result["steps"], 1)
        self.assertEqual(len(result["events"]), 1)
        self.assertIn("Toyota evidence", result["trajectory"])

    def fake_units(self, specs, worker, workers, on_result, **kwargs):
        output = []
        for spec in specs:
            if isinstance(spec, R.RouteSpec):
                self.assertEqual(set(spec.descriptions[0]), {"skill_id", "description"})
                item = {
                    "task_id": spec.task_id,
                    "selection": {
                        "ok": True,
                        "skill_id": spec.descriptions[0]["skill_id"],
                        "raw": "SKILL: " + spec.descriptions[0]["skill_id"],
                    },
                    "failure": None,
                }
            elif worker is EW.execute_fixed:
                item = {
                    "task_id": spec.task_id,
                    "success": spec.skill_body.startswith("NEW"),
                    "events": [{"observation": "scripted outcome", "environment":
                                {"success": spec.skill_body.startswith("NEW")}}],
                    "steps": 1,
                    "skill_key": spec.skill_key,
                    "failure": None,
                }
            else:
                raise AssertionError("L2 must not collect or execute train tasks")
            self.executed.append(spec)
            output.append(item)
            on_result(item)
        return output

    def test_incomplete_or_mutated_input_is_rejected_before_models(self):
        driver = self.prepared()
        path = self.root / "discovery/results/1.json"
        original = path.read_text()
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            L.SerialEvolutionLoop(
                self.cfg, driver.plan, L.LoopPaths(self.root), driver.config
            )
        path.write_text(original)
        value = json.loads(original)
        value["experience_card"]["claims"] = [{"text": "modified"}]
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "discovery hashes|Frozen inputs"):
            L.SerialEvolutionLoop(
                self.cfg, driver.plan, L.LoopPaths(self.root), driver.config
            )

    def test_frozen_routes_are_disjoint_and_val_cannot_read_test(self):
        driver = self.prepared()
        plan = S.SplitPlan.make(
            {0: "train", 1: "val", 2: "val", 3: "test"}, "searchqa", 42
        )
        one = driver.initial[0]
        two = replace(
            one, skill_id="searchqa.two", family_id="two", description="Other scope"
        )

        def route(specs, worker, workers, on_result, **kw):
            for s in specs:
                self.assertIn(s.task_id, (1, 2))
                on_result(
                    {
                        "task_id": s.task_id,
                        "selection": {
                            "ok": True,
                            "skill_id": (
                                one.skill_id if s.task_id == 1 else two.skill_id
                            ),
                        },
                        "failure": None,
                    }
                )

        with patch.object(PL, "run_generic", side_effect=route):
            routes = R.FrozenRoutes(
                self.cfg, plan, [one, two], self.root / "isolated", "val"
            ).run()
        self.assertEqual(routes.groups, {one.skill_id: (1,), two.skill_id: (2,)})
        scorer = V.FixedSkillScorer(
            self.cfg, V.ScoreCache(self.root / "isolated-scores"), routes
        )
        with self.assertRaisesRegex(ValueError, "belong"):
            scorer.score(one, [2], "panel")

    def test_description_only_edit_reuses_body_scores_and_cannot_win(self):
        driver = self.prepared()
        with patch.object(PL, "run_generic", side_effect=self.fake_units):
            routes = R.FrozenRoutes(
                self.cfg, driver.plan, driver.initial, self.root / "routes", "val"
            ).run()
            scorer = V.FixedSkillScorer(
                self.cfg, V.ScoreCache(self.root / "scores.jsonl"), routes
            )
            base = driver.initial[0]
            candidate = replace(base, version=1, description="Different scope")
            before = len(self.executed)
            result = scorer.validate(base.skill_id, base, candidate, [2], "panel")
            self.assertFalse(result.passed)
            self.assertEqual(len(self.executed) - before, 1)
            self.assertEqual(result.score_before, result.score_after)

    def test_real_fixed_executor_uses_one_attempt_without_selector_or_guidance(self):
        driver = self.prepared()
        skill = driver.initial[0]
        models = []

        def model(**kwargs):
            obj = Model(["Action 1: Finish[Toyota]"])
            models.append(obj)
            return obj

        spec = EW.FixedSpec("fixed", "searchqa", 2, skill_key=skill.key,
            skill_body=skill.body,
        )
        with (
            patch.object(PL, "_config", return_value=self.cfg),
            patch.object(F, "LLM_CLS", side_effect=model),
            patch.object(
                R.SkillSelector,
                "select",
                side_effect=AssertionError("No routing during measurement"),
            ),
        ):
            result = EW.execute_fixed(spec)
        self.assertTrue(result["success"], result)
        self.assertIn("Finish[Toyota]", result["trajectory"])
        self.assertTrue(result["events"])
        self.assertEqual(len(models[0].prompts), 1)
        self.assertNotIn("Reference answer:", models[0].prompts[0])

    def test_routing_provider_error_resumes_only_missing_task(self):
        driver = self.prepared()
        root = self.root / "other_routes"

        def broken(specs, worker, workers, on_result, **kw):
            for spec in specs:
                on_result({"task_id": spec.task_id, "failure": failure_record(ProviderUnavailable("offline"),
                                                     unit_id=spec.task_id, stage="routing")})

        routes = R.FrozenRoutes(
            self.cfg, driver.plan, driver.initial, root, "val"
        )
        with patch.object(PL, "run_generic", side_effect=broken):
            with self.assertRaisesRegex(RuntimeError, "Routing incomplete"):
                routes.run()
        self.assertEqual(routes.records, {})
        self.assertFalse((root / "val/complete.json").exists())
        with patch.object(PL, "run_generic", side_effect=self.fake_units):
            restored = R.FrozenRoutes(
                self.cfg, driver.plan, driver.initial, root, "val"
            ).run()
        self.assertEqual(restored.groups[driver.initial[0].skill_id], (2,))

    def test_corrupt_task_file_cannot_hide_behind_task_table_cache(self):
        self.prepared()
        values = json.loads(self.tasks.read_text())
        values[0]["question"] = "Different task"
        self.tasks.write_text(json.dumps(values))
        with self.assertRaisesRegex(ValueError, "task data changed"):
            A.load_cold_start(self.root)

    def test_fixed_executor_reports_service_error_instead_of_task_failure(self):
        driver = self.prepared()
        skill = driver.initial[0]
        spec = EW.FixedSpec("error", "searchqa", 2, skill_key=skill.key,
            skill_body=skill.body,
        )
        with (
            patch.object(PL, "_config", return_value=self.cfg),
            patch.object(
                F, "build_agent", side_effect=RuntimeError("service unavailable")
            ),
        ):
            result = EW.execute_fixed(spec)
        self.assertIn("service unavailable", result["failure"]["message"])

    def test_final_routing_failure_remains_in_overall_denominator(self):
        driver = self.prepared()

        def bad_route(specs, worker, workers, on_result, **kw):
            for spec in specs:
                on_result(
                    {
                        "task_id": spec.task_id,
                        "selection": {"ok": False, "skill_id": ""},
                        "failure": None,
                    }
                )

        with patch.object(PL, "run_generic", side_effect=bad_route):
            result = evolve.test_evaluate(self.cfg, driver.plan, self.root, 1)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(result["routing_failures"], [3])
        self.assertIsNone(result["per_skill"][driver.initial[0].skill_id]["score"])

    def test_cold_start_requires_card_hash_file(self):
        self.prepared()
        (self.root / "discovery/card_hashes.json").unlink()
        with self.assertRaises(FileNotFoundError):
            A.load_cold_start(self.root)

    def test_l1_failure_summary_names_the_failing_round(self):
        driver = self.prepared()
        with patch.object(driver, "_collect_evolution_cards", side_effect=TimeoutError("L1 down")):
            with self.assertRaises(TimeoutError):
                driver.run_evolutions(1)
        failed = json.loads((self.root / "summary.json").read_text())
        self.assertEqual((failed["status"], failed["evolution_round"]), ("needs_attention", 1))
        self.assertNotIn("completed_batches", failed)

    def test_swapping_val_and_test_cannot_change_frozen_protocol(self):
        self.prepared()
        path = self.root / "split.json"
        value = json.loads(path.read_text())
        value["assignment"]["2"] = "test"
        value["assignment"]["3"] = "val"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "frozen manifest"):
            A.load_cold_start(self.root)
