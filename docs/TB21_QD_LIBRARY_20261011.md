# TB2.1 Qwen + DeepSeek Skill Library

Independent run: `runs/tb21-qwen-deepseek-library-qwen-selector-20261011`.

- Input inventory: 178 source-qualified cards, 89 from the DeepSeek raw source and 89 from the Qwen source; no task/family deduplication.
- Roles: Qwen `qwen3.6-flash-distill` selector and executor; `DEEPSEEK_up5zdj` method/evolver.
- Sequence: initial 89 x 3 verifier slots, one L2 evolution round, then a separate frozen-library 89 x 3 panel.
- Infrastructure: 16 workers, global reservation limit 250, Tencent E2B persistence 0, pipeline cache `pipeline-uv095-py3139-v6`.
- Credentials are process-only. The manifest records role names and provenance paths, never key values.

The initial run is started only after the prepare audit confirms exactly 178 cards and 89 source cards per model. Scores are accepted only from the earliest valid identity, trajectory, Skill activation/body, and real verifier reward; timeout, empty, corrupt, and incomplete requests remain unresolved.
