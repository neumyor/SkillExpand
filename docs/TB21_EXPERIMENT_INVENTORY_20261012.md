# TB2.1 experiments since 2026-10-01

Snapshot: 2026-10-12. Read from live jinan40 manifests, status, audits and reports.
Run paths below are relative to `/data2/liyishan/SkillExpand-tb21/SkillExpand/runs`.
Q = qwen3.6-flash-distill. D = DEEPSEEK_up5zdj for the current aligned methods.
DeepSeek raw source uses deepseek-v4-flash-0731; do not conflate source identity
with the method/provider alias. All panels discussed here are closed-set.

## Timeline

| Date | Work actually run | Interpretation |
| --- | --- | --- |
| 10/01 | Qwen version canary on fix-git; full 89 x 3 Qwen rollout | Canary reward 1; full job exited 0 and has 267 trial result files. Artifact count alone is not a complete valid-reward audit. |
| 10/02 | DeepSeek raw canaries and E2 full 89 x 3 rollout; Qwen trajectory import and E1 fix-git integration smokes v1-v4 | Produced the raw source trajectories and tested SkillExpand integration. Smokes are not additional full comparison panels. |
| 10/03-10/06 | E1 progressive freely generated library and family evolution; successive repair/stream/concurrency runs | Qwen source, Qwen methods, Qwen selector/executor. Final source uses repair9 and seven Skills. Recovery directories are continuations or diagnostics, not independent repetitions. |
| 10/04-10/09 | Historical E3/E4/E5 parallel/bootstrap runs and recoveries | Used a single general bootstrap; do not treat as the later freely generated multi-Skill experiments. |
| 10/08 onward | E1 final frozen-library verifier panel | Completed 267/267. |
| 10/08 | Historical E4 bootstrap verifier launch | Cancelled for wrong library dependency; retained one valid canary result. Excluded from current E4. |
| 10/09-10/11 | Fresh aligned E3/E5/E6 library generation, initial 267-slot panels, one L2 round each | Initial panels and L2 audits complete. |
| 10/10 onward | E3_FINAL, current E4, E5_FINAL, E6_FINAL | Independent frozen-library panels. E3/E4/E6 complete; E5_FINAL user-stopped. |
| 10/11-10/12 | E7 parent panel | Completed 267 initial slots, but cached generation used 89 merged cards; historical library panel only. No acceptance as an independent 178-card experiment. |
| 10/12 onward | E7 revision 178-cards-v1 | Fresh DeepSeek generation from actual 178 separate cards produced 23 Skills. Source audit passed; three canaries passed, full initial panel running. |

## Settings

| Experiment | Input to initial library generation | Generator / Evolver | Initial selector | Executor | Initial Skills | Evolution status |
| --- | --- | --- | --- | --- | --- | --- |
| E1 | Qwen 89 task cards from 267 raw attempts | Q | Q | Q | 7 | Completed; final library has two v0 and five v1 heads |
| E2 | DeepSeek no-Skill raw rollout, 89 x 3 | No Skill evolution in this raw baseline | Not applicable | deepseek-v4-flash-0731 | None | Raw source experiment |
| E3 aligned | DeepSeek 89 cards from 267 raw attempts | D | D | D | 20 | Completed, 20 family batches; 15 approved updates |
| E4 current | Full audited E3 final heads | Reuses E3; no new L2 | D | Q | E3's 20 final heads | Transfer verifier panel complete |
| E5 aligned | Qwen + DeepSeek raw trajectories, merged by task into 89 cards; six source attempts per card | D | D | Q | 11 | Completed, 11 family batches; 7 approved updates |
| E6 | Qwen 89 cards from 267 raw attempts | D | Q | Q | 10 | Completed, 10 family batches; 9 approved updates |
| E7 revision | Qwen 89 + DeepSeek 89 separate source-qualified cards, 178 total; 534 source attempts | D | Q | Q | 23 | Initial verifier panel running; L2 and final panel pending |

E5 was not Qwen-only, and its 89 merged cards were not 178 independent cards.
The requested independent dual-source setting is E7 revision. E5_FINAL changed
the selector from D to Q, while retaining Q execution. Its comparison cannot
isolate Skill evolution, and no new v0 control is being added.

L2 in the aligned experiments uses Skill-aware cards derived from each run's
initial execution: E3 122 cards, E5 113 cards, E6 117 cards. These counts differ
from the raw cards used to generate the initial library. Predicted review
acceptance is not a real verifier pass rate. Approved updates do not require
every Skill to advance from v0.

## Real verifier panels

| Panel | Valid slots | pass@1 | pass@3 | State |
| --- | --- | --- | --- | --- |
| E1 final library | 267/267 | 43.45% | 60.67% | Complete, audit passed |
| E3 initial library | 267/267 | 68.16% | 82.02% | Complete |
| E3 final library | 267/267 | 80.15% | 89.89% | Complete, audit passed |
| E4 E3 final library with Qwen executor | 267/267 | 43.82% | 60.67% | Complete, audit passed |
| E5 initial library, D selector / Q executor | 267/267 | 43.07% | 57.30% | Complete |
| E5 final library, Q selector / Q executor | 175/267 | Not reported | Not reported | User-stopped, incomplete audit |
| E6 initial library | 267/267 | 41.95% | 58.43% | Complete |
| E6 final library | 267/267 | 44.57% | 59.55% | Complete, audit passed |
| E7 historical merged-card library | 267/267 | Historical panel only | Historical panel only | Does not satisfy 178-card source acceptance |
| E7 independent 178-card revision | 3 canaries accepted at snapshot | Pending | Pending | Full initial panel running |

pass@1 is the mean success over all three attempts per task (267 total);
pass@3 is the fraction of 89 tasks with at least one success. Only real reward
0/1 with valid identity, trajectory, selection, activation and mounted body is
counted. Missing reward is not zero. AgentTimeoutError can be accepted when a
real valid verifier reward exists.

Initial/final comparisons must disclose internal DNS/mirror and pipeline v6
cache changes. E1/E4 baseline comparability is false in their reports. None of
these panels establishes independent test generalization or statistical
significance.

## Evidence directories

- 10/01 raw job: `/data2/liyishan/tbench2-openclaw-min/jobs/tb21_version_canary_20261001/tb21_full_after_version_canary_20261001`.
- E2 raw job: `/data2/liyishan/tbench2-openclaw-min/jobs/tb21_skillexpand_20261002/tb21_e2_deepseek_raw_20261002`.
- E1 final source: `tb21-e1-progressive-formal-20261006-repair9-stream-concurrent`.
- E1 final panel: `tb21-e1-empirical-final-library-20261008`.
- E3 aligned: `tb21-e3-e1aligned-free-library-20261009`.
- E5 aligned: `tb21-e5-e1aligned-free-library-20261009`.
- E6 aligned: `tb21-e6-e1aligned-qwen-experience-deepseek-evolver-20261009`.
- E3 final panel: `tb21-e3-final-library-empirical-20261010`.
- E4 current panel: `tb21-e4-e3-final-library-empirical-20261010`.
- E5 stopped panel: `tb21-e5-final-library-empirical-20261010`.
- E6 final panel: `tb21-e6-final-library-empirical-20261010`.
- E7 historical parent: `tb21-qwen-deepseek-library-qwen-selector-20261011`.
- E7 accepted revision: `tb21-qwen-deepseek-library-qwen-selector-20261011/revisions/178-cards-v1`.

Provider 503/429, schema/empty-response, parser, transport, capacity and pipeline
cache probes are diagnostic runs. Do not count their directories as additional
independent experiments or import diagnostic reviewer scores into formal panels.
