"""B-26: the run view model (DATA-CONTRACT.md 2), built from one run folder alone.

Reads `intervals.json`, `run_meta.json`, `plan.json` and `run_detail.json` (written by
`twinflow run`), plus the newest sweep linked to this run (`sweep_meta.json` with
`base_run_id`, G6). Picks a display time unit (G7) and writes the sentences (G8).

Words follow DESIGN.md 8.5: test run, likely range, choke point, busy.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from twinflow.report.levers import lever_label, lever_rows, plain
from twinflow.report.run_detail import DETAIL_FILE, PLAN_FILE

TimeUnit = Literal["minutes", "hours", "days"]
_DIVISOR = {"minutes": 60.0, "hours": 3600.0, "days": 86400.0}
_SHORT = {"minutes": "min", "hours": "h", "days": "days"}
_ONE = {"minutes": "minute", "hours": "hour", "days": "day"}
CHOKE = 0.95
NEXT = 0.75
LATE = 0.5
RISK = 0.2
SWEEP_META = "sweep_meta.json"
SUGGESTED_REPS = 30


def time_unit(horizon_s: float, override: str | None = None) -> TimeUnit:
    """G7: under 3 hours minutes, under 3 days hours, else days."""
    if override in _DIVISOR:
        return override  # type: ignore[return-value]
    if horizon_s < 3 * 3600:
        return "minutes"
    if horizon_s < 3 * 86400:
        return "hours"
    return "days"


def display_id(order_id: str) -> str:
    """`wo-9` reads as `WO-9`. Other ids stay as written."""
    return order_id.upper() if re.fullmatch(r"[a-z]{1,4}-?\d+", order_id) else order_id


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _r(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _count_words(n: int) -> str:
    return "one" if n == 1 else str(n)


class _Builder:
    def __init__(self, run_dir: Path, unit_override: str | None) -> None:
        self.run_dir = run_dir
        self.intervals: dict[str, Any] = _read(run_dir / "intervals.json") or {}
        self.meta: dict[str, Any] = _read(run_dir / "run_meta.json") or {}
        self.detail: dict[str, Any] = _read(run_dir / DETAIL_FILE) or {}
        plan = _read(run_dir / PLAN_FILE) or {}
        self.plan: list[dict[str, Any]] = list(plan.get("orders", []))
        self.reps = int(self.intervals.get("reps") or self.detail.get("reps") or 1)
        self.horizon_s = float(self.detail.get("horizon_s") or self._horizon_guess())
        self.unit = time_unit(self.horizon_s, unit_override)
        self.short = _SHORT[self.unit]
        labels = self.detail.get("labels") or {}
        self.centre_labels: dict[str, str] = dict(labels.get("centres") or {})
        self.part_labels: dict[str, str] = dict(labels.get("parts") or {})

    # -- helpers -------------------------------------------------------------

    def _horizon_guess(self) -> float:
        finishes = [iv["hi"] for iv in (self.intervals.get("completion_by_order") or {}).values()]
        return max(finishes, default=3600.0)

    def t(self, seconds: float | None) -> float | None:
        return None if seconds is None else round(seconds / _DIVISOR[self.unit], 3)

    def centre_label(self, centre: str) -> str:
        return self.centre_labels.get(centre) or plain(centre).capitalize()

    def part_label(self, part: str) -> str:
        return self.part_labels.get(part) or plain(part)

    def lower(self, label: str) -> str:
        return label[:1].lower() + label[1:] if not label[:2].isupper() else label

    def amount(self, value: float, digits: int) -> str:
        text = f"{abs(value):.{digits}f}"
        return f"{text} {self.short}"

    # -- sections ------------------------------------------------------------

    def centres(self) -> list[dict[str, Any]]:
        util = self.intervals.get("utilization_by_cell") or {}
        waits = (self.detail.get("per_rep") or {}).get("blocked_starved_by_location") or {}
        queues = self.detail.get("queue_wait_s") or {}
        ids = sorted(set(util) | set(waits) | set(queues))
        busiest = max(ids, key=lambda c: util.get(c, {}).get("mean", 0.0), default=None)
        rows = []
        for cid in ids:
            iv = util.get(cid) or {"mean": 0.0, "lo": 0.0, "hi": 0.0}
            busy = float(iv["mean"])
            status = (
                "choke" if cid == busiest and busy >= CHOKE else "next" if busy >= NEXT else "ok"
            )
            rows.append(
                {
                    "id": cid,
                    "label": self.centre_label(cid),
                    "busy": {"mean": _r(busy), "low": _r(iv["lo"]), "high": _r(iv["hi"])},
                    "blocked": self.t(waits.get(cid, {}).get("blocked", 0.0)),
                    "starved": self.t(waits.get(cid, {}).get("starved", 0.0)),
                    "queue_wait": self.t(queues.get(cid, 0.0)),
                    "status": status,
                }
            )
        rows.sort(key=lambda r: -(r["busy"]["mean"] or 0.0))
        return rows

    def orders(self, choke: str | None) -> list[dict[str, Any]]:
        completion = self.intervals.get("completion_by_order") or {}
        per_rep = self.detail.get("per_rep") or {}
        states = per_rep.get("due_state_by_order") or {}
        steps = self.detail.get("steps") or {}
        rows = []
        plan = self.plan or [
            {"id": oid, "part": "", "qty": 0, "release": 0.0, "due": None} for oid in completion
        ]
        for order in plan:
            oid = str(order["id"])
            iv = completion.get(oid)
            due_s = order.get("due")
            finish = (
                {"mean": self.t(iv["mean"]), "low": self.t(iv["lo"]), "high": self.t(iv["hi"])}
                if iv
                else None
            )
            seen = [s for s in states.get(oid, []) if s != "not_yet_due"]
            late_chance = (
                round(sum(s == "late" for s in seen) / len(seen), 3)
                if self.reps > 1 and seen and due_s is not None
                else None
            )
            finished_runs = int(iv["n"]) if iv else 0
            room = (
                None
                if due_s is None or finish is None
                else round(self.t(due_s) - finish["mean"], 3)  # type: ignore[operator]
            )
            if finished_runs * 2 < self.reps or finish is None:
                status = "unfinished"
            elif due_s is None:
                status = "ok"
            elif late_chance is None:
                status = "late" if (room or 0.0) < 0 else "ok"
            else:
                status = "late" if late_chance >= LATE else "risk" if late_chance >= RISK else "ok"
            order_steps = [
                {
                    "centre": s["centre"],
                    "label": self.centre_label(s["centre"]),
                    "work": self.t(s["work_s"]),
                    "wait": self.t(s["wait_s"]),
                }
                for s in steps.get(oid, [])
            ]
            row: dict[str, Any] = {
                "id": oid,
                "part": str(order.get("part", "")),
                "qty": int(order.get("qty") or 0),
                "release": self.t(order.get("release") or 0.0),
                "due": self.t(due_s),
                "finish": finish,
                "room": room,
                "late_chance": late_chance,
                "status": status,
                "reason": self.reason(status, room, order_steps, choke),
            }
            if order_steps:
                row["steps"] = order_steps
            rows.append(row)
        rows.sort(
            key=lambda r: (
                r["status"] != "unfinished",
                -(r["late_chance"] if r["late_chance"] is not None else -1.0),
                r["room"] if r["room"] is not None else float("inf"),
            )
        )
        return rows

    def longest_wait(self, steps: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        waited = [s for s in steps if (s["wait"] or 0) > 0]
        return max(waited, key=lambda s: s["wait"]) if waited else None

    def reason(
        self,
        status: str,
        room: float | None,
        steps: Sequence[Mapping[str, Any]],
        choke: str | None,
    ) -> str:
        where = self.longest_wait(steps)
        name = self.lower(str(where["label"])) if where else None
        if status == "unfinished":
            return "It did not finish in most test runs."
        if room is None:
            return "No due time, so it cannot be late."
        if status == "late":
            first = (
                f"Likely {self.amount(room, 1)} late." if room < 0 else "Late in most test runs."
            )
            return f"{first} It waits in line at the {name}." if name else first
        if status == "risk":
            first = (
                f"Only {self.amount(room, 1)} of room."
                if room >= 0
                else f"Likely {self.amount(room, 1)} late."
            )
            return f"{first} It waits in line at the {name}." if name else first
        first = f"Done {self.amount(room, 0)} early."
        if where and where["centre"] != choke:
            return f"{first} The {name} has room."
        return first

    def level(self, orders: Sequence[Mapping[str, Any]], on_time: float) -> str:
        if not orders or all(o["due"] is None for o in orders):
            return "unknown"
        if all(o["status"] == "ok" for o in orders):
            return "all_on_time"
        if on_time < 0.7:
            return "many_late"
        return "most_on_time"

    def answer(
        self, orders: list[dict[str, Any]], centres: list[dict[str, Any]], late_per_run: float
    ) -> dict[str, Any]:
        iv = self.intervals.get("on_time_pct") or {"mean": 0.0, "lo": 0.0, "hi": 0.0}
        on_time = {
            "mean": _r(iv["mean"] / 100.0),
            "low": _r(max(0.0, iv["lo"]) / 100.0),
            "high": _r(min(100.0, iv["hi"]) / 100.0),
        }
        level = self.level(orders, on_time["mean"] or 0.0)
        watch = [o["id"] for o in orders if o["status"] in ("late", "risk", "unfinished")]
        choke_row = next((c for c in centres if c["status"] == "choke"), None)
        choke = (
            {
                "centre": choke_row["id"],
                "label": choke_row["label"],
                "busy": choke_row["busy"]["mean"],
            }
            if choke_row
            else None
        )
        total = len(orders)
        if level == "unknown":
            headline = "No due dates, so nothing can be late."
        elif level == "all_on_time":
            headline = f"Yes. All {total} orders have room."
        elif level == "many_late":
            late = sum(o["status"] in ("late", "unfinished") for o in orders) or round(
                (1 - (on_time["mean"] or 0)) * total
            )
            headline = f"No. {late} of {total} orders are likely late."
        else:
            headline = f"Most will. Watch {display_id(watch[0])}." if watch else "Most will."
        result: dict[str, Any] = {
            "level": level,
            "headline": headline,
            "lede": self.lede(orders, watch, on_time["mean"] or 0.0),
            "on_time": on_time,
            "late_orders_per_run": round(late_per_run, 1),
            "watch": watch,
            "choke": choke,
        }
        if self.reps == 1:
            result["command"] = self.rerun_command()
        return result

    def rerun_command(self) -> str:
        model = self.detail.get("model_path", "model.yaml")
        plan = self.detail.get("plan_path", "plan.csv")
        return f"twinflow run {model} --plan {plan} --reps {SUGGESTED_REPS}"

    def lede(self, orders: Sequence[Mapping[str, Any]], watch: Sequence[str], share: float) -> str:
        total = len(orders)
        if total == 0:
            return "This plan has no orders."
        if all(o["due"] is None for o in orders):
            return "Add a due date to each order to see which ones are at risk."
        done = round(share * total)
        if self.reps == 1:
            return (
                f"In this one test run, {done} of {total} orders finished on time. "
                f"This is one test run. Run {SUGGESTED_REPS} to see how sure this is."
            )
        parts = [f"In {self.reps} test runs, about {done} of {total} orders finished on time."]
        by_id = {o["id"]: o for o in orders}
        if watch:
            first = by_id[watch[0]]
            chance = first["late_chance"]
            if chance is not None and chance > 0:
                late_runs = round(chance * self.reps)
                when = (
                    "more than half of the runs"
                    if chance > 0.5
                    else "half of the runs"
                    if chance == 0.5
                    else f"{late_runs} of {self.reps} runs"
                )
                parts.append(f"{display_id(first['id'])} was late in {when}.")
            parts.append(self.shared_cause([by_id[w] for w in watch]))
        else:
            parts.append("Every order has room.")
        return " ".join(p for p in parts if p)

    def shared_cause(self, risky: Sequence[Mapping[str, Any]]) -> str:
        parts_set = {o["part"] for o in risky}
        waits = {
            w["centre"]: w["label"]
            for o in risky
            if (w := self.longest_wait(o.get("steps") or [])) is not None
        }
        same_place = len(waits) == 1 and all(o.get("steps") for o in risky)
        place = self.lower(next(iter(waits.values()))) if same_place else None
        if len(risky) > 1 and len(parts_set) == 1:
            part = self.part_label(next(iter(parts_set)))
            if place:
                return f"Every order at risk is a {part}, and every {part} waits for the {place}."
            return f"Every order at risk is a {part}."
        if place:
            return (
                f"They wait longest at the {place}."
                if len(risky) > 1
                else (f"It waits longest at the {place}.")
            )
        return ""

    def levers(
        self, today_on_time: float, today_late: float, choke: Mapping[str, Any] | None
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        sweep_dir, meta = self.linked_sweep()
        rows: list[dict[str, Any]] = []
        sweep: dict[str, Any] | None = None
        base_values: dict[str, Any] = {}
        if sweep_dir is not None and meta is not None:
            summary = _read(sweep_dir / "summary.json") or []
            base_values = dict(meta.get("base_values") or {})
            rows, _today = lever_rows(
                summary,
                base_values,
                centre_labels=self.centre_labels,
                today_on_time=today_on_time,
                today_late=today_late,
            )
            sweep = {"run_id": sweep_dir.name, "reps": meta.get("reps")}
        rows += self.untested(choke, rows, base_values)
        return rows, sweep

    def untested(
        self,
        choke: Mapping[str, Any] | None,
        tested: Sequence[Mapping[str, Any]],
        base_values: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        if choke is None:
            return []
        centre = str(choke["centre"])
        capacity = int((self.detail.get("capacity") or {}).get(centre, 1))
        ideas: list[tuple[str, Any, list[Any]]] = [
            (f"locations[{centre}].capacity", capacity, [capacity, capacity + 1]),
            (f"locations[{centre}].dispatch", "fifo", ["fifo", "edd"]),
        ]
        done = {i.split("=")[0] for r in tested for i in str(r["id"]).split(",")}
        out = []
        for path, today, values in ideas:
            if path in done or base_values.get(path, today) != today:
                continue
            spec = json.dumps({path: values})
            model = self.detail.get("model_path", "model.yaml")
            plan = self.detail.get("plan_path", "plan.csv")
            reps = max(self.reps, SUGGESTED_REPS)
            out.append(
                {
                    "id": f"{path}={values[-1]}",
                    "label": lever_label(path, values[-1], today, self.centre_labels),
                    "tested": False,
                    "on_time": None,
                    "gain_points": None,
                    "late_orders_per_run": None,
                    "note": f"Not tested yet. Save {spec} as sweep.json, then run the command.",
                    "command": (
                        f"twinflow balance {model} --plan {plan} --sweep sweep.json "
                        f"--reps {reps} --run {self.run_dir.name}"
                    ),
                }
            )
        return out

    def linked_sweep(self) -> tuple[Path | None, dict[str, Any] | None]:
        """The newest sweep whose `sweep_meta.json` names this run as its base."""
        found: list[tuple[str, Path, dict[str, Any]]] = []
        for meta_path in self.run_dir.parent.glob(f"*/{SWEEP_META}"):
            meta = _read(meta_path)
            if isinstance(meta, dict) and meta.get("base_run_id") == self.run_dir.name:
                found.append((meta_path.parent.name, meta_path.parent, meta))
        if not found:
            return None, None
        _name, path, meta = max(found, key=lambda f: f[0])
        return path, meta

    def takeaways(
        self,
        orders: Sequence[Mapping[str, Any]],
        centres: Sequence[Mapping[str, Any]],
        watch: Sequence[str],
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        by_id = {o["id"]: o for o in orders}
        risky = [by_id[w] for w in watch if by_id[w]["status"] != "unfinished"]
        dated = [o for o in orders if o["room"] is not None]
        if risky:
            parts_set = {o["part"] for o in risky}
            first = (
                "The risk is all in one place."
                if len(parts_set) == 1
                else "The risk is spread out."
            )
            text = [first]
            cause = self.shared_cause(risky)
            if len(parts_set) == 1:
                part = next(iter(parts_set))
                release = min(o["release"] or 0.0 for o in risky)
                place = self.longest_wait(risky[0].get("steps") or [])
                if place is not None:
                    text.append(
                        f"{self.part_label(part).capitalize()} orders released at "
                        f"{_ONE[self.unit]} {release:.0f} or later queue at the "
                        f"{self.lower(str(place['label']))}."
                    )
                safe = [o for o in dated if o["part"] != part and o["status"] == "ok"]
                if safe:
                    lo = min(o["room"] for o in safe)
                    hi = max(o["room"] for o in safe)
                    others = sorted({self.part_label(o["part"]) for o in safe})
                    who = others[0].capitalize() if len(others) == 1 else "Other"
                    text.append(f"{who} orders finish {lo:.0f} to {hi:.0f} {self.short} early.")
            elif cause:
                text.append(cause)
            out["orders"] = " ".join(text)
        elif dated:
            closest = min(o["room"] for o in dated)
            out["orders"] = (
                f"No order is at risk. The closest has {self.amount(closest, 1)} of room."
            )
        choke = next((c for c in centres if c["status"] == "choke"), None)
        nxt = next((c for c in centres if c["status"] == "next"), None)
        if choke:
            text = [
                f"The {self.lower(choke['label'])} is the choke point.",
                f"It is busy {round((choke['busy']['mean'] or 0) * 100)}% of the time.",
            ]
            if nxt:
                busy = nxt["busy"]["mean"] or 0.0
                growth = 1.0 / busy - 1.0 if busy else 0.0
                feeds = self.parts_through(nxt["id"])
                who = f"{self.part_label(feeds[0])} orders" if len(feeds) == 1 else "its work"
                text.append(
                    f"The {self.lower(nxt['label'])} is next at {round(busy * 100)}% and will "
                    f"choke if {who} grow by {_fraction(growth)}."
                )
            out["choke"] = " ".join(text)
        elif centres:
            top = centres[0]
            out["choke"] = (
                f"No step is a choke point. The busiest is the {self.lower(top['label'])} "
                f"at {round((top['busy']['mean'] or 0) * 100)}%."
            )
        return out

    def parts_through(self, centre: str) -> list[str]:
        return [r["part"] for r in self.detail.get("routes") or [] if centre in r["steps"]]

    def details(self, centres: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        first = dict(self.detail.get("first_run") or {})
        if not first:
            return {}
        unit = self.t
        return {
            "completed": first.get("completed"),
            "unfinished": first.get("unfinished"),
            "on_time_pct": _r(first.get("on_time_pct"), 1),
            "run_hours": _r(first.get("run_hours")),
            "setup_hours": _r(first.get("setup_hours")),
            "machine_hours": _r(sum((first.get("machine_hours_by_machine") or {}).values())),
            "orders": [
                {
                    "id": oid,
                    "finish": unit(done),
                    "late_by": unit((first.get("lateness_by_order") or {}).get(oid)),
                }
                for oid, done in sorted((first.get("completion_by_order") or {}).items())
            ],
            "centres": [
                {
                    "id": cid,
                    "label": self.centre_label(cid),
                    "busy": _r(min(busy, 1.0)),
                    "starved": unit(
                        first["wait_seconds_by_location"].get(cid, {}).get("starved", 0.0)
                    ),
                    "blocked": unit(
                        first["wait_seconds_by_location"].get(cid, {}).get("blocked", 0.0)
                    ),
                    "cycle": unit((first.get("cycle_seconds_by_location") or {}).get(cid)),
                    "jobs": (first.get("event_counts_by_location") or {}).get(cid, 0),
                    "wip_peak": (first.get("wip_peak_by_location") or {}).get(cid, 0),
                }
                for cid, busy in sorted(
                    (first.get("utilization_by_cell") or {}).items(), key=lambda kv: -kv[1]
                )
            ],
            "machine_hours_by_machine": [
                {"machine": m, "hours": _r(h)}
                for m, h in sorted((first.get("machine_hours_by_machine") or {}).items())
            ],
            "machine_pools": [
                {
                    "resource": r["resource"],
                    "capacity": r["capacity"],
                    "steps": r["steps"],
                    "busy_hours": _r(r["busy_hours"]),
                    "busy": _r(r["util_wall"]),
                }
                for r in first.get("machine_pools") or []
            ],
            "labor_pools": [
                {
                    "resource": r["resource"],
                    "capacity": r["capacity"],
                    "skills": r["skills"],
                    "busy_hours": _r(r["busy_hours"]),
                    "operating_hours": _r(r["operating_hours"]),
                    "busy_on_shift": _r(r["util_operating"]),
                    "busy": _r(r["util_wall"]),
                }
                for r in first.get("labor_pools") or []
            ],
        }

    def wip(self) -> dict[str, Any] | None:
        points = (self.detail.get("first_run") or {}).get("wip") or []
        if not points:
            return None
        times = sorted({float(p["t"]) for p in points})
        index = {t: i for i, t in enumerate(times)}
        by_centre: dict[str, list[float]] = {}
        for p in points:
            series = by_centre.setdefault(p["location_id"], [None] * len(times))  # type: ignore[list-item]
            series[index[float(p["t"])]] = p["wip"]
        for series in by_centre.values():
            last = 0.0
            for i, v in enumerate(series):
                last = v if v is not None else last
                series[i] = last
        return {"t": [self.t(t) for t in times], "by_centre": by_centre}

    def assumptions(self) -> list[str]:
        """One line per kind of default. The same default for many locations becomes one
        line that counts them and names them (as the old report grouped them)."""
        groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for a in self.detail.get("assumptions") or []:
            why = str(a["why_absent"])
            pattern = re.sub(r"for location '.*?'", "for a location", why)
            name = re.search(r"for location '(.*?)'", why)
            groups.setdefault((str(a["default_used"]), pattern), []).append(
                (why, name.group(1) if name else why)
            )
        out = []
        for (used, pattern), items in sorted(groups.items()):
            head = used[:1].upper() + used[1:]
            if len(items) == 1:
                out.append(f"{head}: {items[0][0]}.")
            else:
                names = ", ".join(sorted(n for _w, n in items))
                out.append(f"{head}: {pattern} ({len(items)} locations: {names}).")
        return out

    # -- the model -------------------------------------------------------------

    def build(self) -> dict[str, Any]:
        centres = self.centres()
        choke_id = next((c["id"] for c in centres if c["status"] == "choke"), None)
        orders = self.orders(choke_id)
        late_runs = (self.detail.get("per_rep") or {}).get("late_orders") or []
        late_per_run = (
            mean(late_runs) if late_runs else float(sum(o["status"] == "late" for o in orders))
        )
        answer = self.answer(orders, centres, late_per_run)
        levers, sweep = self.levers(
            answer["on_time"]["mean"] or 0.0, answer["late_orders_per_run"], answer["choke"]
        )
        project = str(self.detail.get("project") or self.run_dir.name)
        unfinished = [o for o in orders if o["status"] == "unfinished"]
        warnings = []
        if unfinished:
            n = len(unfinished)
            warnings.append(
                f"{_count_words(n).capitalize()} order{'s' if n != 1 else ''} did not finish. "
                "Likely cause: the run ended first, or scrap at inspection."
            )
        if not self.plan:
            warnings.append("This run has no plan copy, so release and due times are missing.")
        model: dict[str, Any] = {
            "view": "run",
            "schema_version": 1,
            "about": {
                "title": f"Run viewer: {project}",
                "project": project,
                "run_id": self.run_dir.name,
                "reps": self.reps,
                "seed": int(self.meta.get("base_seed", 0) or 0),
                "interval": float(self.intervals.get("level") or 0.9),
                "time_unit": self.unit,
                "horizon": self.t(self.horizon_s),
                "start": self.detail.get("start"),
                "method": (
                    f"{self.reps} test runs of the same plan with different random "
                    "machine times. Ranges are likely ranges of the mean."
                    if self.reps > 1
                    else "One test run of the plan. Run more to see ranges."
                ),
                "assumptions": self.assumptions(),
                "engine_version": self.meta.get("engine_version"),
                "model_hash": self.meta.get("model_hash"),
                "plan_hash": self.meta.get("plan_hash"),
            },
            "answer": answer,
            "orders": orders,
            "centres": centres,
            "routes": [dict(r) for r in self.detail.get("routes") or []],
            "levers": levers,
            "sweep": sweep,
            "takeaways": self.takeaways(orders, centres, answer["watch"]),
            "details": self.details(centres),
            "agent_summary": [],
            "warnings": warnings,
            "problems": [],
        }
        wip = self.wip()
        if wip is not None:
            model["wip_by_time"] = wip
        labor = (self.detail.get("first_run") or {}).get("labor_hours")
        if labor:
            model["labor"] = [{**row, "hours": _r(row["hours"])} for row in labor]
        model["agent_summary"] = self.summary(model)
        return model

    def summary(self, model: Mapping[str, Any]) -> list[str]:
        answer = model["answer"]
        lines = [answer["headline"], answer["lede"]]
        if model["takeaways"].get("choke"):
            lines.append(model["takeaways"]["choke"])
        tested = [lv for lv in model["levers"] if lv["tested"]]
        if tested and (tested[0]["gain_points"] or 0) > 0:
            best = tested[0]
            lines.append(
                f"Best tested change: {best['label']}. "
                f"{round((answer['on_time']['mean'] or 0) * 100)}% to "
                f"{round(best['on_time'] * 100)}% on time."
            )
        elif model["levers"]:
            idea = model["levers"][0]
            lines.append(f"No fixes tested yet. Test one with: {idea.get('command', '')}")
        return [line for line in lines if line]


def _fraction(share: float) -> str:
    """How much more work fits, in words: 0.2 -> "a fifth"."""
    words = [
        (0.5, "a half"),
        (1 / 3, "a third"),
        (0.25, "a quarter"),
        (0.2, "a fifth"),
        (0.1, "a tenth"),
    ]
    for value, word in words:
        if abs(share - value) <= value * 0.12:
            return word
    return f"{max(1, round(share * 100))}%"


def run_view_model(run_dir: str | Path, *, unit: str | None = None) -> dict[str, Any]:
    """The run viewer's data (DATA-CONTRACT.md 2) from one run folder."""
    return _Builder(Path(run_dir), unit).build()


__all__ = ["display_id", "run_view_model", "time_unit"]
