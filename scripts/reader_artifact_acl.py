"""Grant a reader user access to selected artifacts, executed by their owner."""

import argparse
import ctypes
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("uid", type=int)
    args = parser.parse_args()
    lib = ctypes.CDLL("libacl.so.1", use_errno=True)
    lib.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
    lib.acl_get_file.restype = ctypes.c_void_p
    lib.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.acl_to_text.restype = ctypes.c_void_p
    lib.acl_from_text.argtypes = [ctypes.c_char_p]
    lib.acl_from_text.restype = ctypes.c_void_p
    lib.acl_set_file.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.acl_free.argtypes = [ctypes.c_void_p]

    def grant(path, perm):
        acl = lib.acl_get_file(bytes(path), 0x8000)
        if not acl:
            raise OSError(ctypes.get_errno(), str(path))
        ptr = lib.acl_to_text(acl, None)
        lines = ctypes.string_at(ptr).decode().splitlines()
        lib.acl_free(ptr)
        lib.acl_free(acl)
        prefix = f"user:{args.uid}:"
        old = next((line.split(':')[-1] for line in lines if line.startswith(prefix)), "---")
        perm = ''.join(c if c in old or c in perm else '-' for c in 'rwx')
        mask = next((line.split(':')[-1] for line in lines if line.startswith('mask::')), next(line.split(':')[-1] for line in lines if line.startswith('group::')))
        mask = ''.join(c if c in mask or c in perm else '-' for c in 'rwx')
        lines = [line for line in lines if not line.startswith((prefix, 'mask::'))]
        lines += [prefix + perm, 'mask::' + mask]
        acl = lib.acl_from_text(('\n'.join(lines) + '\n').encode())
        if not acl or lib.acl_set_file(bytes(path), 0x8000, acl):
            raise OSError(ctypes.get_errno(), str(path))
        lib.acl_free(acl)

    names = {'contract.json', 'audit.json', 'amended_audit.json', 'report.json', 'manifest.json',
             'current.json', 'progress.json', 'request.json', 'rollout.json', 'state.json',
             'candidate.json', 'trajectory.json', 'result.json', 'decision.json'}
    files = [p for p in args.root.rglob('*.json') if not p.is_symlink() and (
        p.name in names or p.parent.name in {'versions', 'phases'})]
    dirs = set()
    for path in files:
        dirs.update(path.parents)
    for path in sorted(dirs, key=lambda p: len(p.parts)):
        if str(path) not in {'/', '/home'}:
            grant(path, 'r-x' if path == args.root or args.root in path.parents else '--x')
    for path in files:
        grant(path, 'r--')
    print(f'Read access granted: {len(files)} selected artifacts, {len(dirs)} ancestor directories')


if __name__ == '__main__':
    main()
