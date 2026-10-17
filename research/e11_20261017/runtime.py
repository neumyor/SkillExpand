"""E10 runtime with complete catalog exposure and autonomous package reads."""
import json
from pathlib import Path

from e10_bare_environment import E10BareEnvironment, E10BareTerminus


def autonomous_catalog_instruction(manifest):
    """Render only the frozen catalog and file locations for the executor."""
    rows = manifest.get('catalog')
    identifiers = manifest.get('skill_ids')
    if not isinstance(rows, list) or not rows or not isinstance(identifiers, list):
        raise ValueError('Autonomous catalog is missing')
    if {row.get('skill_id') for row in rows} != set(identifiers):
        raise ValueError('Autonomous catalog and mounted library differ')
    if any(set(row) != {'skill_id', 'description'} for row in rows):
        raise ValueError('Autonomous catalog must not contain Skill bodies')
    return (
        '\n\nAvailable Skill catalog (id and description only):\n'
        + json.dumps(rows, ensure_ascii=False)
        + '\nEach Skill body is at /opt/openclaw-skills/<skill_id>/SKILL.md. '
        'Use existing terminal commands to read it; resources are relative to that file. '
        'You decide whether and when to read any Skill or resource. You may read none, '
        'one or multiple Skills, including another Skill later during execution. '
        'Catalog visibility does not mean the Skill contents have been read.'
    )


class E11Environment(E10BareEnvironment):
    def __init__(self, *args, skill_root, **kwargs):
        self.skill_root = Path(skill_root)
        super().__init__(*args, **kwargs)

    async def start(self, force_build):
        await super().start(force_build)
        entries = [{'path': '/opt/openclaw-skills/' + p.relative_to(self.skill_root).as_posix(),
                    'data': p.read_bytes()} for p in sorted(self.skill_root.rglob('*')) if p.is_file()]
        await self.run_with_e2b_reconnect(
            lambda: self._sandbox.files.write_files(entries, user='root'), operation_name='files.write_files')
        result = await self.exec('chmod -R a-w /opt/openclaw-skills', user='root')
        if result.return_code:
            raise RuntimeError('Cannot mount frozen E11 packages')


class E11Terminus(E10BareTerminus):
    def __init__(self, *args, loading_manifest, **kwargs):
        self.manifest = json.loads(Path(loading_manifest).read_text())
        super().__init__(*args, **kwargs)
        self._prompt_template = self._prompt_template.rstrip() + '\n\n' + self.manifest['user_query_tail']

    async def _build_skills_section(self, environment):
        return None

    async def run(self, instruction, environment, context):
        (self.logs_dir / 'e11_loading_manifest.json').write_text(json.dumps(self.manifest, ensure_ascii=False))
        suffix = self.manifest.get('initial_suffix') or (
            autonomous_catalog_instruction(self.manifest) + '\n\n' + self.manifest['user_query_tail'])
        await super().run(instruction.rstrip() + suffix, environment, context)
