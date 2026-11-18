"""K-table model, seed table, lookup and store (T-163, COMP-025, REQ-123, D-082, DEV-15).

API these tests pin (`twinflow.scheduling.learning.ktable`, the implementer builds to it):

- `KEntry` (Pydantic, frozen, extra forbid): `tau`, `R >= 0`, `eta >= 0`, `m >= 1` (int),
  `K1 in [0.5, 10]`, `K2 in [0.05, 10]`, `runs >= 0` (int).
- `KTable` (Pydantic, frozen, extra forbid): `version: 1`, `entries: tuple[KEntry, ...]`.
- `seed_table() -> KTable`: 256 entries, tau {0.2, 0.4, 0.6, 0.8} x R {0.2, 0.5, 0.8, 1.2}
  x eta {0.05, 0.2, 0.5, 1.0} x m {1, 2, 4, 8}, K from the p.376 formulas and clamps
  (`algorithms.atcs.atcs_k`).
- `lookup(table, stats) -> (K1, K2)`: `stats` is anything with `tau`, `R`, `eta`, `m`
  (an `InstanceStats`, an `AtcsRun`); an exact (tau, R, eta, m) match returns that entry,
  else inverse-distance weighting over the 4 nearest entries in (tau, R, eta, log2 m).
  The interpolation tests use query points whose 4 nearest entries are equidistant, so
  they hold for any IDW power.
- `as_k_lookup(table)`: a picklable callable `(tau, R, eta, m) -> (K1, K2)` equal to
  `lookup`, accepted by `registry.get("atcs", k_lookup=...)` (AtcsRun source `table`).
- `KTableStore(path)`: `save(table)` writes `{"version": 1, "digest": sha256 hex of the
  canonical JSON of the entries, "entries": [...]}` to a temp file in the same directory
  then `os.replace`; `load() -> KTable` raises `KTableIntegrityError` when the digest does
  not match. Canonical JSON = `json.dumps(entries, sort_keys=True, separators=(",", ":"))`.
"""

from __future__ import annotations

import hashlib
import importlib
import itertools
import json
import math
import os
import pickle
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from kernel_cases import load_request
from pydantic import ValidationError

from twinflow.scheduling.algorithms.atcs import atcs_k
from twinflow.scheduling.kernel.stats import InstanceStats
from twinflow.scheduling.registry import get as get_solver
from twinflow.scheduling.solver import solve

TAUS = (0.2, 0.4, 0.6, 0.8)
RS = (0.2, 0.5, 0.8, 1.2)
ETAS = (0.05, 0.2, 0.5, 1.0)
MS = (1, 2, 4, 8)


def _kt() -> ModuleType:
    """The module, imported per test so its absence fails each test, not collection."""
    return importlib.import_module("twinflow.scheduling.learning.ktable")


def _stats(tau: float, R: float, eta: float, m: int) -> InstanceStats:  # noqa: N803
    return InstanceStats(
        work_center_id="WC",
        m=m,
        pbar=1.0,
        sbar=eta,
        cmax_hat=10.0,
        tau=tau,
        R=R,
        eta=eta,
        sampled_pairs=0,
    )


def _entry(**fields: Any) -> Any:
    base: dict[str, Any] = {"tau": 0.5, "R": 0.5, "eta": 0.5, "m": 1, "K1": 2.0, "K2": 1.0}
    return _kt().KEntry(**(base | fields))


def _table(*entries: Any) -> Any:
    return _kt().KTable(version=1, entries=tuple(entries))


# --- model (Data Model KTable / KEntry) -------------------------------------------------------


def test_kentry_valid_fields_build_with_runs_defaulting_or_given() -> None:
    entry = _entry(runs=3)
    assert (entry.tau, entry.R, entry.eta, entry.m, entry.K1, entry.K2, entry.runs) == (
        0.5,
        0.5,
        0.5,
        1,
        2.0,
        1.0,
        3,
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"K1": 0.49},
        {"K1": 10.01},
        {"K2": 0.049},
        {"K2": 10.01},
        {"m": 0},
        {"R": -0.1},
        {"eta": -0.1},
        {"runs": -1},
        {"unexpected": 1},
    ],
    ids=["K1-low", "K1-high", "K2-low", "K2-high", "m-0", "R-neg", "eta-neg", "runs-neg", "extra"],
)
def test_kentry_out_of_range_or_extra_field_is_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _entry(**fields)


def test_kentry_and_ktable_are_frozen() -> None:
    entry = _entry()
    table = _table(entry)
    with pytest.raises(ValidationError):
        entry.K1 = 3.0
    with pytest.raises(ValidationError):
        table.entries = ()


def test_ktable_rejects_extra_field_and_other_version() -> None:
    with pytest.raises(ValidationError):
        _kt().KTable(version=1, entries=(), unexpected=1)
    with pytest.raises(ValidationError):
        _kt().KTable(version=2, entries=())


# --- seed table (p.376) -----------------------------------------------------------------------


def test_seed_table_has_256_entries_on_the_grid_with_p376_k() -> None:
    table = _kt().seed_table()
    assert table.version == 1
    assert len(table.entries) == 256
    points = {(e.tau, e.R, e.eta, e.m): e for e in table.entries}
    assert set(points) == set(itertools.product(TAUS, RS, ETAS, MS))
    for (tau, r, eta, _m), entry in points.items():
        k1, k2 = atcs_k(tau, r, eta)
        assert entry.K1 == pytest.approx(k1, abs=1e-12), (tau, r, eta)
        assert entry.K2 == pytest.approx(k2, abs=1e-12), (tau, r, eta)


def test_seed_table_k_stays_inside_the_clamp_bounds() -> None:
    table = _kt().seed_table()
    # On this grid K1 spans [3.6, 4.7] and K2 [0.1, 1.789]: no clamp binds, the bounds hold.
    assert all(0.5 <= e.K1 <= 10.0 and 0.05 <= e.K2 <= 10.0 for e in table.entries)
    one = next(e for e in table.entries if (e.tau, e.R, e.eta, e.m) == (0.2, 1.2, 1.0, 8))
    assert one.K1 == pytest.approx(6.0 - 2.4)
    assert one.K2 == pytest.approx(0.1)


# --- lookup ----------------------------------------------------------------------------------


@pytest.mark.parametrize("point", [(0.2, 0.2, 0.05, 1), (0.6, 0.8, 0.5, 4), (0.8, 1.2, 1.0, 8)])
def test_lookup_at_a_seed_point_returns_that_entry_exactly(
    point: tuple[float, float, float, int],
) -> None:
    table = _kt().seed_table()
    entry = next(e for e in table.entries if (e.tau, e.R, e.eta, e.m) == point)
    assert _kt().lookup(table, _stats(*point)) == (entry.K1, entry.K2)


def test_lookup_between_seed_points_averages_four_equidistant_neighbors() -> None:
    # (0.3, 0.2, 0.125, 1): the 4 corners tau {0.2, 0.4} x eta {0.05, 0.2} are each 0.125
    # away; every other entry is farther. K1 depends on R only (4.7), K2 is their mean.
    table = _kt().seed_table()
    k1, k2 = _kt().lookup(table, _stats(0.3, 0.2, 0.125, 1))
    corners = [atcs_k(t, 0.2, e) for t in (0.2, 0.4) for e in (0.05, 0.2)]
    assert k1 == pytest.approx(4.7)
    assert k2 == pytest.approx(sum(c[1] for c in corners) / 4)
    assert k2 != pytest.approx(atcs_k(0.3, 0.2, 0.125)[1], abs=1e-3)  # not the formula


def test_lookup_uses_only_the_four_nearest_entries() -> None:
    corners = [
        _entry(tau=t, eta=e, K1=k1)
        for (t, e), k1 in zip(
            itertools.product((0.4, 0.6), (0.4, 0.6)), (1.0, 2.0, 3.0, 4.0), strict=True
        )
    ]
    far = _entry(tau=0.5, eta=0.5, R=0.9, K1=10.0)  # 0.4 away, the corners are 0.141 away
    k1, _k2 = _kt().lookup(_table(*corners, far), _stats(0.5, 0.5, 0.5, 1))
    assert k1 == pytest.approx(2.5)


def test_lookup_measures_machine_count_on_a_log2_axis() -> None:
    # m = 4 is log2-equidistant (1) from m = 2 and m = 8; linearly it is 2 from 2 and 4 from 8.
    entries = [
        _entry(tau=0.4, m=2, K1=1.0),
        _entry(tau=0.6, m=2, K1=2.0),
        _entry(tau=0.4, m=8, K1=5.0),
        _entry(tau=0.6, m=8, K1=6.0),
    ]
    k1, _k2 = _kt().lookup(_table(*entries), _stats(0.5, 0.5, 0.5, 4))
    assert k1 == pytest.approx(3.5)


def test_lookup_result_lies_within_the_neighbors_range() -> None:
    table = _kt().seed_table()
    k1, k2 = _kt().lookup(table, _stats(0.566038, 0.471698, 0.177778, 1))
    assert 0.5 <= k1 <= 10.0
    assert 0.05 <= k2 <= 10.0
    assert all(math.isfinite(v) for v in (k1, k2))


# --- as_k_lookup and ATCS (DEV-15) ------------------------------------------------------------


def test_as_k_lookup_matches_lookup_and_survives_pickling() -> None:
    table = _kt().seed_table()
    k_lookup = _kt().as_k_lookup(table)
    for point in [(0.2, 0.2, 0.05, 1), (0.3, 0.2, 0.125, 1), (0.55, 0.7, 0.3, 3)]:
        assert k_lookup(*point) == pytest.approx(_kt().lookup(table, _stats(*point)))
    restored = pickle.loads(pickle.dumps(k_lookup))  # noqa: S301
    assert restored(0.3, 0.2, 0.125, 1) == pytest.approx(k_lookup(0.3, 0.2, 0.125, 1))


def test_atcs_with_table_k_lookup_reports_source_table_with_table_k() -> None:
    table = _kt().seed_table()
    k_lookup = _kt().as_k_lookup(table)
    result = solve(load_request("pinedo_14_2_1"), solver=get_solver("atcs", k_lookup=k_lookup))
    assert result.verified, result.issues
    (run,) = result.schedule.atcs_runs
    assert run.source == "table"
    k1, k2 = k_lookup(run.tau, run.R, run.eta, run.m)
    assert (run.K1, run.K2) == pytest.approx((k1, k2))


def test_atcs_solver_with_table_k_lookup_survives_pickling() -> None:
    solver = get_solver("atcs", k_lookup=_kt().as_k_lookup(_kt().seed_table()))
    restored = pickle.loads(pickle.dumps(solver))  # noqa: S301
    result = solve(load_request("pinedo_14_2_1"), solver=restored)
    assert result.verified, result.issues
    assert result.schedule.atcs_runs[0].source == "table"


# --- store ------------------------------------------------------------------------------------


def _small_table(k1: float = 2.0) -> Any:
    return _table(_entry(K1=k1), _entry(tau=0.7, m=4, K2=0.3, runs=2))


def test_store_round_trips_a_table(tmp_path: Path) -> None:
    store = _kt().KTableStore(tmp_path / "k.json")
    table = _kt().seed_table()
    store.save(table)
    assert store.load() == table


def test_store_file_has_version_digest_of_canonical_entries(tmp_path: Path) -> None:
    path = tmp_path / "k.json"
    _kt().KTableStore(path).save(_small_table())
    data = json.loads(path.read_text())
    assert set(data) == {"version", "digest", "entries"}
    assert data["version"] == 1
    canonical = json.dumps(data["entries"], sort_keys=True, separators=(",", ":"))
    assert data["digest"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert len(data["entries"]) == 2


def test_store_load_rejects_a_tampered_entry(tmp_path: Path) -> None:
    path = tmp_path / "k.json"
    store = _kt().KTableStore(path)
    store.save(_small_table())
    data = json.loads(path.read_text())
    data["entries"][0]["K1"] = 9.0
    path.write_text(json.dumps(data))
    with pytest.raises(_kt().KTableIntegrityError):
        store.load()


def test_store_load_rejects_a_wrong_or_missing_digest(tmp_path: Path) -> None:
    path = tmp_path / "k.json"
    store = _kt().KTableStore(path)
    store.save(_small_table())
    data = json.loads(path.read_text())
    path.write_text(json.dumps(data | {"digest": "0" * 64}))
    with pytest.raises(_kt().KTableIntegrityError):
        store.load()
    del data["digest"]
    path.write_text(json.dumps(data))
    with pytest.raises(_kt().KTableIntegrityError):
        store.load()


def test_store_writes_a_temp_file_in_the_same_directory_then_os_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k.json"
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src: Any, dst: Any) -> None:
        calls.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    _kt().KTableStore(path).save(_small_table())
    assert len(calls) == 1
    src, dst = calls[0]
    assert dst == path
    assert src != path
    assert src.parent == path.parent


def test_store_crash_between_write_and_replace_keeps_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k.json"
    store = _kt().KTableStore(path)
    old = _small_table(k1=2.0)
    store.save(old)
    before = path.read_bytes()

    def crash(src: Any, dst: Any) -> None:
        raise OSError("simulated crash between write and replace")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="simulated crash"):
        store.save(_small_table(k1=7.0))
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert store.load() == old


def test_store_failed_save_leaves_no_temp_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "k.json"
    store = _kt().KTableStore(path)
    store.save(_small_table())

    def crash(src: Any, dst: Any) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        store.save(_small_table(k1=7.0))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["k.json"]
