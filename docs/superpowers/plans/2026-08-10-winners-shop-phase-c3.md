# Winners-shop Phase C3 Implementation Plan — live Gelato adapter

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Implement `LiveGelatoAdapter` (httpx over the Gelato Ecommerce API) satisfying the `PodAdapter` protocol, wire `build_pod_adapter(live=True)` + a `live_gelato` gate, and respx-test it. The actual live call is an operator smoke, not CI.

**Architecture:** `adapters/pod/live.py` (httpx only, no SDK — OpenRouter/Etsy live-adapter precedent). Factory + gate in `pipeline/`. Fakes/respx in tests; nothing calls Gelato live in the suite.

**Spec:** `docs/superpowers/specs/2026-08-10-winners-folder-shop-phase-c3-design.md`

## Contract to satisfy (from `adapters/pod/interface.py` + `models.py`)
- `PodAdapter`: `create_product(spec: PodProductSpec) -> PodProduct`, `get_product(id) -> PodProduct`, `delete_product(id) -> None`.
- `PodProductSpec`: `ref(provider="gelato", store_id, template_id, variants[PodVariantSpec(format, variant_key, placeholder, fit_method, retail_price)])`, `title`, `description`, `tags`, `print_file_url`, `idempotency_key`, `publish_as_draft=True`.
- `PodProduct`: `provider_product_id, status(created|publishing|linked|failed), etsy_listing_id: int|None, etsy_listing_state("draft"|"active"|None), variant_count, error`.
- `PodWriteError(status_code, error)` — never the raw body.

## Gelato API (confirmed)
- Base `https://ecommerce.gelatoapis.com`, header `X-API-KEY`.
- Create: `POST /v1/stores/{storeId}/products:create-from-template` → `{id, externalId, status(created|publishing|active|publishing_error), ...}`.
- Get: `GET /v1/stores/{storeId}/products/{productId}`. Delete: `DELETE .../products/{productId}`.

---

## Task 1: `LiveGelatoAdapter` + respx tests

**Files:** Create `src/shopsteward/adapters/pod/live.py`; Test `tests/adapters/pod/test_live_gelato.py`

- [ ] **Step 1:** Read `adapters/etsy/live.py` for the httpx pattern (`_safe_error`, `httpx.Client(headers=..., timeout=...)`, error→`*WriteError`). Confirm `respx` is the HTTP mock (used by copy/vision adapter tests).

- [ ] **Step 2: Write the failing tests** `tests/adapters/pod/test_live_gelato.py` (respx):
  - `create_product`: mock `POST .../v1/stores/STORE/products:create-from-template` → 200 `{"id":"p1","externalId":null,"status":"created"}`. Build a valid `PodProductSpec` (store_id="STORE", template_id="tpl", one variant with real `variant_key`/`placeholder`/`fit_method`/`retail_price`, `print_file_url="https://x/f.pdf"`). Assert the returned `PodProduct(provider_product_id="p1", status="created", etsy_listing_id=None, variant_count=1)`. Inspect the sent request JSON: `templateId=="tpl"`, header `X-API-KEY` present, **`isVisibleInTheOnlineStore` is False**, `variants[0].templateVariantId == variant_key`, `variants[0].imagePlaceholders[0]` has `name==placeholder`, `fileUrl==print_file_url`, `fitMethod==fit_method`.
  - `get_product` linked: mock GET → `{"id":"p1","externalId":"5001","status":"active"}` → `PodProduct(status="linked", etsy_listing_id=5001, etsy_listing_state="draft")`.
  - `get_product` still publishing: `status":"active","externalId":null` → `status="publishing"` (not linked). `status":"publishing"` → `publishing`.
  - `get_product` failed: `status":"publishing_error"` (+ an error message field) → `status="failed"`, `error` set.
  - Error mapping: POST → 422 `{"error":"bad template"}` → raises `PodWriteError` with `status_code==422` and message containing "bad template" (and NOT the whole body).
  - `delete_product`: DELETE 204 → no raise; DELETE 404 → `PodWriteError`.
  Run → FAIL.

- [ ] **Step 3: Implement** `src/shopsteward/adapters/pod/live.py`:
```python
"""Live Gelato Ecommerce API client (httpx only, no SDK -- adapters/etsy/live.py
precedent). Satisfies the PodAdapter protocol. Write-safety: publish_as_draft is
pinned True in the model, and this adapter always sends
isVisibleInTheOnlineStore=false, so a Gelato-created Etsy listing is ALWAYS a
draft, never live."""

import httpx

from shopsteward.adapters.pod.interface import PodWriteError
from shopsteward.adapters.pod.models import PodProduct, PodProductSpec

BASE = "https://ecommerce.gelatoapis.com"


def _safe_error(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        err = body.get("error") or body.get("message")
        return err if isinstance(err, str) else None
    return None


def _as_int(external_id) -> int | None:
    try:
        return int(external_id) if external_id not in (None, "") else None
    except (TypeError, ValueError):
        return None


class LiveGelatoAdapter:
    def __init__(self, api_key: str, store_id: str, *, base: str = BASE, timeout: float = 30.0):
        self._store_id = store_id
        self._base = base
        self._client = httpx.Client(headers={"X-API-KEY": api_key}, timeout=timeout)

    def _to_product(self, data: dict, *, variant_count: int) -> PodProduct:
        external = _as_int(data.get("externalId"))
        raw = data.get("status")
        if raw in ("created", "publishing"):
            status = raw
        elif raw == "active":
            status = "linked" if external is not None else "publishing"
        else:  # publishing_error / unknown
            status = "failed"
        return PodProduct(
            provider_product_id=str(data["id"]),
            status=status,
            etsy_listing_id=external if status == "linked" else None,
            etsy_listing_state="draft" if status == "linked" else None,
            variant_count=variant_count,
            error=_safe_error_from_data(data) if status == "failed" else None,
        )

    def create_product(self, spec: PodProductSpec) -> PodProduct:
        url = f"{self._base}/v1/stores/{spec.ref.store_id}/products:create-from-template"
        body = {
            "templateId": spec.ref.template_id,
            "title": spec.title,
            "description": spec.description,
            "isVisibleInTheOnlineStore": False,  # write-safety: Etsy listing stays a DRAFT
            "salesChannels": ["web"],
            "tags": spec.tags,
            "variants": [
                {
                    "templateVariantId": v.variant_key,
                    "imagePlaceholders": [
                        {"name": v.placeholder, "fileUrl": spec.print_file_url,
                         "fitMethod": v.fit_method}
                    ],
                    # ponytail: per-variant retail_price submission is UNVERIFIED against
                    # Gelato's live API (docs omit a price field on create-from-template);
                    # resolve at the operator smoke before trusting live margins.
                }
                for v in spec.ref.variants
            ],
        }
        resp = self._client.post(url, json=body)
        if resp.status_code >= 400:
            raise PodWriteError(resp.status_code, _safe_error(resp))
        return self._to_product(resp.json(), variant_count=len(spec.ref.variants))

    def get_product(self, provider_product_id: str) -> PodProduct:
        url = f"{self._base}/v1/stores/{self._store_id}/products/{provider_product_id}"
        resp = self._client.get(url)
        if resp.status_code >= 400:
            raise PodWriteError(resp.status_code, _safe_error(resp))
        data = resp.json()
        return self._to_product(data, variant_count=len(data.get("variants", []) or []))

    def delete_product(self, provider_product_id: str) -> None:
        url = f"{self._base}/v1/stores/{self._store_id}/products/{provider_product_id}"
        resp = self._client.delete(url)
        if resp.status_code >= 400:
            raise PodWriteError(resp.status_code, _safe_error(resp))
```
Add a tiny `_safe_error_from_data(data)` helper (pull an `error`/`message` string from the product payload if present) or inline it; keep it defensive. (If `get_product`'s response has no `variants`, `variant_count` falls back to 0 — acceptable for a poll result; the create path passes the real count.)

- [ ] **Step 4:** Run → pass. `ruff`, `lint-imports`. Commit `feat(pod): live Gelato adapter (create/get/delete over Ecommerce API)`.

---

## Task 2: Factory + live gate wiring

**Files:** Modify `src/shopsteward/pipeline/listings/pod/factory.py`, `src/shopsteward/pipeline/live_gate.py`, `src/shopsteward/shop.py`; Tests `tests/pipeline/listings/pod/test_pod_adapter_factory.py` (extend), `tests/pipeline/test_live_gate.py` (extend if present)

- [ ] **Step 1:** Add to `pipeline/live_gate.py`:
```python
def live_gelato_open() -> bool:
    return os.environ.get("SHOPSTEWARD_LIVE_GELATO") == "1" and bool(os.environ.get("GELATO_API_KEY"))


def live_gelato_error() -> str:
    return ("Live Gelato product creation is gated on operator approval: set "
            "SHOPSTEWARD_LIVE_GELATO=1 and GELATO_API_KEY, fill real Gelato IDs in "
            "pod.json, then re-run with --live-gelato.")
```

- [ ] **Step 2:** Update `build_pod_adapter` in `pod/factory.py` to construct the live adapter when `live`:
```python
def build_pod_adapter(*, live: bool, store_id: str | None = None):
    from shopsteward.adapters.pod.fake import FakeGelatoAdapter
    if not live:
        return FakeGelatoAdapter()
    import os
    from shopsteward.adapters.pod.live import LiveGelatoAdapter
    if not store_id:
        raise ValueError("live Gelato adapter requires store_id (cfg.gelato.store_id)")
    return LiveGelatoAdapter(api_key=os.environ["GELATO_API_KEY"], store_id=store_id)
```

- [ ] **Step 3:** In `src/shopsteward/shop.py::run_shop_build`, replace the `--live-gelato` "always refuse (C3)" stub: refuse via `live_gelato_open()`/`live_gelato_error()` when `live_gelato` is set but the gate is closed (mirror the other gates), and construct the pod adapter with the store id:
```python
    if live_gelato and not live_gelato_open():
        raise LiveGateClosedError(live_gelato_error())
    ...
    cfg = <pod cfg>
    pod_adapter = build_pod_adapter(live=live_gelato, store_id=cfg.gelato.store_id)
    link = link_pod_drafts(conn, user_id, adapter=pod_adapter, print_file_host=host, cfg=cfg)
```

- [ ] **Step 4:** Update tests: `test_pod_adapter_factory.py` — `build_pod_adapter(live=True, store_id="s")` with `GELATO_API_KEY` stubbed returns a `LiveGelatoAdapter`; `live=True` without store_id raises `ValueError`. Add live-gate open/closed tests. Ensure `run_shop_build(..., live_gelato=True)` with the gate closed still refuses cleanly (update the existing physical test if it asserted the NotImplementedError message).

- [ ] **Step 5:** Run → pass. `ruff`, `lint-imports`. Commit `feat(pod): wire live Gelato adapter into factory + --live-gelato gate`.

---

## Task 3: Full gate + PR

- [ ] `uv run pytest -q` (green), `uv run ruff check src tests` (clean), `uv run lint-imports` (3 kept), `uv run shopsteward shop build --help`.
- [ ] Push + PR. Body must prominently carry the **operator smoke** steps (one winner, `--live-gelato --live-printfile`, verify Etsy DRAFT + resolve the retail_price OPEN item, then `delete_product` cleanup) and that real `pod.json` Gelato IDs + `GELATO_API_KEY` are prerequisites.

---

## Self-review (against the spec)
- Live adapter implements the 3 protocol methods over the confirmed endpoints; write-safety `isVisibleInTheOnlineStore=false` pinned + asserted (Task 1); status/externalId normalization incl. active-without-externalId→publishing (Task 1); error→PodWriteError, no raw body (Task 1); factory + `live_gelato` gate + shop wiring (Task 2); respx-only tests, live call is an operator smoke (Task 3). retail_price submission flagged (ponytail comment + PR smoke step) — the one item docs couldn't confirm.
- No placeholders: the retail_price gap is an explicit, flagged OPEN resolved at smoke, not a silent omission.
