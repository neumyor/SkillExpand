"""Minimal progressive Skill-library loading protocol.

The executor receives only the catalog first.  A selected Skill is resolved to
its body in a second step, so the library itself is never injected wholesale.
"""
from dataclasses import asdict
from typing import Any, Iterable
import json
from pathlib import PurePosixPath

from skillexpand import schema as S
from skillexpand.evaluation.selector import SkillSelector


def catalog(skills: Iterable[S.Skill]) -> list[dict[str, str]]:
    """Return the selector-visible part of the library, without Skill bodies."""
    return [
        {"skill_id": skill.skill_id, "description": skill.description}
        for skill in sorted(skills, key=lambda item: item.skill_id)
    ]


def autonomous_catalog_instruction(skills: Iterable[S.Skill],
                                   root: str = "/opt/openclaw-skills") -> str:
    """Expose routing metadata and file locations without loading any contents."""
    rows = catalog(skills)
    if not rows:
        raise ValueError("Empty autonomous Skill library")
    for row in rows:
        identifier = row['skill_id']
        if PurePosixPath(identifier).name != identifier or identifier in ('.', '..'):
            raise ValueError("Skill ID must be a directory name")
    return (
        "\n\nAvailable Skill catalog (id and description only):\n"
        + json.dumps(rows, ensure_ascii=False)
        + f"\nEach Skill's body is at {root}/<skill_id>/SKILL.md. "
        "Use the existing terminal commands to read it; resources are relative to that file. "
        "You decide whether and when to read any Skill or resource. You may read none, "
        "one or multiple Skills, including another Skill later during execution. "
        "Catalog visibility does not mean the Skill contents have been read."
    )


def select_and_load(host: Any, task_text: str,
                   skills: Iterable[S.Skill]) -> tuple[S.Skill, dict[str, Any]]:
    """Select one Skill from the catalog, then load exactly that Skill body."""
    library = list(skills)
    if not library:
        raise ValueError("progressive Skill library is empty")
    choice = SkillSelector(host).select(task_text, library)
    visible_catalog = catalog(library)
    # Keep the selector evidence self-contained, while making it impossible to
    # accidentally persist a Skill body in the selector-visible catalog.
    if any('body' in item or 'key' in item for item in visible_catalog):
        raise AssertionError('progressive selector catalog leaked Skill body')
    record = {"stage": "select", **asdict(choice), "catalog": visible_catalog,
              "catalog_fingerprint": S.content_hash(visible_catalog)}
    if not choice.ok:
        raise RuntimeError(f"progressive Skill selection failed: {choice.reason}")
    selected = next((skill for skill in library if skill.skill_id == choice.skill_id), None)
    if selected is None:
        raise RuntimeError(f"selector returned unknown Skill: {choice.skill_id}")
    record.update({
        "stage": "load",
        "loaded_skill_id": selected.skill_id,
        "loaded_skill_key": selected.key,
        "loaded_body_chars": len(selected.body),
        "load_stage": "after_selection",
    })
    return selected, record
