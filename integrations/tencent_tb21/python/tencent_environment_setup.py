"""Shared package preparation for bare and Skill Tencent environments."""
import os
import re
import time
from pathlib import Path

from tencent_package_mirrors import configure, APT_PATHS
from tencent_verifier_cache import configure as configure_uv
from tencent_pipeline_cache import write_json


async def prepare_environment(environment):
    if getattr(environment, '_package_setup_ready', False):
        return
    report = {'status': 'preparing', 'task': environment.environment_name,
              'uv_version': '0.9.5', 'stage': 'environment_setup'}
    path = environment.trial_paths.trial_dir / 'package_setup.json'
    start = time.monotonic()
    try:
        mirror = os.getenv('TB21_PACKAGE_MIRROR_URL', 'https://mirrors.tencent.com')
        report['requested_mirror'] = mirror
        if mirror:
            certificate = '/etc/ssl/certs/ca-certificates.crt'
            check = await environment.exec('test -s ' + certificate, user='root', timeout_sec=30)
            if check.return_code:
                await environment._sandbox.files.write(certificate, Path(certificate).read_bytes(),
                                                       user='root', request_timeout=30)
            report['configured_paths'] = await configure(environment._sandbox, mirror)
            if environment.environment_name in ('qemu-startup', 'qemu-alpine-ssh'):
                # The bullseye security mirror index advertises removed packages.
                source = '/etc/apt/sources.list'
                text = await environment._sandbox.files.read(source, user='root', request_timeout=30)
                text = re.sub(r'https?://[^\s]+/debian-security',
                              'https://archive.debian.org/debian-security', text)
                await environment._sandbox.files.write(source, text, user='root', request_timeout=30)
                report['security_archive'] = 'https://archive.debian.org/debian-security'
        report['uv_cache_hit'] = await configure_uv(environment._sandbox)
        if report['uv_cache_hit']:
            check = await environment.exec('chmod 755 /usr/local/bin/curl', user='root', timeout_sec=30)
            if check.return_code:
                raise RuntimeError('Failed to activate cached uv installer')
        sources = {}
        for source in (*APT_PATHS, '/etc/uv/uv.toml', '/etc/resolv.conf'):
            try:
                sources[source] = await environment._sandbox.files.read(source, user='root', request_timeout=30)
            except FileNotFoundError:
                continue
            except Exception as exc:
                from tencent_package_mirrors import missing_file
                if not missing_file(exc):
                    raise
        report.update(status='ready', sources=sources)
        environment._package_setup_ready = True
    except BaseException as exc:
        report.update(status='failed', error_type=type(exc).__name__)
        raise
    finally:
        report['prepare_seconds'] = time.monotonic() - start
        write_json(path, report)
