"""Prepare ALFWorld assets independently of SkillExpand's Git history.

The pinned ExpeL revision contains the exact assets used by the original setup.
Existing files are checked and reused, never silently replaced.
"""
import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

REVISION = "609aa56355f7ebc538bae102c77ece0147341c8a"
URL = f"https://codeload.github.com/LeapLabTHU/ExpeL/tar.gz/{REVISION}"


def unpack(archive, destination):
    destination = Path(destination)
    count = 0
    with tarfile.open(archive, "r:*") as bundle:
        for member in bundle:
            parts = PurePosixPath(member.name).parts
            if len(parts) < 4 or parts[1:3] != ("data", "alfworld"):
                continue
            relative = PurePosixPath(*parts[3:])
            if ".." in relative.parts or relative.is_absolute():
                raise ValueError(f"Unsafe archive path: {member.name}")
            if not member.isfile():
                if member.isdir():
                    continue
                raise ValueError(f"Unsupported archive entry: {member.name}")
            target = destination.joinpath(*relative.parts)
            if target.is_symlink():
                raise ValueError(f"Refusing symlink destination: {target}")
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError(f"Destination escapes data directory: {target}")
            with bundle.extractfile(member) as source:
                content = source.read()
            if target.exists():
                if target.read_bytes() != content:
                    raise ValueError(f"Existing asset differs; use a new --output: {target}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                    output.write(content)
                    temporary = Path(output.name)
                temporary.replace(target)
            count += 1
    if not count:
        raise ValueError("Archive contains no ALFWorld assets")
    return count


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", choices=["alfworld"])
    parser.add_argument("--output", type=Path, default=Path("data/alfworld"))
    parser.add_argument("--archive", type=Path, help="Use a previously downloaded upstream archive")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="skillexpand-data-") as scratch:
        archive = args.archive
        if archive is None:
            archive = Path(scratch) / "alfworld.tar.gz"
            with urllib.request.urlopen(URL, timeout=60) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
        count = unpack(archive, args.output)
        manifest = {"upstream": "LeapLabTHU/ExpeL", "revision": REVISION,
                    "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "files": count}
        (args.output / "download.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Prepared {count} ALFWorld files in {args.output}")
    print("Use --task-file to select your experiment's task list; historical splits are not regenerated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
