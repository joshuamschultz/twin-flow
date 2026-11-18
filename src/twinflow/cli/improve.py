"""`twinflow improve <project> --candidate FILE` (TF-006 T-253, REQ-212, D-144, D-152).

Races the candidate policy against the project's current policies on the same draws. The
candidate is copied into `policies/` only when it ranks first and its paired difference
against the previous best (on the score) passes the capsule's `promote_rule`. The proof goes
to `improve.json`. Exit codes: 0 ok (a win or not), 1 a problem the user can fix, 2 internal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import numpy as np

from twinflow.cli._common import UserError, guarded, open_project, race_settings, staged_project
from twinflow.extensions.loader import Extension
from twinflow.extensions.project import Project
from twinflow.extensions.race import PolicyResult, RaceResult, Stat, race, stat
from twinflow.extensions.report import write_race_report

ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)


def add_improve_subcommands(subparsers: argparse._SubParsersAction[ParserT]) -> None:
    """Attach `improve` to an application's subparser collection."""
    parser = subparsers.add_parser("improve")
    parser.add_argument("project")
    parser.add_argument("--candidate", help="the policy .py file to try")
    parser.add_argument("--replace", action="store_true", help="overwrite a file of the same name")
    parser.add_argument("--episodes", help="how many runs each policy gets")
    parser.add_argument("--seed", help="the random seed for the demand draws")
    parser.add_argument("--out", help="where to write improve.json and the race report")
    parser.set_defaults(handler=lambda args: guarded(_handle_improve, args))


def paired(candidate: Sequence[float], previous: Sequence[float], level: float) -> Stat:
    """Mean, spread and interval of the per-run difference candidate minus previous best."""
    return stat(list(np.subtract(candidate, previous)), level)


def passes(difference: Stat, rule: str) -> bool:
    """The promote rule (D-144): the whole interval above zero, or just the mean above zero."""
    if rule == "mean_above_zero":
        return difference.mean > 0
    return difference.low is not None and difference.low > 0


def _candidate_file(text: str | None) -> Path:
    if text is None:
        raise UserError(
            "improve needs --candidate. Fix: run "
            "`twinflow improve <project> --candidate my_policy.py`."
        )
    path = Path(text)
    if not path.exists():
        raise UserError(f"{path} was not found. Fix: give the path of an existing .py policy file.")
    if not path.is_file() or path.suffix != ".py":
        raise UserError(f"{path} is not a policy file. Fix: give the path of a .py file.")
    return path


def _candidate_extension(project: Project, file: Path) -> Extension:
    """The candidate as the loader sees it, or a user error with the loader's reason."""
    for ext in project.extensions.of("policy"):
        if ext.file.name == file.name:
            if ext.decision != "make_next":
                raise UserError(
                    f"{file} is a dispatch policy; improve races make_next policies. "
                    "Fix: use a policy that picks the product to make next."
                )
            return ext
    reasons = [str(p) for p in project.extensions.problems if p.file and p.file.name == file.name]
    raise UserError("\n".join(reasons) or f"{file} did not load as a policy. Fix: see the docs.")


def _baseline(project: Project, candidate: Extension) -> tuple[list[str], str]:
    """The current project policies (or the built-ins when the project has none)."""
    policies = [p for p in project.extensions.of("policy") if p.decision == "make_next"]
    own = [p.name for p in policies if p.source != "builtin" and p is not candidate]
    if own:
        return own, "the policies in this project"
    if project.engine_extension is not None:  # built-ins only fit make-to-stock twins
        return [], "the policies in this project"
    return [
        p.name for p in policies if p is not candidate
    ], "the built-in policies (this project has none)"


def _settings_text(settings: Any) -> str:
    return ", ".join(f"{k}={v}" for k, v in settings.items()) or "none"


def _row_json(row: PolicyResult, stage: Path, home: Path) -> dict[str, Any]:
    return {
        "name": row.policy,
        "file": row.file.replace(str(stage), str(home)),
        "settings": dict(row.settings),
        "score": row.score.to_json(),
        "dollars": row.dollars.to_json(),
    }


def _reason(candidate: PolicyResult, previous: PolicyResult, rule: str, versus: Stat) -> str | None:
    """Why the candidate is not promoted (None when it wins)."""
    if candidate.rank != 1:
        return (
            f"It ranks {candidate.rank}, not first. The best current policy is {previous.policy}."
        )
    if passes(versus, rule):
        return None
    if versus.low is None:
        return "One run cannot show a win. Fix: raise --episodes."
    return (
        f"It ranks first, but its lead over {previous.policy} is not proven: the "
        f"{versus.n}-run range runs from {versus.low:,.2f} to {versus.high:,.2f}, "
        "and a win needs the range above zero. It may be luck. Raise --episodes to test again."
    )


def _range(value: Stat, level: float, unit: str = "") -> str:
    shown = f"{'+' if value.mean >= 0 else '-'}{unit}{abs(value.mean):,.2f}"
    if value.low is None or value.high is None:
        return shown
    return f"{shown} ({level:.0%} range {value.low:+,.2f} to {value.high:+,.2f})"


def _handle_improve(args: argparse.Namespace) -> int:
    file = _candidate_file(args.candidate)
    home = open_project(args.project).folder
    destination = home / "policies" / file.name
    if destination.exists() and not args.replace:
        raise UserError(
            f"{destination} already exists. Fix: rename your candidate file, "
            "or add --replace to overwrite it if it wins."
        )
    with staged_project(args.project, extra={file.name: file}) as project:
        stage = project.folder
        extension = _candidate_extension(project, file)
        rivals, rivals_about = _baseline(project, extension)
        if not rivals:
            raise UserError(
                f"{home} has no policy to race the candidate against. "
                "Fix: add a policy file to policies/ first."
            )
        settings = race_settings(project, args)
        print(
            f"Racing {extension.name} against {len(rivals)} of {rivals_about} "
            f"on {settings.episodes} runs each (seed {settings.seed}).",
            flush=True,
        )
        result = race(
            project,
            [extension.name, *rivals],
            settings=settings,
            progress=lambda line: print(f"  {line}", flush=True),
        )
    return _finish(args, result, extension.name, file, destination, home, stage)


def _finish(
    args: argparse.Namespace,
    result: RaceResult,
    name: str,
    file: Path,
    destination: Path,
    home: Path,
    stage: Path,
) -> int:
    ranking = result.ranking()
    if name not in {r.policy for r in ranking}:
        reasons = [str(p) for p in result.skipped if p.name == name or p.file == file]
        raise UserError("\n".join(reasons or [str(p) for p in result.skipped]) or "no result")
    candidate = result.result(name)
    others = [r for r in ranking if r.policy != name]
    if not others:
        raise UserError(f"No current policy could be raced. {result.skipped[0]}")
    previous = others[0]
    level, rule = result.settings.interval, result.settings.promote_rule
    versus_score = paired(candidate.episode_score, previous.episode_score, level)
    versus_dollars = paired(candidate.episode_dollars, previous.episode_dollars, level)
    reason = _reason(candidate, previous, rule, versus_score)
    won = reason is None
    print()
    for label, row in (("Candidate", candidate), ("Previous best", previous)):
        print(
            f"{label} {row.policy}: score {row.score.mean:,.2f}, "
            f"dollars ${row.dollars.mean:,.0f} per run (settings: {_settings_text(row.settings)})."
        )
    print(f"Candidate minus previous best, same draws: score {_range(versus_score, level)}")
    print(
        f"Candidate minus previous best, same draws: dollars {_range(versus_dollars, level, '$')}"
    )
    if candidate.dollar_warning:
        print(f"Warning: {candidate.dollar_warning}")
    out = Path(args.out) if args.out else home / "runs" / f"improve-{_stamp()}"
    out.mkdir(parents=True, exist_ok=True)
    copied: Path | None = None
    if won:
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(file, destination)
        copied = destination
        print(f"Won. Copied {file} to {destination}.")
    else:
        print(f"Not copied: {reason}")
    proof = {
        "about": "twinflow improve: a candidate raced against the current best on the same draws.",
        "project": str(home),
        "promoted": won,
        "reason": reason,
        "rule": rule,
        "interval": level,
        "episodes": result.settings.episodes,
        "seed": result.settings.seed,
        "tune_episodes": result.settings.tune_episodes,
        "tune_seed": result.settings.tune_seed,
        "score_name": result.score_name,
        "observation": result.observation,
        "candidate": {
            **_row_json(candidate, stage, home),
            "file": str(file),
            "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
        },
        "previous_best": _row_json(previous, stage, home),
        "versus": {"score": versus_score.to_json(), "dollars": versus_dollars.to_json()},
        "ranking": [r.policy for r in ranking],
        "copied_to": None if copied is None else str(copied),
        "written_at": datetime.now(UTC).isoformat(),
    }
    (out / "improve.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    result.write(out)
    write_race_report(result, out / "race.html")
    print(f"Proof: {out / 'improve.json'}")
    return 0


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
