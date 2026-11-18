"""Pareto menu: non-dominated set and supported flag (T-158; COMP-021, REQ-116, D-078).

API these tests pin (`twinflow.scheduling.portfolio.pareto`):

- `non_dominated(points, axes) -> tuple[str, ...]`: `points` maps a candidate id to its
  objective values (`Mapping[ObjectiveKind, float]`, at least the axes). Values are compared
  signed (`objectives.signed`: maximize-sense kinds negated), all minimized. Of points with
  equal signed values on every axis only the lowest id is kept. Order: by signed value on the
  first axis, then the next axes, then id.
- `supported(points, axes) -> Mapping[str, bool]`: called with a non-dominated set; a point is
  supported when some lambda > 0 (sum 1) makes it optimal for the weighted sum of the signed,
  range-scaled axis values (ties count as optimal): 2 axes by the lower convex hull, 3 axes by
  an LP (`scipy.optimize.linprog`). Keys are exactly the ids passed.
- `menu(points, axes) -> tuple[MenuEntry, ...]`: one `model.MenuEntry(candidate_id, supported,
  values)` per non-dominated id, in `non_dominated` order; `values` holds the raw (unsigned)
  values of exactly the axes.

Fixture 14.2.1: the 24 single-machine sequences, values recomputed by brute force with the
kernel decoder (Pinedo Example 15.5.3; REQ-116).
"""

from __future__ import annotations

import importlib
import itertools
from collections.abc import Mapping
from types import ModuleType

import pytest
from kernel_cases import compile_request, load_request, only_choice

from twinflow.scheduling.kernel.decode import Decoded, decode
from twinflow.scheduling.kernel.index import ProblemIndex
from twinflow.scheduling.kernel.score import fast_values
from twinflow.scheduling.model import ObjectiveKind

WT = ObjectiveKind.WEIGHTED_TARDINESS
LMAX = ObjectiveKind.MAX_LATENESS
SETUP = ObjectiveKind.TOTAL_SETUP
ROB = ObjectiveKind.ROBUSTNESS
SOFT = ObjectiveKind.SOFT_PENALTY

Points = dict[str, dict[ObjectiveKind, float]]

PINEDO_MENU = {
    "2,4,3,1": (98.0, 36.0),
    "1,4,2,3": (101.0, 32.0),
    "2,4,1,3": (110.0, 30.0),
    "1,4,3,2": (129.0, 20.0),
    "1,3,4,2": (177.0, 19.0),
}
PINEDO_ORDER = ("2,4,3,1", "1,4,2,3", "2,4,1,3", "1,4,3,2", "1,3,4,2")


def _pareto() -> ModuleType:
    return importlib.import_module("twinflow.scheduling.portfolio.pareto")


def _all_sequences_14_2_1() -> Points:
    request = load_request("pinedo_14_2_1")
    index = ProblemIndex.build(compile_request(request).problem)
    choice = only_choice(request)
    points: Points = {}
    for order in itertools.permutations("1234"):
        decoded = decode(index, choice, {"M1": [f"{job}-1" for job in order]})
        assert isinstance(decoded, Decoded)
        points[",".join(order)] = dict(fast_values(index, decoded, None, 0.0))
    return points


@pytest.fixture(scope="module")
def sequences() -> Points:
    return _all_sequences_14_2_1()


def test_fixture_values_match_the_book(sequences: Points) -> None:
    assert len(sequences) == 24
    for seq, (wt, lmax) in PINEDO_MENU.items():
        assert (sequences[seq][WT], sequences[seq][LMAX]) == (wt, lmax)


def test_non_dominated_over_14_2_1_is_exactly_the_five_book_schedules(sequences: Points) -> None:
    assert _pareto().non_dominated(sequences, (WT, LMAX)) == PINEDO_ORDER


def test_only_2413_is_unsupported_on_14_2_1(sequences: Points) -> None:
    front = {seq: sequences[seq] for seq in PINEDO_ORDER}
    flags = _pareto().supported(front, (WT, LMAX))
    assert dict(flags) == {seq: seq != "2,4,1,3" for seq in PINEDO_ORDER}


def test_menu_over_14_2_1_lists_the_five_with_flags_and_raw_axis_values(
    sequences: Points,
) -> None:
    entries = _pareto().menu(sequences, (WT, LMAX))
    assert tuple(e.candidate_id for e in entries) == PINEDO_ORDER
    for entry in entries:
        assert entry.supported is (entry.candidate_id != "2,4,1,3")
        assert entry.values == dict(zip((WT, LMAX), PINEDO_MENU[entry.candidate_id], strict=True))
    assert type(entries[0]).__name__ == "MenuEntry"
    model = importlib.import_module("twinflow.scheduling.model")
    assert all(isinstance(e, model.MenuEntry) for e in entries)


def test_three_axis_lp_agrees_with_the_hull_when_the_third_axis_is_constant(
    sequences: Points,
) -> None:
    flat = {seq: {**values, SOFT: 7.0} for seq, values in sequences.items()}
    axes3 = (WT, LMAX, SOFT)
    front = _pareto().non_dominated(flat, axes3)
    assert front == PINEDO_ORDER
    three = _pareto().supported({seq: flat[seq] for seq in front}, axes3)
    two = _pareto().supported({seq: sequences[seq] for seq in front}, (WT, LMAX))
    assert dict(three) == dict(two)


def test_three_axis_point_above_the_simplex_face_is_unsupported() -> None:
    points: Points = {
        "e1": {WT: 1.0, LMAX: 0.0, SETUP: 0.0},
        "e2": {WT: 0.0, LMAX: 1.0, SETUP: 0.0},
        "e3": {WT: 0.0, LMAX: 0.0, SETUP: 1.0},
        "mid_high": {WT: 0.4, LMAX: 0.4, SETUP: 0.4},
    }
    axes = (WT, LMAX, SETUP)
    assert set(_pareto().non_dominated(points, axes)) == set(points)
    flags = _pareto().supported(points, axes)
    assert dict(flags) == {"e1": True, "e2": True, "e3": True, "mid_high": False}


def test_three_axis_point_below_the_simplex_face_is_supported() -> None:
    points: Points = {
        "e1": {WT: 1.0, LMAX: 0.0, SETUP: 0.0},
        "e2": {WT: 0.0, LMAX: 1.0, SETUP: 0.0},
        "e3": {WT: 0.0, LMAX: 0.0, SETUP: 1.0},
        "mid_low": {WT: 0.3, LMAX: 0.3, SETUP: 0.3},
    }
    flags = _pareto().supported(points, (WT, LMAX, SETUP))
    assert all(flags.values())
    assert set(flags) == set(points)


def test_equal_points_keep_the_lowest_id() -> None:
    points: Points = {
        "c9": {WT: 5.0, LMAX: 5.0},
        "a2": {WT: 5.0, LMAX: 5.0},
        "b1": {WT: 5.0, LMAX: 5.0},
        "z0": {WT: 1.0, LMAX: 9.0},
    }
    assert _pareto().non_dominated(points, (WT, LMAX)) == ("z0", "a2")


def test_equal_on_the_axes_but_different_elsewhere_still_counts_as_equal() -> None:
    points: Points = {
        "b": {WT: 5.0, LMAX: 5.0, SETUP: 1.0},
        "a": {WT: 5.0, LMAX: 5.0, SETUP: 99.0},
    }
    assert _pareto().non_dominated(points, (WT, LMAX)) == ("a",)


def test_weakly_dominated_point_is_dropped() -> None:
    points: Points = {"a": {WT: 5.0, LMAX: 5.0}, "b": {WT: 5.0, LMAX: 6.0}}
    assert _pareto().non_dominated(points, (WT, LMAX)) == ("a",)


def test_maximize_sense_axis_is_negated() -> None:
    # Robustness is maximized: higher is better. Signed points (wt, -rob):
    # a (10, -0.2), c (15, -0.4), b (20, -0.9): all non-dominated; the hull a-b at wt 15 is
    # -0.55 < -0.4, so c is unsupported. Unsigned, a would dominate b and c.
    points: Points = {
        "a": {WT: 10.0, ROB: 0.2},
        "b": {WT: 20.0, ROB: 0.9},
        "c": {WT: 15.0, ROB: 0.4},
    }
    pareto = _pareto()
    assert pareto.non_dominated(points, (WT, ROB)) == ("a", "c", "b")
    assert dict(pareto.supported(points, (WT, ROB))) == {"a": True, "b": True, "c": False}
    entries = pareto.menu(points, (WT, ROB))
    assert [e.values[ROB] for e in entries] == [0.2, 0.4, 0.9]


def test_lower_robustness_at_equal_tardiness_is_dominated() -> None:
    points: Points = {"low": {WT: 10.0, ROB: 0.5}, "high": {WT: 10.0, ROB: 0.9}}
    assert _pareto().non_dominated(points, (WT, ROB)) == ("high",)


def test_collinear_middle_point_is_supported() -> None:
    points: Points = {
        "a": {WT: 0.0, LMAX: 2.0},
        "b": {WT: 1.0, LMAX: 1.0},
        "c": {WT: 2.0, LMAX: 0.0},
    }
    assert all(_pareto().supported(points, (WT, LMAX)).values())


def test_hull_scaling_does_not_change_the_flags() -> None:
    # Same shape as 14.2.1's front with the second axis multiplied by 1000.
    points: Points = {seq: {WT: wt, LMAX: lmax * 1000.0} for seq, (wt, lmax) in PINEDO_MENU.items()}
    flags = _pareto().supported(points, (WT, LMAX))
    assert dict(flags) == {seq: seq != "2,4,1,3" for seq in PINEDO_MENU}


def test_single_point_is_its_own_supported_menu() -> None:
    points: Points = {"only": {WT: 3.0, LMAX: 4.0}}
    entries = _pareto().menu(points, (WT, LMAX))
    assert [(e.candidate_id, e.supported) for e in entries] == [("only", True)]


def test_empty_points_give_an_empty_menu() -> None:
    empty: Mapping[str, Mapping[ObjectiveKind, float]] = {}
    assert _pareto().non_dominated(empty, (WT, LMAX)) == ()
    assert _pareto().menu(empty, (WT, LMAX)) == ()


def test_menu_is_deterministic_under_input_order(sequences: Points) -> None:
    reversed_points = dict(reversed(list(sequences.items())))
    assert _pareto().menu(reversed_points, (WT, LMAX)) == _pareto().menu(sequences, (WT, LMAX))
