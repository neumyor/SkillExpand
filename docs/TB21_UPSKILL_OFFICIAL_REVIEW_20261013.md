# HKUDS UpSkill Official Source Alignment

User-confirmed source: https://github.com/HKUDS/UpSkill.
Reviewed revision: ed42b147411bde72620a9a61bc956693413b0607.
Local checkout: /Users/a1/Documents/New project/HKUDS-UpSkill.
The unrelated huggingface/upskill review is superseded; its config was removed.

## Why the Original E9 Differed

The original E9 ran a local paper-inspired batch_notes implementation with
reconstructed JSON Note prompts, BGE/lexical retrieval, failure-only teaching,
quality-threshold admission and post-Ralph curation. It was not wired to the
official TB scripts. There was no verified technical reason to replace the
official prompts and algorithm. Previous statements conflating official TB
scripts with that reconstruction were incorrect.

## Verified Official TB Behavior

- scripts/run_brew.py: build_brew_prompt imported directly. Brew both PASS
  and FAIL baseline trajectories, last 15 message steps, 250 chars per step.
  Teacher uses Write to create CLAUDE.md, solve-task.md and feedback.md.
- scripts/run_curator.py: exact AST-extracted curator_prompt, grouped by task
  metadata category. Truncation: guidance 8000, skills 6000, feedback 4000 chars.
- scripts/run_ralph.py: up to three student validations; failed rounds use the
  exact revise_prompt, first 2000 current-guidance chars, last 10 steps x 200
  chars. The upstream script persists only revised CLAUDE.md.
- scripts/run_serve.sh: category CLAUDE.md deployment, not BGE Top-3 retrieval.
- REPRODUCE.md: Baseline -> Brew -> Curate -> Ralph -> Serve. Ralph updates
  per-task files after category curation; Serve still reads pre-Ralph curated
  files. The revision follows this literal ordering, without silent recuration.

## Independent E9 Revision

Output on jinan40:
/home/xinhaidong/agentbrew-tb21-evo89-test89-20261012/outputs/E9_hkuds_upskill_acp_evo89_test89_20261013

The old E9 evaluator was stopped with its recorded process identity and all
artifacts remain intact. A new empty library and fresh baseline are used;
no old memory, teacher response or accepted panel is imported. The registration
official_revision.json disables accidental old batch_notes resumes.

run_hkuds_upskill_tb21.py uses the official Brew function and extracts the actual
Curator/Ralph f-string AST. It implements the three ACP paths through the
OpenAI Write tool contract. Independent fix-git smoke precedes formal execution;
the supervisor proceeds only when real verifier evidence passes its audit.

Explicit adaptations to the upstream TB2.0/Claude Code experiment:
- User TB2.1 evolve89/test89 closed-set protocol and 89-task vanilla, one each.
- Authorized Qwen student and E8-aligned DeepSeek teacher settings.
- Tencent E2B/Terminus instead of Claude Code; category CLAUDE.md injected as
  immutable system guidance. Teacher has Write only, not the full CC toolset.
- 64 student and 16 teacher workers, shared reservation <=250, cache v6.
- Real executed verifier reward only. Unknowns excluded from PASS/FAIL and
  teacher input; historical infrastructure and failed requests stay preserved.
- Durable request/eval contract binding, infrastructure attempts=2, authoring
  max eight tool turns (explicit transport budget, not an upstream default).

This is official TB prompt/ACP semantics adapted to the authorized execution
environment, not an unmodified official Claude Code replication or Algorithm 1
claim. Smoke cannot prove full 1M context or actual max effort availability.

Focused validation: five tests passed for official prompt sourcing, trace
projection, Write-tool execution/cache settings, real-reward classification
and blocking full acceptance when Ralph has missing verifier results.

The three unchanged upstream Python source files, MIT license and SOURCE.json
are retained in sources/HKUDS-UpSkill, so prompt generation is reproducible
without an untracked sibling checkout. Formal contracts also bind the complete
student eval configuration and teacher settings. All physical rollout usage
and teacher request usage are retained in the final report.

## Launch Checkpoint

Independent smoke supervisor 3213048 and driver 3213049 were verified alive.
fix-git baseline: true verifier reward 1, tests_executed, no exception.
Brew and category Curate both produced all three ACP files via Write tools.
Ralph round 1, Serve and Vanilla all completed with true verifier reward 1.
Smoke audit complete=true, teacher test calls=0; Serve had 2848 chars of
category guidance. Teacher HTTP requests confirmed deepseek-v4-flash-0731,
reasoning_effort=max, max_tokens=65536, top_p=.95, top_k=20, thinking=true.
The smoke used the first runner version; all four outcomes have executed
verifier evidence. Formal execution uses the expanded configuration contract
and missing-Ralph acceptance guard.
Formal driver 3272315 entered baseline, 89-task contract and 64 submitted
task requests verified. Shared launch reservation was 80 (E7 16 + E9 64).
Later process identity and status must be re-read; these PIDs are launch evidence.

The existing tb2-1 heartbeat was updated in place to use this official revision;
old batch_notes instructions are superseded. E7/E8 and stopped E5 controls
were preserved.

Registration official_checkpoint.json records the official output, source,
current phase, smoke results and verified supervisor/driver identities.
Commit a6f798e was pushed via HTTP/1.1 to codex/tb21-progressive-relay-recovery.
