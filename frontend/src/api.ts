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

export type LandingReport = {
  observed: number;
  matched: number;
  manual_drops: number;
  invalid: number;
};

export const scanLanding = async (): Promise<LandingReport> => {
  const res = await fetch("/api/pipeline/landing/scan", { method: "POST" });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `landing scan failed (${res.status})`);
  }
  return res.json();
};

// --- M4: staging templates + mockup compositor ---------------------------

export type TemplateRow = {
  user_id: number;
  template_id: string;
  image_path: string | null;
  sidecar_path: string | null;
  sidecar_hash: string | null;
  room_type: string | null;
  style: string | null;
  lighting: string | null;
  orientation: string | null;
  region_count: number | null;
  avg_hue: number | null;
  tags_json: string | null;
  source: string | null;
  status: string;
  reason: string | null;
};

export type TemplateReport = {
  registered: number;
  updated: number;
  invalid: number;
  unchanged: number;
};

export type MockupRecord = {
  path: string;
  photo_id: string | null;
  landing_file_id: string;
  set_key: string;
  intent: string;
  template_id: string | null;
  params: Record<string, number | string | undefined>;
};

export type MockupJobResult = {
  sets_completed: number;
  mockups_written: number;
  skipped_idempotent: number;
  intents_skipped_no_template: number;
  templates_invalid: number;
};

export type SidecarRegion = {
  kind: string;
  quad: number[][];
  region_width_inches: number;
};

export type SidecarPayload = {
  schema: string;
  template_id: string;
  room_type: string;
  style: string;
  lighting: string;
  orientation: string;
  regions: SidecarRegion[];
  tags: string[];
};

export type TemplateAnnotateResponse = {
  report: TemplateReport;
  template: TemplateRow | null;
  invalid_reason: string | null;
};

export const fetchTemplates = async (): Promise<TemplateRow[]> => {
  const res = await fetch("/api/pipeline/templates");
  if (!res.ok) throw new Error(`fetch templates failed (${res.status})`);
  return res.json();
};

export const scanTemplates = async (): Promise<TemplateReport> => {
  const res = await fetch("/api/pipeline/templates/scan", { method: "POST" });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `scan templates failed (${res.status})`);
  }
  return res.json();
};

export const annotateTemplate = async (
  image_path: string,
  sidecar: SidecarPayload,
): Promise<TemplateAnnotateResponse> => {
  const res = await fetch("/api/pipeline/templates/annotate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ image_path, sidecar }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `annotate failed (${res.status})`);
  }
  return res.json();
};

export const templateImageUrl = (path: string): string =>
  `/api/pipeline/templates/image?path=${encodeURIComponent(path)}`;

export const fetchMockups = async (
  photoId?: string,
): Promise<MockupRecord[]> => {
  const url = photoId
    ? `/api/pipeline/mockups?photo_id=${encodeURIComponent(photoId)}`
    : "/api/pipeline/mockups";
  const res = await fetch(url);
  if (!res.ok) throw new Error(`fetch mockups failed (${res.status})`);
  return res.json();
};

export const runMockups = async (
  photoId?: string,
  force?: boolean,
): Promise<MockupJobResult> => {
  const res = await fetch("/api/pipeline/mockups/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ photo_id: photoId ?? null, force: force ?? false }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `run mockups failed (${res.status})`);
  }
  return res.json();
};

export const mockupImageUrl = (path: string): string =>
  `/api/pipeline/mockups/image?path=${encodeURIComponent(path)}`;

// --- M5a slice 4: listings build + Gate 3 ---------------------------------

export type Economics = {
  price: number;
  etsy_fees: number;
  net: number;
};

export type ListingImage = {
  path: string;
  intent: string;
  rank: number;
};

export type Gate3Card = {
  draft_id: string;
  etsy_listing_id: string | null;
  title: string | null;
  tags: string[];
  description: string | null;
  price: number | null;
  currency: string | null;
  margin_floor: number | null;
  economics: Economics | null;
  images: ListingImage[];
  file_source: string | null;
  state: string;
  retry_error: string | null;
};

export type BuildReport = {
  drafts_built: number;
  pushed: number;
  copy_calls: number;
  skipped_idempotent: number;
  push_failed: number;
};

export type Gate3EditFields = {
  title?: string;
  tags?: string[];
  description?: string;
  price?: number;
};

const _postJson = async <T>(url: string, body: unknown): Promise<T> => {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new Error(detail?.detail ?? `request failed (${res.status})`);
  }
  return res.json();
};

export const runListingsBuild = async (): Promise<BuildReport> =>
  _postJson("/api/pipeline/listings/build", {});

export const fetchGate3Queue = async (): Promise<Gate3Card[]> => {
  const res = await fetch("/api/pipeline/gate3/queue");
  if (!res.ok) throw new Error(`fetch gate3 queue failed (${res.status})`);
  return res.json();
};

export const gate3ImageUrl = (draftId: string, path: string): string =>
  `/api/pipeline/gate3/draft/${draftId}/image?path=${encodeURIComponent(path)}`;

export const editGate3Draft = async (
  draft_id: string,
  fields: Gate3EditFields,
): Promise<Gate3Card> => _postJson("/api/pipeline/gate3/edit", { draft_id, ...fields });

export const publishGate3Draft = async (draft_id: string): Promise<Gate3Card> =>
  _postJson("/api/pipeline/gate3/publish", { draft_id });

export const retryGate3Draft = async (draft_id: string): Promise<Gate3Card> =>
  _postJson("/api/pipeline/gate3/retry", { draft_id });
