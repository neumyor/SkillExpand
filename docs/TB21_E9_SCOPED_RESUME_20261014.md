# Official E9 Scoped Resume, 2026-10-14

The user explicitly approved one extra infrastructure attempt for baseline
configure-git-webserver after the verifier stdin fix. The durable amendment
is run/baseline/configure-git-webserver/budget_amendment_20261014.json.
The original two attempts remain preserved. Total allowed attempts for this
unit are three; all other tasks and phases retain infra_attempts=2. The global
extra-attempts variable is unset. The frozen contract and request identities
are unchanged; this compatibility change and budget exception are explicit.

scripts/resume_hkuds_e9_scoped.py checks official and old rollout children,
driver/supervisor identities, and shared capacity under the global lock.
It reuses the existing completed smoke without issuing another smoke run.
Registration reserves 64 workers and returns to zero when the supervisor
finishes. Capacity at launch was 64/250. Prior process identity records and
status are archived in the existing official output.

Verified live identities: supervisor 1638534, driver 1638818, and physical
attempt 03 child 1639123. The third attempt exists and has no result yet.
No completion or verifier success is claimed. Three focused local tests and
the scoped-budget remote test passed. E5 remains stopped.

E7 concurrently reached 267/267; its final panel audit has complete=true,
267 unique valid results, and zero rejected slots. Full report closure remains
separate from this E9 resume checkpoint.
