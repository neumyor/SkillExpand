"""The structured mode edits one stable rule and preserves the rest."""

import unittest
from types import SimpleNamespace

from skillexpand import structured_skill as SS
from skillexpand.l2.editor import SkillEditor, REASON_NO_OPERATIONS
from skillexpand import schema as S


class StructuredSkillTests(unittest.TestCase):
    def setUp(self):
        self.sections = SS.from_sections({
            "procedure": ["Search for the named item.", "Read the matching source."],
            "conditions": ["If names collide, check the distinguishing detail."],
            "completion_checks": ["Before Finish, verify the answer type."],
        })

    def test_replace_changes_only_target_and_add_keeps_existing_ids(self):
        base = SS.render(self.sections)
        replaced = SS.apply_edit(self.sections, {
            "op": "replace", "section": "conditions", "target_id": "C1",
            "text": "If names collide, verify the requested date and role.",
        })
        self.assertEqual(replaced["procedure"], self.sections["procedure"])
        self.assertEqual(replaced["completion_checks"], self.sections["completion_checks"])
        self.assertEqual(replaced["conditions"][0]["id"], "C1")
        added = SS.apply_edit(replaced, {
            "op": "add", "section": "conditions", "target_id": "C1",
            "text": "If evidence conflicts, search for a primary source.",
        })
        self.assertEqual([r["id"] for r in added["conditions"]], ["C1", "C2"])
        self.assertEqual(SS.parse(SS.render(added)), added)
        self.assertEqual(SS.parse(base), self.sections)

    def test_rejects_whole_body_and_cross_section_or_multi_edits(self):
        bad = [
            {"op": "replace", "section": "conditions", "target_id": "P1", "text": "New."},
            {"op": "delete", "section": "conditions", "target_id": "C1", "text": "New."},
            {"op": "replace", "section": "conditions", "target_id": "C1", "text": "New.",
             "body": "rewrite"},
            {"op": "add", "section": "conditions", "target_id": None, "text": "A.\nB."},
        ]
        for edit in bad:
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                SS.apply_edit(self.sections, edit)
        added = SS.apply_edit(self.sections, {"op": "add", "section": "conditions",
                                            "target_id": None, "text": "Another rule."})
        self.assertEqual(added["conditions"][-1]["id"], "C2")

    def test_planner_edit_is_applied_without_editor_llm_and_invalid_ids_hold(self):
        base = S.Skill("searchqa.f", "f", 0, "lookup", "scope", SS.render(self.sections))
        calls = []
        host = SimpleNamespace(llm=lambda *args, **kwargs: calls.append(args), token_counter=len)
        editor = SkillEditor(host, skill_edit_mode="structured")
        card = SimpleNamespace(experience_id="e1", task_id=1)
        outcome = editor.apply_planner_edit(base, [card], {
            "edit": {"op": "add", "section": "completion_checks", "target_id": None,
                      "text": "Before Finish, verify the requested answer type."}})
        self.assertIsNotNone(outcome.candidate)
        self.assertEqual(calls, [])
        self.assertEqual(SS.parse(outcome.candidate.skill.body)["completion_checks"][-1]["id"], "V2")
        rejected = editor.apply_planner_edit(base, [card], {
            "edit": {"op": "replace", "section": "conditions", "target_id": "P1",
                      "text": "Invalid cross-section target."}})
        self.assertIsNone(rejected.candidate)
        self.assertEqual(rejected.reason, REASON_NO_OPERATIONS)


if __name__ == "__main__":
    unittest.main()
