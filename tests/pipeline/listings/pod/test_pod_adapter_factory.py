import pytest

from shopsteward.adapters.pod.fake import FakeGelatoAdapter
from shopsteward.pipeline.listings.pod.factory import build_pod_adapter


def test_build_fake_pod_adapter():
    assert isinstance(build_pod_adapter(live=False), FakeGelatoAdapter)


def test_live_pod_adapter_is_c3():
    with pytest.raises(NotImplementedError, match="C3"):
        build_pod_adapter(live=True)
