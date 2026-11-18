"""`twinflow states` and `twinflow markov`: read the moves a run captured (TF-006 T-251).

A run folder holds `moves.parquet` (one run), or replication folders that each do (a
`scenario run --out` folder). Both are accepted. Exit codes: 0 ok, 1 a problem the user can
fix (the message names the path and a fix), 2 an internal failure (REQ-210, REQ-213, D-145).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar

from twinflow.extensions.states_model import (
    Gathered,
    NoMovesError,
    StatesError,
    estimate_chain,
    gather,
    states_view_model,
)
from twinflow.report import decision
from twinflow.state.markov import MarkovChain
from twinflow.state.records import LEDGER_PARTS

ParserT = TypeVar("ParserT", bound=argparse.ArgumentParser)

STATES_PAGE = "states.html"


class _UserError(Exception):
    """A problem the user can fix. The message says where and how."""


def add_states_subcommands(subparsers: argparse._SubParsersAction[ParserT]) -> None:
    """Attach `states` and `markov` to an application's subparser collection."""
    states = subparsers.add_parser("states")
    states.add_argument("run_dir")
    states.add_argument("--view", default="family")
    states.add_argument("--top", type=int, default=10)
    states.add_argument("--min-prob", type=float, default=0.05)
    states.add_argument("--html", action="store_true", help="also write states.html")
    states.set_defaults(handler=lambda args: _guarded(_handle_states, args))

    markov = subparsers.add_parser("markov")
    markov.add_argument("run_dir")
    markov.add_argument("--view", default="family")
    markov.add_argument("--top", type=int, default=15)
    markov.add_argument("--min-prob", type=float, default=0.05)
    markov.add_argument("--out", default=None)
    markov.add_argument("--html", action="store_true", help="also write states.html")
    markov.set_defaults(handler=lambda args: _guarded(_handle_markov, args))


def _guarded(handler: Any, args: argparse.Namespace) -> int:
    try:
        return int(handler(args))
    except (_UserError, StatesError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # an internal failure: say so and use exit code 2
        print(f"internal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def _write_page(args: argparse.Namespace, folder: Path, top: int, min_prob: float) -> None:
    """`--html`: write the states page next to the run (or into `--out`)."""
    model = states_view_model(args.run_dir, view=args.view, top=top, min_prob=min_prob)
    page = decision.write(folder / STATES_PAGE, model)
    print(f"Wrote {page}")


def _gather_or_page(args: argparse.Namespace, folder: Path, top: int, min_prob: float) -> Gathered:
    """Read the moves. With `--html` and no moves, still write the page: it says how to fix it."""
    try:
        return gather(args.run_dir)
    except NoMovesError:
        if args.html and Path(args.run_dir).is_dir():
            _write_page(args, folder, top, min_prob)
        raise


# --- states ---------------------------------------------------------------------------------


def _handle_states(args: argparse.Namespace) -> int:
    if args.top < 1:
        raise _UserError(f"--top is {args.top}. Fix: give a whole number of 1 or more.")
    gathered = _gather_or_page(args, Path(args.run_dir), args.top, args.min_prob)
    chain, note = estimate_chain(gathered, args.view, args.top, 0.05)
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
    print(f"Top states (view {chain.view}), share of moves:")
    for label, share in list(chain.share.items())[: args.top]:
        print(f"  {share:6.1%}  {label}")
    print("Ledger by part (all moves):")
    for part in LEDGER_PARTS:
        print(f"  {part:<10} {sum(getattr(m.ledger, part) for m in moves):>14,.2f}")
    print(f"  {'total':<10} {sum(m.ledger.total for m in moves):>14,.2f}")
    if args.html:
        _write_page(args, gathered.folder, args.top, args.min_prob)
    return 0


# --- markov ---------------------------------------------------------------------------------


def _handle_markov(args: argparse.Namespace) -> int:
    if args.top < 1:
        raise _UserError(f"--top is {args.top}. Fix: give a whole number of 1 or more.")
    if not 0.0 <= args.min_prob <= 1.0:
        raise _UserError(f"--min-prob is {args.min_prob}. Fix: give a number from 0 to 1.")
    target = Path(args.out) if args.out else Path(args.run_dir)
    gathered = _gather_or_page(args, target, args.top, args.min_prob)
    chain, note = estimate_chain(gathered, args.view, args.top, args.min_prob)
    target.mkdir(parents=True, exist_ok=True)
    (target / "markov.json").write_text(
        json.dumps(chain.to_json(), indent=2) + "\n", encoding="utf-8"
    )
    (target / "markov.mmd").write_text(chain.to_mermaid(), encoding="utf-8")
    _print_chain(chain, note, target)
    if args.html:
        _write_page(args, target, args.top, args.min_prob)
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
