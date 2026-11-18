"""`twinflow states` and `twinflow markov`: read the moves a run captured (TF-006 T-251).

A run folder holds `moves.parquet` (one run), or replication folders that each do (a
`scenario run --out` folder). Both are accepted. Exit codes: 0 ok, 1 a problem the user can
fix (the message names the path and a fix), 2 an internal failure (REQ-210, REQ-213, D-145).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

import polars as pl

from twinflow.state.groups import GroupedView, default_views
from twinflow.state.markov import MarkovChain, estimate
from twinflow.state.recorder import MOVES_FILE, read_moves
from twinflow.state.records import LEDGER_PARTS, Move

ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)

_CAPTURE_HINT = (
    "Turn on state capture: experiment.state_capture: {every_hours: 4} for a factory model, "
    "then run again. A make-to-stock run writes moves by itself."
)
_FEW_MOVES_PER_EPISODE = 5  # fewer than this per run is too little to read
_SCENARIO_FILE = "scenario.json"
_NO_FACTS = (
    "Note: no product facts (families, demand rates) were found for this run, "
    "so view {view} cannot be built. Showing view full instead."
)


class _UserError(Exception):
    """A problem the user can fix. The message says where and how."""


@dataclass(frozen=True)
class Gathered:
    """Every move found under one folder."""

    folder: Path
    runs: int
    episodes: int
    moves: tuple[Move, ...]


@dataclass(frozen=True)
class Facts:
    """The static model facts a grouped view needs, and the views the model defines."""

    demand_rate: Mapping[str, float] | None
    families: Mapping[str, str] | None
    views: Mapping[str, GroupedView]


def add_states_subcommands(subparsers: argparse._SubParsersAction[ParserT]) -> None:
    """Attach `states` and `markov` to an application's subparser collection."""
    states = subparsers.add_parser("states")
    states.add_argument("run_dir")
    states.add_argument("--view", default="family")
    states.add_argument("--top", type=int, default=10)
    states.set_defaults(handler=lambda args: _guarded(_handle_states, args))

    markov = subparsers.add_parser("markov")
    markov.add_argument("run_dir")
    markov.add_argument("--view", default="family")
    markov.add_argument("--top", type=int, default=15)
    markov.add_argument("--min-prob", type=float, default=0.05)
    markov.add_argument("--out", default=None)
    markov.set_defaults(handler=lambda args: _guarded(_handle_markov, args))


def _guarded(handler: Any, args: argparse.Namespace) -> int:
    try:
        return int(handler(args))
    except _UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # an internal failure: say so and use exit code 2
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


# --- finding and reading the moves ---------------------------------------------------------


def gather(run_dir: str) -> Gathered:
    """Read every `moves.parquet` at or under `run_dir`."""
    folder = Path(run_dir)
    if not folder.is_dir():
        raise _UserError(
            f"{folder} does not exist or is not a folder. "
            "Fix: give the folder a run wrote, such as the one after --out."
        )
    files = sorted(folder.rglob(MOVES_FILE))
    moves: list[Move] = []
    episodes = 0
    for file in files:
        moves.extend(read_moves(file.parent))
        episodes += int(pl.read_parquet(file, columns=["episode"])["episode"].n_unique())
    if not moves:
        raise _UserError(f"No moves found in {folder}. {_CAPTURE_HINT}")
    return Gathered(folder, len(files), episodes, tuple(moves))


def _facts_near(folder: Path) -> Facts:
    """Model facts from the `scenario.json` a run wrote beside its replication folders."""
    views = default_views()
    for place in (folder, *folder.parents[:2]):
        found = place / _SCENARIO_FILE
        if found.is_file():
            return _facts_from(found, views)
    return Facts(None, None, views)


def _facts_from(file: Path, views: dict[str, GroupedView]) -> Facts:
    from twinflow.engines.step.model import parse_step_model

    try:
        model = json.loads(file.read_text(encoding="utf-8")).get("model", {})
    except (OSError, ValueError, AttributeError):
        return Facts(None, None, views)
    if model.get("domain") != "make_to_stock":
        return Facts(None, None, views)
    parsed, _ = parse_step_model(model)
    if parsed is None:
        return Facts(None, None, views)
    return Facts(
        {p.id: p.demand_rate for p in parsed.products},
        {p.id: p.family for p in parsed.products},
        {**views, **parsed.views},
    )


def _choose_view(name: str, facts: Facts) -> tuple[GroupedView, str | None]:
    """The view to use, and a note when it had to fall back to `full`."""
    if name not in facts.views:
        known = ", ".join(sorted(facts.views))
        raise _UserError(f"No view named {name!r}. Fix: use one of: {known}.")
    if name != "full" and facts.demand_rate is None:
        return facts.views["full"], _NO_FACTS.format(view=name)
    return facts.views[name], None


def _estimate(
    gathered: Gathered, view_name: str, top: int, min_prob: float
) -> tuple[MarkovChain, str | None]:
    facts = _facts_near(gathered.folder)
    view, note = _choose_view(view_name, facts)
    chain = estimate(
        gathered.moves,
        view,
        demand_rate=facts.demand_rate,
        families=facts.families,
        top_k=top,
        min_prob=min_prob,
    )
    return chain, note


# --- states ---------------------------------------------------------------------------------


def _handle_states(args: argparse.Namespace) -> int:
    if args.top < 1:
        raise _UserError(f"--top is {args.top}. Fix: give a whole number of 1 or more.")
    gathered = gather(args.run_dir)
    chain, note = _estimate(gathered, args.view, args.top, 0.05)
    moves = gathered.moves
    unique = {s.digest for m in moves for s in (m.before, m.after)}
    start = min(m.before.t for m in moves)
    end = max(m.after.t for m in moves)
    print(f"Moves found in {gathered.folder}")
    print(f"Runs found: {gathered.runs}")
    print(f"Episodes: {gathered.episodes}")
    print(f"Moves: {len(moves)}")
    print(f"Time span: {start:g} to {end:g}")
    print(f"Unique states: {len(unique)}")
    if note:
        print(note)
    if len(moves) < _FEW_MOVES_PER_EPISODE * gathered.episodes:
        print(
            f"Only {len(moves)} moves. Lower experiment.state_capture.every_hours to record more."
        )
    print(f"Top states (view {chain.view}), share of moves:")
    for label, share in list(chain.share.items())[: args.top]:
        print(f"  {share:6.1%}  {label}")
    print("Ledger by part (all moves):")
    for part in LEDGER_PARTS:
        print(f"  {part:<10} {sum(getattr(m.ledger, part) for m in moves):>14,.2f}")
    print(f"  {'total':<10} {sum(m.ledger.total for m in moves):>14,.2f}")
    return 0


# --- markov ---------------------------------------------------------------------------------


def _handle_markov(args: argparse.Namespace) -> int:
    if args.top < 1:
        raise _UserError(f"--top is {args.top}. Fix: give a whole number of 1 or more.")
    if not 0.0 <= args.min_prob <= 1.0:
        raise _UserError(f"--min-prob is {args.min_prob}. Fix: give a number from 0 to 1.")
    gathered = gather(args.run_dir)
    chain, note = _estimate(gathered, args.view, args.top, args.min_prob)
    target = Path(args.out) if args.out else gathered.folder
    target.mkdir(parents=True, exist_ok=True)
    (target / "markov.json").write_text(
        json.dumps(chain.to_json(), indent=2) + "\n", encoding="utf-8"
    )
    (target / "markov.mmd").write_text(chain.to_mermaid(), encoding="utf-8")
    _print_chain(chain, note, target)
    return 0


def _print_chain(chain: MarkovChain, note: str | None, target: Path) -> None:
    print(f"View: {chain.view}")
    print(f"Moves: {chain.moves}")
    print(f"Coverage: {chain.coverage:.1%} of moves start in the {len(chain.states)} states below")
    if note:
        print(note)
    print("States:")
    sticky = set(chain.sticky)
    for state in chain.states:
        mark = "  (sticky)" if state in sticky else ""
        print(f"  {chain.share[state]:6.1%}  {state}{mark}")
    print(f"Likely moves ({len(chain.edges)}):")
    ranked: Sequence[Any] = sorted(chain.edges, key=lambda e: -e.prob)
    for edge in ranked:
        print(f"  {edge.prob:5.2f}  {edge.source}  ->  {edge.target}")
    print(f"Wrote {target / 'markov.json'} and {target / 'markov.mmd'}")
