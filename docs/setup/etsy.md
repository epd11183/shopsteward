# Etsy API setup

## 1. Register an app

1. Go to https://www.etsy.com/developers and create a new app.
2. Note the **Keystring** (this is `ETSY_API_KEY`).
3. Request the scopes ShopSteward needs: `listings_r transactions_r shops_r`.

## 2. OAuth 2.0 (authorization code + PKCE)

Etsy's Open API v3 requires OAuth 2.0 with PKCE — there is no static API
token. Walk the standard authorization-code + PKCE flow (generate a code
verifier/challenge, redirect the operator to Etsy's consent screen, exchange
the returned code for an access + refresh token). See Etsy's official
"Quick Start" guide for the exact endpoints; do not hand-roll this from
memory — the docs change.

The exchange yields:

- an **access token** (short-lived)
- a **refresh token** (long-lived; use it to mint new access tokens)

## 3. Environment variables

Store these in your local `.env` (already gitignored, never committed, and
never read by agents in this repo):

```
ETSY_API_KEY=<keystring>
ETSY_SHOP_ID=<numeric shop id>
ETSY_ACCESS_TOKEN=<oauth access token>
ETSY_REFRESH_TOKEN=<oauth refresh token>
```

`src/shopsteward/adapters/etsy/live.py` (`LiveEtsyAdapter`) reads these as
plain constructor arguments — nothing in core or the CLI wires them up
automatically. That's intentional; see below.

## Smoke test (requires operator approval, PRD §8.4)

Live Etsy sync is gated: the CLI's `sync` command refuses to run without an
explicit `--fixtures` path, and `LiveEtsyAdapter` is not imported anywhere
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
