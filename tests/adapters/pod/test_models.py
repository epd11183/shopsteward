"""Write-safety invariants live on the models now, not only in
FakeGelatoAdapter (review fix-up D): every case here constructed cleanly
before this change."""

import pytest
from pydantic import ValidationError

from shopsteward.adapters.pod.models import PodProductSpec, PodProviderRef, PodVariantSpec


def _variant(**overrides: object) -> dict:
    base = dict(
        format="framed_poster_16x20",
        variant_key="variant-1",
        placeholder="ImageFront",
        retail_price=79.00,
    )
    base.update(overrides)
    return base


def _ref(**overrides: object) -> dict:
    base = dict(
        provider="gelato",
        store_id="store-1",
        template_id="tmpl-1",
        variants=[PodVariantSpec(**_variant())],
    )
    base.update(overrides)
    return base


def _spec(**overrides: object) -> dict:
    base = dict(
        ref=PodProviderRef(**_ref()),
        title="Sunset Over the Bay",
        description="Printed and framed on demand.",
        print_file_url="https://fake.invalid/abc123",
        idempotency_key="draft-1",
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("retail_price", [0.0, -99.0])
def test_variant_spec_rejects_non_positive_retail_price(retail_price: float) -> None:
    with pytest.raises(ValidationError):
        PodVariantSpec(**_variant(retail_price=retail_price))


@pytest.mark.parametrize("field", ["format", "variant_key"])
def test_variant_spec_rejects_empty_string_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        PodVariantSpec(**_variant(**{field: ""}))


def test_provider_ref_rejects_empty_variants() -> None:
    with pytest.raises(ValidationError):
        PodProviderRef(**_ref(variants=[]))


@pytest.mark.parametrize("field", ["title", "description"])
def test_product_spec_rejects_empty_string_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        PodProductSpec(**_spec(**{field: ""}))


def test_product_spec_publish_as_draft_false_is_unconstructible() -> None:
    with pytest.raises(ValidationError):
        PodProductSpec(**_spec(publish_as_draft=False))


def test_variant_key_operator_placeholder_is_rejected() -> None:
    with pytest.raises(ValidationError, match="<OPERATOR>"):
        PodVariantSpec(**_variant(variant_key="<OPERATOR>"))


def test_template_id_operator_placeholder_is_rejected() -> None:
    with pytest.raises(ValidationError, match="<OPERATOR>"):
        PodProviderRef(**_ref(template_id="<OPERATOR>"))


def test_provider_ref_store_id_rejects_empty_and_placeholder() -> None:
    """store_id lands in Gelato's URL path, so a blank or placeholder value would
    build a request against the wrong resource. Sibling of the variant_key and
    template_id guards (review round 3)."""
    variant = PodVariantSpec(format="framed_poster_16x20", variant_key="v1", retail_price=99.0)
    for bad in ("", "<OPERATOR>"):
        with pytest.raises(ValidationError):
            PodProviderRef(provider="gelato", store_id=bad, template_id="t1", variants=[variant])

    ok = PodProviderRef(
        provider="gelato", store_id="real-store", template_id="t1", variants=[variant]
    )
    assert ok.store_id == "real-store"
