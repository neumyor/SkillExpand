"""Offline integration checks of serial L2, real local SearchQA execution and resume."""

import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from omegaconf import OmegaConf
from tests import test_experience_first as fixtures
from tests.test_l1_repair import Model
from skillexpand.persistence import artifacts as A
from skillexpand.l1 import cold_start as C
from skillexpand import cli as evolve
from skillexpand.l2 import loop as L
from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from skillexpand import schema as S
from skillexpand.evaluation import validation as V
from skillexpand.evaluation import routing as R


from skillexpand.l2 import card_review as CR
from skillexpand.l2 import editor as ED


class SerialL2Tests(unittest.TestCase):
    setUp = fixtures.ExperienceFirstTests.setUp
    tearDown = fixtures.ExperienceFirstTests.tearDown
    ask = fixtures.ExperienceFirstTests.ask
    units = fixtures.ExperienceFirstTests.units
    cold = fixtures.ExperienceFirstTests.cold

    def prepared(self, batch_size=1, **config):
        plan = self.cold().run()
        C.freeze(
            self.root / "config.json", OmegaConf.to_container(self.cfg, resolve=True)
        )
        return L.SerialEvolutionLoop(
            self.cfg,
            plan,
            L.LoopPaths(self.root),
            L.EvolutionConfig(batch_size=batch_size, **config),
        )

    def hosts(self, driver, fail_review=False, identical=False, tie=False):
        self.editor_prompts, self.reviewer_prompts = [], []

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

        def reviewer_llm(messages, **kw):
            self.reviewer_prompts.append(messages)
            if fail_review:
                raise RuntimeError("review service unavailable")
            payload = json.loads(messages[-1].content)
            self.assertEqual(set(payload), {"current_skill_key", "current_rules", "candidates", "card"})
            entries = []
            for c in payload["candidates"]:
                good = tie or any("inspect" in r["text"] for r in c["rules"])
                entries.append(
                    {
                        "id": c["id"],
                        "label": "improve" if good else "unchanged",
                        "evidence_ids": [payload["card"]["evidence"][0]["id"]],
                        "rule_ids": c["changed_rule_ids"][:1],
                        "reason": "Inspection supplies the missing evidence check.",
                    }
                )
            return json.dumps({"candidates": entries})

        return (
            SimpleNamespace(token_counter=len, llm=editor_llm),
            SimpleNamespace(token_counter=len, llm=reviewer_llm),
        )

    def run_offline(self, driver, **kwargs):
        editor, reviewer = self.hosts(driver, **kwargs)

        def factory(cfg, path):
            return reviewer if "reviewer-" in str(path) else editor

        with patch.object(F, "build_reasoning_host", side_effect=factory), patch.object(
            PL,
            "run_generic",
            side_effect=self.units,
        ):
            return driver.run()

    def test_review_only_batch_isolation_frozen_description_l3_and_resume(self):
        driver = self.prepared()
        summary = self.run_offline(driver)
        self.assertEqual(summary["completed_batches"], 2)
        self.assertEqual(summary["review_approved_updates"], 1)
        self.assertFalse(summary["empirically_validated"])
        self.assertEqual(summary["admission_executions"], 0)
        self.assertEqual(driver.meta.head().version, 0)
        self.assertEqual(
            driver.skill_heads()[0].description, driver.initial[0].description
        )
        self.assertFalse((self.root / "routes").exists())
        self.assertFalse((self.root / "panel_scores.jsonl").exists())
        self.assertFalse((self.root / "meta_decisions.jsonl").exists())
        for prompt in self.reviewer_prompts:
            payload = json.loads(prompt[-1].content)
            self.assertIn("card_id", payload["card"])
            self.assertNotIn("cards", payload)
            self.assertNotIn("selected_hypothesis", payload)
        restored = L.SerialEvolutionLoop(
            self.cfg, driver.plan, L.LoopPaths(self.root), driver.config
        )
        with patch.object(
            F, "build_reasoning_host", side_effect=AssertionError("resume model")
        ):
            self.assertEqual(restored.run(), summary)

    def test_review_failure_resumes_saved_generation(self):
        driver = self.prepared(batch_size=50)
        with self.assertRaisesRegex(RuntimeError, "review service"):
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
        with self.assertRaisesRegex(RuntimeError,'review service'):
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
        editor,reviewer=self.hosts(driver)
        original=editor.llm
        observed=[]

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

        def units(specs,worker,workers,on_result,**kw):
            for spec in specs:
                with (patch.object(PL,'_config',return_value=self.cfg),
                      patch.object(PL,'_embedder',return_value=None),
                      patch.object(F,'LLM_CLS',side_effect=lambda **kw:
                          Model(['Action 1: Search[Prius]','Action 2: Finish[Toyota]']))):
                    on_result(worker(spec))

        with patch.object(PL,'run_generic',side_effect=units), patch.object(
                F,'build_reasoning_host',side_effect=lambda cfg,path:
                    reviewer if 'reviewer-' in str(path) else editor):
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

    def test_duplicate_bodies_reviewed_once_and_tied_distinct_candidates_hold(self):
        driver = self.prepared(batch_size=50)
        self.assertEqual(
            self.run_offline(driver, identical=True)["reviewed_candidates"], 1
        )

    def test_equal_predictions_hold_current_skill(self):
        driver = self.prepared(batch_size=50)
        self.assertEqual(
            self.run_offline(driver, tie=True)["review_approved_updates"], 0
        )
        self.assertEqual(driver.skill_heads()[0].version, 0)

    def test_reviewer_checks_candidate_and_local_id_coverage(self):
        base = S.Skill("searchqa.s", "s", 0, "S", "scope", "1. keep\n2. old")
        candidates = [
            {"id": "C1", "body": "1. keep\n2. new"},
            {"id": "C2", "body": "1. keep\n2. alternative"},
        ]
        card = {"card_id": "one", "evidence": [{"id": "E1", "path": "/evidence/E1", "value": "observed"}]}
        entries = [
            {
                "id": c["id"],
                "label": "improve",
                "evidence_ids": ["E1"],
                "rule_ids": [c["id"] + "R2"],
                "reason": "changed mechanism",
            }
            for c in candidates
        ]
        raw = {"candidates": entries}
        parsed = CR.parse_card_review(json.dumps(raw), base, candidates, card)
        self.assertEqual(set(parsed), {"C1", "C2"})
        for key, value in [
            ("id", "C2"),
            ("id", []),
            ("label", "solved"),
            ("evidence_ids", ["E999"]),
            ("rule_ids", ["C2R2"]),
            ("rule_ids", ["B1"]),
            ("evidence_ids", []),
            ("evidence_ids", ["E1", "E1"]),
        ]:
            changed = json.loads(json.dumps(raw))
            changed["candidates"][0][key] = value
            with self.assertRaises(ValueError):
                CR.parse_card_review(json.dumps(changed), base, candidates, card)
        with self.assertRaises(ValueError):
            CR.parse_card_review(
                json.dumps({"candidates": entries[:1]}), base, candidates, card
            )

    def test_removed_rule_is_valid_directional_reference(self):
        base = S.Skill("searchqa.s", "s", 0, "S", "scope", "1. keep\n2. removed")
        candidates = [{"id": "C1", "body": "1. keep"}]
        card = {"card_id": "one", "evidence": [{"id": "E1", "path": "/evidence/E1", "value": "observed"}]}
        raw = {
            "candidates": [
                {
                    "id": "C1",
                    "label": "regress",
                    "evidence_ids": ["E1"],
                    "rule_ids": ["B2"],
                    "reason": "lost safeguard",
                }
            ]
        }
        self.assertEqual(
            CR.parse_card_review(json.dumps(raw), base, candidates, card)["C1"][
                "label"
            ],
            "regress",
        )

    def test_full_50_card_aggregation_and_invalid_unit_rejected(self):
        candidates = [{"id": "C1", "body": "new"}]
        cards = [{"card_id": str(i)} for i in range(50)]
        units = {
            c["card_id"]: {"C1": {"card_id": c["card_id"], "label": CR.LABELS[i % 4]}}
            for i, c in enumerate(cards)
        }
        result = CR.aggregate(candidates, cards, units)[0]
        self.assertEqual(
            result["counts"],
            {"improve": 13, "regress": 13, "unchanged": 12, "unknown": 12},
        )
        self.assertEqual(result["unknown_fraction"], 12 / 50)
        with self.assertRaises(ValueError):
            CR.aggregate(candidates, cards, dict(list(units.items())[:-1]))
        units["0"]["C1"]["label"] = "invalid"
        with self.assertRaises(ValueError):
            CR.aggregate(candidates, cards, units)

    def test_evidence_ids_preserve_context_and_are_deterministic(self):
        card = {"schema_version": 5, "card_id": "one", "task": {"text": "q"},
                "execution": {"success": True}, "claims": [], "claim_status": "valid",
                "evidence": [{"id": "t1:e2", "trial": 1, "phase": "autonomous",
                              "action": "Search[x]", "observation": "observed",
                              "effect": "observed", "method": True,
                              "observation_truncated": True}]}
        exp = SimpleNamespace(experience_id="one", experience_card=card)
        self.assertEqual(CR.card_payload([exp]), CR.card_payload([exp]))
        self.assertEqual(CR.card_payload([exp])[0]["evidence"][0]["id"], "t1:e2")
        self.assertEqual(CR.card_payload([exp])[0]["evidence"][0]["path"], "/evidence/0")
        self.assertTrue(CR.card_payload([exp])[0]["evidence"][0]["value"]["observation_truncated"])
        base = SimpleNamespace(key="searchqa.example@v0", body="1. keep\n2. retain")
        payload = CR.review_payload(
            base, [{"id": "C1", "body": "2. keep\n3. retain"}], {}
        )
        self.assertEqual(payload["candidates"][0]["changed_rule_ids"], [])

    def test_resume_reuses_completed_card_review(self):
        driver = self.prepared(batch_size=50)
        original = CR.CardReviewer.review
        calls = []

        def flaky(reviewer, base, candidates, card, correction=None):
            calls.append(card["card_id"])
            if len(calls) == 2:
                raise RuntimeError("second card interrupted")
            return original(reviewer, base, candidates, card, correction)

        with patch.object(CR.CardReviewer, "review", new=flaky):
            with self.assertRaisesRegex(RuntimeError, "second card interrupted"):
                self.run_offline(driver)
        with patch.object(
            ED.SkillEditor, "plan", side_effect=AssertionError("replan")
        ), patch.object(
            ED.SkillEditor, "propose", side_effect=AssertionError("regenerate")
        ):
            self.assertEqual(self.run_offline(driver)["review_approved_updates"], 1)
        self.assertEqual(len(self.reviewer_prompts), 1)
        self.assertEqual(
            json.loads(self.reviewer_prompts[0][-1].content)["card"]["card_id"],
            calls[1],
        )

    def test_one_invalid_card_holds_entire_batch_and_repairs_once(self):
        driver = self.prepared(batch_size=50)
        original = CR.CardReviewer.review
        calls = {}

        def partly_invalid(reviewer, base, candidates, card, correction=None):
            cid = card["card_id"]
            calls[cid] = calls.get(cid, 0) + 1
            if cid == next(iter(calls)):
                return "invalid json"
            return original(reviewer, base, candidates, card, correction)

        with patch.object(CR.CardReviewer, "review", new=partly_invalid):
            result = self.run_offline(driver)
        self.assertEqual(sorted(calls.values()), [1, 2])
        self.assertEqual(result["invalid_batches"], 1)
        self.assertEqual(result["review_approved_updates"], 0)
        self.assertEqual(driver.skill_heads()[0].version, 0)

    def test_unknown_can_reverse_gain_and_equal_predictions_hold(self):
        def row(cid, net, unknown=0):
            return {
                "id": cid,
                "predicted_net": net,
                "counts": {"unknown": unknown},
            }

        self.assertIsNone(CR.choose([row("C1", 1, 1)])[0])
        self.assertIsNone(CR.choose([row("C1", 2, 1), row("C2", 1)])[0])
        self.assertIsNone(CR.choose([row("C1", 1), row("C2", 0, 2)])[0])
        self.assertIsNone(CR.choose([row("C1", 2), row("C2", 2)])[0])

    def test_invalid_review_is_durable_hold(self):
        driver = self.prepared(batch_size=50)
        with patch.object(CR.CardReviewer, "review", return_value="not json"):
            result = self.run_offline(driver)
        self.assertEqual(result["invalid_batches"], 1)
        self.assertEqual(driver.skill_heads()[0].version, 0)
        with patch.object(
            F, "build_reasoning_host", side_effect=AssertionError("retry")
        ):
            self.assertEqual(driver.run(), result)

    def test_editor_enforces_frozen_description(self):
        driver = self.prepared()
        host = SimpleNamespace(
            token_counter=len,
            llm=lambda *a, **k: json.dumps({"description": "changed", "body": "new"}),
        )
        editor = ED.SkillEditor(host, driver.meta.head())
        edit = editor.propose(driver.initial[0], list(driver.cards.values()))
        self.assertIsNone(edit.candidate)

    def test_cli_defaults_and_tail_batches(self):
        driver = self.prepared()
        self.assertEqual(L.EvolutionConfig().candidate_count, 3)
        self.assertEqual(L.EvolutionConfig().batch_size, 50)
        family = driver.initial[0].family_id
        driver.plan = SimpleNamespace(families={family: list(range(123))})
        driver.config.batch_size = 50
        self.assertEqual([len(b["task_ids"]) for b in driver.batches()], [50, 50, 23])
        self.assertEqual(
            evolve.build_parser().parse_args(["--run-dir", "x"]).candidate_count, 3
        )

    def test_evolve_round_runs_skill_aware_l1_then_l2_and_resumes(self):
        driver = self.prepared(batch_size=1)
        editor, reviewer = self.hosts(driver)

        def factory(cfg, path):
            return reviewer if "reviewer-" in str(path) else editor

        with patch.object(PL, "run_generic", side_effect=self.units), patch.object(
            F, "build_reasoning_host", side_effect=factory
        ):
            result = driver.run_evolutions(1)
        self.assertEqual(result["latest_evolution_round"], 1)
        self.assertEqual(driver.skill_heads()[0].version, 1)
        card = json.loads((self.root / "evolution/round-1/cards/0.json").read_text())
        self.assertEqual(card["evolution_round"], 1)
        self.assertEqual(card["initial_skill_key"], "searchqa.family-p001@v0")

        resumed = L.SerialEvolutionLoop(
            driver.cfg, driver.plan, L.LoopPaths(self.root),
            L.EvolutionConfig(batch_size=1),
        )
        with patch.object(PL, "run_generic", side_effect=AssertionError("resampled")), patch.object(
            F, "build_reasoning_host", side_effect=AssertionError("resampled")
        ):
            resumed_result = resumed.run_evolutions(1)
        self.assertEqual(resumed_result["latest_evolution_round"], 1)
        self.assertEqual(resumed.skill_heads()[0].key, driver.skill_heads()[0].key)

    def test_partial_round_resume_and_extend_use_real_l1(self):
        driver = self.prepared(batch_size=1)
        editor, reviewer = self.hosts(driver)
        def factory(cfg, path):
            return reviewer if 'reviewer-' in str(path) else editor
        original = driver._run_batch
        calls = []
        def crash(batch):
            calls.append(batch['task_ids'])
            if len(calls) == 2:
                raise TimeoutError('between batches')
            original(batch)
        with patch.object(PL, 'run_generic', side_effect=self.units), patch.object(
                F, 'build_reasoning_host', side_effect=factory), patch.object(driver, '_run_batch', side_effect=crash):
            with self.assertRaises(TimeoutError):
                driver.run_evolutions(1)
        frozen = (self.root/'evolution/round-1/input.json').read_bytes()
        resumed = L.SerialEvolutionLoop(driver.cfg, driver.plan, L.LoopPaths(self.root), driver.config)
        with patch.object(PL, 'run_generic', side_effect=AssertionError('L1 repeated')), patch.object(
                F, 'build_reasoning_host', side_effect=factory):
            resumed.run_evolutions(1)
        self.assertEqual((self.root/'evolution/round-1/input.json').read_bytes(), frozen)
        with patch.object(PL, 'run_generic', side_effect=self.units), patch.object(
                F, 'build_reasoning_host', side_effect=factory):
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
            on_result({'task_id': specs[1].task_id, 'ok': False, 'error': 'timeout'})
        with patch.object(PL, 'run_generic', side_effect=interrupted), patch.object(
                F, 'build_reasoning_host', side_effect=AssertionError('L2 before complete L1')):
            with self.assertRaisesRegex(RuntimeError, 'L1 interrupted'):
                driver.run()
        self.assertFalse((self.root/'l2_batches').exists())
        saved = (self.root/'evolution/round-1/cards/0.json').read_bytes()
        editor, reviewer = self.hosts(driver)
        def remaining(specs, *args, **kw):
            self.assertEqual([s.task_id for s in specs], [1])
            return self.units(specs, *args, **kw)
        with patch.object(PL, 'run_generic', side_effect=remaining), patch.object(
                F, 'build_reasoning_host', side_effect=lambda cfg, path:
                    reviewer if 'reviewer-' in str(path) else editor):
            driver.run()
        self.assertEqual((self.root/'evolution/round-1/cards/0.json').read_bytes(), saved)

    def test_round_audit_rejects_checkpoint_and_raw_review_corruption(self):
        from skillexpand.l2.audit import audit_round
        driver = self.prepared(batch_size=50)
        self.run_offline(driver)
        checkpoint = self.root/'evolution/round-1/trials/0.json'
        saved = checkpoint.read_text()
        data = json.loads(saved)
        data['trials'][0]['success'] = False
        checkpoint.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            audit_round(self.root, 1)
        checkpoint.write_text(saved)
        review = next((self.root/'l2_proposals').glob('*/review-*.json'))
        data = json.loads(review.read_text())
        data['raw'] = '{}'
        review.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            audit_round(self.root, 1)

    def test_cli_import_and_final_remain_separate(self):
        driver = self.prepared()
        target = self.root.parent / "cli-import"
        editor, reviewer = self.hosts(driver)

        def factory(cfg, path):
            return reviewer if "reviewer-" in str(path) else editor

        with patch.object(F, "build_reasoning_host", side_effect=factory), patch.object(
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
                ]
            )
        summary = json.loads((target / "summary.json").read_text())
        self.assertEqual(summary["review_approved_updates"], 1)
        self.assertEqual(len(list((target/'discovery/initial_skills').glob('*-patterns.json'))),2)
        self.assertFalse((target / "routes").exists())
        cfg, plan, _, _ = A.load_cold_start(target)
        with patch.object(PL, "run_generic", side_effect=self.fake_units):
            final = evolve.final_evaluate(cfg, plan, target, 1)
        self.assertEqual(final["tasks"], 1)
        self.assertEqual(final["successes"], 1)
        from skillexpand.evaluation.audit import audit_final
        final_dir = target/'final'/final['library_hash']
        self.assertEqual(audit_final(target, final_dir)['measured'], 1)
        summary = json.loads((final_dir/'summary.json').read_text())
        summary['successes'] = 0
        (final_dir/'summary.json').write_text(json.dumps(summary))
        with self.assertRaisesRegex(ValueError, 'summary differs'):
            audit_final(target, final_dir)

    def test_failed_executor_keeps_partial_events(self):
        driver = self.prepared()
        skill = driver.initial[0]
        spec = PL.UnitSpec(
            "partial",
            "searchqa",
            2,
            S.ROLE_EVAL,
            S.ARM_EVAL,
            S.MODE_CONSOLIDATED_DIRECT,
            "none",
            skill_key=skill.key,
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
            result = PL.execute_fixed(spec)
        self.assertIn("provider disconnected", result["error"])
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
                    "error": None,
                }
            elif worker is PL.execute_fixed:
                item = {
                    "task_id": spec.task_id,
                    "success": spec.skill_body.startswith("NEW"),
                    "events": [{"observation": "scripted outcome", "environment":
                                {"success": spec.skill_body.startswith("NEW")}}],
                    "steps": 1,
                    "skill_key": spec.skill_key,
                    "error": None,
                }
            else:
                raise AssertionError("L2 must not collect or execute source tasks")
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

    def test_frozen_routes_are_disjoint_and_admission_cannot_read_final(self):
        driver = self.prepared()
        plan = S.SplitPlan.make(
            {0: "source", 1: "admission", 2: "admission", 3: "final"}, "searchqa", 42
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
                        "error": None,
                    }
                )

        with patch.object(PL, "run_generic", side_effect=route):
            routes = R.FrozenRoutes(
                self.cfg, plan, [one, two], self.root / "isolated", "admission"
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
                self.cfg, driver.plan, driver.initial, self.root / "routes", "admission"
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

        spec = PL.UnitSpec(
            "fixed",
            "searchqa",
            2,
            S.ROLE_EVAL,
            S.ARM_EVAL,
            S.MODE_CONSOLIDATED_DIRECT,
            "none",
            skill_key=skill.key,
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
            result = PL.execute_fixed(spec)
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
                on_result({"task_id": spec.task_id, "error": "offline"})

        routes = R.FrozenRoutes(
            self.cfg, driver.plan, driver.initial, root, "admission"
        )
        with patch.object(PL, "run_generic", side_effect=broken):
            with self.assertRaisesRegex(RuntimeError, "Routing incomplete"):
                routes.run()
        self.assertEqual(routes.records, {})
        self.assertFalse((root / "admission/complete.json").exists())
        with patch.object(PL, "run_generic", side_effect=self.fake_units):
            restored = R.FrozenRoutes(
                self.cfg, driver.plan, driver.initial, root, "admission"
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
        spec = PL.UnitSpec(
            "error",
            "searchqa",
            2,
            S.ROLE_EVAL,
            S.ARM_EVAL,
            S.MODE_CONSOLIDATED_DIRECT,
            "none",
            skill_key=skill.key,
            skill_body=skill.body,
        )
        with (
            patch.object(PL, "_config", return_value=self.cfg),
            patch.object(
                F, "build_agent", side_effect=RuntimeError("service unavailable")
            ),
        ):
            result = PL.execute_fixed(spec)
        self.assertIn("service unavailable", result["error"])

    def test_final_routing_failure_remains_in_overall_denominator(self):
        driver = self.prepared()

        def bad_route(specs, worker, workers, on_result, **kw):
            for spec in specs:
                on_result(
                    {
                        "task_id": spec.task_id,
                        "selection": {"ok": False, "skill_id": ""},
                        "error": None,
                    }
                )

        with patch.object(PL, "run_generic", side_effect=bad_route):
            result = evolve.final_evaluate(self.cfg, driver.plan, self.root, 1)
        self.assertEqual(result["score"], 0)
        self.assertEqual(result["tasks"], 1)
        self.assertEqual(result["routing_failures"], [3])
        self.assertIsNone(result["per_skill"][driver.initial[0].skill_id]["score"])

    def test_swapping_admission_and_final_cannot_change_frozen_protocol(self):
        self.prepared()
        path = self.root / "split.json"
        value = json.loads(path.read_text())
        value["assignment"]["2"] = "final"
        value["assignment"]["3"] = "admission"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "frozen manifest"):
            A.load_cold_start(self.root)
