"""Fixed, task-independent projections of authorized original histories.

This module only projects supplied data. It does not read task or verifier files.
Development feedback uses project_tool_results with its default content policy
in both arms; structural_only is the historical B ablation.
"""
from copy import deepcopy
import re


RESULT_LIMIT = 8192
SANITIZED_SEED_BODY = "Follow the public task requirements."
_RESTRICTED = re.compile(r"/tests|verifier|reward\.txt|test[-_]stdout", re.IGNORECASE)


def sanitize_seed(raw_skill):
    """Remove all learned seed content with one task-independent rule.

    The caller archives raw_skill separately. Keep only generic routing/identity
    fields; discard historical body, resources, provenance and unknown extras.
    """
    seed = {
        key: deepcopy(raw_skill[key])
        for key in ("skill_id", "family_id", "version", "name", "description")
    }
    seed.update(body=SANITIZED_SEED_BODY, resources={}, provenance={}, package_mode=False)
    return seed


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _restricted_step(calls, results):
    # Check full originals, including content later omitted by head/tail limits.
    values = [call.get("arguments") for call in calls]
    values.extend(result.get("content", "") for result in results)
    return any(_RESTRICTED.search(text) for value in values for text in _strings(value))


def bounded_content(content):
    if not isinstance(content, str):
        raise ValueError("Tool result content must be a string")
    size = len(content)
    truncated = size > RESULT_LIMIT
    return {
        "content": content[:4096] + content[-4096:] if truncated else content,
        "original_characters": size,
        "truncated": truncated,
        "head_characters": 4096 if truncated else size,
        "tail_characters": 4096 if truncated else 0,
    }


def project_tool_results(trace, structural_only=False):
    """Project tool steps without arguments, narration, or reasoning.

    One restricted command or result removes the entire step's result contents,
    including otherwise allowed sibling results. No task-specific matching is
    used. Structural mode retains only identifiers, names, presence, and counts.
    """
    observations = []
    for index, step in enumerate(trace["steps"]):
        calls = step.get("tool_calls") or []
        observation = step.get("observation")
        if not calls and observation is None:
            continue
        results = [] if observation is None else observation.get("results", [])
        restricted = _restricted_step(calls, results)
        item = {
            "step_index": index,
            "step_id": step.get("step_id"),
            "tool_functions": [call.get("function_name") for call in calls],
            "tool_calls_count": len(calls),
            "observation_present": observation is not None,
            "results_count": len(results),
            "results": [],
        }
        if restricted:
            item["content_status"] = "restricted_evaluation_output_removed"
        elif structural_only:
            item["content_status"] = "historical_free_text_removed"
        else:
            item["results"] = [
                {"result_index": result_index, **bounded_content(result.get("content", ""))}
                for result_index, result in enumerate(results)
            ]
        observations.append(item)
    return observations


def project_history(raw, arm):
    """Return the native package editor evidence shape for frozen A or B."""
    if arm not in ("A", "B"):
        raise ValueError("arm must be A or B")
    structural = arm == "B"
    rows = []
    for row in raw["evidence"]:
        reward = row.get("reward")
        if structural and not (
            isinstance(reward, (int, float))
            and not isinstance(reward, bool)
            and reward == 0
        ):
            continue
        rows.append({
            "id": row["id"],
            "official_reward": reward,
            "loaded_skill": None if structural else row.get("skill_body"),
            "steps": project_tool_results({"steps": row["steps"]}, structural_only=structural),
        })
    memory = {} if structural else {
        key: deepcopy(value["experience_card"])
        for key, value in raw.get("memory", {}).items()
    }
    return {
        "public_contract": raw["public_contract"],
        "frozen_memory": memory,
        "historical_evidence": rows,
        "evidence_status": (
            "B retains only numeric reward-zero historical row structure; all original memory, "
            "historical loaded skills, arguments, narration and tool-result text are removed. "
            if structural else
            "A retains all original historical rows, original memory cards and historical loaded "
            "skills; allowed tool results use the fixed 8192-character head/tail projection. "
        ) + (
            "Both arms remove entire step result contents when original command arguments or "
            "result contents mention restricted evaluation paths or outputs. Legacy memory claims "
            "are unverified. New development feedback is separate from frozen history and follows "
            "the same allowed-result policy in both arms. Both arms and the sanitized-original "
            "baseline use the same inert seed body with no historical resources or provenance. "
            "Generic original routing metadata remains. This removes additional historical "
            "information jointly and does not isolate answer leakage alone."
        ),
    }
