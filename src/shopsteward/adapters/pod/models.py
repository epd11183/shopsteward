"""Pydantic v2 models for the PodAdapter boundary (design §4.1). Deliberately
provider-agnostic: variant_key/placeholder/fit_method are opaque strings
supplied by pod.json config, never parsed by core code (design §4.1's
"leaks" table). Twin of adapters/etsy/models.py.

Write-safety invariants live HERE, not only in FakeGelatoAdapter (review
fix-up D): a Protocol carries no behaviour, so a live adapter (slice 3)
would otherwise inherit none of them. retail_price/variants/format/
variant_key/title/description are all unconstructible if non-positive or
empty; publish_as_draft is pinned to Literal[True] so an unsafe spec is
unconstructible rather than merely refused by the adapter; a variant_key or
template_id still equal to pod.json's shipped "<OPERATOR>" placeholder is
rejected outright -- the whole offline suite would otherwise stay green
against a config that cannot work live."""

from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

_OPERATOR_PLACEHOLDER = "<OPERATOR>"


class PodVariantSpec(BaseModel):
    format: str = Field(min_length=1)  # our format key, e.g. "framed_poster_16x20"
    variant_key: str = Field(min_length=1)  # OPAQUE. gelato: templateVariantId
    placeholder: str | None = None  # gelato imagePlaceholders[].name  ("ImageFront")
    fit_method: str | None = None  # gelato fitMethod                 ("slice")
    retail_price: float = Field(gt=0)  # set HERE, at product creation -- never via Etsy inventory

    @field_validator("variant_key")
    @classmethod
    def _reject_operator_placeholder(cls, value: str) -> str:
        if value == _OPERATOR_PLACEHOLDER:
            raise ValueError(
                f"variant_key is the literal {_OPERATOR_PLACEHOLDER!r} placeholder from "
                "pod.json -- an operator must supply a real value before this can go live"
            )
        return value


class PodProviderRef(BaseModel):
    provider: Literal["gelato"]  # the only provider with a live PodAdapter (design §0a)
    # store_id lands in Gelato's URL path /v1/stores/{storeId}/products, so an empty
    # or placeholder value would build a request against the wrong resource entirely.
    store_id: str = Field(min_length=1)
    template_id: str | None = None  # gelato only; None => non-template create
    variants: list[PodVariantSpec] = Field(min_length=1)

    @field_validator("store_id", "template_id")
    @classmethod
    def _reject_operator_placeholder(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value == _OPERATOR_PLACEHOLDER:
            raise ValueError(
                f"{info.field_name} is the literal {_OPERATOR_PLACEHOLDER!r} placeholder "
                "from pod.json -- an operator must supply a real value before this can "
                "go live"
            )
        return value


class PodProductSpec(BaseModel):
    ref: PodProviderRef
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    print_file_url: str  # transient. NEVER persisted to an event or logged.
    # design §3's draft_id -- STABLE across a crash-then-rerun, unlike
    # print_file_url (a rotating signed URL, design §17 Q1a) or variant_key
    # (identical across every shipped catalog entry until an operator fills
    # it in). The fake -- and, per this model, any adapter -- dedupes on
    # this, never on the transient URL (review fix-up G).
    idempotency_key: str = Field(min_length=1)
    publish_as_draft: Literal[True] = True  # write-safety invariant, decision 41's POD twin


class PodProduct(BaseModel):
    provider_product_id: str
    status: Literal["created", "publishing", "linked", "failed"]  # NORMALISED across providers
    etsy_listing_id: int | None
    etsy_listing_state: Literal["draft", "active"] | None  # must be "draft" when linked
    variant_count: int
    error: str | None = None
