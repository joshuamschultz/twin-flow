/**
 * Typed client for the twinflow FastAPI backend. Every function hits a
 * relative `/api/...` path — the Vite dev server proxies these to
 * `http://localhost:8000` (see vite.config.ts); a production build expects
 * the same origin to serve both the app and the API.
 */

// ---- discovery -----------------------------------------------------------

export interface ModulesResponse {
  objectives: string[];
  costs: string[];
  optimizers: string[];
  models: string[];
}

export interface ModelSummary {
  name: string;
  has_plan: boolean;
}

export interface AdaptersResponse {
  plan_sources: string[];
  result_sinks: string[];
  actual_sources: string[];
  demand_generators: string[];
  forecasters: string[];
  dispatch_policies: string[];
}

// ---- floor graph -----------------------------------------------------------

export type NodeKind = "location" | "stock";

export interface FloorNode {
  id: string;
  kind: NodeKind;
  label: string;
  capacity?: number;
  labor_skill?: string;
  machine?: string;
  time_model?: string;
  uom?: string;
}

export interface FloorEdge {
  source: string;
  target: string;
  label: string;
}

export interface FloorPart {
  part: string;
  steps: string[];
}

export interface FloorGraph {
  nodes: FloorNode[];
  edges: FloorEdge[];
  parts: FloorPart[];
}

// ---- levers -----------------------------------------------------------

export interface Lever {
  path: string;
  label: string;
  current: number;
  min: number;
  max: number;
}

// ---- KPIs + confidence intervals -----------------------------------------------------------

export interface Interval {
  mean: number;
  lo: number;
  hi: number;
  p50: number;
  n: number;
}

export interface KpiSet {
  on_time_pct: number;
  completion_by_order: Record<string, number>;
  lateness_by_order: Record<string, number>;
  utilization_by_cell: Record<string, number>;
  wait_seconds_by_location: Record<string, Record<string, number>>;
  machine_hours_by_machine: Record<string, number>;
  run_hours: number;
  setup_hours: number;
  event_counts_by_location: Record<string, number>;
}

export interface AggregatedKpis {
  reps: number;
  level: number;
  on_time_pct: Interval;
  lateness_by_order: Record<string, Interval>;
  completion_by_order: Record<string, Interval>;
  utilization_by_cell: Record<string, Interval>;
}

export interface FulfillmentKpis {
  orders: number;
  delivered: number;
  backordered: number;
  fill_rate: number;
  on_time_deliveries: number;
  on_time_delivery_pct: number;
  avg_delivery_lateness: number;
}

export interface InventoryKpis {
  average_level: Record<string, number>;
  ending_level: Record<string, number>;
  stockout_seconds: Record<string, number>;
  orders_placed: Record<string, number>;
  total_ordered: Record<string, number>;
}

export interface RunResult {
  kpis: KpiSet;
  intervals: AggregatedKpis;
  fulfillment: FulfillmentKpis;
  inventory: InventoryKpis;
}

export interface SweepPoint {
  levers: Record<string, unknown>;
  kpis: KpiSet;
  intervals: AggregatedKpis;
}

export interface SweepResult {
  points: SweepPoint[];
}

export interface Evaluation {
  levers: Record<string, unknown>;
  kpis: KpiSet;
  intervals: AggregatedKpis;
}

export interface OptimizeHistoryEntry {
  levers: Record<string, unknown>;
  score: number;
}

export interface OptimizeResult {
  objective: string;
  direction: "max" | "min";
  best: Evaluation;
  best_score: number;
  evaluations_used: number;
  history: OptimizeHistoryEntry[];
}

// ---- jobs -----------------------------------------------------------

export type JobKind = "run" | "sweep" | "optimize";
export type JobStatus = "running" | "done" | "error";

export interface Job<T> {
  id: string;
  kind: JobKind;
  status: JobStatus;
  result: T | null;
  error: string | null;
}

// ---- request bodies -----------------------------------------------------------

export interface RunRequest {
  model: string;
  reps: number;
}

export interface SweepRequest {
  model: string;
  sweep: Record<string, unknown[]>;
  reps: number;
}

export interface LeverDomainSpec {
  min?: number;
  max?: number;
  step?: number;
  choices?: unknown[];
}

export interface OptimizeRequest {
  model: string;
  space: Record<string, LeverDomainSpec>;
  objective: string;
  optimizer: string;
  budget: number;
  reps: number;
  seed?: number;
}

// ---- fetch helpers -----------------------------------------------------------

class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) {
    throw new ApiError(`GET ${path} failed: ${response.status}`, response.status);
  }
  return (await response.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new ApiError(`POST ${path} failed: ${response.status}`, response.status);
  }
  return (await response.json()) as T;
}

export const api = {
  health: () => getJson<{ status: string }>("/api/health"),
  modules: () => getJson<ModulesResponse>("/api/modules"),
  models: () => getJson<ModelSummary[]>("/api/models"),
  adapters: () => getJson<AdaptersResponse>("/api/adapters"),
  floor: (name: string) => getJson<FloorGraph>(`/api/models/${encodeURIComponent(name)}/floor`),
  levers: (name: string) => getJson<Lever[]>(`/api/models/${encodeURIComponent(name)}/levers`),

  run: (request: RunRequest) => postJson<Job<RunResult>>("/api/run", request),
  sweep: (request: SweepRequest) => postJson<Job<SweepResult>>("/api/sweep", request),
  optimize: (request: OptimizeRequest) => postJson<Job<OptimizeResult>>("/api/optimize", request),

  job: <T>(id: string) => getJson<Job<T>>(`/api/jobs/${encodeURIComponent(id)}`),
};

/**
 * Poll a job until it finishes (status !== "running"), calling `onTick` after
 * each poll so callers can show a spinner state. Rejects on job "error" with
 * the job's error string, and on request failure.
 */
export async function pollJob<T>(
  id: string,
  options: { intervalMs?: number; onTick?: (job: Job<T>) => void } = {},
): Promise<T> {
  const intervalMs = options.intervalMs ?? 1500;
  for (;;) {
    const job = await api.job<T>(id);
    options.onTick?.(job);
    if (job.status === "done") {
      if (job.result === null) {
        throw new Error(`job ${id} finished with no result`);
      }
      return job.result;
    }
    if (job.status === "error") {
      throw new Error(job.error ?? `job ${id} failed`);
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
}
