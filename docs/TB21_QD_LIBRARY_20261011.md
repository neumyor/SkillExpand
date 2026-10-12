# TB2.1 Qwen + DeepSeek Skill Library

Independent run: `runs/tb21-qwen-deepseek-library-qwen-selector-20261011`.

## Corrected revision (2026-10-12)

The accepted E7 setting is now evaluated in the independent child directory
`revisions/178-cards-v1`. The parent library was generated from 89 merged cards
and its 267 scores remain historical evidence only. Do not import its completion,
Skill bodies or accepted slot records into the corrected revision.

The corrected generator validates 89 cards per source, unique source identities,
task text and three traceable attempts per card. Cache reuse requires the exact
generation request payload; materialization checks the request's actual cards
and completion against the proposal. Unknown source rewards remain unknown.

Revision driver PID 2320647 launched with 16 workers and `--initial-only`.
The global capacity lock records 32/250 reserved workers including E4. Initial
evaluation starts only after fresh generation and materialization pass. L2 is
gated on the completed initial audit and validation of the source-card contract.

Live verification of generation request `library_proposal/usage/000.request.json`
confirmed 178 actual submitted cards, 178 unique source identities, 89 cards per
model and 534 traceable attempts. DeepSeek generation is still in progress;
this does not establish a generated library or any revised-panel scores.
Materialization now writes `generation_source_audit.json`, linking the actual
request, source identities and each Skill's provenance. The library manifest
records the same input identities and request path. Focused cache/aligned tests:
13 passed.

E4 recovery is complete: 267 unique valid results, no rejected slots, complete
audit. Its closed-set report gives pass@1 43.82% and pass@3 60.67%; baseline
comparability is false. Do not restart E4. E5_FINAL remains user-stopped.

## 2026-10-12 17:03 Asia/Shanghai checkpoint

Fresh generation completed with 23 initial Skills. The persisted generation
source audit passes for the actual 178-card request, with 89 cards per model
and matching completion/Skill provenance. All three canaries passed. The
16-worker revision driver is in full initial-library execution with 15/267
accepted slots. Read-only verification rechecked all 15 unique task/attempt
identities, real verifier rewards, trajectories, activation and selected bodies.

Slot 00-2 request 001 failed with RuntimeError and no accepted score; request
002 is active under the existing driver. No new driver or budget reset was
needed. No pipeline_cache.json exists in the revision yet, so no cache hit is
claimed. Initial execution audit, L2 and the final frozen panel remain pending.

E3_FINAL, E4 and E6_FINAL each retain complete audits: 267 valid slots, 267
unique result paths and no rejected slots. E5_FINAL remains user-stopped with
175 historical valid slots and an incomplete audit; it was not resumed.

## 2026-10-12 18:07 Asia/Shanghai checkpoint

Revision initial-panel coverage advanced from 15 to 27/267. All 27 accepted
slots were revalidated for unique identity, real reward, trajectory, activation
and selected Skill body. The 178-card generation audit remains passing.
Driver 2320647 has 16 active Harbor children. Slots 00-1 and 09-1 retain
RuntimeError/TimeoutException history; each is executing request 002 without
an accepted result yet. No duplicate driver, new launch or budget reset was
performed. No pipeline cache evidence file has appeared yet.

E3_FINAL/E4/E6_FINAL remain complete and audited at 267/267. E5_FINAL remains
stopped at 175/267. E7 initial completion audit, L2 and final evaluation remain
pending; the automation stays active.

## 2026-10-12 19:03 Asia/Shanghai checkpoint

Revision initial coverage reached 47/267, up 20 accepted slots. All 47 were
revalidated for unique identity, real verifier reward, trajectory, activation
and selected body. Source audit remains passing. Driver 2320647 and 16 Harbor
children are active. Slots 00-1 and 09-1/09-2/09-3 preserve their first-request
runtime/timeout errors and are executing request 002; no reward is fabricated
and no additional driver or budget reset was needed. Pipeline cache evidence
has not appeared yet. Initial full audit, L2 and final panel remain pending.

E3_FINAL/E4/E6_FINAL remain audited complete at 267/267; E5_FINAL remains
user-stopped at 175/267. No completed or stopped panel was restarted.

## 2026-10-12 20:02 Asia/Shanghai checkpoint

Initial revision coverage reached 63/267, up 16 accepted slots. All 63 unique
slots were revalidated for real reward, trajectory, activation and selected
body; generation source audit remains passing. Prior unresolved slots 00-1
and 09-1 now have valid results. Slot 09-3 is executing request 002; slot
09-2 is executing request 003, the final request permitted for this launch.
Neither currently has an accepted reward. Their timeout histories remain
preserved; no fourth request or budget reset was initiated.

Driver 2320647 and 16 Harbor children remain active. No restart was needed;
pipeline cache evidence has not appeared yet. Initial completion audit, L2
and final frozen panel remain pending. E3_FINAL/E4/E6_FINAL retain complete
267-slot audits; E5_FINAL remains stopped at 175/267.

- Input inventory: 178 source-qualified cards, 89 from the DeepSeek raw source and 89 from the Qwen source; no task/family deduplication.
- Roles: Qwen `qwen3.6-flash-distill` selector and executor; `DEEPSEEK_up5zdj` method/evolver.
- Sequence: initial 89 x 3 verifier slots, one L2 evolution round, then a separate frozen-library 89 x 3 panel.
- Infrastructure: 16 workers, global reservation limit 250, Tencent E2B persistence 0, pipeline cache `pipeline-uv095-py3139-v6`.
- Credentials are process-only. The manifest records role names and provenance paths, never key values.

The initial run is started only after the prepare audit confirms exactly 178 cards and 89 source cards per model. Scores are accepted only from the earliest valid identity, trajectory, Skill activation/body, and real verifier reward; timeout, empty, corrupt, and incomplete requests remain unresolved.
