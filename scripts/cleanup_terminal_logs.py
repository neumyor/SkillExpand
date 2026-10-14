"""Remove completed trial recordings only, retaining an audit manifest."""
import argparse
import json
import os
import stat
from pathlib import Path


def open_inodes():
    if os.geteuid() != 0:
        raise RuntimeError("Root is required to inspect all process file descriptors")
    opened = set()
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            for fd in (proc / 'fd').iterdir():
                try:
                    st = fd.stat()
                    opened.add((st.st_dev, st.st_ino))
                except FileNotFoundError:
                    pass
        except FileNotFoundError:
            pass
    return opened


def eligibility(path, root, opened):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        return None, 'not_regular'
    if (st.st_dev, st.st_ino) in opened:
        return None, 'open'
    for parent in path.parents:
        result = parent / 'result.json'
        if result.is_file() and not result.is_symlink():
            try:
                data = json.loads(result.read_text())
                if not isinstance(data, dict):
                    return None, 'invalid_result'
                finished = data.get('finished_at')
            except (ValueError, OSError):
                return None, 'invalid_result'
            if not finished:
                return None, 'unfinished'
            return dict(path=str(path), size=st.st_size, blocks=st.st_blocks * 512,
                        device=st.st_dev, inode=st.st_ino, mtime=st.st_mtime_ns,
                        result=str(result), finished_at=finished), None
        if parent == root:
            break
    return None, 'missing_result'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', action='append', required=True)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    roots = [Path(p).absolute() for p in args.root]
    opened = open_inodes()
    records, skipped = [], {}
    for root in roots:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
            for name in files:
                if not name.endswith(('.pane', '.cast')):
                    continue
                row, reason = eligibility(Path(directory) / name, root, opened)
                if row:
                    records.append(row)
                else:
                    skipped[reason] = skipped.get(reason, 0) + 1
    manifest = Path(args.manifest)
    with manifest.open('x') as stream:
        for row in records:
            stream.write(json.dumps(row) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    deleted, freed = 0, 0
    if args.apply:
        opened = open_inodes()
        for row in records:
            path = Path(row['path'])
            root = next(r for r in roots if path.is_relative_to(r))
            current, reason = eligibility(path, root, opened)
            if current != row:
                skipped[reason or 'changed'] = skipped.get(reason or 'changed', 0) + 1
                continue
            path.unlink()
            deleted += 1
            freed += row['blocks']
    print(json.dumps(dict(candidates=len(records), deleted=deleted,
                          freed_gib=freed / 2**30, skipped=skipped, manifest=str(manifest))))


if __name__ == '__main__':
    main()
