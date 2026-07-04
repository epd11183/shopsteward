import { useEffect, useMemo, useState } from "react";
import {
  fetchMockups,
  mockupImageUrl,
  runMockups,
  type MockupJobResult,
  type MockupRecord,
} from "../api";

type Group = {
  key: string;
  label: string;
  records: MockupRecord[];
};

export default function Mockups() {
  const [mockups, setMockups] = useState<MockupRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [force, setForce] = useState(false);
  const [runResult, setRunResult] = useState<MockupJobResult | null>(null);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  const refetch = async () => {
    setError(null);
    try {
      setMockups(await fetchMockups());
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    refetch();
  }, []);

  const groups: Group[] = useMemo(() => {
    const byKey = new Map<string, MockupRecord[]>();
    for (const m of mockups) {
      const key = m.photo_id ?? m.set_key;
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key)!.push(m);
    }
    return Array.from(byKey.entries())
      .map(([key, records]) => ({
        key,
        label: records[0].photo_id ?? `file:${records[0].landing_file_id.slice(0, 8)}`,
        records,
      }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [mockups]);

  const selected = groups.find((g) => g.key === selectedKey) ?? null;

  const byIntent = useMemo(() => {
    if (!selected) return [];
    const map = new Map<string, MockupRecord[]>();
    for (const m of selected.records) {
      if (!map.has(m.intent)) map.set(m.intent, []);
      map.get(m.intent)!.push(m);
    }
    return Array.from(map.entries()).sort((a, b) => a[0].localeCompare(b[0]));
  }, [selected]);

  const generate = async () => {
    setRunning(true);
    setError(null);
    try {
      setRunResult(await runMockups(undefined, force));
      await refetch();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <main className="mx-auto max-w-4xl p-8 space-y-6">
      <h1 className="text-2xl font-semibold">ShopSteward — Mockups</h1>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <section className="rounded border p-4 space-y-3">
        <div className="flex items-center gap-3">
          <button
            type="button"
            disabled={running}
            className="rounded bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50"
            onClick={generate}
          >
            {running ? "Generating…" : "Generate mockups"}
          </button>
          <label className="flex items-center gap-1 text-sm text-gray-600">
            <input
              type="checkbox"
              checked={force}
              onChange={(e) => setForce(e.target.checked)}
            />
            force
          </label>
        </div>
        {runResult && (
          <p className="text-sm text-gray-500">
            sets completed {runResult.sets_completed}, mockups written{" "}
            {runResult.mockups_written}, skipped (idempotent){" "}
            {runResult.skipped_idempotent}, intents skipped (no template){" "}
            {runResult.intents_skipped_no_template}, templates invalid{" "}
            {runResult.templates_invalid}
          </p>
        )}
      </section>

      {loading ? (
        <p>Loading…</p>
      ) : (
        <section className="flex gap-6">
          <div className="w-48 shrink-0 space-y-1">
            <h2 className="mb-1 font-medium text-sm text-gray-500">Photos</h2>
            {groups.length === 0 && (
              <p className="text-sm text-gray-500">No mockups yet.</p>
            )}
            {groups.map((g) => (
              <button
                key={g.key}
                type="button"
                className={`block w-full truncate rounded border px-2 py-1 text-left text-sm ${
                  selectedKey === g.key ? "bg-gray-900 text-white" : ""
                }`}
                onClick={() => setSelectedKey(g.key)}
              >
                {g.label} ({g.records.length})
              </button>
            ))}
          </div>

          <div className="flex-1 space-y-6">
            {!selected && (
              <p className="text-sm text-gray-500">
                Select a photo to view its mockup set.
              </p>
            )}
            {byIntent.map(([intent, records]) => (
              <div key={intent}>
                <h3 className="mb-2 font-medium capitalize">
                  {intent.replace(/_/g, " ")}
                </h3>
                <div className="grid grid-cols-3 gap-3">
                  {records.map((m) => {
                    const w = m.params.print_w_in;
                    const h = m.params.print_h_in;
                    const title =
                      w != null && h != null ? `${w}in x ${h}in` : undefined;
                    return (
                      <img
                        key={m.path}
                        src={mockupImageUrl(m.path)}
                        alt={`${intent} mockup`}
                        title={title}
                        className="w-full rounded object-cover"
                      />
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
    </main>
  );
}
