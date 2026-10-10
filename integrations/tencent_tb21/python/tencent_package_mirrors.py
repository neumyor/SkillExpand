"""Configure package mirrors without changing benchmark dependencies or tests."""
import ipaddress
import json
import os
from urllib.parse import urlparse


APT_PATHS = ('/etc/apt/sources.list', '/etc/apt/sources.list.d/ubuntu.sources',
             '/etc/apt/sources.list.d/debian.sources')


def mirror_entries(sources, base):
    base = base.rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Invalid package mirror URL')
    # Minimal Ubuntu images have no CA bundle yet; apt verifies signed indexes.
    apt_base = base.replace('https://mirrors.tencent.com', 'http://mirrors.tencent.com')
    entries = []
    for path, text in sources.items():
        changed = text
        for origin, repo in (
            ('deb.debian.org/debian-security', 'debian-security'),
            ('security.debian.org/debian-security', 'debian-security'),
            ('deb.debian.org/debian', 'debian'),
            ('archive.ubuntu.com/ubuntu', 'ubuntu'),
            ('security.ubuntu.com/ubuntu', 'ubuntu'),
        ):
            for scheme in ('https', 'http'):
                changed = changed.replace(f'{scheme}://{origin}', f'{apt_base}/{repo}')
            changed = changed.replace(f'{base}/{repo}', f'{apt_base}/{repo}')
        if changed != text:
            entries.append({'path': path, 'data': changed})
    entries.extend([
        {'path': '/etc/uv/uv.toml', 'data': f'[[index]]\nurl = "{base}/pypi/simple"\ndefault = true\n'},
        {'path': '/etc/pip.conf', 'data': f'[global]\nindex-url = {base}/pypi/simple\n'},
    ])
    return entries


def missing_file(exc):
    return isinstance(exc, FileNotFoundError) or any(
        token in str(exc).lower() for token in ('not found', 'does not exist', 'no such file'))


async def configure(sandbox, base):
    dns = os.getenv('TB21_PACKAGE_DNS', '183.60.83.19')
    entries = []
    if dns and urlparse(base).hostname in ('mirrors.tencent.com', 'mirrors.tencentyun.com'):
        ipaddress.ip_address(dns)
        resolver = await sandbox.files.read('/etc/resolv.conf', user='root', request_timeout=30)
        fallback = [line for line in resolver.splitlines()
                    if line.strip().startswith('nameserver ') and line.split()[1] != dns]
        resolver = '\n'.join([f'nameserver {dns}', *fallback[:2],
                              'options timeout:2 attempts:2']) + '\n'
        await sandbox.files.write_files([{'path': '/etc/resolv.conf', 'data': resolver}],
                                       user='root', request_timeout=30)
        base = 'https://mirrors.tencentyun.com'
        entries.append({'path': '/opt/tb21-package-network.json', 'data': json.dumps(
            {'dns': dns, 'mirror': base, 'fallback_nameservers': fallback[:2]}, indent=2) + '\n'})
    sources = {}
    for path in APT_PATHS:
        try:
            sources[path] = await sandbox.files.read(path, user='root', request_timeout=30)
        except Exception as exc:
            if not missing_file(exc):
                raise
    entries.extend(mirror_entries(sources, base))
    await sandbox.files.write_files(entries, user='root', request_timeout=30)
    return [entry['path'] for entry in entries]
