import type { Experiment } from "./api";

type Row = Record<string, unknown>;
const rows = (value: unknown): Row[] =>
  Array.isArray(value) ? (value as Row[]) : [];

export function DomainResults({
  result,
}: {
  result: NonNullable<Experiment["result"]>;
}) {
  const samples = result.per_replication ?? [];
  const supply = result.domain === "supply_chain";
  const collection = supply ? "order_forecasts" : "cases";
  const ids = [
    ...new Set(
      samples.flatMap((sample) =>
        rows(sample[collection]).map((row) =>
          String(row.order_id ?? row.case_id),
        ),
      ),
    ),
  ];
  return (
    <>
      <h3>{supply ? "Delivery outlook" : "Case completion outcomes"}</h3>
      <p>
        {supply
          ? "Material availability and configured gates. Dates exclude finite production capacity."
          : "Elapsed simulation time across the modeled tasks and approvals."}
      </p>
      <div className="ws-table-wrap">
        <table>
          <thead>
            <tr>
              <th>{supply ? "Order" : "Case"}</th>
              <th>Completed</th>
              <th>Median</th>
              <th>P90</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {ids.map((id) => {
              const outcomes = samples.flatMap((sample) =>
                rows(sample[collection]).filter(
                  (row) => String(row.order_id ?? row.case_id) === id,
                ),
              );
              const values = outcomes
                .flatMap((row) => {
                  if (
                    supply
                      ? row.status !== "feasible"
                      : row.outcome !== "completed"
                  )
                    return [];
                  const value = supply
                    ? Date.parse(String(row.delivery_at))
                    : Number(row.completed_at);
                  return Number.isFinite(value) ? [value] : [];
                })
                .sort((a, b) => a - b);
              const estimable =
                values.length === samples.length && values.length > 0;
              const quantile = (p: number) => {
                if (!estimable) return "Not estimable";
                const value = values[Math.ceil(p * (values.length - 1))];
                return supply
                  ? new Date(value)
                      .toISOString()
                      .replace("T", " ")
                      .slice(0, 16) + " UTC"
                  : value.toFixed(1);
              };
              return (
                <tr key={id}>
                  <td>{id}</td>
                  <td>
                    {values.length} / {samples.length}
                  </td>
                  <td>{quantile(0.5)}</td>
                  <td>{quantile(0.9)}</td>
                  <td>
                    <span className={`ws-badge ${estimable ? "" : "amber"}`}>
                      {estimable
                        ? "Completed in all runs"
                        : "Blocked / incomplete"}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <details className="ws-outcomes">
        <summary>
          {supply
            ? "Shortages, gates, and affected orders"
            : "Task, approval, and handoff evidence"}
        </summary>
        <pre>
          {JSON.stringify(
            samples.map((sample, index) => ({
              replication: index,
              ...(supply
                ? {
                    shortages: sample.shortages,
                    gates: sample.gate_results,
                    affected_orders: sample.affected_orders,
                  }
                : { trace: sample.trace }),
            })),
            null,
            2,
          )}
        </pre>
      </details>
    </>
  );
}
