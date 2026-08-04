"""Adapter protocol. Core code depends on this, never on a provider SDK/HTTP
client. Twin of adapters/etsy/interface.py's EtsyWriteAdapter/EtsyWriteError.
"""

from typing import Protocol

from shopsteward.adapters.pod.models import PodProduct, PodProductSpec

_MAX_ERROR_LEN = 500


class PodWriteError(RuntimeError):
    """Raised by PodAdapter implementations on any write failure. Carries
    only the HTTP status and the provider's error message -- never a raw
    response body; the message is truncated defensively (EtsyWriteError
    precedent)."""

    def __init__(self, status_code: int, error: str | None):
        self.status_code = status_code
        self.error = error
        message = f"POD provider write failed with HTTP {status_code}"
        if error:
            message += f": {error[:_MAX_ERROR_LEN]}"
        super().__init__(message)


class PodAdapter(Protocol):
    """create_product creates the provider product AND, asynchronously, the
    connected-store Etsy draft (design §0, §7.1) -- it is called at most
    once per draft (design §3's idempotency rule: a confirmed
    provider_product_id is never re-created). get_product polls for the
    async link. delete_product is smoke-test cleanup only (design §14)."""

    def create_product(self, spec: PodProductSpec) -> PodProduct: ...
    def get_product(self, provider_product_id: str) -> PodProduct: ...
    def delete_product(self, provider_product_id: str) -> None: ...
