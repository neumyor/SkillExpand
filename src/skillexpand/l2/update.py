"""Batch-local hypothesis generation, independent review and durable selection."""

import difflib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from skillexpand.l2 import editor as ED
from skillexpand import schema as S
from skillexpand.l2.card_review import PROTOCOL
from skillexpand.l2.card_review import card_payload
from skillexpand.l2.card_review import parse_card_review
from skillexpand.l2.card_review import aggregate
from skillexpand.l2.card_review import choose
from skillexpand.l1.family_discovery import _extract_json
from skillexpand.l1.runner import save


@dataclass
class UpdateResult:
    record: dict
    candidate: object = None


def parse_plan(raw, experiences, limit, structured=False, base_skill=None):
    rows = _extract_json(raw).get("hypotheses")
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("Hypotheses must be a list no larger than K")
    cards = {
        c["card_id"]: {r["id"] for r in c["evidence"]}
        for c in card_payload(experiences)
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
    def __init__(self, editor, reviewer, audit_dir, read_only=False,
                 reviewer_factory=None, acceptance_mode="predicted",
                 val_scorer=None, jev_scorer=None,
                 predicted_review_scope="val", predicted_scorer=None,
                 single_candidate=False):
        self.editor, self.reviewer, self.audit_dir = editor, reviewer, Path(audit_dir)
        self.read_only = read_only
        self.reviewer_factory = reviewer_factory
        if acceptance_mode not in ("predicted", "empirical", "jev"):
            raise ValueError("Unknown acceptance mode")
        if acceptance_mode == "empirical" and val_scorer is None and not read_only:
            raise ValueError("Empirical acceptance requires a val scorer")
        if acceptance_mode == "jev" and jev_scorer is None and not read_only:
            raise ValueError("JEV acceptance requires a JEV scorer")
        if predicted_review_scope not in ("val", "train_cards"):
            raise ValueError("Unknown predicted review scope")
        if (acceptance_mode == "predicted" and predicted_review_scope == "val"
                and predicted_scorer is None and not read_only):
            raise ValueError("Predicted val acceptance requires a predicted scorer")
        self.acceptance_mode = acceptance_mode
        self.predicted_review_scope = predicted_review_scope
        self.val_scorer = val_scorer
        self.jev_scorer = jev_scorer
        self.predicted_scorer = predicted_scorer
        self.single_candidate = bool(single_candidate)

    def run(self, base_skill, experiences, candidate_count=3, batch_patterns=(),
            l2_review_workers=1, acceptance_record=None):
        if self.single_candidate and candidate_count != 1:
            raise ValueError("single_candidate protocol requires candidate_count=1")
        experiences = tuple(experiences)
        if (
            candidate_count < 1
            or not experiences
            or len({e.experience_id for e in experiences}) != len(experiences)
            or any(not ED.accepts(base_skill, e) for e in experiences)
        ):
            raise ValueError(
                "Expected a nonempty train batch assigned to this Skill and positive K"
            )
        if l2_review_workers < 1:
            raise ValueError("l2_review_workers must be positive")
        acceptance_protocol = getattr(
            self.predicted_scorer or self.jev_scorer or self.val_scorer,
            "protocol_hash", None)
        if acceptance_protocol is None and acceptance_record is not None:
            acceptance_protocol = acceptance_record.get("protocol_hash")
        identity = S.content_hash(
            {
                "protocol": PROTOCOL,
                "base": S.to_dict(base_skill),
                "cards": [S.to_dict(e) for e in experiences],
                "K": candidate_count,
                "meta": S.to_dict(self.editor.meta_skill),
                "patterns": list(batch_patterns),
                "skill_edit_mode": self.editor.skill_edit_mode,
                "acceptance_mode": self.acceptance_mode,
                "predicted_review_scope": self.predicted_review_scope,
                "single_candidate": self.single_candidate,
                "acceptance_protocol": acceptance_protocol,
            }
        )
        directory = self.audit_dir / identity

        def cached(name, generate):
            path = directory / (name + ".json")
            if path.exists():
                return json.loads(path.read_text())
            if self.read_only:
                raise ValueError(f'Missing cached L2 response: {path.name}')
            value = generate()
            save(path, value)
            return value

        plan = cached(
            "hypotheses",
            lambda: {"raw": self.editor.plan(base_skill, experiences, candidate_count,
                                               batch_patterns=batch_patterns)},
        )
        record = {
            "proposal_id": identity,
            "base_skill_key": base_skill.key,
            "selection_method": "batch_card_review",
            "requested_candidates": candidate_count,
            "single_candidate": self.single_candidate,
            "skill_edit_mode": self.editor.skill_edit_mode,
            "acceptance_mode": self.acceptance_mode,
            "predicted_review_scope": self.predicted_review_scope,
            "hypotheses": [],
            "proposals": [],
            "reviews": [],
            "selected_candidate_id": None,
            "outcome": "hold",
            "reason": "",
            "empirically_validated": False,
            "jev_validated": False,
            "acceptance": {
                "mode": self.acceptance_mode,
                "scope": self.predicted_review_scope if self.acceptance_mode == "predicted" else None,
                "protocol_hash": acceptance_protocol,
                "executions": 0,
                "jev_requests": 0,
                "predicted_requests": 0,
                "task_ids": [],
                "candidates": [],
            },
        }
        for repair in range(2):
            try:
                hypotheses = parse_plan(
                    plan["raw"], experiences, candidate_count,
                    structured=self.editor.skill_edit_mode == 'structured',
                    base_skill=base_skill)
                break
            except (ValueError, KeyError, TypeError) as exc:
                if repair:
                    record["reason"] = "invalid_hypotheses: " + str(exc)
                    return UpdateResult(record)
                correction = {
                    "error": str(exc),
                    "previous_output": plan["raw"],
                    "instruction": "Fix structure and IDs only. Use supplied card_id/evidence_id pairs; drop unsupported hypotheses.",
                }
                plan = cached(
                    "hypotheses-repair",
                    lambda: {
                        "raw": self.editor.plan(
                            base_skill,
                            experiences,
                            candidate_count,
                            correction=correction,
                            batch_patterns=batch_patterns,
                        )
                    },
                )
        record["hypotheses"] = hypotheses
        candidates, previous = [], []
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
            if candidate:
                if candidate.skill.description != base_skill.description:
                    raise ValueError("Candidate changed frozen description")
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
        payload = [{"id": key, "body": c.skill.body} for key, c in aliases.items()]
        record["candidate_aliases"] = {
            key: c.candidate_id for key, c in aliases.items()
        }

        # The default predicted protocol is an independent validation-panel
        # forecast.  It deliberately does not expose L1 cards or trajectories
        # to the reviewer and therefore skips card-level review entirely.
        if self.acceptance_mode == "predicted" and self.predicted_review_scope == "val":
            if acceptance_record is not None:
                acceptance = acceptance_record
                task_ids = tuple(acceptance.get("task_ids", ()))
                validations = list(acceptance.get("candidates", ()))
            else:
                scorer = self.predicted_scorer
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
                    "mode": "predicted",
                    "scope": "val",
                    "panel": panel_key,
                    "protocol_hash": scorer.protocol_hash,
                    "task_ids": list(task_ids),
                    "candidates": validations,
                    "candidate_ids": [c.candidate_id for c in ordered],
                    "predicted_requests": request_count,
                    "executions": 0,
                    "jev_requests": 0,
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
                reason = "predicted_val_approved: candidate beat the frozen val panel"
            else:
                selected = None
                reason = ("hold: frozen val panel is empty" if not task_ids else
                          "hold: no candidate beat the frozen val panel")
            record.update(
                selection_method="predicted_val_skill_success",
                reviews=[],
                review_errors=[],
                reviewed_card_count=0,
                acceptance=acceptance,
                reason=reason,
                selected_candidate_id=aliases[selected].candidate_id if selected else None,
                outcome="review_approved" if selected else "hold",
                empirically_validated=False,
                jev_validated=False,
            )
            return UpdateResult(record, aliases[selected] if selected else None)

        cards = card_payload(experiences)
        record["review_protocol"] = PROTOCOL
        def review_one(card):
            unit_id = S.content_hash(card)
            reviewer = (self.reviewer_factory(card) if self.reviewer_factory is not None
                        else self.reviewer)
            raw = cached(
                "review-" + unit_id,
                lambda: {"raw": reviewer.review(base_skill, payload, card)},
            )
            parsed = None
            error = None
            for repair in range(2):
                try:
                    parsed = parse_card_review(
                        raw["raw"], base_skill, payload, card
                    )
                    break
                except (ValueError, KeyError, TypeError) as exc:
                    if repair:
                        error = {"card_id": card["card_id"], "error": str(exc)}
                        break
                    correction = {
                        "error": str(exc),
                        "previous_output": raw["raw"],
                        "instruction": (
                            "Fix coverage, IDs, and outcome fields only. Return every candidate "
                            "once with old_outcome and new_outcome in {success,failure,unknown}; "
                            "old_outcome must copy card.current_observed_outcome when known; "
                            "otherwise infer CURRENT separately or use unknown. Do not "
                            "return label/effect; the program derives the relative effect."
                        ),
                    }
                    raw = cached(
                        "review-" + unit_id + "-repair",
                        lambda: {
                            "raw": reviewer.review(
                                base_skill, payload, card, correction=correction
                            )
                        },
                    )
            return card["card_id"], parsed if error is None else None, error

        pool_workers = min(int(l2_review_workers), len(cards))
        if pool_workers == 1:
            reviewed = [review_one(card) for card in cards]
        else:
            with ThreadPoolExecutor(max_workers=pool_workers,
                                    thread_name_prefix="l2-card-review") as pool:
                # Collect in input order after all calls have been submitted. This
                # keeps journals and aggregate counts reproducible while the LLM
                # requests themselves overlap inside the bounded pool.
                futures = [pool.submit(review_one, card) for card in cards]
                reviewed = [future.result() for future in futures]
        units = {card_id: parsed for card_id, parsed, error in reviewed if error is None}
        errors = [error for _, _, error in reviewed if error is not None]
        record["review_errors"] = errors
        record["reviewed_card_count"] = len(units)
        if errors:
            record["reason"] = "invalid_review: incomplete card judgments"
            record["partial_review_units"] = units
            return UpdateResult(record)
        results = aggregate(payload, cards, units)
        acceptance = {
            "mode": self.acceptance_mode,
            "scope": self.predicted_review_scope if self.acceptance_mode == "predicted" else None,
            "protocol_hash": acceptance_protocol,
            "executions": 0,
            "predicted_requests": 0,
            "task_ids": [],
            "candidates": [],
        }
        if self.acceptance_mode == "predicted":
            selected, reason = choose(results)
            acceptance["predicted"] = results
        else:
            # The route group is fixed from the initial Skill descriptions. Every
            # candidate is paired with the current head on exactly that panel.
            if acceptance_record is not None:
                task_ids = tuple(acceptance_record.get("task_ids", ()))
                panel_key = acceptance_record.get("panel", "")
            else:
                scorer = self.jev_scorer if self.acceptance_mode == "jev" else self.val_scorer
                task_ids = tuple(scorer.routes.groups[base_skill.skill_id])
                split_name = "val"
                panel_key = f"{split_name}:{scorer.routes.fingerprint}:{base_skill.skill_id}"
            acceptance.update({"task_ids": list(task_ids), "panel": panel_key})
            validations = []
            if acceptance_record is not None:
                # Offline audit reuses the recorded paired measurements; it must
                # never execute an environment or silently choose a new winner.
                acceptance = acceptance_record
                validations = list(acceptance.get("candidates", []))
                task_ids = tuple(acceptance.get("task_ids", ()))
            elif task_ids:
                alias_by_candidate = {v.candidate_id: k for k, v in aliases.items()}
                for candidate in ordered:
                    scorer = self.jev_scorer if self.acceptance_mode == "jev" else self.val_scorer
                    validation = scorer.validate(
                        base_skill.skill_id, base_skill, candidate.skill, task_ids, panel_key
                    )
                    validations.append({
                        "id": alias_by_candidate[candidate.candidate_id],
                        "candidate_id": candidate.candidate_id,
                        "result": S.to_dict(validation),
                    })
            acceptance["candidates"] = validations
            if acceptance_record is None:
                request_count = sum(
                    int(len(task_ids) - v["result"]["metrics"]["base_from_cache"])
                    + int(len(task_ids) - v["result"]["metrics"]["candidate_from_cache"])
                    for v in validations
                )
                if self.acceptance_mode == "jev":
                    # JEV is a predictive judge and never runs the benchmark
                    # environment.  Keep the historical ``executions`` field
                    # reserved for empirical val episodes.
                    acceptance["jev_requests"] = request_count
                else:
                    acceptance["executions"] = request_count
            passed = [v for v in validations if v["result"].get("passed")]
            if passed:
                winner = max(
                    passed,
                    key=lambda v: (
                        v["result"].get("metrics", {}).get("success_delta", float("-inf")),
                        v["result"].get("metrics", {}).get("mean_candidate", float("-inf")),
                        v["id"],
                    ),
                )
                reason_prefix = "jev_approved" if self.acceptance_mode == "jev" else "empirical_approved"
                panel_name = "frozen val panel" if self.acceptance_mode == "jev" else "frozen val panel"
                selected, reason = winner["id"], f"{reason_prefix}: candidate beat the {panel_name}"
            elif not task_ids:
                selected, reason = None, "hold: val panel is empty"
            else:
                selected, reason = None, "hold: no candidate beat the frozen val panel"
        record.update(
            reviews=results,
            acceptance=acceptance,
            reason=reason,
            selected_candidate_id=aliases[selected].candidate_id if selected else None,
            outcome="review_approved" if selected else "hold",
            empirically_validated=(self.acceptance_mode == "empirical" and bool(acceptance["candidates"])),
            jev_validated=(self.acceptance_mode == "jev" and bool(acceptance["candidates"])),
        )
        return UpdateResult(record, aliases[selected] if selected else None)
