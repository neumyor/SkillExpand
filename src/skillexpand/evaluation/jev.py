"""JEV based Skill judging on a frozen validation route.

JEV is a predictive judge: it estimates whether a fresh executor would solve a
task with a Skill.  It never replaces the environment scorer.  The latter can
still be run on the same validation panel to measure calibration and paired
selection error.
"""

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Sequence
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from skillexpand import schema as S
from skillexpand.evaluation.validation import ScoreCache
from skillexpand.reliability.errors import (
    InvalidInput, JournalConflict, ProviderRejected, ProviderUnavailable, SchemaViolation, StageIncomplete,
)
from skillexpand.reliability.policies import PROVIDER
from skillexpand.reliability.retry import retry_transient
from skillexpand.reliability.units import FailureCollector, map_units
from skillexpand.runtime import agent_factory as F


DEFAULT_URL = "http://127.0.0.1:18013"
PROTOCOL = "jev-success-probability-v1"


def load_panel_records(path):
    """Read frozen per-task executor outcomes for JEV calibration."""
    records = {}
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("failure"):
            continue
        if "skill_key" not in row or "task_id" not in row or "success" not in row:
            raise InvalidInput(f"panel row {line_number} lacks skill_key/task_id/success")
        key = (str(row["skill_key"]), int(row["task_id"]))
        if key in records:
            raise InvalidInput(f"duplicate actual panel row for {key}")
        records[key] = row
    return records


def score_actual_records(records, skill, task_ids):
    """Return actual labels and missing ids for one Skill revision."""
    labels, missing = {}, []
    for task_id in task_ids:
        row = records.get((skill.key, int(task_id)))
        if row is None:
            missing.append(int(task_id))
        else:
            labels[int(task_id)] = bool(row["success"])
    return labels, tuple(missing)


class JevClient:
    """Small HTTP client for MiniCPM5-2B-Jev's ``/v1/systemone`` endpoint."""

    def __init__(self, base_url=None, timeout=30, threshold=0.5):
        self.base_url = (base_url or os.environ.get("JEV_BASE_URL", DEFAULT_URL)).rstrip("/")
        self.timeout = float(timeout)
        self.threshold = float(threshold)
        if not 0.0 <= self.threshold <= 1.0:
            raise InvalidInput("JEV threshold must be between 0 and 1")

    def judge(self, task: str, skill: S.Skill) -> Dict:
        state = (
            f"Benchmark task: {task}\n"
            f"Skill description: {skill.description}\n"
            f"Skill rules:\n{skill.body}"
        )
        payload = {
            "state": state,
            "questions": {
                "success": {
                    "type": "noul",
                    "instructions": (
                        "Will a fresh agent complete this task successfully when it uses the Skill? "
                        "The agent gets one autonomous attempt and cannot observe a rejected answer."
                    ),
                }
            },
        }
        request = Request(
            self.base_url + "/v1/systemone",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json"},
        )
        def send():
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    return json.load(response)
            except HTTPError as exc:
                error = (ProviderUnavailable if exc.code == 429 or exc.code >= 500
                         else ProviderRejected)
                raise error(f"JEV HTTP {exc.code}: {exc.reason}") from exc
            except (URLError, socket.timeout, TimeoutError, ConnectionError, HTTPException) as exc:
                raise ProviderUnavailable(f"JEV unreachable: {exc}") from exc

        try:
            result = retry_transient(send, PROVIDER, sleep=time.sleep)
        except json.JSONDecodeError as exc:
            raise SchemaViolation(f"JEV response is not JSON: {exc}") from exc
        try:
            probabilities = result["answers"]["success"]["probabilities"]
            probability = float(probabilities["true"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SchemaViolation("JEV response lacks answers.success.probabilities.true") from exc
        if not 0.0 <= probability <= 1.0:
            raise SchemaViolation("JEV true probability is outside [0, 1]")
        return {
            "probability_true": probability,
            "predicted_success": probability >= self.threshold,
            "choice": result.get("answers", {}).get("success", {}).get("choice"),
            "confidence": result.get("answers", {}).get("success", {}).get("confidence"),
            "threshold": self.threshold,
            "raw": result,
        }


@dataclass
class JevPanelScore:
    skill_key: str
    body: str
    task_ids: tuple
    predictions: tuple
    from_cache: int = 0
    measured: int = 0

    @property
    def n(self):
        return len(self.predictions)

    @property
    def successes(self):
        return sum(1 for item in self.predictions if item["predicted_success"])

    @property
    def score(self):
        return (self.successes / self.n) if self.n else None

    @property
    def mean_probability(self):
        return (sum(item["probability_true"] for item in self.predictions) / self.n
                if self.n else None)

    def by_task(self):
        return {item["task_id"]: item for item in self.predictions}

    def as_arm(self, arm_id):
        outcomes = tuple(
            S.TaskOutcome(
                task_id=item["task_id"],
                family_id=self.skill_key.split("@", 1)[0].split(".", 1)[-1],
                role=S.ROLE_EVAL,
                success=bool(item["predicted_success"]),
                note=f"jev_probability={item['probability_true']:.6f}",
            )
            for item in self.predictions
        )
        return S.ArmEvaluation(
            arm_id=arm_id, role=S.ROLE_EVAL,
            mode=S.MODE_CONSOLIDATED_DIRECT, skill_key=self.skill_key,
            outcomes=outcomes, executor_fresh=True,
            experience_withheld=True, fewshot_strategy="none",
        )


class JevSkillScorer:
    """Score base and candidate Skills using JEV on the same val route group."""

    def __init__(self, cfg, routes, cache, workers=8, client=None):
        self.cfg = cfg
        self.routes = routes
        self.cache = cache
        self.workers = max(1, int(workers))
        self.client = client or JevClient()
        self.protocol_hash = S.content_hash({
            "protocol": PROTOCOL,
            "benchmark": cfg.benchmark.name,
            "routes": routes.fingerprint,
            "url": self.client.base_url,
            "timeout": self.client.timeout,
            "threshold": self.client.threshold,
        })

    def score(self, skill, task_ids, panel_key):
        task_ids = tuple(sorted(int(t) for t in task_ids))
        if not task_ids or not set(task_ids) <= set(self.routes.groups[skill.skill_id]):
            raise JournalConflict("JEV tasks must belong to the frozen Skill route group")
        keys = {
            # The raw probability is reusable across thresholds, but the persisted
            # ``predicted_success`` bit is thresholded at write time.  Include the
            # protocol identity so changing the endpoint or threshold cannot serve
            # a stale classification from an older calibration run.
            t: ScoreCache.make_key(
                self.cfg.benchmark.name, panel_key, t,
                f"jev:{self.protocol_hash}", skill.body,
            )
            for t in task_ids
        }
        records, pending = {}, []
        for task_id in task_ids:
            hit = self.cache.get(keys[task_id])
            if hit is None:
                pending.append(task_id)
            else:
                records[task_id] = hit

        def one(task_id):
            result = self.client.judge(F.task_text_of(self.cfg, task_id), skill)
            return {"task_id": task_id, "skill_key": skill.key, "cache_key": keys[task_id],
                    "panel_key": panel_key, "protocol_hash": self.protocol_hash, **result}

        def store(task_id, record):
            self.cache.put(keys[task_id], record)
            records[task_id] = record

        collector = FailureCollector("jev-validation", self.cache.path.parent / "evaluation_errors")
        map_units(pending, one, workers=self.workers, collector=collector, on_success=store,
                  name=lambda task_id: keys[task_id], thread_name_prefix="jev-score")
        collector.raise_if_incomplete("Incomplete JEV validation")
        if set(records) != set(task_ids):
            raise StageIncomplete("Incomplete JEV validation (missing results)")
        return JevPanelScore(
            skill.key, skill.body, task_ids,
            tuple(records[t] for t in task_ids),
            from_cache=len(task_ids) - len(pending), measured=len(pending),
        )

    def validate(self, skill_id: str, base_skill: S.Skill, candidate_skill: S.Skill,
                 task_ids: Sequence[int], panel_key: str) -> S.ValidationResult:
        if base_skill.skill_id != skill_id or candidate_skill.skill_id != skill_id:
            raise ValueError("Both JEV arms must evaluate the same Skill")
        base = self.score(base_skill, task_ids, panel_key)
        candidate = self.score(candidate_skill, task_ids, panel_key)
        base_arm, candidate_arm = base.as_arm(S.ARM_BASE), candidate.as_arm(S.ARM_CANDIDATE)
        paired = S.paired_delta(base_arm, candidate_arm)
        passed = (candidate.mean_probability is not None and base.mean_probability is not None
                  and candidate.mean_probability > base.mean_probability)
        reasons = () if passed else ("jev_no_predicted_gain",)
        metrics = {
            "mean_base": base.mean_probability,
            "mean_candidate": candidate.mean_probability,
            "success_delta": (candidate.mean_probability - base.mean_probability
                               if base.mean_probability is not None and candidate.mean_probability is not None
                               else None),
            "predicted_base_successes": float(base.successes),
            "predicted_candidate_successes": float(candidate.successes),
            "n_paired": float(paired.n_paired),
            "base_from_cache": float(base.from_cache),
            "candidate_from_cache": float(candidate.from_cache),
        }
        return S.ValidationResult(
            skill_id=skill_id, panel_key=panel_key, task_ids=tuple(task_ids),
            base_skill_key=base.skill_key, candidate_skill_key=candidate.skill_key,
            arms=(base_arm, candidate_arm), metrics=metrics, passed=passed,
            reasons=reasons, pairs=paired.pairs, returned_to_editor=False,
        )
