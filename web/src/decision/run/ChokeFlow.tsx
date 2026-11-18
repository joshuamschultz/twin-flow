// "Where does the line choke?" (DESIGN.md 4.3). The route as a flow of step cards.
import type { RunCentre, RunViewModel } from "../types";
import { pct, plural, stepName } from "./runText";

type Status = RunCentre["status"];

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
      aria-label={`${name}: busy ${pct(busy)}${status === "choke" ? ", choke point" : status === "next" ? ", next to choke" : ""}`}
    >
      <div className="rv-stepcard-top">
        <b>{name}</b>
        {status === "choke" && (
          <span className="rv-status rv-late">Choke point</span>
        )}
        {status === "next" && (
          <span className="rv-status rv-risk">Next to choke</span>
        )}
      </div>
      <div className="rv-stepcard-v rv-num">{pct(busy)}</div>
      <div className="rv-meter">
        <i style={{ width: `${Math.max(1.5, Math.min(100, busy * 100))}%` }} />
      </div>
      <div className="rv-note">busy</div>
    </div>
  );
}

export function ChokeFlow({
  routes,
  centres,
}: {
  routes: RunViewModel["routes"];
  centres: RunCentre[];
}) {
  const byId = new Map(centres.map((c) => [c.id, c]));
  const widest = Math.max(1, ...routes.map((r) => r.steps.length));
  return (
    <div
      className="rv-flow"
      role="img"
      aria-label={`The route of each part, step by step, with how busy each step is. ${routes.length} ${plural("route", routes.length)}.`}
    >
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
                    title={status === "choke" ? "Work piles up here" : ""}
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
  );
}
