"""Pure schedule mutations for the verifier tests (COMP-012, REQ-077).

Each helper takes a `Schedule` and returns a changed copy; the input is never touched.
Helpers rebuild assignments through the `Assignment` constructor, so a mutation that
breaks `start <= setup_end <= end` fails loudly in the test instead of producing a
schedule the model would reject. `with_nonfinite` is the one exception: it uses
`model_construct` on purpose, to forge a value validation would refuse.

Import it as `from mutations import ...` (pytest puts `tests/scheduling/` on `sys.path`).
"""

from __future__ import annotations

import math

from twinflow.scheduling.model import Assignment, Schedule

AssignmentValue = float | str | None


def assignment_of(schedule: Schedule, operation_id: str) -> Assignment:
    """The single assignment of `operation_id`; KeyError when it has none."""
    for assignment in schedule.assignments:
        if assignment.operation_id == operation_id:
            return assignment
    raise KeyError(operation_id)


def _with_assignments(schedule: Schedule, assignments: tuple[Assignment, ...]) -> Schedule:
    return schedule.model_copy(update={"assignments": assignments})


def replace(schedule: Schedule, operation_id: str, **changes: AssignmentValue) -> Schedule:
    """Replace fields of one assignment, validated by the `Assignment` constructor."""
    original = assignment_of(schedule, operation_id)
    changed = Assignment.model_validate(original.model_dump() | changes)
    return _with_assignments(
        schedule,
        tuple(changed if a is original else a for a in schedule.assignments),
    )


def shift(schedule: Schedule, operation_id: str, delta: float) -> Schedule:
    """Move one assignment in time by `delta` seconds, keeping its setup and run lengths."""
    a = assignment_of(schedule, operation_id)
    return replace(
        schedule,
        operation_id,
        start=a.start + delta,
        setup_end=a.setup_end + delta,
        end=a.end + delta,
    )


def move_to_machine(schedule: Schedule, operation_id: str, machine_id: str) -> Schedule:
    """Put one assignment on another machine at the same times."""
    return replace(schedule, operation_id, machine_id=machine_id)


def shorten_run(schedule: Schedule, operation_id: str, seconds: float) -> Schedule:
    """End the run `seconds` earlier; start and setup_end stay."""
    a = assignment_of(schedule, operation_id)
    return replace(schedule, operation_id, end=a.end - seconds)


def shorten_setup(schedule: Schedule, operation_id: str, seconds: float) -> Schedule:
    """Start `seconds` later; setup_end and end stay, so the setup phase is shorter."""
    a = assignment_of(schedule, operation_id)
    return replace(schedule, operation_id, start=a.start + seconds)


def drop(schedule: Schedule, operation_id: str) -> Schedule:
    """Remove the assignment of `operation_id`."""
    assignment_of(schedule, operation_id)
    kept = tuple(a for a in schedule.assignments if a.operation_id != operation_id)
    return _with_assignments(schedule, kept)


def add(schedule: Schedule, assignment: Assignment) -> Schedule:
    """Append one assignment."""
    return _with_assignments(schedule, (*schedule.assignments, assignment))


def join_batch(schedule: Schedule, operation_id: str, member_id: str) -> Schedule:
    """Give one assignment the machine, times and batch id of another (a batch member)."""
    member = assignment_of(schedule, member_id)
    return replace(
        schedule,
        operation_id,
        machine_id=member.machine_id,
        start=member.start,
        setup_end=member.setup_end,
        end=member.end,
        batch_id=member.batch_id,
    )


def reversed_order(schedule: Schedule) -> Schedule:
    """The same assignments listed in reverse order (times unchanged)."""
    return _with_assignments(schedule, tuple(reversed(schedule.assignments)))


def with_primary(schedule: Schedule, value: float | None) -> Schedule:
    """Set the reported primary objective value (not validated: may be non-finite)."""
    return schedule.model_copy(update={"primary_value": value})


def with_nonfinite(
    schedule: Schedule, operation_id: str, field: str = "end", value: float = math.nan
) -> Schedule:
    """Forge a non-finite time on one assignment; bypasses validation via `model_construct`."""
    original = assignment_of(schedule, operation_id)
    forged = Assignment.model_construct(**(original.model_dump() | {field: value}))
    return _with_assignments(
        schedule,
        tuple(forged if a is original else a for a in schedule.assignments),
    )


def remove_warmup(schedule: Schedule, operation_id: str, seconds: float) -> Schedule:
    """Cut `seconds` of warm-up from one setup phase (D-074): start stays, run keeps its length.

    `setup_end` and `end` move `seconds` earlier. When the assignment is in a batch, every
    member of that batch on the same machine changes alike, so the batch stays aligned.
    """
    target = assignment_of(schedule, operation_id)
    changed = schedule
    for a in schedule.assignments:
        same_batch = (
            target.batch_id is not None
            and a.batch_id == target.batch_id
            and a.machine_id == target.machine_id
        )
        if a is target or same_batch:
            changed = replace(
                changed, a.operation_id, setup_end=a.setup_end - seconds, end=a.end - seconds
            )
    return changed
