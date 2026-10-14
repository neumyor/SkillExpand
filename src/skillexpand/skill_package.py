"""Canonical rendering and materialization for legacy skills and packages."""

from pathlib import Path, PurePosixPath
from typing import Union

from .schema import Skill


def render_skill_md(skill: Skill) -> str:
    """Render the standard entrypoint for a complete skill package."""
    return f"# {skill.name}\n\n{skill.description.strip()}\n\n{skill.body.rstrip()}\n"


def materialize_skill(skill: Skill, root: Union[str, Path], *, package: bool = False) -> Path:
    """Write a skill deterministically and return its ``SKILL.md`` path.

    Legacy mode preserves the historical body bytes. Package mode adds all
    validated UTF-8 resources next to the generated entrypoint.
    """
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    entry = root / "SKILL.md"
    entry.write_text(render_skill_md(skill) if package else skill.body, encoding="utf-8")
    if package:
        for relative, content in sorted(skill.resources.items()):
            target = root / PurePosixPath(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
    return entry


def entrypoint_content(skill: Skill, *, package: bool = False) -> str:
    """Return the exact bytes expected for the mounted entrypoint."""
    return render_skill_md(skill) if package else skill.body
