"""Offline two-task end-to-end checks for experience-first initialization and routing."""

import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from types import SimpleNamespace
from omegaconf import OmegaConf
from skillexpand.l1 import cold_start as C
from skillexpand import cli as evolve
from skillexpand import schema as S
from skillexpand.l2 import loop as L
from skillexpand.l2 import patterns as BP
from skillexpand.runtime import agent_factory as F
from skillexpand.runtime import parallel as PL
from skillexpand.evaluation import validation as V
from skillexpand.persistence import store as ST
from skillexpand.persistence import artifacts as A
from skillexpand.evaluation.selector import SkillSelector
from tests.test_l1_repair import Model


class ExperienceFirstTests(unittest.TestCase):
    def test_pattern_requires_distinct_cards_and_real_evidence(self):
        def exp(card_id):
            return SimpleNamespace(experience_id=card_id,
                experience_card={'schema_version':5,'card_id':card_id,'task':{'text':'q'},
                    'execution':{'success':True},'claims':[],'claim_status':'valid',
                    'evidence':[{'id':'t1:e1','action':'Search[x]','observation':'found',
                                 'method':True,'effect':'observed'}]})
        cards=[exp('a'),exp('b')]
        def raw(support):
            return json.dumps({'patterns':[{'text':'Search before answering',
                'support':support,'counter_card_ids':[]}]})
        a={'card_id':'a','evidence_id':'t1:e1'}
        b={'card_id':'b','evidence_id':'t1:e1'}
        self.assertEqual(len(BP.parse(raw([a,b]),cards)),1)
        with self.assertRaises(ValueError):
            BP.parse(raw([a,a]),cards)
        with self.assertRaises(ValueError):
            BP.parse(raw([a,{'card_id':'b','evidence_id':'invented'}]),cards)
        cards[1].experience_card['evidence'][0].update(method=False,effect='score')
        with self.assertRaisesRegex(ValueError,'Scoring feedback'):
            BP.parse(raw([a,b]),cards)
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "run"
        self.tasks = Path(self.tmp.name) / "tasks.json"
        self.tasks.write_text(
            json.dumps(
                [
                    {
                        "question": f"Find the maker of Prius {i}",
                        "answers": ["Toyota"],
                        "context": "[DOC] Toyota makes Prius.",
                    }
                    for i in range(4)
                ]
            )
        )
        self.cfg = F.load_config("searchqa")
        self.cfg.benchmark.task_file = str(self.tasks)
        self.cfg.agent.llm = "gpt-3.5-turbo"
        self.plan = S.SplitPlan.make(
            {0: "source", 1: "source", 2: "admission", 3: "final"}, "searchqa", 42
        )
        self.calls = []
        self.executed = []

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, prompt):
        self.calls.append(prompt)
        if "recurring behavioral patterns" in prompt:
            return json.dumps({"patterns": []})
        if "capability_tags" in prompt and "TASK_ID:" in prompt:
            return json.dumps(
                {
                    "capability_tags": ["identify maker"],
                    "capability_summary": "Read maker evidence and return the entity",
                }
            )
        if "proposing a reusable task family taxonomy" in prompt:
            return json.dumps(
                {
                    "families": [
                        {
                            "family_id": "family-p001",
                            "name": "Maker lookup",
                            "definition": "Find a maker in evidence",
                            "trigger_conditions": ["maker questions"],
                        }
                    ]
                }
            )
        if "Choose exactly one family" in prompt:
            payload = json.loads(prompt.rsplit("\n", 1)[-1])
            tid = payload["task"]["task_id"]
            return json.dumps(
                {
                    "task_id": tid,
                    "family_id": "family-p001",
                    "match_type": "direct",
                    "rationale": "Same evidence lookup operation",
                }
            )
        if "initial reusable Skill" in prompt:
            return json.dumps(
                {
                    "description": "Look up a manufacturer from supplied textual evidence; excludes arithmetic.",
                    "body": "1. Find the named product in the supplied evidence.\n2. Submit its manufacturer.",
                }
            )
        raise AssertionError("Unexpected model call: " + prompt[:80])

    def units(self, specs, worker, workers, on_result, **kw):
        output = []
        for spec in specs:
            self.executed.append(spec)
            with (
                patch.object(PL, "_config", return_value=self.cfg),
                patch.object(PL, "_embedder", return_value=None),
                patch.object(
                    F,
                    "LLM_CLS",
                    side_effect=lambda **kw: Model(["Action 1: Finish[Toyota]"]),
                ),
            ):
                item = worker(spec)
            output.append(item)
            on_result(item)
        return output

    def cold(self):
        return C.ColdStart(
            self.cfg,
            self.plan,
            self.root,
            k=1,
            cold_start_workers=2,
            family_discovery_workers=2,
            ask=self.ask,
            run_units=self.units,
            card_batch_size=1,
        )

    def test_full_cold_start_has_every_source_card_and_no_heldout_mapping(self):
        plan = self.cold().run()
        self.assertEqual({s.task_id for s in self.executed}, {0, 1})
        self.assertTrue(
            all(s.skill_key is None and not s.skill_aware for s in self.executed)
        )
        self.assertEqual(plan.tasks_in("admission"), (2,))
        self.assertEqual(plan.tasks_in("final"), (3,))
        self.assertEqual(set(plan.families["family-p001"]), {0, 1})
        mapping = json.loads((self.root / "task_skill_map.json").read_text())
        self.assertEqual(
            mapping, {"0": "searchqa.family-p001", "1": "searchqa.family-p001"}
        )
        self.assertEqual(len(list((self.root / "discovery/results").glob("*.json"))), 2)
        initial = json.loads((self.root / "initial_skills.json").read_text())
        self.assertTrue(initial[0]["description"])
        self.assertTrue(initial[0]["body"])
        self.assertEqual(len(initial[0]["provenance"]["source_experience_ids"]), 2)
        self.assertTrue(
            all(
                x.startswith("discovery:")
                for x in initial[0]["provenance"]["source_experience_ids"]
            )
        )
        self.assertFalse((self.root / "experiences.jsonl").exists())
        # Two synthesis batches ensure even the card without a repair lesson is processed.
        synthesis = [p for p in self.calls if "initial reusable Skill" in p]
        self.assertEqual(len(synthesis), 2)
        self.assertIn('"claims": []', synthesis[0])
        before = (len(self.calls), len(self.executed))
        self.assertEqual(self.cold().run(), plan)
        self.assertEqual((len(self.calls), len(self.executed)), before)
        self.assertEqual(evolve.load_clustered_plan(self.root, self.plan), plan)

    def test_initial_pattern_cache_is_reused_and_checked(self):
        cold=C.ColdStart(self.cfg,self.plan,self.root,cold_start_workers=1,family_discovery_workers=1,
                         k=1,supervised=False,ask=self.ask,run_units=self.units,card_batch_size=2)
        cold.run()
        path=self.root/'discovery/initial_skills/family-p001-0-patterns.json'
        value=json.loads(path.read_text())
        self.assertEqual(value['status'],'valid')
        self.assertEqual(value['patterns'],[])
        self.assertEqual(len([p for p in self.calls if 'recurring behavioral patterns' in p]),1)
        before=len(self.calls)
        cold.run()
        self.assertEqual(len(self.calls),before)
        value['card_hashes']['0']='corrupted'
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'Batch pattern input changed'):
            cold.run()
        C.freeze(self.root/'config.json',OmegaConf.to_container(self.cfg,resolve=True))
        with self.assertRaisesRegex(ValueError,'Batch pattern input changed'):
            A.load_cold_start(self.root)

    def test_supported_pattern_reaches_initial_skill_synthesis(self):
        def ask(prompt):
            if 'recurring behavioral patterns' in prompt:
                self.calls.append(prompt)
                return json.dumps({'patterns':[{'text':'Search for the product before answering',
                    'support':[{'card_id':f'discovery:0:searchqa:unassigned:{t}',
                                'evidence_id':'t1:e1'} for t in (0,1)],
                    'counter_card_ids':[]}]})
            return self.ask(prompt)

        def units(specs,worker,workers,on_result,**kw):
            for spec in specs:
                self.executed.append(spec)
                with (patch.object(PL,'_config',return_value=self.cfg),
                      patch.object(PL,'_embedder',return_value=None),
                      patch.object(F,'LLM_CLS',side_effect=lambda **kw:
                          Model(['Action 1: Search[Prius]','Action 2: Finish[Toyota]']))):
                    on_result(worker(spec))

        cold=C.ColdStart(self.cfg,self.plan,self.root,cold_start_workers=1,family_discovery_workers=1,
                         k=1,supervised=False,ask=ask,run_units=units,card_batch_size=2)
        cold.run()
        patterns=json.loads((self.root/'discovery/initial_skills/family-p001-0-patterns.json').read_text())
        self.assertEqual(patterns['status'],'valid')
        self.assertEqual(len(patterns['patterns'][0]['support']),2)
        synthesis=next(p for p in self.calls if 'initial reusable Skill' in p)
        self.assertIn('Search for the product before answering',synthesis)

    def test_changed_prompt_or_task_rejects_resume(self):
        self.cold().run()
        self.cfg.benchmark.l1.reflection_instructions = "changed"
        with self.assertRaisesRegex(ValueError, "Frozen inputs"):
            self.cold()

    def test_partial_source_collection_resumes_without_repeating_completed_task(self):
        original = self.units

        def fail(specs, worker, workers, on_result, **kw):
            original(specs[:1], worker, workers, on_result)
            on_result({"ok": False, "task_id": 1, "error": "network"})

        cold = self.cold()
        cold._run_units = fail
        with self.assertRaisesRegex(RuntimeError, "execution interrupted"):
            cold.run()
        self.assertFalse((self.root / "cold_start_complete.json").exists())
        self.cold().run()
        self.assertEqual([s.task_id for s in self.executed], [0, 1])

    def test_selector_inputs_are_only_descriptions_and_question(self):
        skill = S.Skill(
            "searchqa.one",
            "one",
            0,
            "PRIVATE_NAME",
            "Public description",
            "SECRET_BODY",
        )
        host = SimpleNamespace(
            benchmark_name="searchqa", llm=Model(["SKILL: searchqa.one\nWHY: fits"])
        )
        selector = SkillSelector(host)
        prompt = "\n".join(
            m.content for m in selector.build_prompt("PUBLIC_QUESTION", [skill])
        )
        self.assertIn("Public description", prompt)
        self.assertIn("PUBLIC_QUESTION", prompt)
        self.assertNotIn("SECRET_BODY", prompt)
        self.assertNotIn("PRIVATE_NAME", prompt)
        self.assertNotIn("@v", prompt)
        self.assertEqual(selector.select("q", [skill]).skill_id, skill.skill_id)
        with self.assertRaisesRegex(ValueError, "description"):
            replace(skill, description="")

    def test_routed_real_executor_does_not_see_reference_or_repair(self):
        skill = S.Skill(
            "searchqa.one", "one", 0, "lookup", "Manufacturer lookup", "Use evidence."
        )
        models = []

        def model(**kw):
            m = Model(
                ["SKILL: searchqa.one\nWHY: manufacturer", "Action 1: Finish[Toyota]"]
            )
            models.append(m)
            return m

        spec = PL.UnitSpec(
            "eval",
            "searchqa",
            2,
            S.ROLE_EVAL,
            S.ARM_EVAL,
            S.MODE_CONSOLIDATED_DIRECT,
            "none",
            skill_library=(S.to_dict(skill),),
        )
        with (
            patch.object(PL, "_config", return_value=self.cfg),
            patch.object(F, "LLM_CLS", side_effect=model),
        ):
            result = PL.execute(spec)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["skill_key"], skill.key)
        self.assertEqual(len(models[0].prompts), 2)
        self.assertNotIn("Reference answer:", "\n".join(models[0].prompts))
        self.assertIsNone(result["trajectory"])

    def test_task_concurrency_and_split_before_clustering(self):
        batches = list(C.task_batches(list(range(22)), 8))
        self.assertEqual([n for _, n in batches], [8])
        self.assertEqual([i for batch, _ in batches for i in batch], list(range(22)))
        self.assertEqual(list(C.task_batches([1, 2], 256)), [([1, 2], 2)])
        self.assertEqual(list(C.task_batches([], 256)), [])
        args = SimpleNamespace(split_file=None, seed=42)
        plan = evolve.make_plan(self.cfg, args, self.root)
        self.assertFalse(plan.families)
        self.assertEqual(set(plan.assignment), {0, 1, 2, 3})

    def test_wrong_selector_answer_is_failure_not_silent_reroute(self):
        skill = S.Skill(
            "searchqa.one", "one", 0, "lookup", "Manufacturer lookup", "rule"
        )
        spec = PL.UnitSpec(
            "eval",
            "searchqa",
            2,
            S.ROLE_EVAL,
            S.ARM_EVAL,
            S.MODE_CONSOLIDATED_DIRECT,
            "none",
            skill_library=(S.to_dict(skill),),
        )
        with (
            patch.object(PL, "_config", return_value=self.cfg),
            patch.object(
                F,
                "LLM_CLS",
                side_effect=lambda **kw: Model(["SKILL: invented\nWHY: none"]),
            ),
        ):
            record = PL.execute(spec)
        self.assertFalse(record["success"])
        self.assertEqual(record["steps"], 0)
        self.assertIsNone(record["skill_key"])
        self.assertIsNone(record["error"])
        self.assertEqual(record["failure_mode"], "routing_failure")

    def test_usage_accumulates_across_restart_and_lock_excludes_second_writer(self):
        from skillexpand.persistence.usage import PersistentUsage
        from langchain.schema import LLMResult, Generation

        path = self.root / "usage.json"
        result = LLMResult(
            generations=[[Generation(text="ok")]],
            llm_output={
                "token_usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
                "model_name": "gpt-3.5-turbo",
            },
        )
        for index in range(2):
            usage = PersistentUsage(path)
            usage.on_llm_start({}, ['prompt'], run_id=str(index))
            usage.on_llm_end(result, run_id=str(index))
        data = json.loads(path.read_text())
        self.assertEqual(data["total_tokens"], 12)
        self.assertEqual(data["started_requests"], 2)
        first = L.RunLock(self.root / "lock")
        second = L.RunLock(self.root / "lock")
        first.acquire()
        try:
            with self.assertRaisesRegex(RuntimeError, "already being written"):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_partial_jsonl_tail_is_repaired_before_new_append(self):
        path = self.root / "records.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{"id":1}\n{"id":')
        self.assertEqual(ST.read_jsonl(path), [{"id": 1}])
        with path.open("a") as stream:
            stream.write('{"id":2}\n')
        self.assertEqual(ST.read_jsonl(path), [{"id": 1}, {"id": 2}])
        path.write_bytes(b'{"id":1}\nBROKEN\n{"id":2}\n')
        with self.assertRaises(ST.StoreError):
            ST.read_jsonl(path)

    def test_initial_skill_accepts_ordered_rule_list_without_data_loss(self):
        self.assertEqual(
            C.normalize_initial_skill(
                {"description": "scope", "body": ["1. Inspect.", "2. Act."]}
            ),
            {"description": "scope", "body": "1. Inspect.\n2. Act."},
        )
        for body in ([], ["valid", None], {}, ""):
            with self.assertRaises(ValueError):
                C.normalize_initial_skill({"description": "scope", "body": body})

    def test_discovery_receives_benchmark_runtime_contract(self):
        cold = self.cold()
        cold.ask("TASK_ID: 0 capability_tags")
        self.assertIn('"benchmark": "searchqa"', self.calls[-1])
        self.assertIn("No web or Wikipedia browsing is available", self.calls[-1])


if __name__ == "__main__":
    unittest.main()
