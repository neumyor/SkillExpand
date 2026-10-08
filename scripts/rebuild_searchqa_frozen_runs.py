#!/usr/bin/env python3
"""Rebuild empty SearchQA run directories after a frozen-code repair.

The outer campaign manifest and the inner cold-start manifest are separate
records.  This tool archives an empty failed stage and creates a new inner run
manifest signed against the current frozen source tree.  It refuses to touch a
run that already contains task-level output.
"""

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path


CONDITIONS = (
    "l1_executor-strong",
    "cold_start-strong",
    "l2_planner-strong",
    "l2_reviewer-strong",
)
BOOTSTRAP_FILES = {"manifest.json", "config.json", "split.json", "campaign.lock"}


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def content_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def code_signatures(root):
    source = Path(root) / "code" / "src" / "skillexpand"
    return {
        str(path.relative_to(source)): content_hash(path.read_text())
        for path in sorted(source.rglob("*.py"))
    }


def archive_path(path):
    candidate = path.with_name(path.name + "-stale-v1")
    index = 1
    while candidate.exists():
        index += 1
        candidate = path.with_name(path.name + f"-stale-v{index}")
    return candidate


def task_level_files(run):
    run = Path(run)
    return sorted(
        path for path in run.rglob("*")
        if path.is_file() and path.relative_to(run).parts[0] not in BOOTSTRAP_FILES
    )


def rebuild_condition(matrix, condition):
    root = Path(matrix) / condition
    full_searchqa = root / "full" / "searchqa"
    old_run = full_searchqa / "run"
    if not old_run.is_dir():
        raise FileNotFoundError(old_run)
    extras = task_level_files(old_run)
    if extras:
        names = ", ".join(str(path.relative_to(old_run)) for path in extras[:8])
        raise RuntimeError(
            f"{condition} contains task-level output; refusing automatic rebuild: {names}"
        )

    old_manifest = read(old_run / "manifest.json")
    archived = archive_path(full_searchqa)
    full_searchqa.rename(archived)
    new_searchqa = full_searchqa
    new_run = new_searchqa / "run"
    new_run.mkdir(parents=True, exist_ok=True)
    for name in BOOTSTRAP_FILES:
        shutil.copy2(archived / "run" / name, new_run / name)

    manifest = read(new_run / "manifest.json")
    old_code = dict(manifest.get("code", {}))
    new_code = code_signatures(root)
    manifest["code"] = new_code
    write(new_run / "manifest.json", manifest)
    changed = sorted(
        key for key in set(old_code) | set(new_code)
        if old_code.get(key) != new_code.get(key)
    )
    write(new_run / "repair-migration.json", {
        "status": "complete",
        "reason": "rebuild empty run after frozen repair code synchronization",
        "condition": condition,
        "archived_run": str(archived / "run"),
        "old_code": old_code,
        "new_code": new_code,
        "changed_code": changed,
        "created": time.time(),
    })
    return {
        "condition": condition,
        "archived": str(archived),
        "run": str(new_run),
        "changed_code": changed,
        "old_manifest": str(archived / "run" / "manifest.json"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("matrix", type=Path)
    parser.add_argument("--condition", action="append", choices=CONDITIONS)
    args = parser.parse_args()
    conditions = tuple(args.condition or CONDITIONS)
    results = [rebuild_condition(args.matrix.resolve(), condition) for condition in conditions]
    print(json.dumps({"status": "complete", "results": results}, indent=2))


if __name__ == "__main__":
    main()
