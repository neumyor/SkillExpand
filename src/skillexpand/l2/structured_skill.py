"""A small, deterministic rule format for optional bounded Skill edits."""

import re

SECTIONS = (
    ("procedure", "Procedure", "P"),
    ("conditions", "Conditions", "C"),
    ("completion_checks", "Completion checks", "V"),
)
SECTION_NAMES = {key: title for key, title, _ in SECTIONS}
PREFIXES = {key: prefix for key, _, prefix in SECTIONS}
HEADER = re.compile(r"^## (Procedure|Conditions|Completion checks)$")
RULE = re.compile(r"^([PCV])(\d+)\. (\S.*)$")
LEGACY_RULE = re.compile(r"(?m)^\s*\d+[.)]\s+")


def _one_line(value):
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ValueError("A rule must be nonempty and occupy one line")
    return value.strip()


def from_sections(sections):
    """Assign IDs in code; the synthesizer supplies text, never identity."""
    if not isinstance(sections, dict) or set(sections) != set(SECTION_NAMES):
        raise ValueError("Structured Skill needs procedure, conditions and completion_checks")
    result = {key: [] for key in SECTION_NAMES}
    for key, _, prefix in SECTIONS:
        rows = sections[key]
        if not isinstance(rows, list):
            raise ValueError(f"{key} must be a list")
        for index, value in enumerate(rows, 1):
            result[key].append({"id": f"{prefix}{index}", "text": _one_line(value)})
    if not result["procedure"]:
        raise ValueError("A structured Skill needs a procedure rule")
    return result


def render(sections):
    if set(sections) != set(SECTION_NAMES):
        raise ValueError("Structured Skill has unknown or missing sections")
    lines = []
    seen = set()
    for key, title, prefix in SECTIONS:
        lines.append(f"## {title}")
        for row in sections[key]:
            rid, content = row["id"], _one_line(row["text"])
            if not isinstance(rid, str) or not re.fullmatch(prefix + r"[1-9]\d*", rid) or rid in seen:
                raise ValueError("Invalid or duplicate structured rule ID")
            seen.add(rid)
            lines.append(f"{rid}. {content}")
        lines.append("")
    if not sections["procedure"]:
        raise ValueError("A structured Skill needs a procedure rule")
    return "\n".join(lines).strip()


def parse(body):
    """Parse only the canonical rendered format; never guess section membership."""
    if not isinstance(body, str):
        raise ValueError("Structured Skill body must be text")
    lines = body.strip().splitlines()
    result = {key: [] for key in SECTION_NAMES}
    current = None
    order = []
    for line in lines:
        if not line.strip():
            continue
        header = HEADER.fullmatch(line)
        if header:
            current = next(key for key, title, _ in SECTIONS if title == header.group(1))
            order.append(current)
            continue
        match = RULE.fullmatch(line)
        if current is None or match is None or match.group(1) != PREFIXES[current]:
            raise ValueError("Malformed structured Skill rule or section")
        result[current].append({"id": match.group(1) + match.group(2),
                                "text": _one_line(match.group(3))})
    if order != list(SECTION_NAMES) or render(result) != body.strip():
        raise ValueError("Structured Skill body is not canonical")
    return result


def from_legacy(body):
    """Losslessly group an imported plain body under Procedure, pending future edits."""
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Skill body is empty")
    if body.lstrip().startswith("## Procedure"):
        return parse(body)
    chunks = [chunk.strip() for chunk in LEGACY_RULE.split(body) if chunk.strip()]
    if not chunks:
        chunks = [body.strip()]
    texts = [" ".join(chunk.split()) for chunk in chunks]
    return from_sections({"procedure": texts, "conditions": [], "completion_checks": []})


def apply_edit(sections, edit, max_rules=None):
    """Apply one add/replace operation; all other rule bytes and IDs survive."""
    if not isinstance(edit, dict) or set(edit) != {"op", "section", "target_id", "text"}:
        raise ValueError("Edit must contain only op, section, target_id and text")
    op, section, target, content = (edit[k] for k in ("op", "section", "target_id", "text"))
    if op not in ("add", "replace") or section not in SECTION_NAMES:
        raise ValueError("Unsupported structured Skill edit")
    content = _one_line(content)
    if len(content) > 400 or content.startswith('## ') or RULE.match(content):
        raise ValueError("An edit must contain one concise rule, not a section or rule list")
    updated = {key: [dict(row) for row in rows] for key, rows in sections.items()}
    if op == "replace":
        if not isinstance(target, str):
            raise ValueError("Replace needs an existing target ID")
        matches = [row for row in updated[section] if row["id"] == target]
        if len(matches) != 1 or matches[0]["text"] == content:
            raise ValueError("Replace must change one rule in its own section")
        matches[0]["text"] = content
    else:
        if target is not None and not any(row["id"] == target for row in updated[section]):
            raise ValueError("Add anchor must exist in its own section")
        if max_rules is not None and sum(map(len, updated.values())) >= max_rules:
            raise ValueError("Structured Skill rule budget is full")
        prefix = PREFIXES[section]
        next_id = max((int(row["id"][1:]) for row in updated[section]), default=0) + 1
        position = next((i + 1 for i, row in enumerate(updated[section]) if row["id"] == target),
                        len(updated[section]))
        updated[section].insert(position, {"id": f"{prefix}{next_id}", "text": content})
    render(updated)
    return updated
