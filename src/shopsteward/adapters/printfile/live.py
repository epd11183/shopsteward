"""Live PrintFileHost: Cloudflare R2 via boto3's S3-compatible client
(design §17 Q1/Q1a, operator-approved 2026-08-04 -- hand-rolled SigV4
signing was rejected, a subtle bug there mints URLs that never expire).

Reads exactly four env vars at construction time (pipeline.listings.pod.
factory.build_print_file_host, not this module -- this class never touches
os.environ itself): CLOUDFLARE_R2_KEY, CLOUDFLARE_R2_SECRET,
CLOUDFLARE_R2_ENDPOINT, CLOUDFLARE_R2_BUCKET. MUST NOT read
CLOUDFLARE_R2_TOKEN -- that is a Cloudflare account-management credential
(rotates buckets, workers, DNS...), not an S3 object credential, and there
is no code path here that even looks at it.

NOT wired into any default path; live use is triple-gated by
pipeline.live_gate.live_printfile_open() (--live-printfile +
SHOPSTEWARD_LIVE_PRINTFILE=1 + all four env vars present)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.client import Config

from shopsteward.adapters.printfile.interface import HostedFile


class LiveR2PrintFileHost:
    def __init__(
        self, *, key: str, secret: str, endpoint: str, bucket: str, client: Any | None = None
    ) -> None:
        """`client` is dependency-injectable so tests can pass a hand-rolled
        stub (put_object/generate_presigned_url/delete_object) instead of a
        real boto3 S3 client -- zero network in tests, no moto dependency."""
        self._bucket = bucket
        self._client = client or boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )

    def publish(self, data: bytes, *, name: str, ttl_seconds: int) -> HostedFile:
        key = f"printfiles/{name}"
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
        url = self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=ttl_seconds
        )
        expires_at = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        return HostedFile(key=key, url=url, expires_at=expires_at)

    def revoke(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)
