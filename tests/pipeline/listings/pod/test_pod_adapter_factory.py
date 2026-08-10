import pytest

from shopsteward.adapters.pod.fake import FakeGelatoAdapter
from shopsteward.adapters.pod.live import LiveGelatoAdapter
from shopsteward.pipeline.listings.pod.factory import build_pod_adapter


def test_build_fake_pod_adapter():
    assert isinstance(build_pod_adapter(live=False), FakeGelatoAdapter)


def test_live_pod_adapter_returns_live_gelato_adapter(monkeypatch):
    monkeypatch.setenv("GELATO_API_KEY", "k")
    assert isinstance(build_pod_adapter(live=True, store_id="s"), LiveGelatoAdapter)


def test_live_pod_adapter_requires_store_id(monkeypatch):
    monkeypatch.setenv("GELATO_API_KEY", "k")
    with pytest.raises(ValueError, match="store_id"):
        build_pod_adapter(live=True)
