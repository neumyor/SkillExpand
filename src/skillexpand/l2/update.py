"""Batch-local hypothesis generation and durable, val-panel based selection."""

import difflib
import json
from dataclasses import dataclass
from pathlib import Path
from skillexpand.l2 import editor as ED
from skillexpand import schema as S
from skillexpand.runtime.json_output import extract_json
from skillexpand.l2 import sampled as SM
from skillexpand.persistence.io import save
from skillexpand.reliability.errors import InvalidInput, JournalConflict
from skillexpand.reliability.policies import repair_policy
from skillexpand.reliability.retry import call_with_repair

PLANNER_CORRECTION = ("Fix structure and IDs only. Use supplied card_id/evidence_id pairs; "
                      "drop unsupported hypotheses.")
PROMPTS = ("PLANNER_CORRECTION",)


def attempt_name(base, attempt):
    """Cache file of one model response; repair attempts are numbered from 0."""
    return f"{base}-{attempt}"


@dataclass
class UpdateResult:
    record: dict
    candidate: object = None


def parse_plan(raw, experiences, limit, structured=False, base_skill=None,
               require_claim=False):
    rows = extract_json(raw).get("hypotheses")
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("Hypotheses must be a list no larger than K")
    cards = {
        c["card_id"]: {r["id"] for r in c["evidence"]}
        for c in ED.card_payload(experiences)
    }
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Hypothesis must be an object")
        if any(
            not isinstance(row.get(k), str) or not row[k].strip()
            for k in ("mechanism", "change")
        ):
            raise ValueError("Hypothesis needs mechanism and concrete change")
        normalized = row["mechanism"].strip().casefold()
        if normalized in seen:
            raise ValueError("Duplicate hypothesis mechanism")
        seen.add(normalized)
        refs = row.get("evidence")
        if not isinstance(refs, list) or not refs:
            raise ValueError("Hypothesis needs card evidence")
        for ref in refs:
            if not isinstance(ref, dict):
                raise ValueError("Evidence reference must be an object")
            eid = ref.get("evidence_id")
            if (
                not isinstance(ref.get("card_id"), str)
                or ref["card_id"] not in cards
                or not isinstance(eid, str)
                or eid not in cards[ref["card_id"]]
            ):
                raise ValueError(
                    f"Evidence {ref!r} must reference a supplied card_id and evidence_id."
                )
        if require_claim:
            row['claim'] = SM.parse_claim(row.get('claim'))
        if structured:
            edit = row.get('edit')
            if not isinstance(edit, dict):
                raise ValueError('Structured hypothesis needs an edit')
            if set(edit) != {'op', 'section', 'target_id', 'text'}:
                raise ValueError('Structured edit has an invalid schema')
            # Let structured_skill.apply_edit perform the authoritative ID and
            # section validation, while checking here that the Planner emitted
            # the required fields before candidate materialization.
            if edit['op'] not in ('add', 'replace') or edit['section'] not in (
                    'procedure', 'conditions', 'completion_checks'):
                raise ValueError('Unsupported structured edit')
            if not isinstance(edit['text'], str) or not edit['text'].strip():
                raise ValueError('Structured edit text is empty')
    return rows


class SkillPatchRunner:
    def __init__(self, editor, audit_dir, read_only=False,
                 acceptance_mode="predicted", val_scorer=None,
                 predicted_scorer=None, single_candidate=False,
                 sampled_validator=None, planner_memory=""):
        self.editor, self.audit_dir = editor, Path(audit_dir)
        self.read_only = read_only
        if acceptance_mode == "empirical" and val_scorer is None and not read_only:
            raise InvalidInput("Empirical acceptance requires a val scorer")
        if acceptance_mode == "predicted" and predicted_scorer is None and not read_only:
            raise InvalidInput("Predicted val acceptance requires a predicted scorer")
        if acceptance_mode == "sampled" and sampled_validator is None and not read_only:
            raise InvalidInput("Sampled acceptance requires a sampled validator")
        self.acceptance_mode = acceptance_mode
        self.val_scorer = val_scorer
        self.predicted_scorer = predicted_scorer
        self.sampled_validator = sampled_validator
        self.planner_memory = str(planner_memory or "")
        self.single_candidate = bool(single_candidate)
        # The claim is part of the sampled protocol, so the requirement follows
        # from the mode rather than from a separate switch.
        self.claim_required = SM.claim_required(acceptance_mode)

    def run(self, base_skill, experiences, candidate_count=3, batch_patterns=(),
            acceptance_record=None):
        if self.single_candidate and candidate_count != 1:
            raise InvalidInput("single_candidate protocol requires candidate_count=1")
        experiences = tuple(experiences)
        if (
            candidate_count < 1
            or not experiences
            or len({e.experience_id for e in experiences}) != len(experiences)
            or any(not ED.accepts(base_skill, e) for e in experiences)
        ):
            raise InvalidInput(
                "Expected a nonempty train batch assigned to this Skill and positive K"
            )
        acceptance_protocol = getattr(
            self.predicted_scorer or self.val_scorer or self.sampled_validator,
            "protocol_hash", None)
        if acceptance_protocol is None and acceptance_record is not None:
            acceptance_protocol = acceptance_record.get("protocol_hash")
        identity = S.content_hash(
            {
                "base": S.to_dict(base_skill),
                "cards": [S.to_dict(e) for e in experiences],
                "K": candidate_count,
                "editing_strategy": ED.EDITING_STRATEGY,
                "patterns": list(batch_patterns),
                "skill_edit_mode": self.editor.skill_edit_mode,
                "acceptance_mode": self.acceptance_mode,
                "single_candidate": self.single_candidate,
                "acceptance_protocol": acceptance_protocol,
            }
        )
        directory = self.audit_dir / identity

        def cached_attempt(name, generate):
            """Return ``(value, fresh)``; every model response is durable before use."""
            path = directory / (name + ".json")
            if path.exists():
                return json.loads(path.read_text()), False
            if self.read_only:
                raise JournalConflict(f'Missing cached L2 response: {path.name}')
            value = generate()
            save(path, value)
            return value, True

        def cached(name, generate):
            return cached_attempt(name, generate)[0]

        def plan_request(attempt, previous):
            def generate():
                if previous is None:
                    return {"raw": self.editor.plan(base_skill, experiences, candidate_count,
                                                    batch_patterns=batch_patterns,
                                                    claim_required=self.claim_required,
                                                    planner_memory=self.planner_memory)}
                correction = {"error": str(previous.error), "previous_output": previous.raw,
                              "instruction": PLANNER_CORRECTION}
                return {"raw": self.editor.plan(base_skill, experiences, candidate_count,
                                                correction=correction,
                                                batch_patterns=batch_patterns,
                                                claim_required=self.claim_required,
                                                planner_memory=self.planner_memory)}
            value, fresh = cached_attempt(attempt_name("hypotheses", attempt), generate)
            return value["raw"], fresh
        record = {
            "proposal_id": identity,
            "base_skill_key": base_skill.key,
            "requested_candidates": candidate_count,
            "hypotheses": [],
            "proposals": [],
            "selected_candidate_id": None,
            "outcome": "hold",
            "reason": "",
            "acceptance": {
                "mode": self.acceptance_mode,
                "protocol_hash": acceptance_protocol,
                "executions": 0,
                "predicted_requests": 0,
                "task_ids": [],
                "candidates": [],
            },
        }
        # An exhausted budget raises RepairExhausted: the batch is not journaled,
        # and a resumed stage replays these attempts before resampling.
        hypotheses = call_with_repair(
            repair_policy("planner.hypotheses"), plan_request,
            lambda raw: parse_plan(raw, experiences, candidate_count,
                                   structured=self.editor.skill_edit_mode == 'structured',
                                   base_skill=base_skill,
                                   require_claim=self.claim_required)).value
        record["hypotheses"] = hypotheses
        candidates, previous, claims = [], [], {}
        seen_bodies = {" ".join(base_skill.body.split())}
        for index, hypothesis in enumerate(hypotheses):
            edit = cached(
                f"candidate-{index}",
                    lambda: S.to_dict(
                        self.editor.apply_planner_edit(base_skill, experiences, hypothesis)
                        if self.editor.skill_edit_mode == 'structured' else
                        self.editor.propose(
                            base_skill,
                            experiences,
                            hypothesis=hypothesis,
                            previous_changes=previous,
                        )
                    ),
            )
            candidate = (
                S.from_dict(S.CandidateSkill, edit["candidate"])
                if edit["candidate"]
                else None
            )
            row = {"hypothesis_index": index, "edit": edit, "status": edit["reason"]}
            if self.claim_required:
                row["claim"] = hypothesis["claim"]
            if candidate:
                if candidate.skill.description != base_skill.description:
                    raise JournalConflict("Candidate changed frozen description")
                canonical = " ".join(candidate.skill.body.split())
                if canonical in seen_bodies:
                    row["status"] = "duplicate_or_unchanged_body"
                else:
                    seen_bodies.add(canonical)
                    row["diff"] = "\n".join(
                        difflib.unified_diff(
                            base_skill.body.splitlines(),
                            candidate.skill.body.splitlines(),
                            fromfile="current",
                            tofile="candidate",
                            lineterm="",
                        )
                    )
                    candidates.append(candidate)
                    if self.claim_required:
                        claims[candidate.candidate_id] = SM.claim_of(hypothesis)
                    previous.append(
                        {"mechanism": hypothesis["mechanism"], "diff": row["diff"]}
                    )
            record["proposals"].append(row)
        if not candidates:
            record["reason"] = "no_distinct_supported_candidate"
            return UpdateResult(record)
        # Content-hash order hides author order; reviewer sees only anonymous bodies.
        ordered = sorted(
            candidates, key=lambda c: S.content_hash({"review_order": c.skill.body})
        )
        aliases = {f"C{i+1}": c for i, c in enumerate(ordered)}
        record["candidate_aliases"] = {
            key: c.candidate_id for key, c in aliases.items()
        }

        # The sampled protocol lives in its own module (l2.sampled); this is
        # its only dispatch point.
        if self.acceptance_mode == SM.MODE:
            fields, selected = SM.accept(self.sampled_validator, base_skill, ordered,
                                         aliases, claims, acceptance_record)
            record.update(fields)
            return UpdateResult(record, aliases[selected] if selected else None)

        # Predicted and empirical acceptance score every candidate against the
        # current head on the frozen val panel of the Skill's route group;
        # predicted forecasts the outcomes, empirical executes the episodes.
        predicted = self.acceptance_mode == "predicted"
        if acceptance_record is not None:
            # Offline audit reuses the recorded paired measurements; it must
            # never execute an environment or silently choose a new winner.
            acceptance = acceptance_record
            task_ids = tuple(acceptance.get("task_ids", ()))
            validations = list(acceptance.get("candidates", ()))
        else:
            scorer = self.predicted_scorer if predicted else self.val_scorer
            task_ids = tuple(scorer.routes.groups[base_skill.skill_id])
            panel_key = f"val:{scorer.routes.fingerprint}:{base_skill.skill_id}"
            validations = []
            alias_by_candidate = {v.candidate_id: k for k, v in aliases.items()}
            if task_ids:
                for candidate in ordered:
                    validation = scorer.validate(
                        base_skill.skill_id, base_skill, candidate.skill,
                        task_ids, panel_key
                    )
                    validations.append({
                        "id": alias_by_candidate[candidate.candidate_id],
                        "candidate_id": candidate.candidate_id,
                        "result": S.to_dict(validation),
                    })
            request_count = sum(
                int(len(task_ids) - v["result"]["metrics"]["base_from_cache"])
                + int(len(task_ids) - v["result"]["metrics"]["candidate_from_cache"])
                for v in validations
            )
            acceptance = {
                "mode": self.acceptance_mode,
                "panel": panel_key,
                "protocol_hash": scorer.protocol_hash,
                "task_ids": list(task_ids),
                "candidates": validations,
                "candidate_ids": [c.candidate_id for c in ordered],
                "predicted_requests": request_count if predicted else 0,
                "executions": 0 if predicted else request_count,
            }
        passed = [v for v in validations if v.get("result", {}).get("passed")]
        if passed:
            winner = max(
                passed,
                key=lambda v: (
                    v["result"].get("metrics", {}).get("success_delta", float("-inf")),
                    v["result"].get("metrics", {}).get("mean_candidate", float("-inf")),
                    v["id"],
                ),
            )
            selected = winner["id"]
            reason = (f"{self.acceptance_mode}_val_approved: "
                      "candidate beat the frozen val panel")
        else:
            selected = None
            reason = ("hold: frozen val panel is empty" if not task_ids else
                      "hold: no candidate beat the frozen val panel")
        record.update(
            selection_method=f"{self.acceptance_mode}_val_skill_success",
            acceptance=acceptance,
            reason=reason,
            selected_candidate_id=aliases[selected].candidate_id if selected else None,
            outcome="review_approved" if selected else "hold",
        )
        return UpdateResult(record, aliases[selected] if selected else None)
