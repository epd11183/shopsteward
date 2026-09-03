import httpx
import pytest
import respx

from shopsteward.adapters.pod.interface import PodWriteError
from shopsteward.adapters.pod.live import LiveGelatoAdapter, format_template_variants
from shopsteward.adapters.pod.models import PodProductSpec, PodProviderRef, PodVariantSpec

BASE = "https://ecommerce.gelatoapis.com"


def _spec(**overrides: object) -> PodProductSpec:
    base = dict(
        ref=PodProviderRef(
            provider="gelato",
            store_id="STORE",
            template_id="tpl",
            variants=[
                PodVariantSpec(
                    format="framed_poster_16x20",
                    variant_key="var-1",
                    placeholder="ImageFront",
                    fit_method="slice",
                    retail_price=29.99,
                )
            ],
        ),
        title="Sunset",
        description="A print.",
        tags=["wall art"],
        print_file_url="https://x/f.pdf",
        idempotency_key="draft-1",
    )
    base.update(overrides)
    return PodProductSpec(**base)


def _adapter() -> LiveGelatoAdapter:
    return LiveGelatoAdapter(api_key="k", store_id="STORE")


@respx.mock
def test_create_product_sends_expected_body_and_parses_created() -> None:
    route = respx.post(f"{BASE}/v1/stores/STORE/products:create-from-template").mock(
        return_value=httpx.Response(200, json={"id": "p1", "externalId": None, "status": "created"})
    )
    product = _adapter().create_product(_spec())

    assert product.provider_product_id == "p1"
    # "created" carries no error signal -> non-terminal "publishing" (same
    # bucket the poll loop treats identically to "created" ever did: only
    # "linked"/"failed" are terminal in provider.py::link_pod_drafts).
    assert product.status == "publishing"
    assert product.etsy_listing_id is None
    assert product.etsy_listing_state is None
    assert product.variant_count == 1

    sent = route.calls.last.request
    assert sent.headers["X-API-KEY"] == "k"
    assert "/v1/stores/STORE/products:create-from-template" in str(sent.url)
    import json as _json

    body = _json.loads(sent.content)
    assert body["templateId"] == "tpl"
    assert body["isVisibleInTheOnlineStore"] is False
    variant = body["variants"][0]
    assert variant["templateVariantId"] == "var-1"
    placeholder = variant["imagePlaceholders"][0]
    assert placeholder["name"] == "ImageFront"
    assert placeholder["fileUrl"] == "https://x/f.pdf"
    assert placeholder["fitMethod"] == "slice"


@respx.mock
def test_get_product_active_with_external_id_is_linked() -> None:
    respx.get(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(
            200, json={"id": "p1", "externalId": "5001", "status": "active"}
        )
    )
    product = _adapter().get_product("p1")
    assert product.status == "linked"
    assert product.etsy_listing_id == 5001
    assert product.etsy_listing_state == "draft"


@respx.mock
def test_get_product_active_without_external_id_is_publishing() -> None:
    respx.get(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(200, json={"id": "p1", "externalId": None, "status": "active"})
    )
    product = _adapter().get_product("p1")
    assert product.status == "publishing"
    assert product.etsy_listing_id is None
    assert product.etsy_listing_state is None


@respx.mock
def test_get_product_publishing_status_passthrough() -> None:
    respx.get(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(
            200, json={"id": "p1", "externalId": None, "status": "publishing"}
        )
    )
    product = _adapter().get_product("p1")
    assert product.status == "publishing"


@respx.mock
def test_get_product_publishing_error_is_failed_with_message() -> None:
    respx.get(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "p1",
                "externalId": None,
                "status": "publishing_error",
                "error": "boom",
            },
        )
    )
    product = _adapter().get_product("p1")
    assert product.status == "failed"
    assert product.error is not None
    assert "boom" in product.error


@respx.mock
def test_get_product_publishing_queued_with_no_error_signal_is_not_failed() -> None:
    """Real live Gelato response shape observed 2026-09-02: `status` is a
    bare unenumerated string, and "publishing_queued" (still processing,
    no error) previously fell through the old status whitelist into
    "failed" -- this is the regression this fix targets."""
    respx.get(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "p1",
                "status": "publishing_queued",
                "publishingErrorCode": None,
                "publishingErrorDetails": None,
                "errors": [],
                "isReadyToPublish": True,
                "externalId": None,
                "hasDraft": False,
            },
        )
    )
    product = _adapter().get_product("p1")
    assert product.status == "publishing"
    assert product.error is None


@respx.mock
def test_get_product_error_code_set_with_unrecognized_status_is_failed() -> None:
    """A real error signal (publishingErrorCode set) must be classified as
    failed regardless of what the status string says, with the message
    derived from publishingErrorDetails."""
    respx.get(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "p1",
                "status": "publishing_queued",
                "publishingErrorCode": "invalid_print_file",
                "publishingErrorDetails": "The uploaded print file failed validation.",
                "errors": [],
                "externalId": None,
            },
        )
    )
    product = _adapter().get_product("p1")
    assert product.status == "failed"
    assert product.error == "The uploaded print file failed validation."


@respx.mock
def test_get_product_nonempty_errors_list_is_failed() -> None:
    """A non-empty `errors` list is itself an error signal even if
    publishingErrorCode is null and publishingErrorDetails is absent."""
    respx.get(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "p1",
                "status": "publishing_queued",
                "publishingErrorCode": None,
                "errors": ["print file rejected"],
                "externalId": None,
            },
        )
    )
    product = _adapter().get_product("p1")
    assert product.status == "failed"
    assert product.error == "print file rejected"


@respx.mock
def test_get_product_active_with_errors_still_links_if_external_id_present() -> None:
    """A confirmed active+externalId listing wins over a non-empty `errors`
    list -- if Gelato ever uses `errors` as a non-terminal warnings channel
    on an already-published product, this must not drop a real
    etsy_listing_id and strand the draft re-polling forever."""
    respx.get(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "p1",
                "status": "active",
                "externalId": "12345",
                "errors": ["some non-fatal warning"],
            },
        )
    )
    product = _adapter().get_product("p1")
    assert product.status == "linked"
    assert product.etsy_listing_id == 12345


@respx.mock
def test_create_product_error_maps_to_pod_write_error_without_full_body() -> None:
    respx.post(f"{BASE}/v1/stores/STORE/products:create-from-template").mock(
        return_value=httpx.Response(422, json={"error": "bad template", "secret": "leak-me"})
    )
    with pytest.raises(PodWriteError) as exc_info:
        _adapter().create_product(_spec())
    assert exc_info.value.status_code == 422
    message = str(exc_info.value)
    assert "bad template" in message
    assert "leak-me" not in message


@respx.mock
def test_get_template_returns_parsed_dict() -> None:
    body = {
        "id": "tpl",
        "variants": [
            {"id": "tv-1", "title": "16x20", "imagePlaceholders": [{"name": "ImageFront"}]},
            {
                "templateVariantId": "tv-2",
                "imagePlaceholders": [{"name": "Front"}, {"name": "Back"}],
            },
        ],
    }
    respx.get(f"{BASE}/v1/templates/tpl").mock(return_value=httpx.Response(200, json=body))

    result = _adapter().get_template("tpl")
    assert result == body


@respx.mock
def test_get_template_error_maps_to_pod_write_error() -> None:
    respx.get(f"{BASE}/v1/templates/tpl").mock(
        return_value=httpx.Response(404, json={"error": "no template"})
    )
    with pytest.raises(PodWriteError) as exc_info:
        _adapter().get_template("tpl")
    assert exc_info.value.status_code == 404


def test_format_template_variants_extracts_ids_and_placeholder_names() -> None:
    body = {
        "variants": [
            {"id": "tv-1", "imagePlaceholders": [{"name": "ImageFront"}]},
            {
                "templateVariantId": "tv-2",
                "imagePlaceholders": [{"name": "Front"}, {"name": "Back"}, {}],
            },
        ]
    }
    lines = format_template_variants(body)
    joined = "\n".join(lines)
    assert "tv-1" in joined
    assert "tv-2" in joined
    assert "ImageFront" in joined
    assert "Front" in joined and "Back" in joined
    # a placeholder with no name is skipped, not rendered as an empty line
    assert "placeholder: \n" not in joined + "\n"


@respx.mock
def test_delete_product_success_no_raise() -> None:
    route = respx.delete(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(204)
    )
    _adapter().delete_product("p1")
    assert route.called


@respx.mock
def test_delete_product_failure_raises() -> None:
    respx.delete(f"{BASE}/v1/stores/STORE/products/p1").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )
    with pytest.raises(PodWriteError) as exc_info:
        _adapter().delete_product("p1")
    assert exc_info.value.status_code == 404
