import { useEffect, useState } from "react";
import { api, type AdaptersResponse } from "../api";
import { Spinner } from "./Spinner";

interface Group {
  title: string;
  caption: string;
  names: string[];
}

function buildGroups(adapters: AdaptersResponse): Group[] {
  return [
    {
      title: "Connectors",
      caption: "ERP / MES connectors register here — plan sources, result sinks, and actual sources.",
      names: [...adapters.plan_sources, ...adapters.result_sinks, ...adapters.actual_sources],
    },
    {
      title: "Demand generators",
      caption: "Order and demand generators plug in here to drive a plan from something other than a static CSV.",
      names: adapters.demand_generators,
    },
    {
      title: "Forecasters",
      caption: "Forecasting models — statistical or RL — plug in here to feed predicted demand into the twin.",
      names: adapters.forecasters,
    },
    {
      title: "Dispatch policies",
      caption: "Alternative dispatch/sequencing policies register here to replace the default rule at run time.",
      names: adapters.dispatch_policies,
    },
  ];
}

export function IntegrationsTab() {
  const [adapters, setAdapters] = useState<AdaptersResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .adapters()
      .then(setAdapters)
      .catch((err: unknown) => setError(err instanceof Error ? err.message : String(err)));
  }, []);

  return (
    <div>
      <div className="panel-header">
        <h2>Integrations</h2>
        <span className="hint">registered plug-in surfaces — informational only</span>
      </div>

      {error && <div className="error-banner">{error}</div>}
      {!error && !adapters && <Spinner label="Loading adapters…" />}

      {adapters && (
        <div className="card-grid">
          {buildGroups(adapters).map((group) => (
            <div className="card" key={group.title}>
              <div className="card-title">{group.title}</div>
              <p style={{ fontSize: 12, marginBottom: 12 }}>{group.caption}</p>
              {group.names.length === 0 ? (
                <div className="empty-state" style={{ padding: "12px 0" }}>
                  none registered
                </div>
              ) : (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {group.names.map((name) => (
                    <span className="tag" key={name}>
                      {name}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
