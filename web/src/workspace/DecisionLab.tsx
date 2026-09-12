import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { download, workspaceApi } from "./api";
import { Icon } from "./Icons";

export function DecisionLab({ mode }: { mode: "data" | "schedule" }) {
  const [content, setContent] = useState("");
  const [backend, setBackend] = useState("baseline");
  const [knownAt, setKnownAt] = useState("2026-01-10T00:00:00Z");
  const [fileError, setFileError] = useState("");
  const contracts = useQuery({ queryKey: ["tool-contracts"], queryFn: workspaceApi.contracts });
  const run = useMutation({mutationFn: async () => {
    if (mode === "schedule") return workspaceApi.schedule(JSON.parse(content), backend);
    const text = content.trim();
    const parsed = text.startsWith("[") ? JSON.parse(text) : text.split("\n").filter(Boolean).map(line => JSON.parse(line));
    return workspaceApi.ingest(parsed);
  }});
  const snapshot = useMutation({mutationFn: () => workspaceApi.snapshot(knownAt)});
  const result = mode === "data" ? snapshot.data ?? run.data : run.data;
  const schedule = run.data?.result as {verified: boolean; status: string; starts: Record<string, number>; ends: Record<string, number>; resources: Record<string, string>} | undefined;
  const operations = Object.keys(schedule?.starts ?? {});
  const horizon = Math.max(1, ...Object.values(schedule?.ends ?? {}));
  return <>
    <div className="ws-page-heading"><div><span className="ws-eyebrow">{mode === "data" ? "CONNECT FACTS TO THE MODEL" : "TURN CONSTRAINTS INTO A PLAN"}</span>
      <h1>{mode === "data" ? "A clear view of what is known." : "Find a schedule that fits."}</h1>
      <p>{mode === "data" ? "Retain source events. Reconcile corrections. Build an inspectable snapshot for your agent." : "Model precedence, readiness, qualified resources, and frozen work. Every candidate is checked independently."}</p></div></div>
    <div className="ws-two-column"><section className="ws-card">
      <div className="ws-card-heading"><h2>{mode === "data" ? "Import operational events" : "Scheduling problem"}</h2><button className="ws-button" disabled={!contracts.data} onClick={() => {setContent(JSON.stringify(mode === "data" ? contracts.data!.event_example : contracts.data!.scheduling_example, null, 2));run.reset();snapshot.reset();}}>Load example</button></div>
      <label className="ws-field">Import {mode === "data" ? "JSON / JSONL" : "JSON"}<input type="file" accept=".json,.jsonl" onChange={async event => {
        const file = event.target.files?.[0]; if (!file) return;
        if (file.size > 5_242_880) {setFileError("Choose a file smaller than 5 MB.");return;}
        try {setContent(await file.text());setFileError("");run.reset();snapshot.reset();} catch {setFileError("The file could not be read.");}
      }}/></label>
      <label className="ws-field">{mode === "data" ? "Normalized source records" : "Operations and resource windows"}<textarea className="ws-json-editor" rows={17} value={content} onChange={event => setContent(event.target.value)} spellCheck={false}/></label>
      {mode === "schedule" && <label className="ws-field">Scheduling method<select value={backend} onChange={event => setBackend(event.target.value)}><option value="baseline">Earliest-feasible baseline</option><option value="ortools">CP-SAT optimization</option></select></label>}
      {(fileError || run.error || contracts.error) && <p className="ws-error" role="alert">{fileError || run.error?.message || contracts.error?.message}</p>}
      <button className="ws-button primary" disabled={!content || run.isPending} onClick={() => run.mutate()}><Icon name="play" size={16}/>{run.isPending ? "Working…" : mode === "data" ? "Import events" : "Find schedule"}</button>
      {mode === "data" && run.data && <p role="status">{String(run.data.inserted)} events inserted · {String(run.data.duplicates)} duplicates retained once.</p>}
    </section><aside className="ws-stack">
      {mode === "data" && <section className="ws-card"><h2>Build a historical snapshot</h2><p>Only facts known by the cutoff participate. Export the snapshot so your agent can map entities into a scenario.</p><label className="ws-field">Known at · ISO timestamp<input value={knownAt} onChange={event => setKnownAt(event.target.value)}/></label><button className="ws-button" disabled={snapshot.isPending} onClick={() => snapshot.mutate()}>Reconcile snapshot</button>{snapshot.error && <p role="alert" className="ws-error">{snapshot.error.message}</p>}</section>}
      {schedule && <section className="ws-card"><div className="ws-card-heading"><h2>Schedule result</h2><span className={`ws-badge ${schedule.verified ? "" : "amber"}`}>{schedule.status} · {schedule.verified ? "Verified" : "No verified plan"}</span></div>
        <div className="ws-schedule-bars">{operations.map(id => <div key={id}><strong>{id} <small>{schedule.resources[id]}</small></strong><div className="ws-schedule-track"><span style={{marginLeft: `${100 * schedule.starts[id] / horizon}%`, width: `${100 * (schedule.ends[id] - schedule.starts[id]) / horizon}%`}}/></div><small>{schedule.starts[id]} → {schedule.ends[id]}</small></div>)}</div>
        {schedule.verified && <button className="ws-button" onClick={() => download("schedule.csv", "operation,resource,start,end\n" + operations.map(id => [id, schedule.resources[id], schedule.starts[id], schedule.ends[id]].map(value => `"${String(value).replace(/"/g, '""')}"`).join(",")).join("\n"), "text/csv")}>Export schedule CSV</button>}
      </section>}
      {result && <section className="ws-card"><div className="ws-card-heading"><h2>Retained evidence</h2><button className="ws-button" onClick={() => download(`${mode}-evidence.json`, JSON.stringify(result, null, 2))}><Icon name="down" size={16}/>Export JSON</button></div><pre className="ws-source">{JSON.stringify(result, null, 2)}</pre></section>}
      {!result && <section className="ws-card ws-assumptions"><Icon name="spark"/><h2>{mode === "data" ? "Keep the source attached." : "Feasibility before improvement."}</h2><p>{mode === "data" ? "Stable source IDs and revisions keep retries idempotent. Missing and stale facts stay visible instead of becoming confident guesses." : "An optimization timeout is not proof of infeasibility. Review status, verification, objective, and bounds in the evidence."}</p></section>}
    </aside></div>
  </>;
}
