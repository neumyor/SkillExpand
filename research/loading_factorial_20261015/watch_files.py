"""Linux inotify observer for the mounted immutable library, with no model guidance."""
import ctypes
import json
import os
from pathlib import Path
import select
import struct
import sys
import time


def main(root, output, ready):
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'inotify_init1')
    paths = {}
    for path in sorted(Path(root).rglob('*')):
        if path.is_file():
            wd = libc.inotify_add_watch(fd, os.fsencode(path), 0x01 | 0x20 | 0x10)
            if wd < 0:
                raise OSError(ctypes.get_errno(), 'inotify_add_watch')
            paths[wd] = str(path)
    with open(output, 'a', buffering=1) as stream:
        Path(ready).write_text(json.dumps({'pid': os.getpid(), 'watched_files': len(paths)}))
        while True:
            if not select.select([fd], [], [], 1)[0]:
                continue
            data = os.read(fd, 1024 * 1024)
            offset = 0
            while offset < len(data):
                wd, mask, cookie, length = struct.unpack_from('iIII', data, offset)
                offset += 16 + length
                kind = 'overflow' if mask & 0x4000 else 'access' if mask & 1 else 'open' if mask & 0x20 else 'close'
                stream.write(json.dumps({'at': time.time(), 'path': paths.get(wd),
                                         'kind': kind, 'mask': mask}) + '\n')


if __name__ == '__main__':
    main(*sys.argv[1:])
