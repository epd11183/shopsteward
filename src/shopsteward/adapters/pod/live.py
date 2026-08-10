"""Live Gelato Ecommerce API client (httpx only, no SDK -- adapters/etsy/live.py
precedent). Satisfies the PodAdapter protocol. Write-safety: publish_as_draft is
pinned True in the model, and this adapter always sends
isVisibleInTheOnlineStore=false, so a Gelato-created Etsy listing is ALWAYS a
draft, never live.

Wired into pod/factory.py::build_pod_adapter behind the live_gelato_open()
gate (pipeline/live_gate.py). The live HTTP call itself is exercised only
by an operator smoke, never by the test suite (respx-mocked here)."""

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


def _safe_error_from_data(data: dict) -> str | None:
    err = data.get("error") or data.get("message")
    return err if isinstance(err, str) else None


def _as_int(external_id: object) -> int | None:
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
                        {
                            "name": v.placeholder,
                            "fileUrl": spec.print_file_url,
                            "fitMethod": v.fit_method,
                        }
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
