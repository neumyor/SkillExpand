"""Prompt registry; original ALFWorld and QA prompt contents are preserved."""
from typing import Callable, List
from . import alfworld, searchqa
from .templates.human import *
from .templates.system import *

FEWSHOTS = dict(
    searchqa=searchqa.FEWSHOTS,
    alfworld=alfworld.FEWSHOTS,
)

REFLECTION_FEWSHOTS = dict(
    searchqa=searchqa.REFLECTION_FEWSHOTS,
    alfworld=alfworld.REFLECTION_FEWSHOTS,
)

SYSTEM_INSTRUCTION = dict(
    searchqa=searchqa.SYSTEM_INSTRUCTION,
    alfworld=alfworld.SYSTEM_INSTRUCTION,
)

SYSTEM_REFLECTION_INSTRUCTION = dict(
    searchqa=searchqa.SYSTEM_REFLECTION_INSTRUCTION,
    alfworld=None,
)

HUMAN_INSTRUCTION = dict(
    searchqa=searchqa.HUMAN_INSTRUCTION,
    alfworld=alfworld.HUMAN_INSTRUCTION,
)

HUMAN_REFLECTION_INSTRUCTION = dict(
    searchqa=searchqa.HUMAN_REFLECTION_INSTRUCTION,
    alfworld=alfworld.HUMAN_REFLECTION_INSTRUCTION,
)

SYSTEM_CRITIQUE_INSTRUCTION = dict(
    searchqa=dict(compare_existing_rules=searchqa.SYSTEM_CRITIQUE_EXISTING_RULES_INSTRUCTION, all_success_existing_rules=searchqa.SYSTEM_CRITIQUE_ALL_SUCCESS_EXISTING_RULES_INSTRUCTION),
    alfworld=dict(compare_existing_rules=alfworld.SYSTEM_CRITIQUE_EXISTING_RULES_INSTRUCTION, all_success_existing_rules=alfworld.SYSTEM_CRITIQUE_ALL_SUCCESS_EXISTING_RULES_INSTRUCTION),
)

LLM_PARSER = dict(
    searchqa=searchqa.LLM_PARSER,
    alfworld=alfworld.LLM_PARSER,
)

OBSERVATION_FORMATTER = dict(
    searchqa=searchqa.OBSERVATION_FORMATTER,
    alfworld=alfworld.OBSERVATION_FORMATTER,
)

STEP_IDENTIFIER = dict(
    searchqa=searchqa.STEP_IDENTIFIER,
    alfworld=alfworld.STEP_IDENTIFIER,
)

CYCLER = dict(
    searchqa=searchqa.CYCLER,
    alfworld=alfworld.CYCLER,
)

REFLECTION_PREFIX = dict(
    searchqa=searchqa.REFLECTION_PREFIX,
    alfworld=alfworld.REFLECTION_PREFIX,
)

PREVIOUS_TRIALS_FORMATTER = dict(
    searchqa=searchqa.PREVIOUS_TRIALS_FORMATTER,
    alfworld=alfworld.PREVIOUS_TRIALS_FORMATTER,
)

STEP_STRIPPER = dict(
    searchqa=searchqa.STEP_STRIPPER,
    alfworld=alfworld.STEP_STRIPPER,
)

def STEP_CYCLER(benchmark: str, lines: str, cycler: Callable, step_identifier: Callable, stripper: Callable = lambda x, y: x) -> List[str]:
    steps = []
    scratch_pad = ''
    for line in cycler(lines):
        step_type = step_identifier(line)
        stripped_line = stripper(line, step_type)
        scratch_pad += stripped_line + '\n'
        if step_type == 'observation':
            steps.append(scratch_pad.strip())
            scratch_pad = ''
    if scratch_pad != '':
        steps.append(scratch_pad.strip())
    return steps
