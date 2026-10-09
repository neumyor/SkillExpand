# Authorized Total Concurrency 100

## Completion Audit, 2026-10-09

E3 and E5 have finished both batches (50 + 39 unique cards). Their PIDs
1497234 and 1502190 have exited. Each run contains 534 unique reviewer cache
keys, six entries per task, and complete paired 89-task candidate panels.
`scripts/audit_tb21_completed_batches.py` passed for both directories and
wrote `completion_audit.json` and `completion_report.md` in each run.

- E3: `runs/tb21-e3-20261009-up5zdj-concurrent42`; zero approved updates,
  final `general@v0`. Base predicted mean 0.730899; best candidate 0.694944.
  All five candidates failed strict improvement. Evolution completed without
  an evolved Skill; the corrected E4 transfer dependency is unmet.
- E5: `runs/tb21-e5-20261009-up5zdj-concurrent42`; one approved update,
  final `general@v1`. First-batch selected candidate mean 0.743034 versus
  base 0.713483. Second-batch candidates 0.678876 and 0.540787 failed against
  v1, so it is retained.
- E1 empirical: PID 1740668 remains alive, 16 workers, 222/267 verifier slots
  at this inspection. E3/E5 release their 84 workers; no E4 run was launched.

These means are closed-set predicted reviewer results, not verifier pass rates.
No independent test generalization or uplift over incomplete raw baseline is
claimed. Historical startup details below describe the previous running state.

User requested 100 concurrent workers on 2026-10-09. Allocation preserves the
running E1 empirical panel: E1 16, E3 42, E5 42. Total reserved workers: 100.

## Current Runs

- E1: `runs/tb21-e1-empirical-final-library-20261008`, PID 1740668.
- E3: `runs/tb21-e3-20261009-up5zdj-concurrent42`, PID 1497234; 196 valid reviewer cache records reused.
- E5: `runs/tb21-e5-20261009-up5zdj-concurrent42`, PID 1502190; 468 valid reviewer cache records reused and the first-batch journal retained.

E3/E5 use `DEEPSEEK_up5zdj`, thinking, streaming, `max_tokens=65536`, and
Tencent E2B relay with template-default startup. Task persistence stays 0.
Old processes and their relay sandboxes were stopped before restarting;
`concurrency_transition.json` records interrupted requests as configuration
restarts. No reward was fabricated and no correction budget was reset.

Startup inspection confirmed both stage processes running, with 71 E3 and 47 E5
request identities created and no new provider 429 events at that instant.
This is not completion evidence. Production batches, gate, journal and audit
remain necessary; only candidate mean greater than base mean permits commit.
Results remain closed-set predicted acceptance, not independent verifier scores.

E4 must wait for a real audited E3 Skill. Its cancelled bootstrap empirical
directory must not be resumed. Any future E4 launch must first free capacity;
the total worker ceiling is shared across all stages.
