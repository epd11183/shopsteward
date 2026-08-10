"""In-memory PodAdapter twin for Gelato -- the default everywhere (tests +
the offline `pod build` default, mirroring FakeEtsyWriteAdapter). Enforces
Gelato-specific requirements the shared models (adapters/pod/models.py)
can't express: template_id and every variant's placeholder are Gelato's
own template-create requirements, so a spec missing either is rejected
before anything is created. Generic write-safety invariants (positive
price, at least one variant, publish_as_draft pinned True, no leftover
"<OPERATOR>" placeholder) are unconstructible on the models themselves now
(review fix-up D) -- this adapter no longer re-checks them.

get_product simulates Gelato's asynchronous link: status starts
"publishing" and flips to "linked" (with a monotonic etsy_listing_id and
etsy_listing_state="draft") once a product has been polled
`links_after_polls` times; polling an already-linked product again is a
no-op read (etsy_listing_id stays put).

Dedupe keys on `spec.idempotency_key` -- the draft_id, design §3 -- not on
print_file_url or the variant-key set (review fix-up G). The real hazard is
a crash-then-rerun across processes, and the design REQUIRES the host to
issue ROTATING signed URLs (§17 Q1a), so the same draft's print_file_url
differs on every retry and would slip straight past a URL-keyed dedupe.
Conversely every shipped `variant_key` is the literal "<OPERATOR>"
placeholder, so a (print_file_url, variant_keys)-keyed dedupe fired falsely
on two genuinely different product types built from one photo. draft_id is
stable across both cases. `delete_product` clears the entry, so the design
§14 cleanup rehearsal (create -> delete -> recreate) works.

`etsy_listings`: on the real Gelato integration, Gelato itself creates the
Etsy draft the moment the product links (CLAUDE.md's "POD-first listing
creation") -- a real `FakeEtsyWriteAdapter.listings[id]` never exists until
that happens either. This optional constructor arg lets an orchestrator
(shop.py) that already holds a `FakeEtsyWriteAdapter` pass its `.listings`
dict so THIS fake can seed the matching placeholder row at the moment it
mints `etsy_listing_id` -- otherwise a caller chaining link_pod_drafts ->
enrich_pod_drafts offline would 404 on an id no Etsy fake ever heard of.
None (the default, and every pre-existing direct FakeGelatoAdapter() test)
skips this entirely.

`calls` records every method invocation (name, kwargs) for assertions.
"""

from typing import Any

from shopsteward.adapters.pod.interface import PodWriteError
from shopsteward.adapters.pod.models import PodProduct, PodProductSpec


class FakeGelatoAdapter:
    def __init__(
        self,
        *,
        links_after_polls: int = 2,
        etsy_listings: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self._links_after_polls = links_after_polls
        self._next_product_id = 1
        self._next_etsy_listing_id = 5000
        self._products: dict[str, dict[str, Any]] = {}
        self._by_idempotency_key: dict[str, str] = {}
        self._etsy_listings = etsy_listings
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create_product(self, spec: PodProductSpec) -> PodProduct:
        if spec.ref.template_id is None:
            raise PodWriteError(422, "template_id is required (Gelato template-create path)")
        for variant in spec.ref.variants:
            if not variant.placeholder:
                raise PodWriteError(
                    422, f"variant {variant.variant_key!r} is missing a placeholder"
                )

        if spec.idempotency_key in self._by_idempotency_key:
            raise PodWriteError(409, f"a product for draft {spec.idempotency_key!r} already exists")

        product_id = str(self._next_product_id)
        self._next_product_id += 1
        self._by_idempotency_key[spec.idempotency_key] = product_id
        self._products[product_id] = {
            "idempotency_key": spec.idempotency_key,
            "polls": 0,
            "variant_count": len(spec.ref.variants),
            "etsy_listing_id": None,
            "status": "publishing",
            # Only needed to seed etsy_listings on link (see class docstring)
            # -- placeholder title/description at create time either way
            # (pod/provider.py's _build_spec), overwritten by real copy at
            # enrich time via update_listing.
            "title": spec.title,
            "description": spec.description,
            "price": spec.ref.variants[0].retail_price if spec.ref.variants else 0.0,
        }
        self.calls.append(
            (
                "create_product",
                {"provider_product_id": product_id, "idempotency_key": spec.idempotency_key},
            )
        )
        return PodProduct(
            provider_product_id=product_id,
            status="publishing",
            etsy_listing_id=None,
            etsy_listing_state=None,
            variant_count=len(spec.ref.variants),
        )

    def get_product(self, provider_product_id: str) -> PodProduct:
        row = self._require(provider_product_id)
        row["polls"] += 1
        if row["status"] != "linked" and row["polls"] >= self._links_after_polls:
            row["status"] = "linked"
            row["etsy_listing_id"] = self._next_etsy_listing_id
            if self._etsy_listings is not None:
                self._etsy_listings[self._next_etsy_listing_id] = {
                    "title": row["title"],
                    "description": row["description"],
                    "price": row["price"],
                    "quantity": 1,
                    "tags": [],
                    "state": "draft",
                    "images": [],
                    "files": [],
                }
            self._next_etsy_listing_id += 1
        self.calls.append(
            ("get_product", {"provider_product_id": provider_product_id, "polls": row["polls"]})
        )
        return PodProduct(
            provider_product_id=provider_product_id,
            status=row["status"],
            etsy_listing_id=row["etsy_listing_id"],
            etsy_listing_state="draft" if row["status"] == "linked" else None,
            variant_count=row["variant_count"],
        )

    def delete_product(self, provider_product_id: str) -> None:
        row = self._require(provider_product_id)
        del self._products[provider_product_id]
        self._by_idempotency_key.pop(row["idempotency_key"], None)
        self.calls.append(("delete_product", {"provider_product_id": provider_product_id}))

    def _require(self, provider_product_id: str) -> dict[str, Any]:
        row = self._products.get(provider_product_id)
        if row is None:
            raise PodWriteError(404, f"unknown provider_product_id {provider_product_id}")
        return row
