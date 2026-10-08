"""Prompt registry for the inherited executor; prompt contents are preserved."""
from . import alfworld, searchqa
from .templates.human import RULE_TEMPLATE

FEWSHOTS = dict(
    searchqa=searchqa.FEWSHOTS,
    alfworld=alfworld.FEWSHOTS,
)

SYSTEM_INSTRUCTION = dict(
    searchqa=searchqa.SYSTEM_INSTRUCTION,
    alfworld=alfworld.SYSTEM_INSTRUCTION,
)

HUMAN_INSTRUCTION = dict(
    searchqa=searchqa.HUMAN_INSTRUCTION,
    alfworld=alfworld.HUMAN_INSTRUCTION,
)

LLM_PARSER = dict(
    searchqa=searchqa.LLM_PARSER,
    alfworld=alfworld.LLM_PARSER,
)

OBSERVATION_FORMATTER = dict(
    searchqa=searchqa.OBSERVATION_FORMATTER,
    alfworld=alfworld.OBSERVATION_FORMATTER,
)

__all__ = ['FEWSHOTS', 'HUMAN_INSTRUCTION', 'LLM_PARSER', 'OBSERVATION_FORMATTER',
           'RULE_TEMPLATE', 'SYSTEM_INSTRUCTION']
