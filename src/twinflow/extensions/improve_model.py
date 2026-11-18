"""TF-006 B-29: the data behind the improve view (DATA-CONTRACT 4, I1 to I5).

`improve_view_model(proof, result, project)` turns the `improve.json` that `twinflow improve`
writes, plus the race it ran, into one JSON-ready dict: did the new rule win, the proof as a
checklist, and what changes on the floor.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from twinflow import __version__
from twinflow.extensions.project import Project
from twinflow.extensions.race import PolicyResult, RaceResult, Stat
from twinflow.extensions.report_model import (
    confidence_between,
    floor_between,
    label,
    stat_json,
)

SCHEMA_VERSION = 1
RACE_FILE = "race.html"
RULE_WORDS = {
    "interval_above_zero": "the likely range must be above zero",
    "mean_above_zero": "the average must be above zero",
}


def _money(value: float) -> str:
    amount = round(value)
    return f"{'-' if amount < 0 else ''}${abs(amount):,}"


def _score_stat(item: Stat) -> dict[str, Any]:
    return {
        "mean": round(item.mean, 2),
        "low": None if item.low is None else round(item.low, 2),
        "high": None if item.high is None else round(item.high, 2),
        "std": None if item.std is None else round(item.std, 2),
        "n": item.n,
    }


def _versus(stat: Mapping[str, Any]) -> Stat:
    return Stat(
        mean=stat["mean"],
        std=stat.get("std"),
        low=stat["low"],
        high=stat["high"],
        n=stat.get("n", 0),
    )


def _outcome(
    proof: Mapping[str, Any], candidate: PolicyResult, previous: PolicyResult, ranked: int
) -> dict[str, Any]:
    name, old = label(candidate.policy), label(previous.policy)
    diff = candidate.dollars.mean - previous.dollars.mean
    more = f"{_money(abs(diff))} {'more' if diff >= 0 else 'less'}"
    copied = proof.get("copied_to")
    if proof["promoted"]:
        return {
            "promoted": True,
            "level": "won",
            "headline": f"Yes. {name} is now your best rule.",
            "reason": f"It makes {more} per run than {old}, the old best.",
            "copied_to": copied,
        }
    if candidate.rank == 1:
        versus = proof["versus"]["score"]
        low, high = versus.get("low"), versus.get("high")
        shown = _money if proof.get("score_name") == "dollars" else (lambda v: f"{v:,.0f}")
        rule = RULE_WORDS.get(str(proof["rule"]), "the lead must be above zero")
        if low is None or high is None:
            reason = "There was only one run. Run more test runs."
        else:
            reason = (
                f"The likely range of the gain goes from {shown(low)} to {shown(high)}. "
                f"For a win, {rule}. Run more test runs to narrow it."
            )
        return {
            "promoted": False,
            "level": "ahead_not_proven",
            "headline": f"Not yet. {name} is ahead, but the lead is not proven.",
            "reason": reason,
            "copied_to": None,
        }
    return {
        "promoted": False,
        "level": "lost",
        "headline": f"No. {old} is still your best rule.",
        "reason": f"{name} ranks {candidate.rank} of {ranked}. It makes {more} per run than {old}.",
        "copied_to": None,
    }


def _checks(
    proof: Mapping[str, Any], candidate: PolicyResult, previous: PolicyResult, problems: int
) -> list[dict[str, Any]]:
    score = proof["versus"]["score"]
    rule = str(proof["rule"])
    ranked = len(proof["ranking"])
    show = _money if proof.get("score_name") == "dollars" else (lambda v: f"{v:,.2f}")
    shown = (
        "only one run"
        if score.get("low") is None
        else f"range {show(score['low'])} to {show(score['high'])}"
    )
    passes = score["mean"] > 0 if rule == "mean_above_zero" else (score.get("low") or 0) > 0
    warning = candidate.dollar_warning
    return [
        {
            "key": "ranked_first",
            "label": f"Ranked first of {ranked}",
            "pass": candidate.rank == 1,
            "detail": f"It ranks {candidate.rank}. The old best is {label(previous.policy)}.",
        },
        {
            "key": "proven",
            "label": f"Rule used: {RULE_WORDS.get(rule, rule)}",
            "pass": passes,
            "detail": f"Gain over the old best: {shown}.",
        },
        {
            "key": "same_orders",
            "label": "Same orders for both rules",
            "pass": True,
            "detail": (
                f"Both rules ran the same {proof['episodes']} demand draws (seed {proof['seed']})."
            ),
        },
        {
            "key": "no_problems",
            "label": "No problems found",
            "pass": problems == 0 and not warning,
            "detail": warning
            or ("No rule broke in the race." if problems == 0 else f"{problems} rule(s) broke."),
        },
    ]


def improve_view_model(
    proof: Mapping[str, Any], result: RaceResult, project: Project | None = None
) -> dict[str, Any]:
    """The improve view model for one `improve.json` and the race behind it."""
    project = project or result.project
    candidate = result.result(proof["candidate"]["name"])
    previous = result.result(proof["previous_best"]["name"])
    level = float(proof["interval"])
    confidence = confidence_between(candidate, previous, level)
    problems = [
        {
            "policy": p.name,
            "file": str(p.file) if p.file else None,
            "line": p.line,
            "message": p.message,
            "fix": p.fix,
        }
        for p in result.skipped
    ]
    outcome = _outcome(proof, candidate, previous, len(proof["ranking"]))
    project_name = str(proof.get("project", "")).rstrip("/").rsplit("/", 1)[-1] or "project"
    model: dict[str, Any] = {
        "view": "improve",
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "twinflow_version": __version__,
        "about": {
            "title": f"Improve: {label(candidate.policy)}",
            "project": project_name,
            "episodes": proof["episodes"],
            "seed": proof["seed"],
            "interval": level,
            "rule": proof["rule"],
            "written_at": proof["written_at"],
            "score": proof.get("score_name", "dollars"),
        },
        "outcome": outcome,
        "candidate": _row(candidate, proof["candidate"]),
        "previous_best": _row(previous, proof["previous_best"]),
        "versus": {
            "dollars": stat_json(_versus(proof["versus"]["dollars"])),
            "score": _score_stat(_versus(proof["versus"]["score"])),
            "wins": confidence["wins"],
            "runs": confidence["runs"],
            "gaps": confidence["gaps"],
        },
        "confidence": confidence,
        "floor": floor_between(candidate, previous, project, result.kept),
        "checks": _checks(proof, candidate, previous, len(problems)),
        "race_ref": RACE_FILE,
        "warnings": [],
        "problems": problems,
    }
    model["agent_summary"] = _summary(model)
    return model


def _row(row: PolicyResult, proof_row: Mapping[str, Any]) -> dict[str, Any]:
    out = {
        "policy": row.policy,
        "label": label(row.policy),
        "settings": dict(row.settings),
        "rank": row.rank,
        "dollars": stat_json(row.dollars),
        "score": _score_stat(row.score),
    }
    if "sha256" in proof_row:
        out.update(file=proof_row["file"], sha256=proof_row["sha256"])
    return out


def _summary(model: Mapping[str, Any]) -> list[str]:
    outcome, versus = model["outcome"], model["versus"]
    lines = [outcome["headline"], outcome["reason"], model["confidence"]["detail"]]
    lines.append(
        f"It won {versus['wins']} of {versus['runs']} runs against "
        f"{model['previous_best']['label']}."
    )
    lines += [
        f"{c['label']}: {'yes' if c['pass'] else 'no'}. {c['detail']}" for c in model["checks"]
    ]
    if outcome["copied_to"]:
        lines.append(f"The rule was copied to {outcome['copied_to']}.")
    return lines


__all__ = ["improve_view_model"]
