"""LiveR2PrintFileHost, exercised against a hand-rolled stub client (no
network, no moto) via the constructor's `client=` injection point."""

from shopsteward.adapters.printfile.live import LiveR2PrintFileHost


class _StubS3Client:
    def __init__(self) -> None:
        self.put_calls: list[dict] = []
        self.delete_calls: list[dict] = []
        self.presign_calls: list[dict] = []

    def put_object(self, *, Bucket, Key, Body):  # noqa: N803 - boto3's own param casing
        self.put_calls.append({"Bucket": Bucket, "Key": Key, "Body": Body})

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):  # noqa: N803
        self.presign_calls.append(
            {"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn}
        )
        return f"https://r2.example/{Params['Bucket']}/{Params['Key']}?sig=stub&ttl={ExpiresIn}"

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.delete_calls.append({"Bucket": Bucket, "Key": Key})


def _host(client: _StubS3Client) -> LiveR2PrintFileHost:
    return LiveR2PrintFileHost(
        key="k",
        secret="s",
        endpoint="https://example.r2.cloudflarestorage.com",
        bucket="prints",
        client=client,
    )


def test_publish_uploads_and_returns_a_presigned_url():
    client = _StubS3Client()
    host = _host(client)

    hosted = host.publish(b"print-master-bytes", name="abc123", ttl_seconds=3600)

    assert client.put_calls == [
        {"Bucket": "prints", "Key": "printfiles/abc123", "Body": b"print-master-bytes"}
    ]
    assert hosted.key == "printfiles/abc123"
    assert "sig=stub" in hosted.url
    assert client.presign_calls[0]["ExpiresIn"] == 3600
    assert client.presign_calls[0]["Params"] == {"Bucket": "prints", "Key": "printfiles/abc123"}


def test_publish_sets_an_expiry_ttl_seconds_in_the_future():
    from datetime import UTC, datetime

    client = _StubS3Client()
    host = _host(client)
    before = datetime.now(UTC)

    hosted = host.publish(b"data", name="abc", ttl_seconds=60)

    expires_at = datetime.fromisoformat(hosted.expires_at)
    assert expires_at > before


def test_revoke_deletes_the_object():
    client = _StubS3Client()
    host = _host(client)

    host.revoke("printfiles/abc123")

    assert client.delete_calls == [{"Bucket": "prints", "Key": "printfiles/abc123"}]


def test_never_reads_the_management_token_env_var():
    # design §17 Q1a: CLOUDFLARE_R2_TOKEN is a Cloudflare account-management
    # credential, not an S3 object credential. A live regression guard --
    # if a future edit adds a fallback read of it, this fails. (The module
    # docstrings NAME the var to explain why not; only an actual env read
    # of it is the regression this guards against.)
    import inspect

    from shopsteward.adapters.printfile import live
    from shopsteward.pipeline.listings.pod import factory

    assert 'os.environ.get("CLOUDFLARE_R2_TOKEN"' not in inspect.getsource(live)
    assert 'os.environ.get("CLOUDFLARE_R2_TOKEN"' not in inspect.getsource(factory)
