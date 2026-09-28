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


def parse_plan(raw, experiences, limit):
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
    return rows


class SkillPatchRunner:
    def __init__(self, editor, reviewer, audit_dir, read_only=False,
                 reviewer_factory=None):
        self.editor, self.reviewer, self.audit_dir = editor, reviewer, Path(audit_dir)
        self.read_only = read_only
        self.reviewer_factory = reviewer_factory

    def run(self, base_skill, experiences, candidate_count=3, batch_patterns=(),
            l2_review_workers=1):
        experiences = tuple(experiences)
        if (
            candidate_count < 1
            or not experiences
            or len({e.experience_id for e in experiences}) != len(experiences)
            or any(not ED.accepts(base_skill, e) for e in experiences)
        ):
            raise ValueError(
                "Expected a nonempty source batch assigned to this Skill and positive K"
            )
        if l2_review_workers < 1:
            raise ValueError("l2_review_workers must be positive")
        identity = S.content_hash(
            {
                "protocol": PROTOCOL,
                "base": S.to_dict(base_skill),
                "cards": [S.to_dict(e) for e in experiences],
                "K": candidate_count,
                "meta": S.to_dict(self.editor.meta_skill),
                "patterns": list(batch_patterns),
                "skill_edit_mode": self.editor.skill_edit_mode,
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
            "skill_edit_mode": self.editor.skill_edit_mode,
            "hypotheses": [],
            "proposals": [],
            "reviews": [],
            "selected_candidate_id": None,
            "outcome": "hold",
            "reason": "",
            "empirically_validated": False,
        }
        for repair in range(2):
            try:
                hypotheses = parse_plan(plan["raw"], experiences, candidate_count)
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
                        "instruction": "Fix coverage and IDs only. Return every candidate once, citing supplied IDs.",
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
        selected, reason = choose(results)
        record.update(
            reviews=results,
            reason=reason,
            selected_candidate_id=aliases[selected].candidate_id if selected else None,
            outcome="review_approved" if selected else "hold",
        )
        return UpdateResult(record, aliases[selected] if selected else None)
