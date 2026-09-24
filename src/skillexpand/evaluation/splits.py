"""Partition tasks before any experience collection; source clusters are attached later."""
import random
from skillexpand import schema as S
from typing import Dict
DEFAULT_RATIOS={'source':.5,'admission':.25,'final':.25}
class SplitError(ValueError):
    pass

def allocate(n: int, ratios: Dict[str, float],
             minimum: int = 1) -> Dict[str, int]:
    """Split ``n`` items by ``ratios`` with an exact total and no starvation.

    Largest-remainder apportionment, then a floor of ``minimum`` per split
    (relaxed only when ``n`` is too small to honour it).  Starvation matters: a
    family with 17 tasks and a 0.5/0.25/0.25 split must not end up with a single
    final task, because a 1-task evaluation set cannot distinguish anything.
    """
    order = list(ratios.keys())
    if n <= 0:
        return {k: 0 for k in order}
    if abs(sum(ratios.values()) - 1.0) > 1e-9:
        raise SplitError(f'ratios must sum to 1.0, got {sum(ratios.values())}')

    raw = {k: n * ratios[k] for k in order}
    counts = {k: int(raw[k]) for k in order}
    remainder = n - sum(counts.values())
    # Hand out leftovers by descending fractional part, ties by declared order.
    for k in sorted(order, key=lambda k: (-(raw[k] - counts[k]), order.index(k))):
        if remainder <= 0:
            break
        counts[k] += 1
        remainder -= 1

    if minimum > 0 and n >= minimum * len(order):
        for k in order:
            while counts[k] < minimum:
                donor = max(order, key=lambda d: counts[d])
                if counts[donor] <= minimum:
                    break
                counts[donor] -= 1
                counts[k] += 1
    return counts


def build_split_plan(task_ids_by_family,benchmark='searchqa',seed=42,ratios=None,minimum=1):
    ids=sorted(t for tasks in task_ids_by_family.values() for t in tasks)
    if len(ids)!=len(set(ids)):
        raise SplitError('Duplicate task IDs')
    random.Random(seed).shuffle(ids)
    counts=allocate(len(ids),ratios or DEFAULT_RATIOS,minimum)
    assignment={};offset=0
    for role,count in counts.items():
        assignment.update({t:role for t in ids[offset:offset+count]});offset+=count
    return S.SplitPlan.make(assignment,benchmark,seed)


def select_tasks(plan,family,split,limit=None):
    ids=sorted(t for t in plan.families.get(family,()) if plan.split_of(t)==split)
    return ids if limit is None else ids[:limit]


def describe(plan):
    return f'{plan.benchmark}: '+', '.join(f'{role}={len(plan.tasks_in(role))}' for role in S.SPLITS)
