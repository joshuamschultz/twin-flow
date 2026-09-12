import { useState } from "react";
import ReactFlow, { Background, Controls, MarkerType } from "reactflow";
import "reactflow/dist/style.css";
import { download, type Scenario, type Experiment } from "./api";
import { Icon } from "./Icons";

export function ScenarioView({
  scenario,
  jobs,
  onRun,
  onBranch,
  busy,
}: {
  scenario: Scenario;
  jobs: Experiment[];
  onRun: (reps: number) => void;
  onBranch: () => void;
  busy: boolean;
}) {
  const [view, setView] = useState("overview");
  const [reps, setReps] = useState(10);
  const latest = jobs.find(
    (job) => job.scenario_id === scenario.id && job.status === "completed",
  );
  const graph = scenario.summary;
  const domain = String(scenario.capsule.model.domain ?? "manufacturing");
  const rawOrders = (scenario.capsule.snapshot.production_plan ?? scenario.capsule.snapshot.orders ?? scenario.capsule.snapshot.cases ?? []) as Record<string, unknown>[];
  const orders = rawOrders.map(row => ({
    work_order_id: String(row.work_order_id ?? row.id),
    part: String(row.part ?? row.item_id ?? "Case"),
    qty: String(row.qty ?? row.quantity ?? 1),
    due_date: String(row.due_date ?? row.due_at ?? row.due ?? "Not specified"),
  }));
  const latestMetric = latest?.result?.metrics;
  const headline = domain === "manufacturing" ? latestMetric?.on_time_pct : domain === "office" ? latestMetric?.completed_cases : latestMetric?.feasible_count;
  return (
    <>
      <div className="ws-page-heading">
        <div>
          <div className="ws-breadcrumb">
            Scenario library <span>/</span>{" "}
            {scenario.parent_id ? "Alternative" : "Baseline"}
          </div>
          <h1>{scenario.name}</h1>
          <p>Explore the flow. Test a change. Understand the outcome.</p>
        </div>
        <div className="ws-actions">
          <button
            className="ws-button"
            onClick={() =>
              download(
                `${scenario.name}.twin.json`,
                JSON.stringify(scenario.capsule, null, 2),
              )
            }
          >
            <Icon name="down" size={16} />
            Export
          </button>
          <button className="ws-button" onClick={onBranch}>
            <Icon name="branch" size={16} />
            Branch
          </button>
          <button
            className="ws-button primary"
            onClick={() => onRun(reps)}
            disabled={busy}
          >
            <Icon name="play" size={16} />
            {busy ? "Starting…" : "Run experiment"}
          </button>
        </div>
      </div>
      <div className="ws-metrics">
        <Metric
          label={domain === "supply_chain" ? "Items" : "Processes"}
          value={graph.processes}
          hint="Connected model elements"
          icon="flow"
        />
        <Metric
          label={domain === "office" ? "Cases in scope" : "Orders in scope"}
          value={graph.orders}
          hint="From this snapshot"
          icon="layers"
        />
        <Metric
          label="Material stocks"
          value={graph.stocks}
          hint="Declared inventory points"
          icon="box"
        />
        <Metric
          label={domain === "manufacturing" ? "On-time · simulated" : "Completed / feasible"}
          value={
            headline === undefined ? "—" : `${headline.toFixed(1)}${domain === "manufacturing" ? "%" : ""}`
          }
          hint={
            latest
              ? `${latest.reps} replications · latest result`
              : "Run a baseline to see outcomes"
          }
          icon="clock"
        />
      </div>
      <div className="ws-subnav" role="tablist" aria-label="Scenario views">
        {["overview", "process", "source"].map((tab) => (
          <button
            role="tab"
            aria-selected={view === tab}
            key={tab}
            className={view === tab ? "active" : ""}
            onClick={() => setView(tab)}
          >
            {tab === "overview"
              ? "Overview"
              : tab === "process"
                ? "Process map"
                : "Scenario source"}
          </button>
        ))}
        <span className="ws-badge amber">Provisional model</span>
      </div>
      {view === "overview" && (
        <div className="ws-two-column">
          <section className="ws-card">
            <div className="ws-card-heading">
              <div>
                <h2>Work moving through the system</h2>
                <p>The demand this scenario is designed to evaluate.</p>
              </div>
              <span className="ws-badge">{orders.length} orders</span>
            </div>
            <div className="ws-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Order</th>
                    <th>Part / work item</th>
                    <th>Quantity</th>
                    <th>{domain === "supply_chain" ? "Due date" : "Due · simulation time"}</th>
                  </tr>
                </thead>
                <tbody>
                  {orders.slice(0, 100).map((order) => (
                    <tr key={order.work_order_id}>
                      <td className="ws-strong">{order.work_order_id}</td>
                      <td>{order.part}</td>
                      <td>{order.qty}</td>
                      <td className="ws-mono">{order.due_date}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {orders.length === 0 && (
                <p className="ws-table-empty">
                  No demand in this snapshot.
                </p>
              )}
              {orders.length > 100 && (
                <p className="ws-table-empty">
                  Showing 100 orders. Export for the complete demand set.
                </p>
              )}
            </div>
          </section>
          <aside className="ws-stack">
            <section className="ws-card">
              <span className="ws-eyebrow">EXPERIMENT SETTINGS</span>
              <h2>Explore the uncertainty.</h2>
              <p>
                Repeat the same scenario with different random draws. More
                replications improve the estimate.
              </p>
              <label className="ws-field">
                Replications
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={reps}
                  onChange={(event) => setReps(Number(event.target.value))}
                />
              </label>
              <div className="ws-meta-row">
                <span>Comparison seed</span>
                <strong>42</strong>
              </div>
              <div className="ws-meta-row">
                <span>Snapshot</span>
                <strong>
                  {String(scenario.capsule.snapshot.id ?? "Embedded")}
                </strong>
              </div>
            </section>
            <section className="ws-card ws-assumptions">
              <Icon name="spark" />
              <h2>Know what the model assumes.</h2>
              <p>
                Imported models are provisional until validated against actual
                operations. Simulated dates are conditional estimates.
              </p>
              <details>
                <summary>View declared assumptions</summary>
                <pre>
                  {JSON.stringify(scenario.capsule.assumptions, null, 2)}
                </pre>
              </details>
            </section>
          </aside>
        </div>
      )}
      {view === "process" && (
        <section className="ws-card ws-map">
          <div className="ws-card-heading">
            <div>
              <h2>The shape of your operation</h2>
              <p>Pan, zoom, and trace each route through the model.</p>
            </div>
            <span className="ws-badge">{graph.nodes.length} nodes</span>
          </div>
          <div className="ws-flow-canvas">
            <ReactFlow
              fitView
              nodes={graph.nodes.map((node, i) => ({
                id: node.id,
                position: { x: (i % 4) * 245, y: Math.floor(i / 4) * 145 },
                data: {
                  label: (
                    <div className="ws-flow-node">
                      <span className="ws-eyebrow">{node.kind}</span>
                      <strong>{node.label}</strong>
                      {node.capacity !== undefined && (
                        <small>
                          {node.capacity} resource
                          {node.capacity !== 1 ? "s" : ""} · {node.skill}
                        </small>
                      )}
                    </div>
                  ),
                },
                style: {
                  width: 200,
                  borderRadius: 12,
                  border: "1px solid #ceddd5",
                  padding: 4,
                  background: "#fff",
                },
              }))}
              edges={graph.edges.map((edge, i) => ({
                id: `edge-${i}`,
                source: edge.source,
                target: edge.target,
                label: edge.label,
                type: "smoothstep",
                markerEnd: { type: MarkerType.ArrowClosed },
                style: { stroke: "#578477", strokeWidth: 1.5 },
              }))}
              nodesDraggable
              elementsSelectable
            >
              <Background color="#d7dfda" gap={20} />
              <Controls />
            </ReactFlow>
          </div>
        </section>
      )}
      {view === "source" && (
        <section className="ws-card">
          <div className="ws-card-heading">
            <div>
              <h2>A portable, inspectable contract</h2>
              <p>
                The same content agents import, validate, and use to build
                alternatives.
              </p>
            </div>
            <button
              className="ws-button"
              onClick={() =>
                download(
                  "scenario.twin.json",
                  JSON.stringify(scenario.capsule, null, 2),
                )
              }
            >
              <Icon name="down" size={16} />
              Download JSON
            </button>
          </div>
          <pre className="ws-source">
            {JSON.stringify(scenario.capsule, null, 2)}
          </pre>
          <div className="ws-meta-row">
            <span>Content digest</span>
            <code>{scenario.digest.slice(0, 24)}…</code>
          </div>
        </section>
      )}
    </>
  );
}

function Metric({
  label,
  value,
  hint,
  icon,
}: {
  label: string;
  value: string | number;
  hint: string;
  icon: string;
}) {
  return (
    <div className="ws-metric">
      <div>
        <span>{label}</span>
        <Icon name={icon} size={18} />
      </div>
      <strong>{value}</strong>
      <small>{hint}</small>
    </div>
  );
}
