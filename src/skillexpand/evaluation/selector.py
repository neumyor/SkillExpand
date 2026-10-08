"""Route held-out tasks using only task text and Skill descriptions."""
import json
import re
from dataclasses import dataclass
from langchain.schema import HumanMessage, SystemMessage
from skillexpand import schema as S
from skillexpand.reliability.errors import SchemaViolation
from skillexpand.reliability.policies import repair_policy
from skillexpand.reliability.retry import call_with_repair, fresh

REASON_AGENT = 'description_route'
REASON_UNPARSABLE = 'unparsable'
REASON_UNKNOWN_SKILL = 'unknown_skill_id'
REASON_EMPTY = 'empty_answer'
REASON_NO_SKILLS = 'no_skills_available'

@dataclass
class SelectionOutcome:
    skill_id: str
    source: str = S.SELECTION_AGENT
    reason: str = REASON_AGENT
    raw: str = ''
    why: str = ''
    prompt_chars: int = 0
    ok: bool = True

    @property
    def failed(self):
        return not self.ok

    @staticmethod
    def failure(mode, reason, raw='', prompt_chars=0):
        return SelectionOutcome('', mode, reason, raw, prompt_chars=prompt_chars, ok=False)


def render_skill_block(skills):
    if any(not s.description.strip() for s in skills):
        raise ValueError('Every routable Skill requires a nonempty description')
    if len({s.skill_id for s in skills}) != len(skills):
        raise ValueError('Duplicate Skill IDs')
    return json.dumps([{'skill_id':s.skill_id,'description':s.description}
                       for s in sorted(skills,key=lambda s:s.skill_id)],ensure_ascii=False)


def parse_selection(raw, known_ids):
    match=re.search(r'^\s*SKILL:\s*([^\n]+)',raw or '',re.M)
    value=match.group(1).strip() if match else ''
    return value if value in known_ids else None


def parse_why(raw):
    match=re.search(r'^\s*WHY:\s*([^\n]+)',raw or '',re.M)
    return match.group(1).strip() if match else ''


class SkillSelector:
    def __init__(self,host_agent,adapter=None):
        from skillexpand.l1.adapters import Adapter
        self.host=host_agent
        self.adapter=adapter or Adapter()
        self.calls=[]

    def build_prompt(self,task_text,skills):
        return [SystemMessage(content=self.adapter.selector_prompt(
                    getattr(self.host,'benchmark_name','unknown'))),
                HumanMessage(content='SKILLS:\n'+render_skill_block(skills)+'\nTASK:\n'+task_text)]

    def select(self,task_text,skills):
        """Route one task. Provider failures propagate; an invalid choice is a routing outcome."""
        if not skills:
            return SelectionOutcome.failure(S.SELECTION_AGENT,REASON_NO_SKILLS)
        prompt=self.build_prompt(task_text,skills)
        size=sum(len(m.content) for m in prompt)
        known=[s.skill_id for s in skills]
        def request():
            raw=self.host.llm(prompt,replace_newline=False)
            self.calls.append(raw)
            return raw
        def parse(raw):
            chosen=parse_selection(raw,known)
            if chosen is None:
                raise SchemaViolation('no listed skill_id selected')
            return chosen
        result=call_with_repair(repair_policy('selector.route'),fresh(request),parse)
        if result.degraded:
            raw=result.last_failure.raw
            return SelectionOutcome.failure(S.SELECTION_AGENT,
                REASON_EMPTY if not raw else REASON_UNPARSABLE,raw,size)
        raw=self.calls[-1]
        return SelectionOutcome(result.value,raw=raw,why=parse_why(raw),prompt_chars=size)
