from pathlib import Path

import pytest

from skillexpand import schema as S
from skillexpand.skill_package import materialize_skill, render_skill_md


def make_skill(**kwargs):
    return S.Skill("demo", "family", 1, "Demo", "Use the package", "body", **kwargs)


def test_legacy_materialization_preserves_body(tmp_path):
    skill = make_skill(resources={"scripts/run.py": "print('ok')"})
    path = materialize_skill(skill, tmp_path)
    assert path.read_text() == "body"
    assert not (tmp_path / "scripts/run.py").exists()


def test_package_materialization_writes_entrypoint_and_resources(tmp_path):
    skill = make_skill(resources={"scripts/run.py": "print('ok')", "references/note.txt": "ref"}, package_mode=True)
    path = materialize_skill(skill, tmp_path, package=skill.package_mode)
    assert path.read_text() == render_skill_md(skill)
    assert (tmp_path / "scripts/run.py").read_text() == "print('ok')"
    assert (tmp_path / "references/note.txt").read_text() == "ref"


@pytest.mark.parametrize("name", ["/absolute", "../escape", "scripts/../escape", "SKILL.md",
                                 "./SKILL.md", "SKILL.md/note.txt", ".", "./",
                                 "scripts//run.py", "scripts/run.py/", "bad\x00path"])
def test_resource_paths_are_confined(name):
    with pytest.raises(ValueError):
        make_skill(resources={name: "x"})


@pytest.mark.parametrize("resources", [
    {"refs": "file", "refs/note.txt": "nested"},
    {"refs/note.txt": "nested", "refs": "file"},
])
def test_resource_files_cannot_also_be_parent_directories(resources):
    with pytest.raises(ValueError, match="parent file"):
        make_skill(resources=resources)
