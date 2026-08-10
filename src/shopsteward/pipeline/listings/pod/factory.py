"""Single construction path for PrintFileHost instances (build_etsy_write_
adapter, pipeline/listings/push.py, precedent): FakePrintFileHost unless the
caller has already confirmed pipeline.live_gate.live_printfile_open().
`build_pod_adapter` (Gelato create/poll, slice 3) lives here too once it
exists -- this file is the single "which live adapter" decision point for
the whole pod/ package, same role config.py plays for pod.json."""

import os

from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.adapters.printfile.interface import PrintFileHost

_R2_ENV_VARS = (
    "CLOUDFLARE_R2_KEY",
    "CLOUDFLARE_R2_SECRET",
    "CLOUDFLARE_R2_ENDPOINT",
    "CLOUDFLARE_R2_BUCKET",
)


def build_print_file_host(*, live: bool) -> PrintFileHost:
    if not live:
        return FakePrintFileHost()

    from shopsteward.adapters.printfile.live import LiveR2PrintFileHost

    values = {var: os.environ.get(var) for var in _R2_ENV_VARS}
    missing = [var for var, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"{', '.join(missing)} must be set for live print-file hosting.")

    return LiveR2PrintFileHost(
        key=values["CLOUDFLARE_R2_KEY"],
        secret=values["CLOUDFLARE_R2_SECRET"],
        endpoint=values["CLOUDFLARE_R2_ENDPOINT"],
        bucket=values["CLOUDFLARE_R2_BUCKET"],
    )


def build_pod_adapter(*, live: bool, etsy_listings: dict | None = None):
    """`etsy_listings` (fake mode only): a `FakeEtsyWriteAdapter.listings`
    dict to seed when a product links, so a caller chaining link then
    enrich offline (shop.py) doesn't 404 against an id no Etsy fake ever
    heard of -- see FakeGelatoAdapter's docstring."""
    from shopsteward.adapters.pod.fake import FakeGelatoAdapter

    if not live:
        return FakeGelatoAdapter(etsy_listings=etsy_listings)
    raise NotImplementedError("live Gelato adapter is Phase C3 (not yet built)")
