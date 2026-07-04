import { useEffect, useRef, useState } from "react";
import {
  annotateTemplate,
  fetchTemplates,
  scanTemplates,
  templateImageUrl,
  type SidecarRegion,
  type TemplateReport,
  type TemplateRow,
} from "../api";

type Point = [number, number];
type DraftRegion = { corners: Point[]; region_width_inches: number };

const ORIENTATIONS = ["landscape", "portrait", "square", "any"] as const;

const emptyRegion = (): DraftRegion => ({ corners: [], region_width_inches: 24 });

export default function Templates() {
  const [templates, setTemplates] = useState<TemplateRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanReport, setScanReport] = useState<TemplateReport | null>(null);
  const [mode, setMode] = useState<"grid" | "annotate">("grid");

  const refetch = async () => {
    setError(null);
    try {
      setTemplates(await fetchTemplates());
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refetch();
  }, []);

  const rescan = async () => {
    setScanning(true);
    setError(null);
    try {
      setScanReport(await scanTemplates());
      await refetch();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setScanning(false);
    }
  };

  return (
    <main className="mx-auto max-w-4xl p-8 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">ShopSteward — Templates</h1>
        <div className="flex gap-2">
          <button
            type="button"
            className={`rounded border px-3 py-1.5 text-sm ${
              mode === "grid" ? "bg-gray-900 text-white" : ""
            }`}
            onClick={() => setMode("grid")}
          >
            Grid
          </button>
          <button
            type="button"
            className={`rounded border px-3 py-1.5 text-sm ${
              mode === "annotate" ? "bg-gray-900 text-white" : ""
            }`}
            onClick={() => setMode("annotate")}
          >
            Annotate
          </button>
        </div>
      </div>

      {error && <p className="text-sm text-red-600">{error}</p>}

      {mode === "grid" ? (
        <>
          <div className="flex items-center gap-3">
            <button
              type="button"
              disabled={scanning}
              className="rounded bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50"
              onClick={rescan}
            >
              {scanning ? "Scanning…" : "Rescan"}
            </button>
            {scanReport && (
              <span className="text-sm text-gray-500">
                registered {scanReport.registered}, updated{" "}
                {scanReport.updated}, invalid {scanReport.invalid}, unchanged{" "}
                {scanReport.unchanged}
              </span>
            )}
          </div>

          {loading ? (
            <p>Loading…</p>
          ) : (
            <div className="grid grid-cols-3 gap-4">
              {templates.map((t) => (
                <div key={t.template_id} className="rounded border p-3 space-y-2">
                  {t.image_path ? (
                    <img
                      src={templateImageUrl(t.image_path)}
                      alt={t.template_id}
                      className="h-32 w-full rounded object-cover"
                    />
                  ) : (
                    <div className="h-32 w-full rounded bg-gray-100" />
                  )}
                  <div className="text-sm font-medium truncate">
                    {t.template_id}
                  </div>
                  <div className="flex flex-wrap gap-1 text-xs">
                    {t.room_type && (
                      <span className="rounded border px-2 py-0.5">
                        {t.room_type}
                      </span>
                    )}
                    {t.style && (
                      <span className="rounded border px-2 py-0.5">
                        {t.style}
                      </span>
                    )}
                    {t.orientation && (
                      <span className="rounded border px-2 py-0.5">
                        {t.orientation}
                      </span>
                    )}
                    {t.region_count != null && (
                      <span className="rounded border px-2 py-0.5">
                        {t.region_count} region{t.region_count === 1 ? "" : "s"}
                      </span>
                    )}
                  </div>
                  {t.status === "invalid" && (
                    <div className="rounded bg-red-50 px-2 py-1 text-xs text-red-600">
                      invalid: {t.reason}
                    </div>
                  )}
                </div>
              ))}
              {templates.length === 0 && (
                <p className="col-span-3 text-sm text-gray-500">
                  No templates registered yet. Rescan to pick up sidecars.
                </p>
              )}
            </div>
          )}
        </>
      ) : (
        <Annotator onSaved={refetch} />
      )}
    </main>
  );
}

function Annotator({ onSaved }: { onSaved: () => void }) {
  const [imagePath, setImagePath] = useState("");
  const [naturalSize, setNaturalSize] = useState<{ w: number; h: number } | null>(
    null,
  );
  const [displaySize, setDisplaySize] = useState<{ w: number; h: number } | null>(
    null,
  );
  const [regions, setRegions] = useState<DraftRegion[]>([emptyRegion()]);
  const [templateId, setTemplateId] = useState("");
  const [roomType, setRoomType] = useState("");
  const [style, setStyle] = useState("");
  const [lighting, setLighting] = useState("");
  const [orientation, setOrientation] = useState<(typeof ORIENTATIONS)[number]>(
    "landscape",
  );
  const [tags, setTags] = useState("");
  const [saving, setSaving] = useState(false);
  const [verdict, setVerdict] = useState<{
    valid: boolean;
    reason: string | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);

  const onImageLoad = () => {
    const img = imgRef.current;
    if (!img) return;
    setNaturalSize({ w: img.naturalWidth, h: img.naturalHeight });
    setDisplaySize({ w: img.clientWidth, h: img.clientHeight });
  };

  const onImageClick = (e: React.MouseEvent<HTMLImageElement>) => {
    if (!naturalSize || !displaySize) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const dispX = e.clientX - rect.left;
    const dispY = e.clientY - rect.top;
    const natX = (dispX * naturalSize.w) / displaySize.w;
    const natY = (dispY * naturalSize.h) / displaySize.h;
    setRegions((prev) => {
      const next = prev.map((r) => ({ ...r, corners: [...r.corners] }));
      const last = next[next.length - 1];
      if (last.corners.length >= 4) return prev;
      last.corners.push([natX, natY]);
      return next;
    });
  };

  const addRegion = () => {
    setRegions((prev) => {
      const last = prev[prev.length - 1];
      if (!last || last.corners.length < 4 || prev.length >= 6) return prev;
      return [...prev, emptyRegion()];
    });
  };

  const undoLastCorner = () => {
    setRegions((prev) => {
      const next = prev.map((r) => ({ ...r, corners: [...r.corners] }));
      for (let i = next.length - 1; i >= 0; i--) {
        if (next[i].corners.length > 0) {
          next[i].corners.pop();
          if (next[i].corners.length === 0 && i > 0) next.pop();
          return next;
        }
      }
      return prev;
    });
  };

  const setRegionWidth = (index: number, width: number) => {
    setRegions((prev) =>
      prev.map((r, i) => (i === index ? { ...r, region_width_inches: width } : r)),
    );
  };

  const scaleHint = (region: DraftRegion): string | null => {
    if (region.corners.length < 2) return null;
    const [tl, tr] = region.corners;
    const topEdgePx = Math.hypot(tr[0] - tl[0], tr[1] - tl[1]);
    if (!region.region_width_inches) return null;
    const ppi = topEdgePx / region.region_width_inches;
    const spanPx24 = Math.round(24 * ppi);
    return `implied ppi ≈ ${ppi.toFixed(1)} — a 24-inch print spans ~${spanPx24}px`;
  };

  const toDisplay = (pt: Point): Point => {
    if (!naturalSize || !displaySize) return pt;
    return [
      (pt[0] * displaySize.w) / naturalSize.w,
      (pt[1] * displaySize.h) / naturalSize.h,
    ];
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    setVerdict(null);
    try {
      const completeRegions: SidecarRegion[] = regions
        .filter((r) => r.corners.length === 4)
        .map((r) => ({
          kind: "wall_print",
          quad: r.corners,
          region_width_inches: r.region_width_inches,
        }));
      const res = await annotateTemplate(imagePath, {
        schema: "shopsteward.stagingtemplate/1",
        template_id: templateId,
        room_type: roomType,
        style,
        lighting,
        orientation,
        regions: completeRegions,
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
      });
      if (res.invalid_reason) {
        setVerdict({ valid: false, reason: res.invalid_reason });
      } else {
        setVerdict({ valid: true, reason: null });
      }
      onSaved();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="space-y-4">
      <div>
        <label className="block text-sm text-gray-500 mb-1">
          Image path (must be inside a template library directory)
        </label>
        <input
          className="w-full rounded border px-3 py-2 text-sm"
          placeholder="config/defaults/staging_templates/livingroom-warm-01.jpg"
          value={imagePath}
          onChange={(e) => {
            setImagePath(e.target.value);
            setNaturalSize(null);
            setDisplaySize(null);
          }}
        />
      </div>

      {imagePath && (
        <div className="relative inline-block">
          <img
            ref={imgRef}
            src={templateImageUrl(imagePath)}
            alt="template"
            className="max-w-full rounded cursor-crosshair"
            onLoad={onImageLoad}
            onClick={onImageClick}
          />
          {displaySize && (
            <svg
              className="pointer-events-none absolute left-0 top-0"
              width={displaySize.w}
              height={displaySize.h}
            >
              {regions.map((region, ri) => {
                const pts = region.corners.map(toDisplay);
                return (
                  <g key={ri}>
                    {pts.length >= 2 && (
                      <polygon
                        points={pts.map((p) => p.join(",")).join(" ")}
                        fill="rgba(16,185,129,0.15)"
                        stroke="rgb(16,185,129)"
                        strokeWidth={2}
                      />
                    )}
                    {pts.map((p, i) => (
                      <circle
                        key={i}
                        cx={p[0]}
                        cy={p[1]}
                        r={5}
                        fill="rgb(16,185,129)"
                      />
                    ))}
                  </g>
                );
              })}
            </svg>
          )}
        </div>
      )}

      <div className="flex gap-2">
        <button
          type="button"
          className="rounded border px-3 py-1.5 text-sm"
          onClick={addRegion}
        >
          Add region
        </button>
        <button
          type="button"
          className="rounded border px-3 py-1.5 text-sm"
          onClick={undoLastCorner}
        >
          Undo last corner
        </button>
      </div>

      <div className="space-y-2">
        {regions.map((region, i) => (
          <div key={i} className="flex items-center gap-3 text-sm">
            <span className="w-20 text-gray-500">
              Region {i + 1} ({region.corners.length}/4)
            </span>
            <label className="flex items-center gap-1">
              width (in)
              <input
                type="number"
                className="w-20 rounded border px-2 py-1"
                value={region.region_width_inches}
                onChange={(e) =>
                  setRegionWidth(i, Number(e.target.value) || 0)
                }
              />
            </label>
            <span className="text-gray-500">{scaleHint(region)}</span>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-3">
        <Field label="template_id" value={templateId} onChange={setTemplateId} />
        <Field label="room_type" value={roomType} onChange={setRoomType} />
        <Field label="style" value={style} onChange={setStyle} />
        <Field label="lighting" value={lighting} onChange={setLighting} />
        <div>
          <label className="block text-sm text-gray-500 mb-1">
            orientation
          </label>
          <select
            className="w-full rounded border px-3 py-2 text-sm"
            value={orientation}
            onChange={(e) =>
              setOrientation(e.target.value as (typeof ORIENTATIONS)[number])
            }
          >
            {ORIENTATIONS.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        </div>
        <Field label="tags (comma-separated)" value={tags} onChange={setTags} />
      </div>

      <button
        type="button"
        disabled={saving || !imagePath || !templateId}
        className="rounded bg-emerald-600 px-4 py-2 text-sm text-white disabled:opacity-50"
        onClick={save}
      >
        {saving ? "Saving…" : "Save"}
      </button>

      {error && <p className="text-sm text-red-600">{error}</p>}

      {verdict &&
        (verdict.valid ? (
          <p className="rounded bg-emerald-50 px-3 py-2 text-sm text-emerald-700">
            valid
          </p>
        ) : (
          <p className="rounded bg-red-50 px-3 py-2 text-sm text-red-600">
            invalid: {verdict.reason}
          </p>
        ))}
    </section>
  );
}

const Field = ({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) => (
  <div>
    <label className="block text-sm text-gray-500 mb-1">{label}</label>
    <input
      className="w-full rounded border px-3 py-2 text-sm"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  </div>
);
