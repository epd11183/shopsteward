import { useEffect, useState } from "react";
import { fetchSummary, type Summary } from "./api";

export default function App() {
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
