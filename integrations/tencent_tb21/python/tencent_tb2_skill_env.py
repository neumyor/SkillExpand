"""Tencent E2B environment that mounts the matching task skill per trial."""

from __future__ import annotations

import os
import shlex
from pathlib import Path, PurePosixPath
from typing import Any

from tencent_tb2_snapshot import TencentSnapshotE2BEnvironment
from tencent_package_mirrors import configure
from tencent_verifier_cache import configure as configure_verifier_cache


_TASK_SKILL_DIRECTORY_OVERRIDES = {
    # The authoring export normalizes the task's dot to a dash in this one name.
    "install-windows-3.11": "install-windows-3-11",
}
_MAX_BATCH_FILES = 64
_MAX_BATCH_BYTES = 8 * 1024 * 1024


class TencentTaskSkillE2BEnvironment(TencentSnapshotE2BEnvironment):
    """Upload exactly one task skill before Terminus-2 starts its session."""

    def __init__(
        self,
        *args: Any,
        skill_root: str,
        skill_mount_root: str = "/opt/openclaw-skills",
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._skill_root = Path(skill_root).resolve()
        self._skill_mount_root = PurePosixPath(skill_mount_root)

    def _task_skill_source(self) -> tuple[Path, PurePosixPath]:
        task_name = self.environment_name
        if not task_name or Path(task_name).name != task_name:
            raise ValueError(f"invalid task name for skill mount: {task_name!r}")
        if not self._skill_root.is_dir() or self._skill_root.is_symlink():
            raise ValueError(f"skill root is not a directory: {self._skill_root}")

        source = self._skill_root / _TASK_SKILL_DIRECTORY_OVERRIDES.get(task_name, task_name)
        if source.is_symlink() or not source.is_dir() or source.parent != self._skill_root:
            raise ValueError(f"task skill directory is unavailable: {source}")
        skill_file = source / "SKILL.md"
        if not skill_file.is_file() or skill_file.is_symlink():
            raise ValueError(f"task skill is missing SKILL.md: {skill_file}")
        for path in source.rglob("*"):
            if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                raise ValueError(f"task skill contains unsupported path: {path}")
        return source, self._skill_mount_root / task_name

    async def _mount_task_skill(self) -> None:
        source, target = self._task_skill_source()
        if self._sandbox is None:
            raise RuntimeError("Sandbox not found while mounting task skill")

        async def file_operation(operation: Any, operation_name: str) -> Any:
            return await self.run_with_e2b_reconnect(
                operation, operation_name=operation_name
            )

        batches: list[list[dict[str, bytes | str]]] = []
        batch: list[dict[str, bytes | str]] = []
        batch_bytes = 0
        for file_path in sorted(path for path in source.rglob("*") if path.is_file()):
            relative = PurePosixPath(file_path.relative_to(source).as_posix())
            payload = file_path.read_bytes()
            if batch and (
                len(batch) >= _MAX_BATCH_FILES
                or batch_bytes + len(payload) > _MAX_BATCH_BYTES
            ):
                batches.append(batch)
                batch = []
                batch_bytes = 0
            batch.append({"path": str(target / relative), "data": payload})
            batch_bytes += len(payload)

        if batch:
            batches.append(batch)

        for entries in batches:
            await file_operation(
                lambda entries=entries: self._sandbox.files.write_files(entries, user="root"),
                "files.write_files",
            )

        result = await self.exec(
            f"chmod -R a-w -- {shlex.quote(str(target))}", user="root"
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"failed to make task skill read-only: {result.stderr or result.stdout}"
            )

    async def start(self, force_build: bool) -> None:
        await super().start(force_build)
        mirror = os.getenv('TB21_PACKAGE_MIRROR_URL', 'https://mirrors.tencent.com')
        if mirror:
            certificate_path = '/etc/ssl/certs/ca-certificates.crt'
            certificate_check = await self.exec('test -s ' + certificate_path, user='root', timeout_sec=30)
            if certificate_check.return_code != 0:
                await self._sandbox.files.write_files(
                    [{'path': certificate_path, 'data': Path(certificate_path).read_bytes()}], user='root'
                )
            paths = await configure(self._sandbox, mirror)
            self.logger.info('Configured package mirror %s in %s', mirror, paths)
        prerequisite = await self.exec('command -v tmux', user='root', timeout_sec=30)
        if prerequisite.return_code != 0:
            update = await self.exec('apt-get update', user='root', timeout_sec=120)
            install = await self.exec('apt-get install -y tmux', user='root', timeout_sec=120)
            if update.return_code != 0 or install.return_code != 0:
                raise RuntimeError('Failed to install the required tmux agent runtime: ' +
                                   update.stderr[-1000:] + install.stderr[-1000:])
        await self._mount_task_skill()
        if await configure_verifier_cache(self._sandbox):
            result = await self.exec('chmod 755 /usr/local/bin/curl', user='root', timeout_sec=30)
            if result.return_code != 0:
                raise RuntimeError('Failed to activate the cached uv installer')
            self.logger.info('Configured cached uv 0.9.5 installer')
