"""Read completed cold starts as immutable input to a new L2 protocol."""

import json
from pathlib import Path

from omegaconf import OmegaConf

from skillexpand.runtime import agent_factory as F
from skillexpand import schema as S
from skillexpand.persistence import store as ST
from skillexpand.persistence.io import read_split
from skillexpand.reliability.errors import JournalConflict
from skillexpand.persistence.io import freeze
from skillexpand.l1.family_discovery import load_family_plan
from skillexpand.l1 import patterns as BP
from skillexpand.l1.protocol import projection


FILES = (
    "config.json",
    "split.json",
    "manifest.json",
    "clusters.json",
    "task_skill_map.json",
    "initial_skills.json",
    "cold_start_complete.json",
)


def load_cold_start(root):
    root = Path(root)
    values = {name: json.loads((root / name).read_text()) for name in FILES}
    manifest = values["manifest.json"]
    if (
        manifest["split"] != values["split.json"]
        or manifest["config"] != values["config.json"]
    ):
        raise JournalConflict("Cold-start split/config differs from its frozen manifest")
    cfg = OmegaConf.create(values["config.json"])
    plan = read_split(root / "split.json")
    clusters = load_family_plan(root / "clusters.json", benchmark=plan.benchmark)
    mapping = values["task_skill_map.json"]
    initial = values["initial_skills.json"]
    complete = values["cold_start_complete.json"]
    expected = {
        str(t): f"{plan.benchmark}.{f}" for t, f in clusters.task_to_family.items()
    }
    train = set(plan.tasks_in(S.SPLIT_TRAIN))
    if complete.get("train_count") != len(train):
        raise JournalConflict("Cold-start train count mismatch")
    if mapping != expected or set(clusters.task_to_family) != train:
        raise JournalConflict("Cold-start mapping must cover exactly train tasks")
    if complete["mapping_hash"] != S.content_hash(mapping) or complete[
        "initial_skills_hash"
    ] != S.content_hash(initial):
        raise JournalConflict("Cold-start artifact hash mismatch")
    table = F.task_table(cfg, refresh=True)
    if set(plan.assignment) != set(range(len(table))) or values["manifest.json"][
        "task_table_hash"
    ] != S.content_hash(table):
        raise JournalConflict("Cold-start task data changed")
    skills = tuple(S.from_dict(S.Skill, item) for item in initial)
    if len(skills) != len(clusters.families) or {s.family_id for s in skills} != set(
        clusters.families
    ):
        raise JournalConflict("Initial library does not match clusters")
    cards = {}
    for t in sorted(train):
        exp = S.from_dict(
            S.TaskExperience,
            json.loads((root / "discovery/results" / f"{t}.json").read_text()),
        )
        if (
            exp.task_id != t
            or exp.benchmark != plan.benchmark
            or exp.split != S.SPLIT_TRAIN
            or exp.initial_skill_key is not None
            or exp.selected_skill_id is not None
            or not exp.experience_id.startswith("discovery:")
            or not exp.experience_card
            or exp.experience_card.get("schema_version") != 5
            or exp.experience_card.get("task", {}).get("task_id") != t
        ):
            raise JournalConflict(f"Invalid cold-start experience: task {t}")
        cards[t] = exp
    expected_hashes = json.loads((root / "discovery/card_hashes.json").read_text())
    actual_hashes = {
        str(t): S.content_hash(projection(e.experience_card)) for t, e in cards.items()
    }
    if actual_hashes != expected_hashes:
        raise JournalConflict("Cold-start cards differ from discovery hashes")
    if len({e.experience_id for e in cards.values()}) != len(cards):
        raise JournalConflict("Duplicate cold-start experience IDs")
    batch_size = manifest['card_batch_size']
    for family, ids in sorted(clusters.families_index.items()):
        for index, start in enumerate(range(0, len(ids), batch_size)):
            batch = ids[start:start + batch_size]
            path = root / 'discovery' / 'initial_skills' / f'{family}-{index}-patterns.json'
            result = json.loads(path.read_text())
            expected_hashes = {str(t): S.content_hash(projection(cards[t].experience_card)) for t in batch}
            BP.validate_cache(result, [cards[t] for t in batch], expected_hashes)
    for skill in skills:
        ids = tuple(sorted(clusters.families_index[skill.family_id]))
        if (
            skill.skill_id != f"{plan.benchmark}.{skill.family_id}"
            or skill.version != 0
            or not skill.description.strip()
            or not skill.body.strip()
            or tuple(skill.provenance.source_task_ids) != ids
            or tuple(skill.provenance.source_experience_ids)
            != tuple(cards[t].experience_id for t in ids)
        ):
            raise JournalConflict("Initial Skill provenance does not match train cards")
    plan = S.SplitPlan.make(
        plan.assignment, plan.benchmark, plan.seed, clusters.families_index
    )
    return cfg, plan, skills, cards


def import_cold_start(source, target):
    """Copy completed inputs, never mutable Skill heads or old evolution results."""
    source, target = Path(source).resolve(), Path(target).resolve()
    cfg, plan, skills, cards = load_cold_start(source)
    identity = {
        "source": str(source),
        "files": {
            n: S.content_hash(json.loads((source / n).read_text())) for n in FILES
        },
        "cards": {str(t): S.content_hash(S.to_dict(e)) for t, e in cards.items()},
    }
    freeze(target / "cold_start_import.json", identity)
    for name in FILES:
        freeze(target / name, json.loads((source / name).read_text()))
    freeze(target / "discovery/card_hashes.json",
           json.loads((source / "discovery/card_hashes.json").read_text()))
    for t, exp in cards.items():
        freeze(target / "discovery/results" / f"{t}.json", S.to_dict(exp))
    for path in sorted((source / 'discovery' / 'initial_skills').glob('*-patterns.json')):
        freeze(target / 'discovery' / 'initial_skills' / path.name,
               json.loads(path.read_text()))
    library = ST.SkillLibrary(target / "skills.jsonl", benchmark=plan.benchmark)
    for skill in skills:
        if skill.family_id not in library.families:
            library._append_new(skill)
        elif library.history(skill.family_id)[0] != skill:
            raise JournalConflict("Imported initial Skill differs from local history")
    return cfg, plan
