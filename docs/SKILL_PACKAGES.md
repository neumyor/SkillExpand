# Generic L2 Skill packages

`SkillEditor.propose_package(base_skill, evidence, feedback=())` proposes a
complete Skill body and optional UTF-8 resources from public contracts,
historical evidence and separately supplied development observations. Its
system prompt is task independent. The model chooses resource purpose,
language and content; researchers do not supply task repair templates.
Routing descriptions remain frozen, and a resource-only revision is a new
candidate. Unsupported or unchanged proposals produce no candidate.

The response contract is:

```json
{"body": "Complete revised instructions", "resources": {"references/note.txt": "Supporting evidence"}, "rationale": "Supported difference"}
```

The model may instead return `{"no_change": true, "reason": "..."}`.
Callers own input selection and candidate acceptance; proposal generation
does not establish empirical improvement.

`Skill.resources` maps canonical relative paths to text. Paths cannot replace
`SKILL.md`, escape the package or conflict with another resource's directory.
`package_mode=True` opts into a rendered entrypoint containing name,
description and body plus those files. `materialize_skill` and
`entrypoint_content` share the rendering contract. Legacy Skills retain
body-only execution and omit empty package defaults from serialization.

Harbor rollout and the aligned/empirical TB2.1 execution scripts mount full
packages. The empirical summary verifies the entrypoint and resources against
the saved Skill before crediting an official result. The native body-only
`FixedSkillScorer` and `PredictedSkillScorer` explicitly reject packages;
package acceptance uses the separate resource-aware Harbor path. This avoids
reusing a body-only score for an unmeasured resource revision.

The October 14 experiment used the native editor, frozen original inputs,
model-generated packages and mechanical development feedback. Its final
candidate passed one complete batch of five fresh Qwen environments under
the unchanged official verifier. Earlier failed and unresolved trials were
retained. This is single-task acceptance, not evidence of cross-task transfer.
Raw requests, traces and final packages remain in the separately delivered
`generic_skill_evo_20261014` research directory; they are not runtime defaults.

Run the focused package checks with:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src python -m pytest -q \
  tests/test_package_editor.py tests/test_skill_package.py \
  tests/test_skill_legacy_serialization.py tests/test_tb21_empirical.py \
  tests/test_serial_l2.py
```
