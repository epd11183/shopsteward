export type ListingRow = {
  listing_id: number;
  title: string;
  views: number;
  num_favorers: number;
  price_usd: number;
};
export type Summary = {
  total_revenue_usd: number;
  total_orders: number;
  active_listings: number;
  revenue_by_day: Record<string, number>;
  top_listings: ListingRow[];
};
export const fetchSummary = async (): Promise<Summary> =>
  (await fetch("/api/analytics/summary")).json();

export type PresetFamily = {
  name: string;
  description: string;
  settings: Record<string, number | string>;
};

export type IngestReport = {
  ingest_job_id: string;
  mode: string;
  paired: number;
  duplicates: number;
  unpaired: number;
  photo_ids: string[];
};

export type IngestRequest = {
  path: string;
  mode: string;
  preset_family?: string | null;
  event?: string | null;
  output_folder?: string | null;
};

export type IngestResponse = {
  report: IngestReport;
  edit_job_id: string | null;
};

export type IngestJobRow = {
  user_id: number;
  ingest_job_id: string;
  path: string;
  mode: string;
  paired: number;
  duplicates: number;
  unpaired: number;
  status: string;
};

export type EditJobRow = {
  user_id: number;
  edit_job_id: string;
  preset_family: string;
  mode: string;
  photo_count: number;
  status: string;
  error: string | null;
};

export type JobsResponse = {
  ingest_jobs: IngestJobRow[];
  edit_jobs: EditJobRow[];
  photos: Record<string, number>;
};

export const fetchPresetFamilies = async (): Promise<PresetFamily[]> =>
  (await fetch("/api/editing/preset-families")).json();

export const postIngest = async (
  request: IngestRequest,
): Promise<IngestResponse> => {
  const res = await fetch("/api/editing/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `ingest failed (${res.status})`);
  }
  return res.json();
};

export const fetchEditingJobs = async (): Promise<JobsResponse> =>
  (await fetch("/api/editing/jobs")).json();

export type Gate1Card = {
  photo_id: string;
  base_name: string;
  composite: number;
  technical: number | null;
  commercial: number | null;
  subject: string;
  strongest_room_style: string;
  one_risk: string;
  rationale: string;
  escalated: boolean;
  state: string;
  edit_job_id: string | null;
  dispatch_state: string | null;
};

export type ScoringRunResult = {
  scored: number;
  queued: number;
  escalated: number;
  failed: number;
  cap_hit: boolean;
};

export type LandingReport = {
  observed: number;
  matched: number;
  manual_drops: number;
  invalid: number;
};

export const gate1PreviewUrl = (photo_id: string): string =>
  `/api/pipeline/gate1/photo/${photo_id}/preview`;

export const fetchGate1Queue = async (
  state: "pending" | "snoozed",
): Promise<Gate1Card[]> => {
  const res = await fetch(`/api/pipeline/gate1/queue?state=${state}`);
  if (!res.ok) throw new Error(`gate1 queue failed (${res.status})`);
  return res.json();
};

export const decideGate1 = async (
  photo_id: string,
  decision: "approve" | "reject" | "snooze" | "requeue",
): Promise<Gate1Card> => {
  const res = await fetch("/api/pipeline/gate1/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ photo_id, decision }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `decide failed (${res.status})`);
  }
  return res.json();
};

export const undoGate1 = async (photo_id: string): Promise<unknown> => {
  const res = await fetch("/api/pipeline/gate1/undo", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ photo_id }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `undo failed (${res.status})`);
  }
  return res.json();
};

export const runScoring = async (): Promise<ScoringRunResult> => {
  const res = await fetch("/api/pipeline/score/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `score run failed (${res.status})`);
  }
  return res.json();
};

export const scanLanding = async (): Promise<LandingReport> => {
  const res = await fetch("/api/pipeline/landing/scan", { method: "POST" });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `landing scan failed (${res.status})`);
  }
  return res.json();
};
