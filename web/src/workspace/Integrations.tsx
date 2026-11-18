// What can connect to the twin: the registered plug-in surfaces (moved from the retired
// classic tools, B-37). Read only.
import { useQuery } from "@tanstack/react-query";
import { api, type AdaptersResponse } from "../api";

type Group = { title: string; caption: string; names: string[] };

function groups(adapters: AdaptersResponse): Group[] {
  return [
    {
      title: "Connectors",
      caption:
        "ERP and MES connectors: where plans come from, where results go, and where actuals come from.",
      names: [
        ...adapters.plan_sources,
        ...adapters.result_sinks,
        ...adapters.actual_sources,
      ],
    },
    {
      title: "Demand makers",
      caption: "They make orders from something other than a fixed file.",
      names: adapters.demand_generators,
    },
    {
      title: "Forecasters",
      caption: "They feed predicted demand into the twin.",
      names: adapters.forecasters,
    },
    {
      title: "Dispatch rules",
      caption: "They replace the default rule for what runs next.",
      names: adapters.dispatch_policies,
    },
  ];
}

export function Integrations() {
  const adapters = useQuery({
    queryKey: ["adapters"],
    queryFn: api.adapters,
  });
  return (
    <section className="ws-card">
      <h2>What can connect</h2>
      {adapters.isLoading && <p role="status">Loading.</p>}
      {adapters.error && (
        <p role="alert">Could not load the list: {adapters.error.message}</p>
      )}
      {adapters.data &&
        groups(adapters.data).map((group) => (
          <div key={group.title} style={{ marginTop: 14 }}>
            <h3>{group.title}</h3>
            <p>{group.caption}</p>
            <p>{group.names.length ? group.names.join(", ") : "None yet."}</p>
          </div>
        ))}
    </section>
  );
}
