"""COMP-151: the make-to-stock domain adapter (REQ-195, D-134).

Registered with the domain registry as `make_to_stock`, so a `.twin.yaml` capsule with
`model.domain: make_to_stock` validates, describes and evaluates like the office and
supply-chain capsules.

`evaluate` runs one built-in rule (`engines/step/rules.py`, plain functions: this adapter
loads no user code, D-139) for a number of episodes on common random numbers and writes the
moves. Extra keys in `limits` carry the experiment's choices: `replications` (episodes,
default 1) and `policy` (a rule name, default `lowest_cover`).

`validate` returns errors only. Notes about missing money (D-133) are info issues: they are
in `describe(...)["notes"]` and in the evaluate result, because the capsule layer treats any
validation issue as a failure and a twin must run before finance gives numbers.

The domain registry package imports this module, so `twinflow.domain` is imported inside
the methods to avoid a circular import.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from twinflow.engines.contract import run_episode
from twinflow.engines.step.engine import StepEngine, demand_draws
from twinflow.engines.step.model import StepModel, parse_step_model
from twinflow.engines.step.rules import RULE_NAMES, make_decider
from twinflow.state.issues import Issue
from twinflow.state.recorder import StateRecorder
from twinflow.state.records import Move, Snapshot, snapshot_from_dict

if TYPE_CHECKING:
    from twinflow.domain.registry import DomainIssue

DEFAULT_POLICY = "lowest_cover"
MAX_REPLICATIONS = 10_000


def _domain_issue(issue: Issue) -> DomainIssue:
    from twinflow.domain.registry import DomainIssue

    return DomainIssue(f"$.{issue.path}", issue.message, issue.severity, issue.suggestion or None)


def _plain(issue: Issue) -> dict[str, str]:
    return {
        "path": f"$.{issue.path}",
        "message": issue.message,
        "severity": issue.severity,
        "suggestion": issue.suggestion,
    }


def _start_state(
    model: StepModel, snapshot: Mapping[str, Any]
) -> tuple[Snapshot | None, list[Issue]]:
    """The optional saved start state in `snapshot.state` (REQ-191), checked against the model."""
    raw = snapshot.get("state")
    if raw is None:
        return None, []
    path = "snapshot.state"
    try:
        state = snapshot_from_dict(raw)
        StepEngine(model).start(0, state)
    except (TypeError, ValueError, KeyError) as exc:
        return None, [Issue(path, str(exc), "error", "Save the state from a run of this model.")]
    return state, []


class MakeToStockAdapter:
    domain = "make_to_stock"
    capabilities = frozenset({"make_to_stock.step"})
    experiment_keys = frozenset({"replications", "policy"})

    def validate(self, model: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[DomainIssue]:
        parsed, issues = parse_step_model(model)
        if parsed is not None:
            issues = issues + _start_state(parsed, snapshot)[1]
        return [_domain_issue(i) for i in issues if i.severity == "error"]

    def describe(self, model: Mapping[str, Any], snapshot: Mapping[str, Any]) -> dict[str, object]:
        parsed, issues = parse_step_model(model)
        if parsed is None:
            raise ValueError("; ".join(i.message for i in issues if i.severity == "error"))
        return {
            "domain": self.domain,
            "revision": parsed.revision,
            "line": parsed.line_id,
            "horizon": parsed.horizon,
            "start_cover_days": parsed.start_cover_days,
            "families": list(dict.fromkeys(parsed.families)),
            "nodes": [{"id": p.id, "family": p.family, "role": p.family} for p in parsed.products],
            "edges": [],
            "views": sorted(parsed.views),
            "notes": [_plain(i) for i in issues if i.severity == "info"],
        }

    def evaluate(
        self,
        model: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        *,
        seed: int = 0,
        artifact_dir: str | None = None,
        limits: Mapping[str, Any] | None = None,
    ) -> dict[str, object]:
        parsed, issues = parse_step_model(model)
        start, start_issues = (None, []) if parsed is None else _start_state(parsed, snapshot)
        problems = [i for i in issues + start_issues if i.severity == "error"]
        options = limits or {}
        policy = str(options.get("policy", DEFAULT_POLICY))
        reps = options.get("replications", 1)
        if policy not in RULE_NAMES:
            valid = ", ".join(RULE_NAMES)
            problems.append(
                Issue(
                    "experiment.policy",
                    f"No rule named {policy!r}.",
                    "error",
                    f"Use one of: {valid}.",
                )
            )
        if isinstance(reps, bool) or not isinstance(reps, int) or not 1 <= reps <= MAX_REPLICATIONS:
            problems.append(
                Issue(
                    "experiment.replications",
                    f"replications must be 1 to {MAX_REPLICATIONS}.",
                    "error",
                    "Write a whole number.",
                )
            )
        if parsed is None or problems:
            return {"status": "invalid", "issues": [_plain(i) for i in problems]}
        return _run(parsed, start, policy, int(reps), seed, artifact_dir, issues)


def _run(
    model: StepModel,
    start: Snapshot | None,
    policy: str,
    reps: int,
    seed: int,
    artifact_dir: str | None,
    issues: Sequence[Issue],
) -> dict[str, object]:
    engine = StepEngine(model, demand_draws(model, reps, seed))
    settings = {"seed": seed} if policy == "random" else {}
    decide = make_decider(policy, model, **settings)
    episodes = [run_episode(engine, decide, episode, start) for episode in range(reps)]
    if artifact_dir is not None:
        recorder = StateRecorder(run_id=Path(artifact_dir).name)
        for episode, moves in enumerate(episodes):
            for move in moves:
                recorder.add(move, episode, policy)
        recorder.flush(artifact_dir)
    profits = [sum(m.ledger.total for m in moves) for moves in episodes]
    return {
        "status": "completed",
        "domain": MakeToStockAdapter.domain,
        "policy": policy,
        "replications": reps,
        "days": len(episodes[0]),
        "horizon": model.horizon,
        "metrics": _metrics(model, episodes),
        "episode_profits": profits,
        "profit_std": statistics.stdev(profits) if reps > 1 else 0.0,
        "notes": [_plain(i) for i in issues if i.severity == "info"],
    }


def _is_switch(model: StepModel, move: Move) -> tuple[bool, bool]:
    """(changed product, changed family) for one move."""
    current = move.before.setup[model.line_id]
    if move.action in (None, "idle") or move.action == current or current is None:
        return False, False
    families = dict(zip(model.product_ids, model.families, strict=True))
    return True, families[move.action] != families[current]


def _metrics(model: StepModel, episodes: Sequence[Sequence[Move]]) -> dict[str, float]:
    """Per-episode means of money, fill rate and changeovers (the RL repo's `summarize`)."""
    count = len(episodes)
    moves = [m for episode in episodes for m in episode]
    demand = sum(v for m in moves for k, v in m.events.items() if k.startswith("demand."))
    sold = sum(v for m in moves for k, v in m.events.items() if k.startswith("sold."))
    switches = [_is_switch(model, m) for m in moves]
    parts = {p: sum(getattr(m.ledger, p) for m in moves) / count for p in _PARTS}
    return {
        "mean_profit": sum(m.ledger.total for m in moves) / count,
        "fill_rate": sold / demand if demand else 1.0,
        "changeovers_per_episode": sum(s[0] for s in switches) / count,
        "cross_family_per_episode": sum(s[1] for s in switches) / count,
        **parts,
    }


_PARTS = ("revenue", "production", "setup", "holding", "lost_sales")
