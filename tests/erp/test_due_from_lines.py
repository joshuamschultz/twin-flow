"""Due date from ALL linked sales lines, one shared helper (TF-005 review ruling C).

Contract pinned here (the implementation follows these tests):

* PINNED module and names: `twinflow.erp._dates.order_due_day(order, lines_by_key) -> date |
  None` and `twinflow.erp._dates.order_promised_day(order, lines_by_key) -> date | None`.
  `lines_by_key` maps `twinflow.erp._checks.line_key(order_type, order_nbr, line_nbr)` to the
  `SalesOrderLine` (the validator's `Index.sales_lines` shape). A link whose line is missing
  is ignored.
* Per line due = `promised_date` else `requested_date`. Job due day = the EARLIEST per-line
  due across all linked lines, else the order's `due_date` (None when nothing is set).
* Promised day = the earliest `promised_date` across linked lines (None if none); the job's
  `promised_ship` is 00:00 local of that day.
* Validator: `no_due_date` iff `order_due_day` is None; PINNED `past_due_no_promise` iff
  `order_promised_day` is None and `order_due_day` is strictly before `as_of`'s local date
  (a promise on any linked line means the order is promised, not unpromised-past).
* ONE implementation: `validate.py` and the build path (`_jobs.py` or `build.py`) import both
  helpers from `twinflow.erp._dates`; no other ERP module reads `requested_date`.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from _factories import lineage

import twinflow.erp as erp_package
from twinflow.erp._checks import line_key
from twinflow.erp.build import BuildResult, build_problem_from_erp
from twinflow.erp.digest import snapshot_digest
from twinflow.erp.profile import PlanningProfile, load_planning_profile
from twinflow.erp.schema import ErpSnapshot, ProductionOrder, SalesOrderLine
from twinflow.erp.validate import validate_snapshot
from twinflow.scheduling import Job

AS_OF = "2026-10-12T05:00:00-05:00"  # Monday; "today" is 2026-10-12 in America/Chicago
TZ = ZoneInfo("America/Chicago")
ORDER_DUE = "2026-11-20"

Data = dict[str, Any]
ERP_DIR = Path(erp_package.__file__).parent


# --- snapshot builders --------------------------------------------------------------------------


def _line(nbr: int, *, promised: str | None, requested: str | None) -> Data:
    return {
        "order_type": "SO",
        "order_nbr": f"SO-{nbr}",
        "line_nbr": 1,
        "customer_id": "C1",
        "item_id": "SPR",
        "qty_open": 10.0,
        "requested_date": requested,
        "promised_date": promised,
        "lineage": lineage("sales_order_line", "SO", f"SO-{nbr}", "1"),
    }


def _link(nbr: int) -> Data:
    return {"order_type": "SO", "order_nbr": f"SO-{nbr}", "line_nbr": "1"}


def _order(links: list[int], due_date: str | None = ORDER_DUE) -> Data:
    op = {
        "op_id": "10",
        "sequence": 10,
        "work_center_id": "form",
        "options": [
            {
                "work_center_id": "form",
                "machine_id": "form-1",
                "setup_seconds": 0.0,
                "run_seconds_per_unit": 1.0,
                "fixed_run_seconds": 0.0,
                "preference": 0,
                "lineage": lineage("option", "form-1"),
            }
        ],
        "setup_params": {},
        "queue_seconds": 0.0,
        "move_seconds": 0.0,
        "status": "pending",
        "qty_to_produce": 10.0,
        "qty_complete": 0.0,
        "qty_scrapped": 0.0,
        "qty_remaining": 10.0,
        "lineage": lineage("order_operation", "MO-1", "10"),
    }
    return {
        "id": "MO-1",
        "order_type": "MO",
        "item_id": "SPR",
        "quantity": 10.0,
        "status": "released",
        "rush": False,
        "hold": False,
        "due_date": due_date,
        "sales_links": [_link(nbr) for nbr in links],
        "operations": [op],
        "materials": [],
        "lineage": lineage("production_order", "MO-1"),
    }


def _snapshot(order: Data, lines: list[Data]) -> ErpSnapshot:
    data: Data = {
        "schema_version": "twinflow.erp/1",
        "as_of": AS_OF,
        "plant_timezone": "America/Chicago",
        "source": {
            "system": "hand",
            "instance": "due",
            "extractor": "test",
            "extractor_version": "1",
            "generation": None,
        },
        "source_issues": [],
        "items": [{"id": "SPR", "uom": "ea", "attributes": {}, "lineage": lineage("item", "SPR")}],
        "boms": [],
        "routings": [],
        "work_centers": [{"id": "form", "attributes": {}, "lineage": lineage("wc", "form")}],
        "machines": [
            {
                "id": "form-1",
                "work_center_id": "form",
                "efficiency": 1.0,
                "active": True,
                "lineage": lineage("machine", "form-1"),
            }
        ],
        "calendars": [],
        "labor_pools": [],
        "production_orders": [order],
        "sales_order_lines": lines,
        "customers": [{"id": "C1", "priority_class": None, "lineage": lineage("customer", "C1")}],
        "vendors": [],
        "purchase_lines": [],
        "inventory": [],
    }
    snapshot = ErpSnapshot.model_validate(data)
    return snapshot.model_copy(update={"digest": snapshot_digest(snapshot)})


def _profile() -> PlanningProfile:
    return load_planning_profile("[erp]\n")


def _lines_by_key(snapshot: ErpSnapshot) -> dict[tuple[str, str, str], SalesOrderLine]:
    return {
        line_key(line.order_type, line.order_nbr, line.line_nbr): line
        for line in snapshot.sales_order_lines
    }


def _helpers() -> tuple[Any, Any]:
    from twinflow.erp._dates import order_due_day, order_promised_day

    return order_due_day, order_promised_day


def _days(snapshot: ErpSnapshot) -> tuple[dt.date | None, dt.date | None]:
    order_due_day, order_promised_day = _helpers()
    order: ProductionOrder = snapshot.production_orders[0]
    lines = _lines_by_key(snapshot)
    return order_due_day(order, lines), order_promised_day(order, lines)


def _start_of(day: dt.date, snapshot: ErpSnapshot) -> float:
    midnight = dt.datetime.combine(day, dt.time(), tzinfo=TZ)
    return midnight.timestamp() - snapshot.as_of.timestamp()


def _job(result: BuildResult) -> Job:
    assert result.status == "ready", [i.code for i in result.issues if i.severity == "hard"]
    assert result.request is not None
    (job,) = result.request.problem.jobs
    return job


def _codes(snapshot: ErpSnapshot) -> set[str]:
    return {i.code for i in validate_snapshot(snapshot, _profile()).issues}


# --- the shared helper --------------------------------------------------------------------------

# The review case: the FIRST link carries a later promise, the SECOND an earlier request.
LATER_PROMISE_FIRST = [
    _line(1, promised="2026-10-30", requested="2026-10-20"),
    _line(2, promised=None, requested="2026-10-25"),
]


def test_order_due_day_takes_the_earliest_line_due_not_the_first_link() -> None:
    snapshot = _snapshot(_order([1, 2]), LATER_PROMISE_FIRST)
    due, promised = _days(snapshot)
    # Line 1 is due on its promise (10-30, its request does not count); line 2 on its request.
    assert due == dt.date(2026, 10, 25)
    assert promised == dt.date(2026, 10, 30)


def test_order_promised_day_is_the_earliest_promise_across_lines() -> None:
    lines = [
        _line(1, promised="2026-10-30", requested=None),
        _line(2, promised="2026-10-22", requested="2026-10-15"),
    ]
    due, promised = _days(_snapshot(_order([1, 2]), lines))
    assert (due, promised) == (dt.date(2026, 10, 22), dt.date(2026, 10, 22))


@pytest.mark.parametrize(
    ("links", "lines", "order_due", "expected"),
    [
        ([], [], ORDER_DUE, (dt.date(2026, 11, 20), None)),
        ([], [], None, (None, None)),
        ([1], [_line(1, promised=None, requested=None)], ORDER_DUE, (dt.date(2026, 11, 20), None)),
        ([1], [_line(1, promised=None, requested=None)], None, (None, None)),
        # A link whose sales line is not in the snapshot is ignored (it is not a date source).
        (
            [9, 1],
            [_line(1, promised=None, requested="2026-10-28")],
            None,
            (dt.date(2026, 10, 28), None),
        ),
        # The order's own due date never beats a linked line, even when it is earlier.
        (
            [1],
            [_line(1, promised=None, requested="2026-11-25")],
            ORDER_DUE,
            (dt.date(2026, 11, 25), None),
        ),
    ],
    ids=[
        "no-links",
        "nothing",
        "dateless-line",
        "dateless-line-no-due",
        "missing-link",
        "line-wins",
    ],
)
def test_order_due_day_fallbacks(
    links: list[int],
    lines: list[Data],
    order_due: str | None,
    expected: tuple[dt.date | None, dt.date | None],
) -> None:
    assert _days(_snapshot(_order(links, order_due), lines)) == expected


def test_order_due_day_does_not_depend_on_link_order() -> None:
    forward = _days(_snapshot(_order([1, 2]), LATER_PROMISE_FIRST))
    backward = _days(_snapshot(_order([2, 1]), LATER_PROMISE_FIRST))
    assert forward == backward


# --- the builder uses it ------------------------------------------------------------------------


def test_build_job_due_is_end_of_the_earliest_line_due_and_promised_ship_its_promise() -> None:
    snapshot = _snapshot(_order([1, 2]), LATER_PROMISE_FIRST)
    job = _job(build_problem_from_erp(snapshot, _profile()))
    assert job.due == pytest.approx(_start_of(dt.date(2026, 10, 26), snapshot))  # end of 10-25
    assert job.promised_ship == pytest.approx(_start_of(dt.date(2026, 10, 30), snapshot))


def test_build_promised_ship_comes_from_a_later_link_when_the_first_has_no_promise() -> None:
    lines = [
        _line(1, promised=None, requested="2026-10-20"),
        _line(2, promised="2026-10-28", requested="2026-10-27"),
    ]
    snapshot = _snapshot(_order([1, 2]), lines)
    job = _job(build_problem_from_erp(snapshot, _profile()))
    assert job.due == pytest.approx(_start_of(dt.date(2026, 10, 21), snapshot))  # end of 10-20
    assert job.promised_ship == pytest.approx(_start_of(dt.date(2026, 10, 28), snapshot))


# --- the validator agrees with the builder ------------------------------------------------------


def test_validate_past_due_no_promise_uses_the_earliest_line_due() -> None:
    # Neither line is promised; the first link's request is in the future, the second's past.
    lines = [
        _line(1, promised=None, requested="2026-10-20"),
        _line(2, promised=None, requested="2026-10-05"),
    ]
    snapshot = _snapshot(_order([1, 2]), lines)
    assert "past_due_no_promise" in _codes(snapshot)
    job = _job(build_problem_from_erp(snapshot, _profile()))
    assert job.due == pytest.approx(_start_of(dt.date(2026, 10, 6), snapshot))  # end of 10-05
    assert job.due is not None and job.due < 0


def test_validate_a_promise_on_any_line_means_not_unpromised_past_due() -> None:
    # PINNED: the earliest due (line 2's past request) is past, but line 1 is promised.
    lines = [
        _line(1, promised="2026-10-30", requested=None),
        _line(2, promised=None, requested="2026-10-05"),
    ]
    snapshot = _snapshot(_order([1, 2]), lines)
    assert "past_due_no_promise" not in _codes(snapshot)


CONSISTENCY_CASES: list[tuple[list[int], list[Data], str | None]] = [
    ([1, 2], LATER_PROMISE_FIRST, ORDER_DUE),
    (
        [1, 2],
        [
            _line(1, promised=None, requested="2026-10-20"),
            _line(2, promised=None, requested="2026-10-05"),
        ],
        None,
    ),
    (
        [2, 1],
        [
            _line(1, promised=None, requested="2026-10-11"),
            _line(2, promised=None, requested="2026-10-30"),
        ],
        None,
    ),
    ([1], [_line(1, promised=None, requested="2026-10-12")], None),  # today: not past
    ([1], [_line(1, promised=None, requested="2026-10-11")], None),  # yesterday: past
    (
        [1, 2],
        [_line(1, promised="2026-10-01", requested=None), _line(2, promised=None, requested=None)],
        None,
    ),
    ([1], [_line(1, promised=None, requested=None)], "2026-10-02"),
    ([1], [_line(1, promised=None, requested=None)], None),
    ([], [], None),
    ([], [], "2026-10-11"),
]


@pytest.mark.parametrize(("links", "lines", "order_due"), CONSISTENCY_CASES)
def test_validator_due_issues_match_the_built_job(
    links: list[int], lines: list[Data], order_due: str | None
) -> None:
    """`no_due_date` iff the job has no due; `past_due_no_promise` iff it ended before today
    (end of a past day is before `as_of`, so due < 0) and has no promise."""
    snapshot = _snapshot(_order(links, order_due), lines)
    codes = _codes(snapshot)
    job = _job(build_problem_from_erp(snapshot, _profile()))
    assert ("no_due_date" in codes) == (job.due is None)
    unpromised_past = job.due is not None and job.due < 0 and job.promised_ship is None
    assert ("past_due_no_promise" in codes) == unpromised_past


# --- one implementation -------------------------------------------------------------------------


def _imported_names(module: Path, source: str) -> set[str]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == source
        for alias in node.names
    }


HELPERS = {"order_due_day", "order_promised_day"}


def test_validator_imports_the_shared_due_helpers() -> None:
    assert HELPERS <= _imported_names(ERP_DIR / "validate.py", "twinflow.erp._dates")


def test_builder_imports_the_shared_due_helpers() -> None:
    found = set().union(
        *(
            _imported_names(ERP_DIR / name, "twinflow.erp._dates")
            for name in ("_jobs.py", "build.py")
        )
    )
    assert HELPERS <= found


def test_no_erp_module_but_the_helper_reads_requested_date() -> None:
    """The precedence lives in `_dates.py` only; nobody else re-derives it from the line."""
    readers = sorted(
        path.name
        for path in ERP_DIR.glob("*.py")
        if path.name not in {"_dates.py", "schema.py"}
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "requested_date"
    )
    assert readers == []
