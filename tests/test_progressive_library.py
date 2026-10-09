import json

from skillexpand import schema as S
from skillexpand.evaluation.progressive import catalog, select_and_load
from skillexpand.evaluation.selector import SkillSelector


def _skill(skill_id, family, description):
    return S.Skill(skill_id, family, 0, family, description, f"SECRET BODY {skill_id}")


class _Host:
    benchmark_name = "terminalbench"

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def llm(self, prompt, replace_newline=False):
        self.prompts.append(prompt)
        return self.answer


def test_catalog_is_body_free_and_selection_loads_after_selection():
    skills = [_skill("terminalbench.family-p001", "family-p001", "git repair"),
              _skill("terminalbench.family-p002", "family-p002", "package build")]
    host = _Host("SKILL: terminalbench.family-p002\nWHY: build task")
    selected, record = select_and_load(host, "build the package", skills)

    assert selected.skill_id == "terminalbench.family-p002"
    assert all("body" not in item and "key" not in item for item in record["catalog"])
    assert record["load_stage"] == "after_selection"
    assert record["loaded_skill_key"] == selected.key
    prompt_text = "\n".join(message.content for message in host.prompts[0])
    assert "SECRET BODY" not in prompt_text
    assert "git repair" in prompt_text and "package build" in prompt_text


def test_selection_provenance_round_trips_without_body_leak():
    skills = [_skill("terminalbench.family-p001", "family-p001", "git repair")]
    host = _Host("SKILL: terminalbench.family-p001\nWHY: repository state")
    _, record = select_and_load(host, "repair repository", skills)
    encoded = json.loads(json.dumps(record))
    assert encoded["catalog"] == [{"skill_id": skills[0].skill_id,
                                    "description": skills[0].description}]
    assert encoded["loaded_body_chars"] == len(skills[0].body)


def test_selector_rejects_unknown_skill_without_loading():
    skills = [_skill("terminalbench.family-p001", "family-p001", "git repair")]
    host = _Host("SKILL: terminalbench.unknown\nWHY: wrong")
    choice = SkillSelector(host).select("repair repository", skills)
    assert choice.ok is False
    assert choice.reason == "unparsable"
    assert catalog(skills)[0].keys() == {"skill_id", "description"}
