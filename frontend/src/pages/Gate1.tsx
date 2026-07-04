import { useCallback, useEffect, useRef, useState } from "react";
import {
  decideGate1,
  fetchGate1Queue,
  gate1PreviewUrl,
  runScoring,
  scanLanding,
  undoGate1,
  type Gate1Card,
} from "../api";

type LastAction = {
  photo_id: string;
  decision: "approve" | "reject" | "snooze";
  base_name: string;
  dispatch_state: string | null;
};

const compositeColor = (composite: number): string => {
  if (composite >= 80) return "bg-emerald-600 text-white";
  if (composite >= 60) return "bg-amber-500 text-white";
  return "bg-gray-400 text-white";
};

export default function Gate1() {
  const [queue, setQueue] = useState<Gate1Card[]>([]);
  const [snoozed, setSnoozed] = useState<Gate1Card[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastAction, setLastAction] = useState<LastAction | null>(null);
  const [showSnoozed, setShowSnoozed] = useState(false);
  const [running, setRunning] = useState(false);
  const busyRef = useRef(false);

  const refetch = useCallback(async () => {
    setError(null);
    try {
      const [pending, snoozedRows] = await Promise.all([
        fetchGate1Queue("pending"),
        fetchGate1Queue("snoozed"),
      ]);
      setQueue(pending);
      setSnoozed(snoozedRows);
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refetch();
  }, [refetch]);

  const current = queue[0] ?? null;
  const next = queue[1] ?? null;

  const decide = useCallback(
    async (decision: "approve" | "reject" | "snooze") => {
      if (!current || busyRef.current) return;
      busyRef.current = true;
      const card = current;
      setQueue((prev) => prev.slice(1));
      try {
        const updated = await decideGate1(card.photo_id, decision);
        setLastAction({
          photo_id: card.photo_id,
          decision,
          base_name: card.base_name,
          dispatch_state:
            decision === "approve"
              ? (updated.dispatch_state ?? "dispatched")
              : null,
        });
        if (decision === "snooze") {
          setSnoozed((prev) => [updated, ...prev]);
        }
      } catch (e) {
        setError(String(e instanceof Error ? e.message : e));
        await refetch();
      } finally {
        busyRef.current = false;
      }
    },
    [current, refetch],
  );

  const undoLast = useCallback(async () => {
    if (!lastAction || busyRef.current) return;
    busyRef.current = true;
    try {
      await undoGate1(lastAction.photo_id);
      setLastAction(null);
      await refetch();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      busyRef.current = false;
    }
  }, [lastAction, refetch]);

  const requeue = useCallback(
    async (photo_id: string) => {
      if (busyRef.current) return;
      busyRef.current = true;
      try {
        await decideGate1(photo_id, "requeue");
        await refetch();
      } catch (e) {
        setError(String(e instanceof Error ? e.message : e));
      } finally {
        busyRef.current = false;
      }
    },
    [refetch],
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.isContentEditable)
      ) {
        return;
      }
      switch (event.key.toLowerCase()) {
        case "a":
          decide("approve");
          break;
        case "r":
          decide("reject");
          break;
        case "s":
          decide("snooze");
          break;
        case "z":
          undoLast();
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [decide, undoLast]);

  const runScoringAndRefetch = async () => {
    setRunning(true);
    setError(null);
    try {
      await runScoring();
      await refetch();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setRunning(false);
    }
  };

  const scanLandingAndRefetch = async () => {
    setRunning(true);
    setError(null);
    try {
      await scanLanding();
      await refetch();
    } catch (e) {
      setError(String(e instanceof Error ? e.message : e));
    } finally {
      setRunning(false);
    }
  };

  if (loading) return <p className="p-8">Loading…</p>;

  return (
    <main className="mx-auto max-w-4xl p-8 space-y-6">
      <h1 className="text-2xl font-semibold">ShopSteward — Gate 1</h1>

      {error && <p className="text-sm text-red-600">{error}</p>}

      {lastAction && (
        <div className="rounded border bg-gray-50 px-3 py-2 text-sm flex items-center justify-between">
          <span>
            {lastAction.base_name} — {lastAction.decision}
            {lastAction.dispatch_state && (
              <span className="ml-2 rounded bg-emerald-100 px-2 py-0.5 text-emerald-700">
                {lastAction.dispatch_state} → Lightroom
              </span>
            )}
          </span>
          <span className="text-gray-500">
            press <kbd className="rounded border px-1">z</kbd> to undo
          </span>
        </div>
      )}

      {!current ? (
        <section className="rounded border p-8 text-center space-y-4">
          <p className="text-lg font-medium">Queue is clear</p>
          <div className="flex justify-center gap-3">
            <button
              type="button"
              disabled={running}
              className="rounded bg-gray-900 px-4 py-2 text-sm text-white disabled:opacity-50"
              onClick={runScoringAndRefetch}
            >
              {running ? "Running…" : "Run scoring"}
            </button>
            <button
              type="button"
              disabled={running}
              className="rounded border px-4 py-2 text-sm disabled:opacity-50"
              onClick={scanLandingAndRefetch}
            >
              {running ? "Scanning…" : "Scan landing folder"}
            </button>
          </div>
        </section>
      ) : (
        <section className="rounded border p-4 space-y-4">
          <div className="relative">
            <img
              src={gate1PreviewUrl(current.photo_id)}
              alt={current.base_name}
              className="mx-auto max-h-[60vh] object-contain rounded"
            />
            <span
              className={`absolute top-2 left-2 rounded px-2 py-1 text-sm font-semibold ${compositeColor(
                current.composite,
              )}`}
            >
              {Math.round(current.composite)}
              {current.escalated && <span className="ml-1">Pro ✓</span>}
            </span>
          </div>

          <div className="text-sm text-gray-500">{current.base_name}</div>

          <div className="flex flex-wrap gap-2 text-xs">
            {current.subject && (
              <span className="rounded border px-2 py-1">
                {current.subject}
              </span>
            )}
            {current.strongest_room_style && (
              <span className="rounded border px-2 py-1">
                {current.strongest_room_style}
              </span>
            )}
            {current.one_risk && (
              <span className="rounded border border-red-300 px-2 py-1 text-red-600">
                {current.one_risk}
              </span>
            )}
          </div>

          {current.rationale && (
            <p className="text-sm italic text-gray-600">
              {current.rationale}
            </p>
          )}

          <div className="space-y-1">
            <MiniBar label="Technical" value={current.technical} />
            <MiniBar label="Commercial" value={current.commercial} />
          </div>

          <div className="flex gap-2 pt-2">
            <button
              type="button"
              className="rounded bg-emerald-600 px-4 py-2 text-sm text-white"
              onClick={() => decide("approve")}
            >
              Approve (A)
            </button>
            <button
              type="button"
              className="rounded bg-red-600 px-4 py-2 text-sm text-white"
              onClick={() => decide("reject")}
            >
              Reject (R)
            </button>
            <button
              type="button"
              className="rounded border px-4 py-2 text-sm"
              onClick={() => decide("snooze")}
            >
              Snooze (S)
            </button>
          </div>
        </section>
      )}

      {next && (
        <img
          src={gate1PreviewUrl(next.photo_id)}
          alt=""
          className="hidden"
          aria-hidden="true"
        />
      )}

      <section>
        <button
          type="button"
          className="text-sm font-medium text-gray-700"
          onClick={() => setShowSnoozed((v) => !v)}
        >
          {showSnoozed ? "▾" : "▸"} Snoozed ({snoozed.length})
        </button>
        {showSnoozed && (
          <div className="mt-3 flex flex-wrap gap-3">
            {snoozed.length === 0 && (
              <p className="text-sm text-gray-500">Nothing snoozed.</p>
            )}
            {snoozed.map((card) => (
              <button
                key={card.photo_id}
                type="button"
                className="rounded border p-1 text-left hover:bg-gray-50"
                onClick={() => requeue(card.photo_id)}
                title="Click to requeue"
              >
                <img
                  src={gate1PreviewUrl(card.photo_id)}
                  alt={card.base_name}
                  className="h-24 w-24 object-cover rounded"
                />
                <div className="text-xs text-gray-500 mt-1">
                  {Math.round(card.composite)}
                </div>
              </button>
            ))}
          </div>
        )}
      </section>
    </main>
  );
}

const MiniBar = ({
  label,
  value,
}: {
  label: string;
  value: number | null;
}) => (
  <div className="flex items-center gap-2 text-xs">
    <span className="w-20 text-gray-500">{label}</span>
    <div className="h-2 flex-1 rounded bg-gray-100">
      <div
        className="h-2 rounded bg-gray-700"
        style={{ width: `${value ?? 0}%` }}
      />
    </div>
    <span className="w-8 text-right text-gray-500">
      {value === null ? "—" : Math.round(value)}
    </span>
  </div>
);
