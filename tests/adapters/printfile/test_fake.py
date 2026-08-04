import hashlib

from shopsteward.adapters.printfile.fake import FakePrintFileHost
from shopsteward.adapters.printfile.interface import PrintFileHost

_conforms: PrintFileHost = FakePrintFileHost()


def test_publish_url_is_deterministic_sha256_of_bytes() -> None:
    host = FakePrintFileHost()
    data = b"print-file-bytes"
    hosted = host.publish(data, name="print.jpg", ttl_seconds=3600)

    expected_key = hashlib.sha256(data).hexdigest()
    assert hosted.key == expected_key
    assert hosted.url == f"https://fake.invalid/{expected_key}"


def test_publish_is_deterministic_across_calls() -> None:
    host = FakePrintFileHost()
    data = b"same-bytes"
    first = host.publish(data, name="a.jpg", ttl_seconds=60)
    second = host.publish(data, name="a.jpg", ttl_seconds=60)
    assert first.key == second.key
    assert first.url == second.url


def test_revoke_removes_the_file() -> None:
    host = FakePrintFileHost()
    hosted = host.publish(b"bytes", name="a.jpg", ttl_seconds=60)
    assert hosted.key in host._files

    host.revoke(hosted.key)
    assert hosted.key not in host._files


def test_revoke_unknown_key_is_a_no_op() -> None:
    host = FakePrintFileHost()
    host.revoke("never-published")
    assert host.calls == [("revoke", {"key": "never-published"})]


def test_publish_returns_a_fixed_expires_at_sentinel() -> None:
    # the fake's docstring calls expires_at a deliberate purity invariant
    # (never derived from a clock) -- pin that a caller actually gets it.
    host = FakePrintFileHost()
    hosted = host.publish(b"bytes", name="a.jpg", ttl_seconds=60)
    assert hosted.expires_at == "9999-12-31T00:00:00Z"


def test_calls_log_records_every_invocation() -> None:
    host = FakePrintFileHost()
    hosted = host.publish(b"bytes", name="a.jpg", ttl_seconds=60)
    host.revoke(hosted.key)

    assert host.calls == [
        ("publish", {"key": hosted.key, "name": "a.jpg", "ttl_seconds": 60, "bytes": 5}),
        ("revoke", {"key": hosted.key}),
    ]
