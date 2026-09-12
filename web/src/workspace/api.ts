export interface GraphNode {
  id: string;
  kind: string;
  label: string;
  capacity?: number;
  skill?: string;
}
export interface Scenario {
  id: string;
  name: string;
  digest: string;
  parent_id: string | null;
  created_at: string;
  capsule: {
    model: Record<string, unknown>;
    snapshot: Record<string, unknown>;
    assumptions: unknown;
    [key: string]: unknown;
  };
  summary: {
    nodes: GraphNode[];
    edges: { source: string; target: string; label: string }[];
    orders: number;
    processes: number;
    stocks: number;
  };
  validity: string;
}
export interface Band {
  mean: number;
  lo: number;
  hi: number;
  p50: number;
  n: number;
}
export interface Experiment {
  id: string;
  scenario_id: string;
  status: string;
  reps: number;
  seed: number;
  created_at: string;
  error: string | null;
  result: {
    domain?: string;
    per_replication?: Record<string, unknown>[];
    metrics: Record<string, number>;
    intervals: {
      completion_by_order: Record<string, Band>;
      completion_distributions?: Record<
        string,
        {
          quantiles: Record<string, number> | null;
          observed_count: number;
          censored_count: number;
          estimability: string;
        }
      >;
      on_time_pct: Band;
    };
    outcomes: {
      replication: number;
      outcome: string;
      termination_reason: string;
    }[];
    interpretation: string;
    [key: string]: unknown;
  } | null;
}
export interface Draft {
  id: string;
  name: string;
  status: string;
  questions: { id: string; question: string }[];
  facts: Record<string, unknown>;
  next_step: string;
}
export interface Example {
  id: string;
  name: string;
  domain?: string;
}

let serviceToken = "";
export function setServiceToken(value: string) {
  serviceToken = value;
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/workspace${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "Content-Type": "application/json",
      ...(serviceToken ? { Authorization: `Bearer ${serviceToken}` } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item: { msg: string }) => item.msg).join("; ")
          : `Request failed (${response.status}). Please try again.`,
    );
  }
  return response.json() as Promise<T>;
}

export interface ActionProposal { id: string; proposal: {digest: string; action_json: string; result_id: string; expected_operational_revision: string}; created_at: string; mode: string }
export const workspaceApi = {
  proposals: () => request<ActionProposal[]>("/proposals"),
  propose: (job_id: string, action: Record<string, unknown>) => request<ActionProposal>("/proposals", {job_id, action}),
  approve: (id: string, reviewed_digest: string, reviewer: string) => request<{approval_id: string}>(`/proposals/${id}/approve`, {reviewed_digest, reviewer}),
  deliver: (id: string, approval_id: string, current_revision: string, request_key: string) => request<Record<string, unknown>>(`/proposals/${id}/dry-run`, {approval_id, current_revision, request_key}),

  contracts: () =>
    request<{
      event_example: unknown[];
      scheduling_example: Record<string, unknown>;
      snapshot_example: { known_at: string };
    }>("/tool-contracts"),
  ingest: (records: unknown[]) =>
    request<Record<string, unknown>>("/data/events", { records }),
  snapshot: (known_at: string) =>
    request<Record<string, unknown>>("/data/snapshots", {
      known_at,
      freshness_rules: [],
    }),
  schedule: (problem: Record<string, unknown>, backend: string) =>
    request<Record<string, unknown>>("/schedules", {
      problem,
      backend,
      time_limit: 10,
    }),
  answerDraft: (id: string, answers: { id: string; answer: string }[]) =>
    request<Draft>(`/drafts/${id}/answers`, { answers }),

  scenarios: () => request<Scenario[]>("/scenarios"),
  examples: () => request<Example[]>("/examples"),
  load: (example_id: string) =>
    request<Scenario>("/examples/load", { example_id }),
  import: (name: string, content: string) =>
    request<Scenario>("/scenarios", { name, content }),
  branch: (id: string, name: string, content: string) =>
    request<Scenario>(`/scenarios/${id}/branch`, { name, content }),
  jobs: () => request<Experiment[]>("/jobs"),
  evaluate: (id: string, reps: number) =>
    request<Experiment>(`/scenarios/${id}/evaluate`, {
      reps,
      seed: 42,
      request_key: crypto.randomUUID(),
    }),
  cancel: (id: string) => request<Experiment>(`/jobs/${id}/cancel`, {}),
  drafts: () => request<Draft[]>("/drafts"),
  draft: (name: string, facts: Record<string, string>) =>
    request<Draft>("/drafts", { name, facts }),
  compare: (baseline_id: string, candidate_id: string) =>
    request<{ delta: Record<string, number>; interpretation: string }>(
      "/compare",
      { baseline_id, candidate_id },
    ),
};

export function download(
  name: string,
  content: string,
  type = "application/json",
) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
