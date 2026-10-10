"""T-204: the 600-order shop validates and builds in under 5 s, and its JSON stays under 16 MiB."""

from __future__ import annotations

import time

import pytest
from fixtures.generate import scaled_shop

from twinflow.erp.build import build_problem_from_erp
from twinflow.erp.digest import MAX_SNAPSHOT_BYTES
from twinflow.erp.profile import load_planning_profile

PROFILE = """
[erp.class_weights]
"A-KEY" = 4
"B-CORE" = 2
"C-SPOT" = 1
"""


@pytest.mark.slow
def test_scaled_shop_validates_and_builds_within_budget() -> None:
    snapshot = scaled_shop(orders=600)
    size = len(snapshot.model_dump_json().encode())
    profile = load_planning_profile(PROFILE)

    started = time.perf_counter()
    result = build_problem_from_erp(snapshot, profile)
    elapsed = time.perf_counter() - started

    print(f"scaled_shop: {size / 2**20:.2f} MiB, validate+build {elapsed:.3f} s")
    assert result.status == "ready"
    assert result.request is not None
    assert len(snapshot.production_orders) == 600
    assert size < MAX_SNAPSHOT_BYTES
    assert elapsed < 5.0
