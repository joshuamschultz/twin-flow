"""Regression for Polars-safe multiprocessing startup in replication workers."""

from __future__ import annotations

import multiprocessing
from pathlib import Path
from unittest import mock

from twinflow.plan.loader import WorkOrder
from twinflow.plan.replication import ReplicationRunner


class _PoolDouble:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls: list[tuple[object, list[tuple[object, ...]]]] = []

    def __enter__(self) -> _PoolDouble:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def starmap(self, function: object, args: list[tuple[object, ...]]) -> list[object]:
        self.calls.append((function, args))
        return self.results


def test_replication_runner_uses_explicit_spawn_context_without_mutating_global_method(
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "model.yaml"
    model_path.write_text("model contents are not read by the dispatch test", encoding="utf-8")
    pool = _PoolDouble([object(), object()])
    context = mock.Mock()
    context.Pool.return_value = pool
    original_method = multiprocessing.get_start_method(allow_none=True)

    with (
        mock.patch("multiprocessing.get_context", return_value=context) as get_context,
        mock.patch(
            "multiprocessing.Pool",
            side_effect=AssertionError("unsafe default multiprocessing context used"),
        ) as default_pool,
    ):
        results = ReplicationRunner(str(model_path)).run(
            [WorkOrder("WO-1", "part", 1, "0", "10")],
            reps=2,
            base_seed=7,
            artifact_dir=tmp_path / "runs",
        )

    assert results == pool.results
    get_context.assert_called_once_with("spawn")
    context.Pool.assert_called_once()
    default_pool.assert_not_called()
    assert multiprocessing.get_start_method(allow_none=True) == original_method
