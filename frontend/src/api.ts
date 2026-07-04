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
