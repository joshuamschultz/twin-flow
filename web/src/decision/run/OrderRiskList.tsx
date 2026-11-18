// "Which orders are at risk, and why?" (DESIGN.md 4.2). One bar per order to a due tick.
import { useState } from "react";
import type { RunOrder } from "../types";
import {
  axisTicks,
  displayId,
  pillText,
  plural,
  signedTime,
  sortOrders,
  unitShort,
  type TimeUnit,
} from "./runText";

type Tone = "gain" | "loss";

export function OrderTimeline({
  release,
  finish,
  low,
  high,
  due,
  tone,
  horizon,
  label,
}: {
  release: number;
  finish: number | null;
  low: number | null;
  high: number | null;
  due: number | null;
  tone: Tone;
  horizon: number;
  label: string;
}) {
  const x = (v: number) =>
    `${Math.max(0, Math.min(100, (v / horizon) * 100))}%`;
  const w = (a: number, b: number) =>
    `${Math.max(0, ((b - a) / horizon) * 100)}%`;
  return (
    <div className="rv-track" role="img" aria-label={label} title={label}>
      <div className="rv-track-line" />
      {finish != null && (
        <div
          className={`rv-bar rv-${tone}`}
          style={{ left: x(release), width: w(release, finish) }}
        />
      )}
      {low != null && high != null && (
        <div
          className="rv-whisker"
          style={{ left: x(low), width: w(low, high) }}
        />
      )}
      {due != null && <div className="rv-due" style={{ left: x(due) }} />}
    </div>
  );
}

export function OrderSteps({
  order,
  unit,
}: {
  order: RunOrder;
  unit: TimeUnit;
}) {
  const steps = order.steps ?? [];
  if (!steps.length)
    return <p className="rv-note">No step times for this order.</p>;
  const total = Math.max(...steps.map((s) => s.work + s.wait), 1e-9);
  const u = unitShort(unit);
  return (
    <div
      className="rv-steps"
      aria-label={`Time at each step for ${displayId(order.id)}`}
    >
      <div className="rv-legend">
        <span>
          <i className="rv-gain" />
          Work
        </span>
        <span>
          <i className="rv-loss" />
          Wait in line
        </span>
      </div>
      {steps.map((s) => (
        <div className="rv-step-row" key={s.centre}>
          <span>{s.label}</span>
          <div className="rv-stack">
            <i
              className="rv-gain"
              style={{ width: `${(s.work / total) * 100}%` }}
            />
            <i
              className="rv-loss"
              style={{ width: `${(s.wait / total) * 100}%` }}
            />
          </div>
          <span className="rv-num">
            {s.work.toFixed(1)} + {s.wait.toFixed(1)} {u}
          </span>
        </div>
      ))}
    </div>
  );
}

function tone(o: RunOrder): Tone {
  return o.status === "ok" ? "gain" : "loss";
}

export function orderSentence(o: RunOrder, unit: TimeUnit): string {
  const u = unitShort(unit);
  const parts = [
    `${displayId(o.id)}: released at ${o.release.toFixed(0)} ${u}`,
  ];
  if (o.finish?.mean != null) {
    parts.push(`likely done at ${o.finish.mean.toFixed(1)}`);
    if (o.finish.low != null && o.finish.high != null)
      parts.push(
        `range ${o.finish.low.toFixed(1)} to ${o.finish.high.toFixed(1)}`,
      );
  } else parts.push("did not finish");
  if (o.due != null) parts.push(`due at ${o.due.toFixed(1)}`);
  return parts.join(", ") + ".";
}

export function OrderRiskList({
  orders,
  horizon,
  unit,
}: {
  orders: RunOrder[];
  horizon: number;
  unit: TimeUnit;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const ticks = axisTicks(horizon);
  const span = Math.max(horizon, ticks[ticks.length - 1] ?? 0, 1e-9);
  const u = unitShort(unit);
  const sorted = sortOrders(orders);
  return (
    <div
      className="rv-orders"
      role="list"
      aria-label={`${orders.length} orders, sorted by the chance of being late`}
    >
      <div className="rv-legend">
        <span>
          <i className="rv-gain" />
          Finishes with room
        </span>
        <span>
          <i className="rv-loss" />
          Finish may pass the due time
        </span>
        <span>
          <i className="rv-key-due" />
          Due
        </span>
        <span>
          <i className="rv-key-range" />
          Likely range of the finish
        </span>
      </div>
      <div className="rv-ord rv-ord-head" aria-hidden="true">
        <span>Order</span>
        <span>Part</span>
        <span className="rv-axis">
          {ticks.map((t, i) => (
            <span
              key={t}
              style={{
                left: `${(t / span) * 100}%`,
                transform: `translateX(${i === 0 ? "0" : i === ticks.length - 1 && t / span > 0.95 ? "-100%" : "-50%"})`,
              }}
            >
              {t} {u}
            </span>
          ))}
        </span>
        <span>Late chance</span>
        <span className="rv-why">Why</span>
      </div>
      {sorted.map((o) => (
        <div role="listitem" key={o.id}>
          <button
            type="button"
            className="rv-ord"
            aria-expanded={open === o.id}
            onClick={() => setOpen(open === o.id ? null : o.id)}
          >
            <span className="rv-id">{displayId(o.id)}</span>
            <span>
              {o.qty} {plural(o.part, o.qty)}
            </span>
            <OrderTimeline
              release={o.release}
              finish={o.finish?.mean ?? null}
              low={o.finish?.low ?? null}
              high={o.finish?.high ?? null}
              due={o.due}
              tone={tone(o)}
              horizon={span}
              label={orderSentence(o, unit)}
            />
            <span>
              <span className={`rv-status rv-${o.status}`}>{pillText(o)}</span>
            </span>
            <span className="rv-why">{o.reason}</span>
          </button>
          {open === o.id && (
            <div className="rv-drill">
              <OrderSteps order={o} unit={unit} />
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function OrderTable({
  orders,
  unit,
}: {
  orders: RunOrder[];
  unit: TimeUnit;
}) {
  const f = (v: number | null | undefined) => (v == null ? "-" : v.toFixed(1));
  return (
    <table className="rv-table" aria-label="Every order in numbers">
      <thead>
        <tr>
          <th>Order</th>
          <th>Part</th>
          <th>Qty</th>
          <th>Released</th>
          <th>Due</th>
          <th>Likely finish</th>
          <th>Range</th>
          <th>Room</th>
          <th>Late chance</th>
        </tr>
      </thead>
      <tbody>
        {sortOrders(orders).map((o) => (
          <tr key={o.id}>
            <td>{displayId(o.id)}</td>
            <td>{o.part}</td>
            <td>{o.qty}</td>
            <td>{f(o.release)}</td>
            <td>{f(o.due)}</td>
            <td>{f(o.finish?.mean)}</td>
            <td>
              {o.finish?.low != null && o.finish.high != null
                ? `${f(o.finish.low)} to ${f(o.finish.high)}`
                : "-"}
            </td>
            <td>{o.room == null ? "-" : signedTime(o.room, unit)}</td>
            <td>
              {o.late_chance == null
                ? "-"
                : `${Math.round(o.late_chance * 100)}%`}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
