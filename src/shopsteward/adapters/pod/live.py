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


def format_template_variants(data: dict) -> list[str]:
    """Copy-pasteable lines describing each template variant's templateVariantId
    (-> pod.json variant_key) and imagePlaceholder names (-> pod.json
    placeholder). Defensive against the exact key nesting: the Get Template
    response gives each variant an id and imagePlaceholders[{name}]; we read
    templateVariantId or id, and skip placeholders with no name."""
    lines: list[str] = []
    variants = data.get("variants") or []
    for v in variants:
        variant_id = v.get("templateVariantId") or v.get("id") or "?"
        title = v.get("title") or v.get("name")
        header = f"variant_key: {variant_id}"
        if title:
            header += f"   ({title})"
        lines.append(header)
        for p in v.get("imagePlaceholders") or []:
            name = p.get("name")
            if name:
                lines.append(f"    placeholder: {name}")
    return lines


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
        """Classify by Gelato's actual error signals, NOT by guessing at a
        status-string whitelist -- Gelato's OpenAPI spec types `status` as a
        bare, unconstrained string (no published enum), and a real live
        response has been observed with `status: "publishing_queued"`
        (still-processing, `errors: []`, `publishingErrorCode: null`) which a
        whitelist-based mapping misclassified as "failed" on the very first
        poll. `errors` (non-empty) and `publishingErrorCode` (truthy) are the
        unambiguous, documented-shape error signals; a status string
        containing "error" is a belt-and-suspenders fallback for a future
        error-shaped status with those fields empty for some reason."""
        external = _as_int(data.get("externalId"))
        raw = data.get("status")
        raw_str = raw if isinstance(raw, str) else ""
        linked = raw_str == "active" and external is not None
        failed = not linked and (
            bool(data.get("publishingErrorCode"))
            or bool(data.get("errors"))
            or "error" in raw_str.lower()
        )
        if linked:
            # A confirmed active+externalId listing wins over any error
            # signal: if Gelato ever uses `errors` as a non-terminal warnings
            # channel on an already-published product, treating it as
            # "failed" here would drop a REAL etsy_listing_id and leave the
            # draft permanently re-polling a product that already succeeded.
            status = "linked"
        elif failed:
            status = "failed"
        else:
            # created / publishing / publishing_queued / active-with-no-
            # externalId-yet / any other unrecognized non-error status: all
            # still-processing, non-terminal from the poll loop's point of
            # view (provider.py's link_pod_drafts only branches on "linked"
            # and "failed"; everything else keeps polling until poll_max).
            status = "publishing"

        error_message = None
        if status == "failed":
            details = data.get("publishingErrorDetails")
            error_code = data.get("publishingErrorCode")
            errors_list = data.get("errors")
            if isinstance(details, str) and details:
                error_message = details
            elif errors_list:
                # A real error signal (non-empty `errors`) is the whole
                # reason this draft is being marked failed -- surface it
                # verbatim rather than the (often benign) status string, or
                # the operator gets a provider_failed event with no usable
                # cause.
                error_message = "; ".join(str(e) for e in errors_list)
            elif error_code:
                error_message = f"Gelato publishingErrorCode={error_code!r}"
            else:
                error_message = _safe_error_from_data(data) or (
                    f"Gelato reported an unrecognized/error status: {raw!r}"
                )

        return PodProduct(
            provider_product_id=str(data["id"]),
            status=status,
            etsy_listing_id=external if status == "linked" else None,
            etsy_listing_state="draft" if status == "linked" else None,
            variant_count=variant_count,
            error=error_message,
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
                    # No price is sent here by design: Gelato's
                    # create-from-template body has NO price field. Retail price
                    # is configured on the Gelato TEMPLATE in the dashboard and
                    # inherited at create. Our computed spec.retail_price is a
                    # recommendation surfaced by `pod build --dry-run` for the
                    # operator to enter on the template -- never submitted here.
                }
                for v in spec.ref.variants
            ],
        }
        resp = self._client.post(url, json=body)
        if resp.status_code >= 400:
            raise PodWriteError(resp.status_code, _safe_error(resp))
        return self._to_product(resp.json(), variant_count=len(spec.ref.variants))

    def get_template(self, template_id: str) -> dict:
        """Fetch a Gelato template (its variants + imagePlaceholder names).
        Read-only helper for `pod template show` -- discovers the
        templateVariantId + placeholder names to fill into pod.json."""
        url = f"{self._base}/v1/templates/{template_id}"
        resp = self._client.get(url)
        if resp.status_code >= 400:
            raise PodWriteError(resp.status_code, _safe_error(resp))
        return resp.json()

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
