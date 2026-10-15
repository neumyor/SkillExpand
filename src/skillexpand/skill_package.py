"""Canonical rendering and materialization for legacy skills and packages."""

from pathlib import Path, PurePosixPath
from typing import Union
import json
import yaml

from .schema import Skill


def render_skill_md(skill: Skill) -> str:
    """Render the standard entrypoint for a complete skill package."""
    if skill.entrypoint_format == 'frontmatter':
        return ('---\nname: ' + json.dumps(skill.name, ensure_ascii=False)
                + '\ndescription: ' + json.dumps(skill.description, ensure_ascii=False)
                + '\n---\n\n' + skill.body.rstrip() + '\n')
    return f"# {skill.name}\n\n{skill.description.strip()}\n\n{skill.body.rstrip()}\n"


def normalize_package_body(body: str, description: str) -> str:
    """Keep internal metadata separate from an optional model-written frontmatter."""
    text = body.strip()
    if text.startswith('---\n'):
        lines = text.splitlines()
        try:
            end = lines.index('---', 1)
        except ValueError as exc:
            raise ValueError('Unclosed Skill frontmatter') from exc
        try:
            metadata = yaml.safe_load('\n'.join(lines[1:end]))
        except yaml.YAMLError as exc:
            raise ValueError('Invalid Skill frontmatter') from exc
        if (not isinstance(metadata, dict) or not isinstance(metadata.get('name'), str)
                or metadata.get('description') != description):
            raise ValueError('Skill frontmatter must agree with routing description')
        text = '\n'.join(lines[end + 1:]).strip()
        if text.startswith('---\n'):
            raise ValueError('Repeated Skill frontmatter')
    if not text:
        raise ValueError('Skill package body is empty')
    return text


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
