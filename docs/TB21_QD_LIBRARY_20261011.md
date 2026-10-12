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

- Input inventory: 178 source-qualified cards, 89 from the DeepSeek raw source and 89 from the Qwen source; no task/family deduplication.
- Roles: Qwen `qwen3.6-flash-distill` selector and executor; `DEEPSEEK_up5zdj` method/evolver.
- Sequence: initial 89 x 3 verifier slots, one L2 evolution round, then a separate frozen-library 89 x 3 panel.
- Infrastructure: 16 workers, global reservation limit 250, Tencent E2B persistence 0, pipeline cache `pipeline-uv095-py3139-v6`.
- Credentials are process-only. The manifest records role names and provenance paths, never key values.

The initial run is started only after the prepare audit confirms exactly 178 cards and 89 source cards per model. Scores are accepted only from the earliest valid identity, trajectory, Skill activation/body, and real verifier reward; timeout, empty, corrupt, and incomplete requests remain unresolved.
