# E4 frozen-library empirical evaluation

## Cancelled: wrong experimental input

This launch was cancelled and is excluded from E4 results. The intended E4
transfer experiment requires E3-produced DeepSeek SkillExpand Skills. E3 has
no production batch commit yet, only bootstrap v0. Evaluating bootstrap v0
does not satisfy E4's intended setting. Retain canary artifacts for diagnosis;
do not import them into a future E4 transfer evaluation. Start the intended
E4 in a new directory only after the E3 Skill dependency is satisfied.

User authorized E4 real execution after E1/E3/E5 were launched. Independent
directory: `runs/tb21-e4-empirical-library-20261008` on jinan40 under
`/data2/liyishan/SkillExpand-tb21/SkillExpand`.

Source: `tb21-e4-qwen-deepseek-parallel-20261006-recovery6`. Freeze the actual
reviewed `terminalbench.terminalbench.general@v0` bootstrap Skill. Source has
89 unique exact reviewer identities and no evolved Skill version. This run
must not be described as evaluation of an evolved E4 library.

Run 89 tasks x 3 independent attempts with Qwen executor
`qwen3.6-flash-distill` and the source-configured DeepSeek selector
`deepseek-v4-flash-0731-tencent`. Each attempt uses metadata-only catalog,
selector, loading exactly one Skill, then Tencent E2B Harbor execution.
Task persistence=0, 16 workers. At preparation other active runs reserved 32
workers, making the total 48, below 100.

First run fix-git, log-summary-date-ranges and constraints-scheduling once.
Valid canary results count toward 267 and automatically enable full execution.
Only missing/infrastructure slots are retried; valid slots are not repeated.
Verifier results, trajectories, selection and mounted Skill evidence are audited.
Stage completes only with 267 valid unique slots and a complete audit.

Use `scripts/run_tb21_empirical.py --stage E4` and
`scripts/summarize_tb21_empirical.py` for continuation and report generation.
The source baseline is the same bare Qwen rollout used for E1 comparison;
its strict verifier audit remains incomplete. User acceptance of the raw
input gate does not create a complete verifier-backed baseline score.
No improvement delta is claimed before complete matched baseline evidence.

Report pass@1 as mean success over all three attempts per task; pass@3 as
the fraction of tasks with at least one success. Scope is closed-set execution,
not independent test generalization. Existing E1/E3/E5 runs are preserved.
Focused empirical tests on jinan40: 8 passed.
