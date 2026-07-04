# Etsy API setup

*Amended 2026-07-04 per PRD §13 decision 35 (token manager, `etsy auth`
command, read-only scopes first).*

## 1. Register an app (one-time, ~10 minutes)

1. Go to https://www.etsy.com/developers/register and sign in with your
   regular Etsy account.
2. Register a new application (e.g. "ShopSteward"). Under **Manage your
   apps** you'll find the **API Key keystring** (`ETSY_API_KEY`) and a
   **shared secret**. Treat both like passwords.
3. Add the callback URL `http://localhost:8322/oauth/redirect` to the app's
   redirect URIs (used by `shopsteward etsy auth`).
4. Notes:
   - The keystring is **not active until Etsy approves** the registration
     (usually quick).
   - New apps start with **personal access** — sufficient for managing your
     own shop. Do NOT request commercial access; it's only for apps serving
     other sellers.
   - Apps with no successful API request in **6 months are marked dormant
     and banned**. Moot once scheduled analytics pulls run.

## 2. One-time consent: `shopsteward etsy auth`

Etsy Open API v3 uses OAuth 2.0 authorization-code + PKCE. The
`shopsteward etsy auth` command (pre-M5 Etsy wiring) does the whole dance:

1. Generates the PKCE verifier/challenge and a state value.
2. Starts a localhost listener on port 8322 and opens Etsy's consent page
   in your browser — click **Allow** while logged in as your shop account.
3. Exchanges the returned code for tokens and writes them to the token
   store (below).
4. Auto-discovers your shop id from the token's user-id prefix and stores
   it (override with `ETSY_SHOP_ID` if you ever need to).

Scopes requested now (read-only, matches the §8.4 smoke test):
`listings_r transactions_r shops_r`. M5 re-runs consent once to add the
write scopes listing creation needs — least privilege until then.

## 3. Token lifetimes and the token store

- **Access token: 1 hour.** Never configure it by hand.
- **Refresh token: 90 days, and it ROTATES** — every refresh returns a new
  refresh token that replaces the old one.

Because of the rotation, tokens live in `data/etsy_tokens.json` — runtime-
owned, gitignored, and read-denied to agents (same protection class as
`.env`). The `EtsyTokenStore` refreshes the access token automatically when
expired and persists the rotated refresh token immediately after every
refresh. **Tokens must never be written to the event log** — events are
append-only, so a secret in an event can never be deleted.

Your `.env` holds only:

```
ETSY_API_KEY=<keystring>
# optional override; normally auto-discovered by `etsy auth`:
# ETSY_SHOP_ID=<numeric shop id>
```

## Smoke test (requires operator approval, PRD §8.4)

Live Etsy sync is gated: the CLI's `sync` command refuses to run without an
explicit `--fixtures` path, and `LiveEtsyAdapter` is not wired anywhere
outside its own tests. Before wiring it in:

1. Get explicit operator (Eric) sign-off to hit the real API.
2. Run one manual, read-only pass (`get_shop`, `list_listings`,
   `list_receipts`) against the real shop, outside of the test suite.
3. Record the raw responses locally (never commit them as-is).
4. Scrub every real identifier — shop id, listing ids, receipt ids, buyer
   info, titles, tags — and replace with fixture-style placeholders
   consistent with `tests/fixtures/etsy/*.json`.
5. Replace the hand-written fixtures with the scrubbed, real-shaped ones so
   the adapter contract keeps matching what Etsy actually returns.
6. Only after that: wire `LiveEtsyAdapter` behind an explicit CLI flag or
   config toggle, defaulting off.
