import { useEffect, useState } from "react";
import {
  fetchEditingJobs,
  fetchPresetFamilies,
  postIngest,
  type EditJobRow,
  type IngestJobRow,
  type IngestReport,
  type JobsResponse,
  type PresetFamily,
} from "../api";

const STATUS_COLOR: Record<string, string> = {
  dispatched: "text-amber-600",
  completed: "text-emerald-600",
  failed: "text-red-600",
};

export default function Ingest() {
  const [path, setPath] = useState("");
  const [mode, setMode] = useState<"hero" | "mass">("hero");
  const [presetFamilies, setPresetFamilies] = useState<PresetFamily[]>([]);
  const [presetFamily, setPresetFamily] = useState("");
  const [event, setEvent] = useState("");
  const [outputFolder, setOutputFolder] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [report, setReport] = useState<IngestReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<JobsResponse | null>(null);

  useEffect(() => {
    fetchPresetFamilies()
      .then((families) => {
        setPresetFamilies(families);
        if (families.length > 0) setPresetFamily(families[0].name);
      })
      .catch(() => setPresetFamilies([]));
  }, []);

  useEffect(() => {
    const poll = () => fetchEditingJobs().then(setJobs).catch(() => {});
    poll();
    const id = setInterval(poll, 3000);
    return () => clearInterval(id);
  }, []);

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    setReport(null);
    try {
      const res = await postIngest({
        path,
        mode,
        preset_family: mode === "mass" ? presetFamily : null,
        event: mode === "mass" ? event : null,
        output_folder: outputFolder || null,
      });
      setReport(res.report);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="mx-auto max-w-4xl p-8 space-y-8">
      <h1 className="text-2xl font-semibold">ShopSteward — Ingest</h1>

      <section className="rounded border p-4 space-y-4">
        <div>
          <label className="block text-sm text-gray-500 mb-1">
            Folder path
          </label>
          <input
            className="w-full rounded border px-3 py-2 text-sm"
            placeholder="C:\photos\2026-06-14_smith-wedding"
            value={path}
            onChange={(e) => setPath(e.target.value)}
          />
        </div>

        <div>
          <label className="block text-sm text-gray-500 mb-1">Mode</label>
          <div className="flex gap-2">
            <button
              type="button"
              className={`rounded border px-3 py-1.5 text-sm ${
                mode === "hero" ? "bg-gray-900 text-white" : ""
              }`}
              onClick={() => setMode("hero")}
            >
              Hero
            </button>
            <button
              type="button"
              className={`rounded border px-3 py-1.5 text-sm ${
                mode === "mass" ? "bg-gray-900 text-white" : ""
              }`}
              onClick={() => setMode("mass")}
            >
              Mass
            </button>
          </div>
        </div>

        {mode === "mass" && (
          <>
            <div>
              <label className="block text-sm text-gray-500 mb-1">
                Preset family
              </label>
              <select
                className="w-full rounded border px-3 py-2 text-sm"
                value={presetFamily}
                onChange={(e) => setPresetFamily(e.target.value)}
              >
                {presetFamilies.map((f) => (
                  <option key={f.name} value={f.name}>
                    {f.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm text-gray-500 mb-1">
                Event name
              </label>
              <input
                className="w-full rounded border px-3 py-2 text-sm"
                placeholder="smith-wedding"
                value={event}
                onChange={(e) => setEvent(e.target.value)}
              />
            </div>
          </>
        )}

        <div>
          <label className="block text-sm text-gray-500 mb-1">
            Output folder (optional)
          </label>
          <input
            className="w-full rounded border px-3 py-2 text-sm"
            placeholder="leave blank for default"
            value={outputFolder}
            onChange={(e) => setOutputFolder(e.target.value)}
          />
        </div>

        <button
          type="button"
          disabled={submitting || !path}
          className="rounded bg-emerald-600 px-4 py-2 text-sm text-white disabled:opacity-50"
          onClick={submit}
        >
          {submitting ? "Ingesting…" : "Ingest"}
        </button>

        {error && <p className="text-sm text-red-600">{error}</p>}

        {report && (
          <div className="text-sm rounded border p-3 bg-gray-50 space-y-1">
            <div>ingest_job_id: {report.ingest_job_id}</div>
            <div>paired: {report.paired}</div>
            <div>duplicates: {report.duplicates}</div>
            <div>unpaired: {report.unpaired}</div>
          </div>
        )}
      </section>

      <section>
        <h2 className="mb-2 font-medium">Photo status</h2>
        <div className="flex flex-wrap gap-2">
          {jobs &&
            Object.entries(jobs.photos).map(([status, count]) => (
              <span
                key={status}
                className="rounded border px-2 py-1 text-xs text-gray-600"
              >
                {status}: {count}
              </span>
            ))}
        </div>
      </section>

      <section>
        <h2 className="mb-2 font-medium">Edit jobs</h2>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500">
              <th>Job</th>
              <th>Preset</th>
              <th>Status</th>
              <th>Photos</th>
            </tr>
          </thead>
          <tbody>
            {jobs?.edit_jobs.map((j: EditJobRow) => (
              <tr key={j.edit_job_id} className="border-t">
                <td>{j.edit_job_id.slice(0, 8)}</td>
                <td>{j.preset_family}</td>
                <td className={STATUS_COLOR[j.status] ?? ""}>{j.status}</td>
                <td>{j.photo_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section>
        <h2 className="mb-2 font-medium">Ingest jobs</h2>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500">
              <th>Path</th>
              <th>Mode</th>
              <th>Paired</th>
              <th>Duplicates</th>
              <th>Unpaired</th>
            </tr>
          </thead>
          <tbody>
            {jobs?.ingest_jobs.map((j: IngestJobRow) => (
              <tr key={j.ingest_job_id} className="border-t">
                <td>{j.path}</td>
                <td>{j.mode}</td>
                <td>{j.paired}</td>
                <td>{j.duplicates}</td>
                <td>{j.unpaired}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
