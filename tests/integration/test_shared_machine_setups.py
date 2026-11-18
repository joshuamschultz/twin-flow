"""Steps that share one machine but declare different `setup_key`s (TF-006 engine fix).

A step's pull rule prefers jobs in the machine's current setup group (D-047). When two
steps share one machine, the other step can leave the machine in a group that none of
this step's jobs belong to. Before the fix, the step then saw "nothing eligible" and
slept until its next arrival, so its queued jobs never ran: no one held the machine and
no one waited for it. D-047 says a job outside the current setup pays the changeover;
it never says the job waits for ever.
"""

from __future__ import annotations

from pathlib import Path

from twinflow.instrumentation import compute_kpis
from twinflow.instrumentation.sweep import orders_frame
from twinflow.model import load_model, plan_origin
from twinflow.plan.driver import RunDriver
from twinflow.plan.loader import load_plan

_MODEL = """
defaults: {cycle_time_cv: 0.0}
stocks: []
part_types:
  - {name: a_raw, uom: piece, attributes: {}}
  - {name: b_raw, uom: piece, attributes: {}}
  - {name: a, uom: piece, attributes: {}}
  - {name: b, uom: piece, attributes: {}}
machines: [{name: saw}]
labor: {pools: [{name: p, skills: [s], headcount: 1}]}
processes: []
bom: []
locations:
  - {name: saw_a, consumes: [{thing: a_raw, qty: 1, uom: piece}],
     emits: [{thing: a, qty: 1, uom: piece}], setup_key: blade_a, changeover_seconds: 5,
     time_model: {kind: rate_based, rate: 0.1}, machine: saw, labor_skill: s}
  - {name: saw_b, consumes: [{thing: b_raw, qty: 1, uom: piece}],
     emits: [{thing: b, qty: 1, uom: piece}], setup_key: blade_b, changeover_seconds: 5,
     time_model: {kind: rate_based, rate: 0.1}, machine: saw, labor_skill: s}
routing:
  - {part: a, steps: [saw_a]}
  - {part: b, steps: [saw_b]}
"""

# wo-2 reaches saw_b after saw_a has left the idle saw set up for blade_a.
_PLAN = (
    "work_order_id,part,qty,start_date,due_date\n"
    "wo-1,a,1,0,100\n"
    "wo-2,b,1,50,200\n"
    "wo-3,a,1,100,300\n"
)


def _completions(tmp_path: Path) -> dict[str, float | None]:
    model_path = tmp_path / "shared.yaml"
    model_path.write_text(_MODEL, encoding="utf-8")
    plan_path = tmp_path / "plan.csv"
    plan_path.write_text(_PLAN, encoding="utf-8")
    compiled = load_model(str(model_path))
    work_orders = load_plan(str(plan_path), compiled.registry)
    result = RunDriver(compiled).run(work_orders, seed=0, replication_index=0)
    orders = orders_frame(work_orders, compiled, result.event_log_path)
    kpis = compute_kpis(result.event_log_path, orders, result.horizon)
    return dict(kpis.completion_by_order)


def test_a_job_outside_the_shared_machines_setup_still_runs(tmp_path: Path) -> None:
    completions = _completions(tmp_path)
    assert all(done is not None for done in completions.values()), completions


def test_switching_setup_groups_on_a_shared_machine_pays_the_changeover(
    tmp_path: Path,
) -> None:
    completions = _completions(tmp_path)
    # wo-2 arrives at 50 on a saw set for blade_a: 5 s changeover + 10 s run.
    assert completions["wo-2"] == 65.0
    # wo-3 arrives at 100 on a saw now set for blade_b: it changes back.
    assert completions["wo-3"] == 115.0


_SHOWCASE = Path(__file__).resolve().parents[2] / "examples" / "showcase" / "cnc-job-shop"


def test_the_cnc_job_shop_finishes_every_order_with_a_setup_per_part() -> None:
    """The showcase's band saw and CMM each serve three parts with their own setups."""
    compiled = load_model(str(_SHOWCASE / "shop.yaml"))
    keys_by_machine: dict[str, set[str]] = {}
    for spec in compiled.locations:
        keys_by_machine.setdefault(spec.machine.machine_id, set()).update(
            spec.pull_rule.setup_key_of.values()
        )
    assert len(keys_by_machine["band_saw"]) == 3
    assert len(keys_by_machine["cmm"]) == 3

    work_orders = load_plan(
        str(_SHOWCASE / "data" / "open-orders.csv"),
        compiled.registry,
        origin=plan_origin(compiled),
    )
    result = RunDriver(compiled).run(work_orders, seed=0, replication_index=0)
    orders = orders_frame(work_orders, compiled, result.event_log_path)
    kpis = compute_kpis(result.event_log_path, orders, result.horizon)
    assert len(kpis.completion_by_order) == 15
    assert all(done is not None for done in kpis.completion_by_order.values())
