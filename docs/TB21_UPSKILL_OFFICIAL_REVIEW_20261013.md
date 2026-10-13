# UpSkill Official Source Review

Source reviewed: https://github.com/huggingface/upskill
Revision: 5462a245bed95aab3a5d2264197e893d27318663 (v0.2.1).
Local checkout: /Users/a1/Documents/New project/upskill-official.

The user's intended official repository still needs confirmation. This source
does not implement E9's tb_batch_notes/Ralph/Note-BGE/curator pipeline. Earlier
conversation claims connecting UpSkill to ACP TB or Algorithm 1 were unsupported.
E9's historical configuration explicitly describes reconstructed prompts and
algorithm differences; it is an adaptation result, not an official UpSkill run.

## Verified Call Path

- src/upskill/agent_cards/skill_gen.md: official skill-generator instructions,
  SKILL.md YAML frontmatter and 200-400 word body. No JSON Note schema.
- src/upskill/cli.py:_load_trace_context: text prefix of 4000 characters; JSON
  is parsed and pretty-printed before taking that prefix.
- src/upskill/generate.py:generate_skill: official task/trace-to-SKILL.md call.
- generate_tests: 3-5 synthetic cases, structured TestCaseSuite.
- cli.py:_run_generate_refinement_loop: max_refine_attempts defaults to 2;
  evaluates baseline and skill, stops on beneficial result or <0.05 plateau.
- generate.py:refine_skill: first 500 body characters and first three failure
  descriptions, returns a complete SKILL.md.
- model_resolution.py: generate-time refinement evaluates the generation
  model; --eval-model is a separate extra evaluation, not student refinement.
- evaluate.py/executors: fast-agent local or HF Jobs execution. No native
  TB2.1/Tencent Harbor executor is included in this reviewed repository.

## Prepared Settings

Independent template: experiments/upskill-official/upskill.config.yaml.
Uses current authorized DeepSeek teacher and Qwen student identities with
official model routing, default two refinement evaluations, one run and local
executor. Provider endpoint and credentials must be supplied to fast-agent in
memory; no credentials are stored in this template.

This template is not a launched benchmark and does not migrate E9. TB2.1 real
verifier integration would be an explicit executor adaptation. Replacing
official synthetic evaluation with Harbor rewards changes the method and must
be recorded, not described as the unmodified official workflow.

Original E9 artifacts/contracts remain preserved pending repository selection.

Validation: template parsed by the reviewed official Config and resolve_models
functions with Pydantic 2.12.5. Generation model and separate extra eval role
match the official routing. Full runtime install/test was not completed:
local uv is unavailable and the existing SkillExpand venv uses Pydantic 1.
