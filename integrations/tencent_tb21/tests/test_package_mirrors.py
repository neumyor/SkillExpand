import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import tencent_package_mirrors as mirrors


class PackageMirrorTests(unittest.IsolatedAsyncioTestCase):
    def sandbox(self):
        async def read(path, **kwargs):
            if path == '/etc/resolv.conf':
                return 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n'
            if path == '/etc/apt/sources.list':
                return 'deb http://archive.ubuntu.com/ubuntu jammy main\n'
            raise FileNotFoundError(path)
        return SimpleNamespace(files=SimpleNamespace(
            read=AsyncMock(side_effect=read), write_files=AsyncMock()))

    async def test_internal_dns_retains_fallback_and_updates_all_indexes(self):
        sandbox = self.sandbox()
        with patch.dict(os.environ, {'TB21_PACKAGE_DNS': '183.60.83.19'}):
            await mirrors.configure(sandbox, 'https://mirrors.tencent.com')
        calls = sandbox.files.write_files.call_args_list
        resolver = calls[0].args[0][0]['data']
        self.assertTrue(resolver.startswith('nameserver 183.60.83.19\n'))
        self.assertIn('nameserver 1.1.1.1', resolver)
        entries = {e['path']: e['data'] for e in calls[1].args[0]}
        self.assertIn('https://mirrors.tencentyun.com/ubuntu', entries['/etc/apt/sources.list'])
        for path in ['/etc/uv/uv.toml', '/etc/pip.conf']:
            self.assertIn('https://mirrors.tencentyun.com/pypi/simple', entries[path])

    async def test_opt_out_and_non_tencent_source_preserve_dns(self):
        for dns, base in [('', 'https://mirrors.tencent.com'),
                          ('183.60.83.19', 'https://example.org')]:
            sandbox = self.sandbox()
            with patch.dict(os.environ, {'TB21_PACKAGE_DNS': dns}):
                await mirrors.configure(sandbox, base)
            self.assertEqual(sandbox.files.write_files.await_count, 1)
            entries = sandbox.files.write_files.call_args.args[0]
            self.assertNotIn('/etc/resolv.conf', [e['path'] for e in entries])
            self.assertIn(base + '/pypi/simple', entries[-1]['data'])


if __name__ == '__main__':
    unittest.main()
