"""Problem validator and boundary parse (COMP-006, REQ-071, REQ-084).

`validate` reports every scheduling-meaning issue of a request or a compiled problem, never
only the first. Each issue has a code, a JSON path and a message. Paths are rooted at the
request and use dotted field names with `[i]` list indices, the format `overrides.py`
uses: `problem.jobs[0].operations[1].options[0].machine_id`, `overrides[2].id`. A
compiled problem uses the same paths, rooted at its `problem` field.

`parse_request` is the input boundary for surfaces: it turns JSON or a mapping into a
`SolveRequest` or into `schema` / `schema_version` issues, so callers never see a raw
`ValidationError`. Issue text is cut to the model's text bound, so attacker-sized keys
cannot break issue construction.

Checks the Pydantic model enforces at parse time are kept only where a compiled problem
can still break them, because `model_copy` and `model_construct` skip validation:
no_options, lag_bounds, restart_rule, calendar_order, schema_version.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError
from pydantic_core import ErrorDetails

from twinflow.scheduling.model import (
    MAX_TEXT_LENGTH,
    SCHEMA_VERSION,
    BatchCapacity,
    Calendar,
    CompiledProblem,
    Machine,
    Operation,
    Precedence,
    Problem,
    ScheduleIssue,
    SolveRequest,
)
from twinflow.scheduling.overrides import apply_overrides

ROOT_PATH = "$"
_UNION_TAG_FIELDS = ("kind", "mode")


class _HasId(Protocol):
    @property
    def id(self) -> str: ...


@dataclass(frozen=True)
class _Index:
    """Lookups over one problem, built once. The first of a duplicated id wins."""

    problem: Problem
    operations: tuple[tuple[str, Operation], ...]
    op_by_id: dict[str, Operation]
    op_path: dict[str, str]
    machines: dict[str, Machine]
    work_centers: frozenset[str]
    calendars: frozenset[str]
    pools: frozenset[str]
    setup_functions: frozenset[str]


def _index(problem: Problem) -> _Index:
    operations = tuple(
        (f"problem.jobs[{j}].operations[{o}]", op)
        for j, job in enumerate(problem.jobs)
        for o, op in enumerate(job.operations)
    )
    op_by_id: dict[str, Operation] = {}
    op_path: dict[str, str] = {}
    for path, op in operations:
        op_by_id.setdefault(op.id, op)
        op_path.setdefault(op.id, path)
    machines: dict[str, Machine] = {}
    for machine in problem.machines:
        machines.setdefault(machine.id, machine)
    return _Index(
        problem=problem,
        operations=operations,
        op_by_id=op_by_id,
        op_path=op_path,
        machines=machines,
        work_centers=frozenset(wc.id for wc in problem.work_centers),
        calendars=frozenset(calendar.id for calendar in problem.calendars),
        pools=frozenset(pool.id for pool in problem.operator_pools),
        setup_functions=frozenset(function.id for function in problem.setup_functions),
    )


def _issue(code: str, path: str, message: str) -> ScheduleIssue:
    return ScheduleIssue(code=code, path=path[:MAX_TEXT_LENGTH], message=message[:MAX_TEXT_LENGTH])


# --- public API -----------------------------------------------------------------------------


def validate(subject: SolveRequest | CompiledProblem) -> list[ScheduleIssue]:
    """Every issue of a raw request or a compiled problem; empty means valid.

    A request also reports the override issues of compiling it (D-066); a compiled
    problem reports the override issues it carries.
    """
    if isinstance(subject, CompiledProblem):
        return [*_problem_issues(subject.problem), *subject.override_issues]
    return _request_issues(subject)


def parse_request(data: Mapping[str, Any] | str | bytes) -> SolveRequest | list[ScheduleIssue]:
    """Parse JSON text or a mapping into a `SolveRequest`, or return every schema issue.

    Errors at `schema_version` get code `schema_version`; every other error, and text
    that is not JSON, gets code `schema` (the JSON root path is `$`).
    """
    try:
        raw = json.loads(data) if isinstance(data, str | bytes) else data
    except (ValueError, RecursionError) as error:
        return [_issue("schema", ROOT_PATH, f"invalid JSON: {error}")]
    try:
        return SolveRequest.model_validate(raw)
    except ValidationError as error:
        details = error.errors(include_url=False, include_input=False)
        return [_schema_issue(detail, raw) for detail in details]


# --- request and boundary -------------------------------------------------------------------


def _request_issues(request: SolveRequest) -> list[ScheduleIssue]:
    issues: list[ScheduleIssue] = []
    if request.schema_version != SCHEMA_VERSION:
        message = f"schema_version must be {SCHEMA_VERSION}"
        issues.append(_issue("schema_version", "schema_version", message))
    problem_issues = _problem_issues(request.problem)
    issues.extend(problem_issues)
    issues.extend(_duplicates(_id_paths("overrides", request.overrides), "overrides"))
    # Compiling a problem with duplicate ids is undefined; those ids are already reported.
    if not any(issue.code == "duplicate_id" for issue in problem_issues):
        issues.extend(apply_overrides(request).override_issues)
    return issues


def _schema_issue(detail: ErrorDetails, raw: object) -> ScheduleIssue:
    loc = tuple(detail["loc"])
    code = "schema_version" if loc == ("schema_version",) else "schema"
    return _issue(code, _json_path(loc, raw), detail["msg"])


def _json_path(loc: tuple[int | str, ...], data: object) -> str:
    """Pydantic `loc` as a request path; discriminated-union tags are not path steps."""
    path = ""
    node = data
    for part in loc:
        if _is_union_tag(node, part):
            continue
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
        node = _child(node, part)
    return path.removeprefix(".") or ROOT_PATH


def _is_union_tag(node: object, part: int | str) -> bool:
    if not isinstance(node, Mapping) or part in node:
        return False
    return any(node.get(field) == part for field in _UNION_TAG_FIELDS)


def _child(node: object, part: int | str) -> object:
    if isinstance(node, Mapping):
        return node.get(part)
    if isinstance(node, Sequence) and isinstance(part, int) and 0 <= part < len(node):
        return node[part]
    return None


# --- problem checks -------------------------------------------------------------------------


def _problem_issues(problem: Problem) -> list[ScheduleIssue]:
    index = _index(problem)
    issues = [issue for check in _PROBLEM_CHECKS for issue in check(index)]
    for op_check in _OPERATION_CHECKS:
        for path, op in index.operations:
            issues.extend(op_check(index, path, op))
    return issues


def _id_paths(prefix: str, items: Sequence[_HasId]) -> Iterator[tuple[str, str]]:
    return ((f"{prefix}[{i}]", item.id) for i, item in enumerate(items))


def _duplicates(paths_and_ids: Iterable[tuple[str, str]], what: str) -> Iterator[ScheduleIssue]:
    seen: set[str] = set()
    for path, item_id in paths_and_ids:
        if item_id in seen:
            yield _issue("duplicate_id", f"{path}.id", f"duplicate id {item_id} in {what}")
        seen.add(item_id)


def _duplicate_ids(index: _Index) -> Iterator[ScheduleIssue]:
    """Ids must be unique per collection; operation ids across all jobs."""
    problem = index.problem
    collections: tuple[tuple[str, Sequence[_HasId]], ...] = (
        ("jobs", problem.jobs),
        ("machines", problem.machines),
        ("work_centers", problem.work_centers),
        ("calendars", problem.calendars),
        ("operator_pools", problem.operator_pools),
        ("setup_functions", problem.setup_functions),
    )
    for name, items in collections:
        yield from _duplicates(_id_paths(f"problem.{name}", items), name)
    yield from _duplicates(((path, op.id) for path, op in index.operations), "operations")


def _missing(
    known: Collection[str], value: str | None, path: str, what: str
) -> Iterator[ScheduleIssue]:
    if value is not None and value not in known:
        yield _issue("unknown_reference", path, f"unknown {what} {value}")


def _resource_references(index: _Index) -> Iterator[ScheduleIssue]:
    problem = index.problem
    for i, machine in enumerate(problem.machines):
        base = f"problem.machines[{i}]"
        wc_path = f"{base}.work_center_id"
        yield from _missing(index.work_centers, machine.work_center_id, wc_path, "work center")
        yield from _missing(index.calendars, machine.calendar_id, f"{base}.calendar_id", "calendar")
        setup_path = f"{base}.setup_function_id"
        yield from _missing(
            index.setup_functions, machine.setup_function_id, setup_path, "setup function"
        )
    for i, wc in enumerate(problem.work_centers):
        setup_path = f"problem.work_centers[{i}].setup_function_id"
        yield from _missing(
            index.setup_functions, wc.setup_function_id, setup_path, "setup function"
        )
    for i, pool in enumerate(problem.operator_pools):
        calendar_path = f"problem.operator_pools[{i}].calendar_id"
        yield from _missing(index.calendars, pool.calendar_id, calendar_path, "calendar")


def _cycles(index: _Index) -> Iterator[ScheduleIssue]:
    """One issue per strongly connected component of the precedence graph that loops."""
    graph = {
        op_id: [
            link.operation_id for link in op.predecessors if link.operation_id in index.op_by_id
        ]
        for op_id, op in index.op_by_id.items()
    }
    for component in _components(graph, _finish_order(graph)):
        if len(component) > 1 or component[0] in graph[component[0]]:
            op_ids = sorted(component)
            path = f"{index.op_path[op_ids[0]]}.predecessors"
            yield _issue("cycle", path, f"precedence cycle through {', '.join(op_ids)}")


def _finish_order(graph: Mapping[str, Sequence[str]]) -> list[str]:
    """Nodes in depth-first finish order (iterative: deep chains must not recurse)."""
    seen: set[str] = set()
    order: list[str] = []
    for root in graph:
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(graph[root]))]
        while stack:
            node, children = stack[-1]
            child = next((c for c in children if c not in seen), None)
            if child is None:
                stack.pop()
                order.append(node)
            else:
                seen.add(child)
                stack.append((child, iter(graph[child])))
    return order


def _components(graph: Mapping[str, Sequence[str]], order: list[str]) -> list[list[str]]:
    """Kosaraju's second pass: strongly connected components over the reversed graph."""
    reverse: dict[str, list[str]] = {node: [] for node in graph}
    for node, targets in graph.items():
        for target in targets:
            reverse[target].append(node)
    assigned: set[str] = set()
    components: list[list[str]] = []
    for root in reversed(order):
        if root in assigned:
            continue
        assigned.add(root)
        component, stack = [], [root]
        while stack:
            node = stack.pop()
            component.append(node)
            fresh = [n for n in reverse[node] if n not in assigned]
            assigned.update(fresh)
            stack.extend(fresh)
        components.append(component)
    return components


def _positive_finite(value: float) -> bool:
    return math.isfinite(value) and value > 0


def _restart_rules(index: _Index) -> Iterator[ScheduleIssue]:
    for i, machine in enumerate(index.problem.machines):
        rule = machine.restart
        if rule is None:
            continue
        if not (
            _positive_finite(rule.idle_threshold_seconds) and _positive_finite(rule.warmup_seconds)
        ):
            yield _issue(
                "restart_rule",
                f"problem.machines[{i}].restart",
                f"machine {machine.id}: restart seconds must be finite and > 0",
            )


def _calendar_order(index: _Index) -> Iterator[ScheduleIssue]:
    for i, calendar in enumerate(index.problem.calendars):
        yield from _calendar_interval_issues(calendar, f"problem.calendars[{i}].intervals")


def _calendar_interval_issues(calendar: Calendar, path: str) -> Iterator[ScheduleIssue]:
    """Intervals must be finite, start < end, sorted and disjoint (D-065)."""
    previous_end = -math.inf
    for k, (start, end) in enumerate(calendar.intervals):
        finite = math.isfinite(start) and math.isfinite(end)
        if not (finite and previous_end <= start < end):
            message = f"calendar {calendar.id}: intervals must be sorted, disjoint, start < end"
            yield _issue("calendar_order", f"{path}[{k}]", message)
        previous_end = max(previous_end, end)


_PROBLEM_CHECKS: tuple[Callable[[_Index], Iterator[ScheduleIssue]], ...] = (
    _duplicate_ids,
    _resource_references,
    _cycles,
    _restart_rules,
    _calendar_order,
)


# --- operation checks -----------------------------------------------------------------------


def _operation_references(index: _Index, path: str, op: Operation) -> Iterator[ScheduleIssue]:
    wc_path = f"{path}.work_center_id"
    yield from _missing(index.work_centers, op.work_center_id, wc_path, "work center")
    for k, link in enumerate(op.predecessors):
        link_path = f"{path}.predecessors[{k}].operation_id"
        yield from _missing(index.op_by_id, link.operation_id, link_path, "operation")
    for k, option in enumerate(op.options):
        option_path = f"{path}.options[{k}].machine_id"
        yield from _missing(index.machines, option.machine_id, option_path, "machine")
    for k, need in enumerate(op.operator_needs):
        need_path = f"{path}.operator_needs[{k}].pool_id"
        yield from _missing(index.pools, need.pool_id, need_path, "operator pool")
    pin_machine = op.pin.machine_id if op.pin else None
    yield from _missing(index.machines, pin_machine, f"{path}.pin.machine_id", "machine")
    status_path = f"{path}.status.machine_id"
    yield from _missing(index.machines, op.status.machine_id, status_path, "machine")


def _options_outside_work_center(
    index: _Index, path: str, op: Operation
) -> Iterator[ScheduleIssue]:
    for k, option in enumerate(op.options):
        machine = index.machines.get(option.machine_id)
        if machine is not None and machine.work_center_id != op.work_center_id:
            yield _issue(
                "option_outside_work_center",
                f"{path}.options[{k}].machine_id",
                f"machine {machine.id} is not in work center {op.work_center_id}",
            )


def _no_options(index: _Index, path: str, op: Operation) -> Iterator[ScheduleIssue]:
    if not op.options:
        yield _issue("no_options", f"{path}.options", f"operation {op.id} has no machine option")


def _lag_bounds(index: _Index, path: str, op: Operation) -> Iterator[ScheduleIssue]:
    for k, link in enumerate(op.predecessors):
        if link.min_lag < 0 or (link.max_lag is not None and link.max_lag < link.min_lag):
            yield _issue(
                "lag_bounds",
                f"{path}.predecessors[{k}]",
                f"need 0 <= min_lag <= max_lag after {link.operation_id}",
            )


def _overlaps(index: _Index, path: str, op: Operation) -> Iterator[ScheduleIssue]:
    for k, link in enumerate(op.predecessors):
        predecessor = index.op_by_id.get(link.operation_id)
        reason = _overlap_problem(link, op, predecessor)
        if reason is not None:
            overlap_path = f"{path}.predecessors[{k}].overlap_quantity"
            yield _issue("overlap_requires_forbid", overlap_path, reason)


def _overlap_problem(link: Precedence, op: Operation, predecessor: Operation | None) -> str | None:
    """Why a transfer-lot overlap is invalid: both ops must be `forbid`, q <= the lot (D-061)."""
    if link.overlap_quantity is None or predecessor is None:
        return None
    if "pause" in (op.shift_crossing, predecessor.shift_crossing):
        return f"overlap from {predecessor.id} needs shift_crossing forbid on both operations"
    if link.overlap_quantity > predecessor.quantity:
        return f"overlap_quantity exceeds the lot of {predecessor.id}"
    return None


def _batch_capacity(index: _Index, machine_id: str) -> BatchCapacity | None:
    machine = index.machines.get(machine_id)
    return machine.batch if machine is not None else None


def _batch_rules(index: _Index, path: str, op: Operation) -> Iterator[ScheduleIssue]:
    """A batch op needs a batch machine option, run time independent of quantity, and a lot
    that fits the capacity."""
    if op.batch_family is None:
        return
    has_batch_option = False
    for k, option in enumerate(op.options):
        batch = _batch_capacity(index, option.machine_id)
        if batch is None:
            continue
        has_batch_option = True
        if option.unit_seconds != 0:
            message = f"batch option {option.machine_id} needs unit_seconds 0"
            yield _issue("batch_rules", f"{path}.options[{k}].unit_seconds", message)
        if op.quantity > batch.capacity:
            message = f"quantity exceeds batch capacity of {option.machine_id}"
            yield _issue("batch_rules", f"{path}.quantity", message)
    if not has_batch_option:
        message = f"batch_family {op.batch_family} but no option machine batches"
        yield _issue("batch_rules", f"{path}.batch_family", message)


def _option_machines(op: Operation) -> set[str]:
    return {option.machine_id for option in op.options}


def _pin_outside_options(index: _Index, path: str, op: Operation) -> Iterator[ScheduleIssue]:
    machine_id = op.pin.machine_id if op.pin else None
    if machine_id is not None and machine_id not in _option_machines(op):
        message = f"pinned machine {machine_id} is not an option of {op.id}"
        yield _issue("pin_outside_options", f"{path}.pin.machine_id", message)


def _pin_window(index: _Index, path: str, op: Operation) -> Iterator[ScheduleIssue]:
    pin = op.pin
    if pin is None or pin.earliest_start is None or pin.latest_start is None:
        return
    if pin.earliest_start > pin.latest_start:
        yield _issue("pin_window", f"{path}.pin", "pin earliest_start is after latest_start")


def _in_progress_machine(index: _Index, path: str, op: Operation) -> Iterator[ScheduleIssue]:
    status = op.status
    if status.state != "in_progress":
        return
    if status.remaining_seconds is None or status.machine_id not in _option_machines(op):
        message = "in-progress operation needs remaining_seconds and an option machine_id"
        yield _issue("in_progress_machine", f"{path}.status", message)


_OPERATION_CHECKS: tuple[Callable[[_Index, str, Operation], Iterator[ScheduleIssue]], ...] = (
    _operation_references,
    _options_outside_work_center,
    _no_options,
    _lag_bounds,
    _overlaps,
    _batch_rules,
    _pin_outside_options,
    _pin_window,
    _in_progress_machine,
)
