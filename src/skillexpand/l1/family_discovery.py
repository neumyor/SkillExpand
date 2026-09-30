"""Family taxonomy proposal and forced-choice task assignment."""
from __future__ import annotations

import hashlib
import ast
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Sequence


class DiscoveryError(ValueError):
    """Raised when a discovery artifact is malformed or incomplete."""


@dataclass(frozen=True)
class TaskTag:
    task_id: int
    capability_tags: tuple[str, ...]
    capability_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {'task_id': self.task_id, 'capability_tags': list(self.capability_tags),
                'capability_summary': self.capability_summary}


@dataclass(frozen=True)
class FamilyProposal:
    family_id: str
    name: str
    definition: str
    trigger_conditions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {'family_id': self.family_id, 'name': self.name,
                'definition': self.definition,
                'trigger_conditions': list(self.trigger_conditions)}


@dataclass(frozen=True)
class FamilyAssignment:
    task_id: int
    family_id: str
    match_type: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {'task_id': self.task_id, 'family_id': self.family_id,
                'match_type': self.match_type, 'rationale': self.rationale}


@dataclass(frozen=True)
class FamilyPlan:
    benchmark: str
    mode: str
    task_to_family: Dict[int, str]
    families: Dict[str, Dict[str, Any]]
    tags: tuple[TaskTag, ...] = ()
    proposals: tuple[FamilyProposal, ...] = ()
    assignments: tuple[FamilyAssignment, ...] = ()
    mapping_hash: str = ''

    def __post_init__(self) -> None:
        mapping = {str(k): v for k, v in sorted(self.task_to_family.items())}
        digest = hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest()
        if self.mapping_hash and self.mapping_hash != digest:
            raise DiscoveryError('family mapping hash does not match task_to_family')
        object.__setattr__(self, 'mapping_hash', digest)
        validate_mapping(self.task_to_family, self.families)

    @property
    def families_index(self) -> Dict[str, list[int]]:
        out: Dict[str, list[int]] = {family: [] for family in self.families}
        for task_id, family in self.task_to_family.items():
            out.setdefault(family, []).append(int(task_id))
        return {family: sorted(ids) for family, ids in sorted(out.items())}

    def to_dict(self) -> dict[str, Any]:
        return {
            'schema_version': 2,
            'benchmark': self.benchmark,
            'mode': self.mode,
            'mapping_hash': self.mapping_hash,
            'task_to_family': {str(k): v for k, v in sorted(self.task_to_family.items())},
            'families': self.families,
            'tags': [tag.to_dict() for tag in self.tags],
            'proposals': [proposal.to_dict() for proposal in self.proposals],
            'assignments': [assignment.to_dict() for assignment in self.assignments],
        }


def validate_mapping(task_to_family: Mapping[int, str],
                     families: Mapping[str, Mapping[str, Any]]) -> None:
    if not task_to_family:
        raise DiscoveryError('family mapping is empty')
    if not families:
        raise DiscoveryError('family plan has no families')
    unknown = set(task_to_family.values()) - set(families)
    if unknown:
        raise DiscoveryError(f'task mapping references unknown families: {sorted(unknown)}')
    empty = [family for family in families if family not in set(task_to_family.values())]
    if empty:
        raise DiscoveryError(f'family plan contains empty families: {empty}')
    if len(set(task_to_family)) != len(task_to_family):
        raise DiscoveryError('duplicate task ids in family mapping')


def _text(value: Any, field: str) -> str:
    text = str(value or '').strip()
    if not text:
        raise DiscoveryError(f'{field} is empty')
    return text


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise DiscoveryError(f'{field} must be a list')
    result = tuple(_text(item, field) for item in value)
    if len(set(result)) != len(result):
        raise DiscoveryError(f'{field} contains duplicates')
    return result


def parse_tags(raw: Mapping[str, Any], task_ids: Iterable[int]) -> tuple[TaskTag, ...]:
    expected = {int(task_id) for task_id in task_ids}
    rows = raw.get('tags') if isinstance(raw, Mapping) else None
    if not isinstance(rows, list):
        raise DiscoveryError('tag artifact must contain a tags list')
    tags: list[TaskTag] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise DiscoveryError('each tag must be an object')
        task_id = int(row['task_id'])
        if task_id in seen:
            raise DiscoveryError(f'duplicate tag for task {task_id}')
        tags.append(TaskTag(task_id, _string_list(row['capability_tags'], 'capability_tags'),
                            _text(row['capability_summary'], 'capability_summary')))
        seen.add(task_id)
    if seen != expected:
        raise DiscoveryError('tags must cover every task exactly once')
    return tuple(sorted(tags, key=lambda item: item.task_id))


def parse_proposals(raw: Mapping[str, Any]) -> tuple[FamilyProposal, ...]:
    """Parse taxonomy definitions; proposal membership never contains task IDs."""
    rows = raw.get('families') if isinstance(raw, Mapping) else None
    if not isinstance(rows, list) or not rows:
        raise DiscoveryError('proposal artifact must contain a non-empty families list')
    proposals: list[FamilyProposal] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise DiscoveryError('each family proposal must be an object')
        family_id = _text(row.get('family_id'), 'family_id')
        if not re.fullmatch(r'family-p\d+', family_id):
            raise DiscoveryError(f'family_id must use family-pNNN format: {family_id!r}')
        if family_id in seen:
            raise DiscoveryError(f'duplicate family proposal: {family_id}')
        if 'candidate_task_ids' in row or 'exclusion_criteria' in row:
            raise DiscoveryError('family proposals must not contain task IDs or exclusion criteria')
        proposals.append(FamilyProposal(
            family_id=family_id,
            name=_text(row.get('name'), 'family name'),
            definition=_text(row.get('definition'), 'family definition'),
            trigger_conditions=_string_list(row.get('trigger_conditions'), 'trigger_conditions'),
        ))
        seen.add(family_id)
    return tuple(proposals)


def _repair_proposal_ids(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """Canonicalize model-chosen opaque IDs when the model ignores the ID contract.

    Family IDs have no semantic content: assignments only use them as references.
    If one proposal uses a non-canonical ID, rewrite the whole proposal list by
    row order so every reference remains unique and deterministic. Structural
    fields and family meanings are untouched; malformed rows still go through the
    strict parser and are rejected.
    """
    rows = raw.get('families') if isinstance(raw, Mapping) else None
    if not isinstance(rows, list):
        return raw
    invalid = any(
        isinstance(row, Mapping)
        and not re.fullmatch(r'family-p\d+', str(row.get('family_id') or '').strip())
        for row in rows
    )
    if not invalid:
        return raw
    repaired = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, Mapping):
            repaired.append(row)
            continue
        value = dict(row)
        value['family_id'] = f'family-p{index:03d}'
        repaired.append(value)
    return {**raw, 'families': repaired}


def parse_assignments(raw: Mapping[str, Any], task_ids: Iterable[int],
                     proposals: Sequence[FamilyProposal]) -> tuple[FamilyAssignment, ...]:
    rows = raw.get('assignments') if isinstance(raw, Mapping) else None
    if not isinstance(rows, list):
        raise DiscoveryError('assignment artifact must contain an assignments list')
    expected = {int(task_id) for task_id in task_ids}
    allowed = {proposal.family_id for proposal in proposals}
    assignments: list[FamilyAssignment] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise DiscoveryError('each family assignment must be an object')
        task_id = int(row['task_id'])
        if task_id in seen or task_id not in expected:
            raise DiscoveryError(f'invalid or duplicate assignment task id: {task_id}')
        family_id = _text(row.get('family_id'), 'assignment family_id')
        if family_id not in allowed:
            raise DiscoveryError(f'assignment selected unknown family: {family_id!r}')
        match_type = _text(row.get('match_type', 'direct'), 'assignment match_type')
        if match_type not in ('direct', 'best_fit'):
            raise DiscoveryError(f'unsupported assignment match_type: {match_type!r}')
        assignments.append(FamilyAssignment(task_id, family_id, match_type,
                                             _text(row.get('rationale'), 'assignment rationale')))
        seen.add(task_id)
    if seen != expected:
        raise DiscoveryError('family assignments must cover every task exactly once')
    return tuple(sorted(assignments, key=lambda item: item.task_id))


def make_family_plan(benchmark: str, tags: Sequence[TaskTag],
                     proposals: Sequence[FamilyProposal],
                     assignments: Sequence[FamilyAssignment],
                     mode: str = 'forced_choice_assignment') -> FamilyPlan:
    task_ids = {tag.task_id for tag in tags}
    assignment_ids = {assignment.task_id for assignment in assignments}
    if len(assignments) != len(task_ids) or assignment_ids != task_ids:
        raise DiscoveryError('assignments and tags cover different task ids')
    proposal_by_id = {proposal.family_id: proposal for proposal in proposals}
    if any(assignment.family_id not in proposal_by_id for assignment in assignments):
        raise DiscoveryError('assignments reference unknown family')
    assignment_map = {assignment.task_id: assignment.family_id for assignment in assignments}
    families: Dict[str, Dict[str, Any]] = {}
    for family_id, proposal in proposal_by_id.items():
        members = sorted(task_id for task_id, assigned in assignment_map.items()
                         if assigned == family_id)
        if not members:
            continue
        families[family_id] = {
            'name': proposal.name,
            'definition': proposal.definition,
            'trigger_conditions': list(proposal.trigger_conditions),
            'task_ids': members,
        }
    return FamilyPlan(benchmark=benchmark, mode=mode,
                      task_to_family=assignment_map, families=families,
                      tags=tuple(tags), proposals=tuple(proposals),
                      assignments=tuple(assignments))


def select_representatives(tags: Sequence[TaskTag], limit: int = 128) -> tuple[TaskTag, ...]:
    if limit < 1:
        raise DiscoveryError('representative limit must be positive')
    by_signature: Dict[tuple[str, ...], TaskTag] = {}
    for tag in sorted(tags, key=lambda item: item.task_id):
        signature = tuple(sorted(set(item.strip().lower() for item in tag.capability_tags)))
        by_signature.setdefault(signature, tag)
    candidates = sorted(by_signature.values(), key=lambda item: item.task_id)
    if len(candidates) <= limit:
        return tuple(candidates)
    indexes = sorted({round(i * (len(candidates) - 1) / (limit - 1))
                      for i in range(limit)}) if limit > 1 else [0]
    return tuple(candidates[index] for index in indexes)


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


def _extract_json(text: str, required_keys: Sequence[str] | None = None) -> dict[str, Any]:
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
    raise DiscoveryError('LLM response did not contain a complete JSON object')


def _ask_json(llm: Callable[[str], str], prompt: str, stage: str,
              attempts: int = 3) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            suffix = '' if attempt == 0 else (
                '\nPrevious response was invalid. Return exactly one valid JSON object '
                'and no markdown or commentary.')
            return _extract_json(llm(prompt + suffix))
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.0 * (attempt + 1))
    raise DiscoveryError(f'{stage} failed after {attempts} attempts: {last_error}') from last_error


FAMILY_CONTRACT = """Classify stable task requirements and operation semantics, not solver performance.
Family membership must remain the same if the task is solved on the first attempt, after retries,
with guidance, or remains unsolved. Do not classify by topic, answer entity, attempt count,
success/failure, observed search count, or repair status. A partial or failed trace is evidence
about tools and actions, not a complete definition of the task's required capabilities.
Describe reusable capabilities without instance names or answers. Preserve uncertainty.
"""


def tag_tasks(tasks: Mapping[int, str], llm: Callable[[str], str],
              on_tag: Callable[[TaskTag], None] | None = None,
              max_workers: int = 64) -> tuple[TaskTag, ...]:
    if max_workers < 1:
        raise DiscoveryError('tag worker count must be positive')

    def tag_one(task_id: int) -> TaskTag:
        prompt = (
            'You are extracting reusable capabilities from one task experience card.\n'
            'Describe required operations and completion conditions, not solver performance, topic, '
            'or answer entities. A diagnostic or assisted completion is not a proven procedure.\n'
            'Return JSON only: {"capability_tags":["..."],"capability_summary":"..."}.\n\n'
            + FAMILY_CONTRACT + f'\nTASK_ID: {task_id}\nTASK:\n{tasks[task_id]}'
        )
        value = _ask_json(llm, prompt, f'tagging task {task_id}')
        return TaskTag(task_id, _string_list(value.get('capability_tags'), 'capability_tags'),
                       _text(value.get('capability_summary'), 'capability_summary'))

    out: dict[int, TaskTag] = {}
    task_ids = sorted(tasks)
    for start in range(0, len(task_ids), max_workers):
        wave = task_ids[start:start + max_workers]
        with ThreadPoolExecutor(max_workers=min(max_workers, len(wave)),
                                thread_name_prefix='family-tag') as pool:
            futures = {pool.submit(tag_one, task_id): task_id for task_id in wave}
            for future in as_completed(futures):
                tag = future.result()
                out[tag.task_id] = tag
                if on_tag is not None:
                    on_tag(tag)
    return tuple(out[task_id] for task_id in sorted(out))


def propose_families(tags: Sequence[TaskTag], llm: Callable[[str], str],
                     target_family_count: int | None = None) -> tuple[FamilyProposal, ...]:
    payload = json.dumps({'representative_tags': [tag.to_dict() for tag in tags]}, ensure_ascii=False)
    target = f'Use exactly {target_family_count} families.\n' if target_family_count else \
        'Choose the smallest defensible number of families.\n'
    prompt = (
        'You are proposing a reusable task family taxonomy from representative capability tags.\n'
        'A family must share the same required operations, decision process, operation order, '
        'and completion contract. Do not group by topic or answer entity.\n' + target +
        'For each family return only family_id, name, definition, and trigger_conditions. '
        'family_id must be an opaque sequential ID exactly matching family-p001, family-p002, and so on. '
        'Trigger conditions are positive task requirements used for routing. Do not return '
        'exclusion criteria or task IDs. Return JSON only with a families list.\n\n' +
        FAMILY_CONTRACT + '\n' + payload
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            value = _extract_json(llm(prompt if attempt == 0 else
                prompt + '\nPrevious output was invalid. Return only the requested family taxonomy.'))
            proposals = parse_proposals(_repair_proposal_ids(value))
            if target_family_count is not None and len(proposals) != target_family_count:
                raise DiscoveryError(f'expected exactly {target_family_count} families, got {len(proposals)}')
            return proposals
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
    raise DiscoveryError(f'family proposal failed after 3 attempts: {last_error}') from last_error


def assign_families(tags: Sequence[TaskTag], proposals: Sequence[FamilyProposal],
                    llm: Callable[[str], str], batch_size: int = 64,
                    on_batch: Callable[[Sequence[FamilyAssignment]], None] | None = None,
                    existing: Sequence[FamilyAssignment] = (),
                    task_cards: Mapping[int, Any] | None = None
                    ) -> tuple[FamilyAssignment, ...]:
    if not proposals:
        raise DiscoveryError('cannot assign tasks without family proposals')
    if batch_size < 1:
        raise DiscoveryError('assignment worker count must be positive')
    expected = {tag.task_id for tag in tags}
    allowed = {proposal.family_id for proposal in proposals}
    completed = {item.task_id for item in existing}
    if len(completed) != len(existing) or not completed.issubset(expected):
        raise DiscoveryError('existing family assignments contain duplicate or unknown task ids')
    if any(item.family_id not in allowed or item.match_type not in ('direct', 'best_fit')
           for item in existing):
        raise DiscoveryError('existing family assignments contain an invalid family or match type')
    family_payload = [proposal.to_dict() for proposal in proposals]

    def assign_one(tag: TaskTag) -> FamilyAssignment:
        card = task_cards.get(tag.task_id) if task_cards is not None else None
        payload = json.dumps({'families': family_payload, 'task': tag.to_dict(),
                              'experience_card_projection': card}, ensure_ascii=False)
        prompt = (
            'Choose exactly one family for this task experience card. Compare the complete family '
            'taxonomy with the task goal and required operations. Use the task goal as authoritative; '
            'a partial or failed execution trace does not remove capabilities required by the goal. '
            'Do not create a family and do not return null. If no family is perfect, choose the '
            'closest family and set match_type to best_fit. Return JSON only: '
            '{"task_id":123,"family_id":"family-p001","match_type":"direct|best_fit",'
            '"rationale":"..."}.\n\n' + FAMILY_CONTRACT + '\n' + payload
        )
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                retry_prompt = prompt if attempt == 0 else (
                    prompt + '\nPrevious output was invalid. Choose one supplied family_id '
                    'and return the exact JSON schema.')
                value = _extract_json(llm(retry_prompt))
                return parse_assignments({'assignments': [value]}, [tag.task_id], proposals)[0]
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        raise DiscoveryError(f'family assignment failed for task {tag.task_id}: {last_error}') from last_error

    pending = tuple(tag for tag in tags if tag.task_id not in completed)
    assignments: list[FamilyAssignment] = list(existing)
    for start in range(0, len(pending), batch_size):
        wave = pending[start:start + batch_size]
        batch: list[FamilyAssignment] = []
        with ThreadPoolExecutor(max_workers=min(batch_size, len(wave)),
                                thread_name_prefix='family-assign') as pool:
            futures = {pool.submit(assign_one, tag): tag.task_id for tag in wave}
            for future in as_completed(futures):
                item = future.result()
                batch.append(item)
                if on_batch is not None:
                    on_batch([item])
        assignments.extend(sorted(batch, key=lambda item: item.task_id))
    return tuple(sorted(assignments, key=lambda item: item.task_id))

def write_artifacts(out_dir: Path, plan: FamilyPlan) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'capability_tags.json').write_text(
        json.dumps({'schema_version': 2, 'tags': [tag.to_dict() for tag in plan.tags]},
                   ensure_ascii=False, indent=2) + '\n')
    (out_dir / 'family_proposals.json').write_text(
        json.dumps({'schema_version': 2, 'families': [proposal.to_dict() for proposal in plan.proposals]},
                   ensure_ascii=False, indent=2) + '\n')
    (out_dir / 'family_assignments.json').write_text(
        json.dumps({'schema_version': 2, 'assignments': [item.to_dict() for item in plan.assignments]},
                   ensure_ascii=False, indent=2) + '\n')
    (out_dir / 'family_plan.json').write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + '\n')


def load_family_plan(path: Path, benchmark: str | None = None) -> FamilyPlan:
    value = json.loads(Path(path).read_text())
    if int(value.get('schema_version', 0)) != 2:
        raise DiscoveryError('unsupported family plan schema_version')
    plan_benchmark = _text(value.get('benchmark'), 'benchmark')
    if benchmark and plan_benchmark != benchmark:
        raise DiscoveryError(f'family plan benchmark {plan_benchmark!r} != {benchmark!r}')
    raw_mapping = value.get('task_to_family')
    if not isinstance(raw_mapping, Mapping):
        raise DiscoveryError('family plan task_to_family must be an object')
    mapping = {int(task_id): _text(family, 'family_id') for task_id, family in raw_mapping.items()}
    families = value.get('families')
    if not isinstance(families, Mapping):
        raise DiscoveryError('family plan families must be an object')
    for family_id, metadata in families.items():
        if not isinstance(metadata, Mapping):
            raise DiscoveryError(f'family metadata for {family_id!r} must be an object')
        declared = metadata.get('task_ids')
        expected = {task_id for task_id, family in mapping.items() if family == family_id}
        if not isinstance(declared, list) or set(int(task_id) for task_id in declared) != expected:
            raise DiscoveryError(f'family metadata task_ids mismatch for {family_id!r}')
    tags = parse_tags({'tags': value.get('tags', [])}, mapping)
    proposals = parse_proposals({'families': value.get('proposals', [])})
    assignments = parse_assignments({'assignments': value.get('assignments', [])}, mapping, proposals)
    plan = make_family_plan(plan_benchmark, tags, proposals, assignments,
                            mode=_text(value.get('mode', 'forced_choice_assignment'), 'mode'))
    if plan.task_to_family != mapping or plan.families != families or plan.mapping_hash != value.get('mapping_hash'):
        raise DiscoveryError('family plan metadata differs from assignments')
    return plan
