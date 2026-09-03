import pytest

from shopsteward.adapters.pod.fake import FakeGelatoAdapter
from shopsteward.adapters.pod.interface import PodAdapter, PodWriteError
from shopsteward.adapters.pod.models import PodProductSpec, PodProviderRef, PodVariantSpec

_conforms: PodAdapter = FakeGelatoAdapter()


def _spec(**overrides: object) -> PodProductSpec:
    base = dict(
        ref=PodProviderRef(
            provider="gelato",
            store_id="store-1",
            template_id="tmpl-1",
            variants=[
                PodVariantSpec(
                    format="framed_poster_16x20",
                    variant_key="variant-1",
                    placeholder="ImageFront",
                    fit_method="slice",
                    retail_price=79.00,
                )
            ],
        ),
        title="Sunset Over the Bay",
        description="Printed and framed on demand.",
        tags=["wall art"],
        print_file_url="https://fake.invalid/abc123",
        idempotency_key="draft-1",
    )
    base.update(overrides)
    return PodProductSpec(**base)


def test_create_product_starts_publishing() -> None:
    adapter = FakeGelatoAdapter()
    product = adapter.create_product(_spec())
    assert product.status == "publishing"
    assert product.etsy_listing_id is None
    assert product.etsy_listing_state is None
    assert product.variant_count == 1


def test_get_product_flips_to_linked_after_n_polls() -> None:
    adapter = FakeGelatoAdapter(links_after_polls=2)
    product = adapter.create_product(_spec())

    first_poll = adapter.get_product(product.provider_product_id)
    assert first_poll.status == "publishing"
    assert first_poll.etsy_listing_id is None

    second_poll = adapter.get_product(product.provider_product_id)
    assert second_poll.status == "linked"
    assert second_poll.etsy_listing_state == "draft"
    assert isinstance(second_poll.etsy_listing_id, int)


def test_polling_an_already_linked_product_again_keeps_the_same_etsy_listing_id() -> None:
    adapter = FakeGelatoAdapter(links_after_polls=1)
    product = adapter.create_product(_spec())

    first = adapter.get_product(product.provider_product_id)
    second = adapter.get_product(product.provider_product_id)
    third = adapter.get_product(product.provider_product_id)

    assert first.status == second.status == third.status == "linked"
    assert first.etsy_listing_id == second.etsy_listing_id == third.etsy_listing_id


def test_etsy_listing_id_is_monotonic_across_products() -> None:
    adapter = FakeGelatoAdapter(links_after_polls=1)
    first = adapter.create_product(
        _spec(idempotency_key="draft-1", print_file_url="https://fake.invalid/one")
    )
    second = adapter.create_product(
        _spec(
            idempotency_key="draft-2",
            print_file_url="https://fake.invalid/two",
            ref=PodProviderRef(
                provider="gelato",
                store_id="store-1",
                template_id="tmpl-1",
                variants=[
                    PodVariantSpec(
                        format="framed_poster_16x20",
                        variant_key="variant-2",
                        placeholder="ImageFront",
                        retail_price=79.00,
                    )
                ],
            ),
        )
    )

    linked_first = adapter.get_product(first.provider_product_id)
    linked_second = adapter.get_product(second.provider_product_id)
    assert linked_second.etsy_listing_id == linked_first.etsy_listing_id + 1


def test_create_product_requires_template_id() -> None:
    adapter = FakeGelatoAdapter()
    spec = _spec(
        ref=PodProviderRef(
            provider="gelato",
            store_id="store-1",
            template_id=None,
            variants=[
                PodVariantSpec(
                    format="framed_poster_16x20",
                    variant_key="variant-1",
                    placeholder="ImageFront",
                    retail_price=79.00,
                )
            ],
        )
    )
    with pytest.raises(PodWriteError, match="template_id"):
        adapter.create_product(spec)


def test_create_product_requires_placeholder_on_every_variant() -> None:
    adapter = FakeGelatoAdapter()
    spec = _spec(
        ref=PodProviderRef(
            provider="gelato",
            store_id="store-1",
            template_id="tmpl-1",
            variants=[
                PodVariantSpec(
                    format="framed_poster_16x20",
                    variant_key="variant-1",
                    placeholder=None,
                    retail_price=79.00,
                )
            ],
        )
    )
    with pytest.raises(PodWriteError, match="placeholder"):
        adapter.create_product(spec)


def test_duplicate_create_same_idempotency_key_raises() -> None:
    # the real hazard the dedupe key defends against: a crash-then-rerun of
    # the SAME draft, where the print-file host issues a different (rotating,
    # signed) URL on the retry (review fix-up G).
    adapter = FakeGelatoAdapter()
    adapter.create_product(
        _spec(idempotency_key="draft-1", print_file_url="https://fake.invalid/one")
    )
    with pytest.raises(PodWriteError) as exc_info:
        adapter.create_product(
            _spec(idempotency_key="draft-1", print_file_url="https://fake.invalid/two")
        )
    assert exc_info.value.status_code == 409


def test_same_print_file_and_variant_key_but_different_idempotency_key_is_not_a_duplicate() -> None:
    # the old dedupe key (print_file_url, variant_keys) fired falsely here:
    # two DIFFERENT product types built from one photo can share a
    # variant_key.
    adapter = FakeGelatoAdapter()
    adapter.create_product(_spec(idempotency_key="draft-1"))
    product = adapter.create_product(_spec(idempotency_key="draft-2"))
    assert product.status == "publishing"


def test_create_delete_recreate_with_the_same_idempotency_key_works() -> None:
    # design §14's cleanup rehearsal: create -> delete -> recreate.
    adapter = FakeGelatoAdapter()
    first = adapter.create_product(_spec(idempotency_key="draft-1"))
    adapter.delete_product(first.provider_product_id)
    second = adapter.create_product(_spec(idempotency_key="draft-1"))
    assert second.status == "publishing"


def test_get_product_unknown_id_raises_404() -> None:
    adapter = FakeGelatoAdapter()
    with pytest.raises(PodWriteError) as exc_info:
        adapter.get_product("nope")
    assert exc_info.value.status_code == 404


def test_delete_product_removes_and_then_get_raises() -> None:
    adapter = FakeGelatoAdapter()
    product = adapter.create_product(_spec())
    adapter.delete_product(product.provider_product_id)
    with pytest.raises(PodWriteError):
        adapter.get_product(product.provider_product_id)


def test_calls_log_records_every_invocation() -> None:
    adapter = FakeGelatoAdapter(links_after_polls=1)
    product = adapter.create_product(_spec())
    adapter.get_product(product.provider_product_id)
    adapter.delete_product(product.provider_product_id)

    names = [name for name, _ in adapter.calls]
    assert names == ["create_product", "get_product", "delete_product"]
