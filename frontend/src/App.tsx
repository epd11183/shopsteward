import { useEffect, useState } from "react";
import { fetchSummary, type Summary } from "./api";
import Ingest from "./pages/Ingest";
import Gate1 from "./pages/Gate1";

export default function App() {
  const [tab, setTab] = useState<"analytics" | "ingest" | "gate1">(
    "analytics",
  );
  return (
    <>
      <header className="border-b">
        <div className="mx-auto max-w-4xl flex items-center gap-6 px-8 py-3">
          <span className="font-semibold">ShopSteward</span>
          <nav className="flex gap-2">
            <TabButton
              label="Analytics"
              active={tab === "analytics"}
              onClick={() => setTab("analytics")}
            />
            <TabButton
              label="Ingest"
              active={tab === "ingest"}
              onClick={() => setTab("ingest")}
            />
            <TabButton
              label="Gate 1"
              active={tab === "gate1"}
              onClick={() => setTab("gate1")}
            />
          </nav>
        </div>
      </header>
      {tab === "analytics" ? (
        <Analytics />
      ) : tab === "ingest" ? (
        <Ingest />
      ) : (
        <Gate1 />
      )}
    </>
  );
}

const TabButton = ({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) => (
  <button
    type="button"
    className={`rounded px-3 py-1.5 text-sm ${
      active ? "bg-gray-900 text-white" : "text-gray-500"
    }`}
    onClick={onClick}
  >
    {label}
  </button>
);

function Analytics() {
  const [s, setS] = useState<Summary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    fetchSummary()
      .then(setS)
      .catch((e) => setErr(String(e)));
  }, []);
  if (err) return <p className="p-8 text-red-600">Failed to load: {err}</p>;
  if (!s) return <p className="p-8">Loading…</p>;
  const maxDay = Math.max(...Object.values(s.revenue_by_day), 1);
  return (
    <main className="mx-auto max-w-4xl p-8 space-y-8">
      <h1 className="text-2xl font-semibold">ShopSteward — Analytics</h1>
      <section className="grid grid-cols-3 gap-4">
        <Stat label="Revenue" value={`$${s.total_revenue_usd.toFixed(2)}`} />
        <Stat label="Orders" value={String(s.total_orders)} />
        <Stat label="Active listings" value={String(s.active_listings)} />
      </section>
      <section>
        <h2 className="mb-2 font-medium">Revenue by day</h2>
        {Object.entries(s.revenue_by_day).map(([day, usd]) => (
          <div key={day} className="flex items-center gap-2 text-sm">
            <span className="w-24 text-gray-500">{day}</span>
            <div
              className="h-4 bg-emerald-500"
              style={{ width: `${(usd / maxDay) * 100}%` }}
            />
            <span>${usd.toFixed(2)}</span>
          </div>
        ))}
      </section>
      <section>
        <h2 className="mb-2 font-medium">Top listings</h2>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500">
              <th>Title</th>
              <th>Views</th>
              <th>Favorites</th>
              <th>Price</th>
            </tr>
          </thead>
          <tbody>
            {s.top_listings.map((l) => (
              <tr key={l.listing_id} className="border-t">
                <td>{l.title}</td>
                <td>{l.views}</td>
                <td>{l.num_favorers}</td>
                <td>${l.price_usd.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
const Stat = ({ label, value }: { label: string; value: string }) => (
  <div className="rounded border p-4">
    <div className="text-sm text-gray-500">{label}</div>
    <div className="text-xl font-semibold">{value}</div>
  </div>
);
