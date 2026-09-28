"""The structured mode edits one stable rule and preserves the rest."""

import unittest

from skillexpand.l2 import structured_skill as SS
from skillexpand.l2.card_review import review_payload
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
        with self.assertRaisesRegex(ValueError, "budget"):
            SS.apply_edit(self.sections, {"op": "add", "section": "conditions",
                                          "target_id": None, "text": "Another rule."}, max_rules=4)

    def test_review_diff_ignores_section_headers_and_stable_ids(self):
        base = S.Skill("searchqa.f", "f", 0, "lookup", "scope", SS.render(self.sections))
        changed = SS.render(SS.apply_edit(self.sections, {
            "op": "replace", "section": "conditions", "target_id": "C1",
            "text": "If names collide, verify the requested date and role.",
        }))
        payload = review_payload(base, [{"id": "C1", "body": changed}], {"card_id": "e1"})
        self.assertEqual(len(payload["current_rules"]), 4)
        self.assertEqual(len(payload["candidates"][0]["changed_rule_ids"]), 2)


if __name__ == "__main__":
    unittest.main()
