"""Tier 0 — probabilistic quality gate (RoutingPolicy, the pure decision function).

A quality gate sends a whole firing's output down one pass/fail branch by chance,
partitioning [0, 1) by cumulative branch probability. Distinct from a scrap rate,
which splits every firing's qty (D-043).
"""

from __future__ import annotations

from twinflow.primitives.location import RoutingPolicy


def test_choose_partitions_the_unit_interval_by_cumulative_probability() -> None:
    pass_sink: list = []
    fail_sink: list = []
    policy = RoutingPolicy([(0.3, pass_sink), (0.7, fail_sink)])

    assert policy.choose(0.0) is pass_sink
    assert policy.choose(0.29) is pass_sink
    assert policy.choose(0.3) is fail_sink
    assert policy.choose(0.99) is fail_sink


def test_choose_edge_draw_of_one_falls_to_the_last_branch() -> None:
    a: list = []
    b: list = []
    policy = RoutingPolicy([(0.5, a), (0.5, b)])
    assert policy.choose(1.0) is b


def test_single_branch_of_probability_one_always_chosen() -> None:
    only: list = []
    policy = RoutingPolicy([(1.0, only)])
    assert policy.choose(0.0) is only
    assert policy.choose(0.999) is only
