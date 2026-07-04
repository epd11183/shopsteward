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
