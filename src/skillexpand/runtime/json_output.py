"""Extract one JSON object from free-form LLM output."""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Sequence

from skillexpand.reliability.errors import JsonExtractionError


def _balanced_json_objects(text: str) -> list[str]:
    """Return complete JSON-like object spans, respecting quoted braces.

    A model often puts a valid object after a short explanation, or emits two
    objects while correcting itself.  ``find('{')``/``rfind('}')`` joins those
    objects together and makes an otherwise recoverable response unparsable.
    This small scanner deliberately does *not* try to repair an unterminated
    object: accepting a truncated response would turn a format error into data.
    """
    objects = []
    start = None
    depth = 0
    quoted = False
    escaped = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == '{':
            if depth == 0:
                start = index
            depth += 1
        elif char == '}' and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start:index + 1])
                start = None
    return objects


def _json_candidates(text: str) -> list[str]:
    text = str(text or '').replace('\ufeff', '').strip()
    candidates = []
    # Prefer fenced blocks, while still scanning the whole response below.  The
    # latter handles reasoning text before/after an unfenced answer.
    for match in re.finditer(r'```(?:json|javascript|js)?\s*(.*?)\s*```', text, re.S | re.I):
        candidates.extend(_balanced_json_objects(match.group(1)))
        candidates.append(match.group(1).strip())
    candidates.extend(_balanced_json_objects(text))
    candidates.append(text)
    # Preserve order but avoid repeatedly parsing the same large response.
    return list(dict.fromkeys(item for item in candidates if item))


def _load_json_candidate(candidate: str) -> dict[str, Any] | None:
    candidate = candidate.strip()
    attempts = [candidate]
    # Trailing commas are a common harmless generation error.  Do this only
    # outside strings so a reason containing `,}` is left untouched.
    repaired = []
    quoted = False
    escaped = False
    for index, char in enumerate(candidate):
        if quoted:
            repaired.append(char)
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        if char == ',' and index + 1 < len(candidate) and candidate[index + 1:].lstrip().startswith(('}', ']')):
            continue
        repaired.append(char)
    attempts.append(''.join(repaired))
    # Some gateways prepend ``json`` to an otherwise valid object.
    if candidate.lower().startswith('json'):
        attempts.append(candidate[4:].lstrip(': \n'))
    for item in list(attempts):
        try:
            value = json.loads(item)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value
    # A few local models use Python quotes/booleans despite being asked for JSON.
    # literal_eval is intentionally the last resort and the result still has to
    # be a dictionary; arbitrary code is never evaluated.
    try:
        value = ast.literal_eval(attempts[1])
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def extract_json(text: str, required_keys: Sequence[str] | None = None) -> dict[str, Any]:
    """Extract one complete object from model output.

    ``required_keys`` is used by callers with a small response contract to pick
    the right object when the model includes an input echo or multiple attempts.
    It does not weaken validation: callers still validate types and ranges after
    extraction.  Incomplete/truncated JSON is intentionally rejected.
    """
    required = set(required_keys or ())
    parsed = []
    for candidate in _json_candidates(text):
        value = _load_json_candidate(candidate)
        if value is not None:
            parsed.append(value)
    if required:
        for value in parsed:
            if required <= set(value):
                return value
    if parsed:
        return parsed[0]
    raise JsonExtractionError('LLM response did not contain a complete JSON object')
