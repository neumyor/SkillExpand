# Authorized Budget Reset

The user explicitly authorized: "重置所有预算，E7,E8 缺失补跑".
This supersedes the earlier prohibition on budget resets for E7, E8 and E9.
E5 remains stopped and Final panels are not rerun.

Each experiment retains budget_reset_20261013.json with the prior status,
authorization and new epoch. Old requests, results, failed attempts, canceled
executions and contracts remain available; accepted binary scores are reused.

- E7: only missing initial slot 77-3, up to three new requests 007-009.
  The complete initial/source audit still gates L2 and final evaluation.
- E8: 13 missing trial identities, up to two new physical executions each in
  budget_reset_20261013/attempt-1 and attempt-2. Original model, context,
  sampling and retry identity are retained. Ready task caches are injected
  only at verifier start. The corrected custom Tencent adapter is checked
  before dispatch. Accepted original scores are never replaced.
- E9: original --resume config/work-dir with explicit infrastructure amendment
  TB21_AUTHORIZED_INFRA_EXTRA_ATTEMPTS=2. Historical attempts 01-02 remain;
  new infrastructure attempts 03-04 are available. This compatibility change
  is an explicit budget-contract amendment, not a silent unchanged-contract
  claim. Existing valid executions are reused. Authoring/teacher algorithm
  budgets retain their original values until a concrete missing unit needs
  the new authorization applied.

Credential injection uses authorized Desktop env into process memory over SSH
stdin. Shared recursive capacity remains at most 250 under the common lock.
All results remain closed-set and missing rewards remain unknown.

## 2026-10-13 Settings Compatibility Recovery

E9's prior resume stopped before formal execution because its current method
and eval settings differed from the frozen contract. The user confirmed the
E8-aligned Teacher settings and Student thinking=true are authorized. Resume
now validates those precise executable changes, records both contracts in
contract.budget-reset-compatibility-20261013.json, and retains the original
contract and historical request identities. Source and task-snapshot changes
remain explicitly disclosed infrastructure amendments.

E8's latest strict recovery audit accepts five new genuine successes:
259/267 valid scores (202 successes, 57 failures), with eight missing.
E7 remains 266/267 in initial execution; request 007 for slot 77-3 is active,
and L2 remains gated. E9 was relaunched with supervisor 1443537 under the
shared lock, reserving 93/250 workers in total; launch is not acceptance.
