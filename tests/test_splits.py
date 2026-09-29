"""Partition checks independent of discovered task clusters."""
from skillexpand.evaluation import splits as SP
from skillexpand import schema as S


def test_partition_covers_each_task_once():
    plan=SP.build_split_plan({'tasks':list(range(100))},seed=42)
    assert set(plan.assignment)==set(range(100))
    assert [len(plan.tasks_in(s)) for s in S.SPLITS]==[50,25,25]
    assert not plan.families


def test_partition_is_reproducible_and_cluster_independent():
    a=SP.build_split_plan({'tasks':list(range(40))},seed=42)
    b=SP.build_split_plan({'a':list(range(20)),'b':list(range(20,40))},seed=42)
    assert a.assignment==b.assignment
    assert a.assignment!=SP.build_split_plan({'tasks':list(range(40))},seed=43).assignment


def test_duplicate_ids_and_invalid_ratios_rejected():
    for fn in (lambda:SP.build_split_plan({'a':[1],'b':[1]}),
               lambda:SP.allocate(10,{'train':.7,'test':.5})):
        try:fn()
        except ValueError:pass
        else:raise AssertionError('Invalid partition accepted')


def test_small_partition_keeps_all_three_roles():
    plan=SP.build_split_plan({'tasks':[0,1,2]})
    assert all(len(plan.tasks_in(s))==1 for s in S.SPLITS)


def test_source_membership_never_assigns_heldout():
    plan=S.SplitPlan.make({0:'train',1:'val',2:'test'},'x',42,{'cluster':(0,)})
    assert SP.select_tasks(plan,'cluster','train')==[0]
    assert SP.select_tasks(plan,'cluster','val')==[]
    assert plan.family_of(2) is None
