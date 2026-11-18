// "Where does the line choke?" (DESIGN.md 4.3). The route as a flow of step cards.
import { ChartFrame } from "../ChartFrame";
import { StatusPill } from "../primitives";
import { pct, stepName } from "../run";
import type { RunCentre, RunViewModel } from "../types";

type Status = RunCentre["status"];

function statusWords(status: Status): string {
  return status === "choke"
    ? ", choke point"
    : status === "next"
      ? ", next to choke"
      : "";
}

export function StepCard({
  name,
  busy,
  status,
}: {
  name: string;
  busy: number;
  status: Status;
}) {
  return (
    <div
      className={`rv-stepcard rv-${status}`}
      title={`${name}: busy ${pct(busy)}${statusWords(status)}`}
    >
      <div className="rv-stepcard-top">
        <b>{name}</b>
        {status === "choke" && <StatusPill kind="late" label="Choke point" />}
        {status === "next" && <StatusPill kind="risk" label="Next to choke" />}
      </div>
      <div className="rv-stepcard-v tf-num">{pct(busy)}</div>
      <div className="rv-meter">
        <i style={{ width: `${Math.max(1.5, Math.min(100, busy * 100))}%` }} />
      </div>
      <div className="tf-note">busy</div>
    </div>
  );
}

function CentreTable({
  routes,
  byId,
}: {
  routes: RunViewModel["routes"];
  byId: Map<string, RunCentre>;
}) {
  return (
    <table className="tf-table" aria-label="How busy each step is">
      <thead>
        <tr>
          <th>Part</th>
          <th>Step</th>
          <th>Busy</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {routes.flatMap((r) =>
          r.steps.map((s) => {
            const c = byId.get(s);
            return (
              <tr key={`${r.part}-${s}`}>
                <td>{r.part}</td>
                <td>{c?.label ?? s}</td>
                <td>{pct(c?.busy.mean ?? 0)}</td>
                <td>
                  {c?.status === "choke"
                    ? "Choke point"
                    : c?.status === "next"
                      ? "Next to choke"
                      : "Has room"}
                </td>
              </tr>
            );
          }),
        )}
      </tbody>
    </table>
  );
}

export function ChokeFlow({
  routes,
  centres,
  label,
}: {
  routes: RunViewModel["routes"];
  centres: RunCentre[];
  label: string;
}) {
  const byId = new Map(centres.map((c) => [c.id, c]));
  const widest = Math.max(1, ...routes.map((r) => r.steps.length));
  return (
    <ChartFrame
      label={label}
      table={() => <CentreTable routes={routes} byId={byId} />}
    >
      {() => (
        <div className="rv-flow">
          {routes.map((route) => (
            <div
              className="rv-flow-row"
              key={route.part}
              style={{
                gridTemplateColumns: `76px repeat(${widest}, minmax(0, 1fr))`,
              }}
            >
              <div className="rv-flow-part">
                {route.part[0].toUpperCase() + route.part.slice(1)}s
              </div>
              {route.steps.map((step, i) => {
                const c = byId.get(step);
                const status: Status = c?.status ?? "ok";
                return (
                  <div className="rv-flow-cell" key={`${step}-${i}`}>
                    {i > 0 && (
                      <div
                        className={`rv-link${status === "choke" ? " rv-hot" : ""}`}
                      />
                    )}
                    <StepCard
                      name={stepName(c?.label ?? step, route.part)}
                      busy={c?.busy.mean ?? 0}
                      status={status}
                    />
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}
    </ChartFrame>
  );
}
