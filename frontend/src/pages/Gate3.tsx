import { useCallback, useEffect, useState } from "react";
import {
  editGate3Draft,
  fetchGate3Queue,
  gate3ImageUrl,
  publishGate3Draft,
  retryGate3Draft,
  runListingsBuild,
  type BuildReport,
  type Gate3Card,
} from "../api";

export default function Gate3() {
  const [queue, setQueue] = useState<Gate3Card[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [buildResult, setBuildResult] = useState<BuildReport | null>(null);
  const [busyDraftId, setBusyDraftId] = useState<string | null>(null);

  const refetch = useCallback(async () => {
    setError(null);
    try {
      setQueue(await fetchGate3Queue());
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refetch();
  }, [refetch]);

  const runBuild = async () => {
    setRunning(true);
    setError(null);
    try {
      setBuildResult(await runListingsBuild());
      await refetch();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setRunning(false);
    }
  };

  const publishable = queue.filter((c) => c.state === "pushed");
  const failed = queue.filter(
    (c) => c.state === "push_failed" || c.state === "publish_failed",
  );

  const publish = async (draftId: string) => {
    if (busyDraftId) return;
    setBusyDraftId(draftId);
    setError(null);
    try {
      await refetchAfter(() => publishGate3Draft(draftId));
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusyDraftId(null);
    }
  };

  const retry = async (draftId: string) => {
    if (busyDraftId) return;
    setBusyDraftId(draftId);
    setError(null);
    try {
      await refetchAfter(() => retryGate3Draft(draftId));
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setBusyDraftId(null);
    }
  };

  const refetchAfter = async (action: () => Promise<Gate3Card>) => {
    await action();
    await refetch();
  };

  if (loading) return <p className="p-8">Loading…</p>;

  return (
    <main className="mx-auto max-w-4xl p-8 space-y-6">
      <h1 className="text-2xl font-semibold">ShopSteward — Gate 3</h1>

      {error && <p className="text-sm text-red-600">{error}</p>}

      <section className="rounded border p-4 flex items-center gap-3">
        <button
          type="button"
          disabled={running}
          className="rounded bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50"
          onClick={runBuild}
        >
          {running ? "Building…" : "Build listings"}
        </button>
        {buildResult && (
          <p className="text-sm text-gray-500">
            built {buildResult.drafts_built}, pushed {buildResult.pushed},
            push failed {buildResult.push_failed}
          </p>
        )}
      </section>

      {publishable.length === 0 ? (
        <p className="text-sm text-gray-500">Nothing waiting to publish.</p>
      ) : (
        <div className="space-y-4">
          {publishable.map((card) => (
            <Gate3CardView
              key={card.draft_id}
              card={card}
              busy={busyDraftId === card.draft_id}
              onPublish={() => publish(card.draft_id)}
              onSaved={refetch}
            />
          ))}
        </div>
      )}

      {failed.length > 0 && (
        <section>
          <h2 className="mb-2 font-medium text-sm text-gray-500">
            Failed ({failed.length})
          </h2>
          <div className="space-y-2">
            {failed.map((card) => (
              <div
                key={card.draft_id}
                className="rounded border border-red-300 p-3 text-sm flex items-center justify-between gap-3"
              >
                <div>
                  <div className="font-medium">
                    {card.title ?? card.draft_id}
                  </div>
                  <div className="text-red-600">
                    {card.state}: {card.retry_error ?? "unknown error"}
                  </div>
                </div>
                <button
                  type="button"
                  disabled={busyDraftId === card.draft_id}
                  className="shrink-0 rounded border px-3 py-1.5 text-xs disabled:opacity-50"
                  onClick={() => retry(card.draft_id)}
                >
                  Retry
                </button>
              </div>
            ))}
          </div>
        </section>
      )}
    </main>
  );
}

const Gate3CardView = ({
  card,
  busy,
  onPublish,
  onSaved,
}: {
  card: Gate3Card;
  busy: boolean;
  onPublish: () => void;
  onSaved: () => void;
}) => {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(card.title ?? "");
  const [tags, setTags] = useState(card.tags.join(", "));
  const [description, setDescription] = useState(card.description ?? "");
  const [price, setPrice] = useState(String(card.price ?? ""));
  const [descOpen, setDescOpen] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const hero = card.images[0];

  const save = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      const fields: Parameters<typeof editGate3Draft>[1] = {};
      if (title !== card.title) fields.title = title;
      const newTags = tags
        .split(",")
        .map((t) => t.trim())
        .filter((t) => t.length > 0);
      if (newTags.join(",") !== card.tags.join(",")) fields.tags = newTags;
      if (description !== card.description) fields.description = description;
      const priceNum = Number(price);
      if (!Number.isNaN(priceNum) && priceNum !== card.price)
        fields.price = priceNum;

      if (Object.keys(fields).length > 0) {
        await editGate3Draft(card.draft_id, fields);
        onSaved();
      }
      setEditing(false);
    } catch (e) {
      setSaveError(String(e instanceof Error ? e.message : e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="rounded border p-4 space-y-3">
      <div className="flex gap-4">
        {hero && (
          <img
            src={gate3ImageUrl(card.draft_id, hero.path)}
            alt={card.title ?? "listing"}
            className="h-32 w-32 shrink-0 rounded object-cover"
          />
        )}
        <div className="flex-1 space-y-1">
          {editing ? (
            <input
              className="w-full rounded border px-2 py-1 text-sm font-medium"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          ) : (
            <div className="font-medium">{card.title}</div>
          )}

          {card.economics && (
            <div className="text-sm text-gray-600">
              ${card.economics.price.toFixed(2)} − fees ≈ $
              {card.economics.net.toFixed(2)} margin
            </div>
          )}

          {editing ? (
            <input
              className="w-full rounded border px-2 py-1 text-xs"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="comma-separated tags"
            />
          ) : (
            <div className="flex flex-wrap gap-1 text-xs">
              {card.tags.map((tag) => (
                <span key={tag} className="rounded border px-1.5 py-0.5">
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>

      <div>
        <button
          type="button"
          className="text-xs font-medium text-gray-500"
          onClick={() => setDescOpen((v) => !v)}
        >
          {descOpen ? "▾" : "▸"} Description
        </button>
        {descOpen &&
          (editing ? (
            <textarea
              className="mt-1 w-full rounded border px-2 py-1 text-sm"
              rows={4}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          ) : (
            <p className="mt-1 text-sm text-gray-600 whitespace-pre-wrap">
              {card.description}
            </p>
          ))}
      </div>

      {editing && (
        <div className="flex items-center gap-2 text-sm">
          <label className="text-gray-500">Price</label>
          <input
            className="w-24 rounded border px-2 py-1"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
          />
          {card.margin_floor != null && (
            <span className="text-xs text-gray-500">
              floor ${card.margin_floor.toFixed(2)}
            </span>
          )}
        </div>
      )}

      {saveError && <p className="text-sm text-red-600">{saveError}</p>}

      <div className="flex gap-2 pt-1">
        <button
          type="button"
          disabled={busy}
          className="rounded bg-emerald-600 px-4 py-2 text-sm text-white disabled:opacity-50"
          onClick={onPublish}
        >
          {busy ? "Publishing…" : "Publish"}
        </button>
        {editing ? (
          <>
            <button
              type="button"
              disabled={saving}
              className="rounded border px-4 py-2 text-sm disabled:opacity-50"
              onClick={save}
            >
              {saving ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              className="rounded px-4 py-2 text-sm text-gray-500"
              onClick={() => setEditing(false)}
            >
              Cancel
            </button>
          </>
        ) : (
          <button
            type="button"
            className="rounded border px-4 py-2 text-sm"
            onClick={() => setEditing(true)}
          >
            Edit
          </button>
        )}
      </div>
    </section>
  );
};
