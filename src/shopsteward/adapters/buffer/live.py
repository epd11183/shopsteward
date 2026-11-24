"""Live Buffer adapter: httpx GraphQL against https://api.buffer.com/graphql
with a bearer key (design §3). Operation and field names were read from the
installed @bufferapp/cli 2.3.1 generated documents; nothing here has been run
against the network. Construction is gated by `live_buffer_open()` at the
call site; this module never reads the environment and never logs the key."""

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, cast, get_args

import httpx

from shopsteward.adapters.buffer.interface import BufferError, BufferUnknownOutcome
from shopsteward.adapters.buffer.models import BufferChannel, BufferPost, BufferPostSpec, Service

BASE_URL = "https://api.buffer.com"
_PAGE_SIZE = 50
_MAX_PAGES = (
    20  # ponytail: hard stop on runaway pagination; raise if a channel ever exceeds 1000 posts
)

_ACCOUNT_Q = "query Account { account { organizations { id } } }"

_CHANNELS_Q = """query Channels($input: ChannelsInput!) {
  channels(input: $input) {
    id service name displayName isDisconnected isLocked isQueuePaused
    metadata { __typename ... on PinterestMetadata { boards { name serviceId } } }
  }
}"""

_POST_FIELDS = """id channelId status text dueAt sentAt createdAt externalLink
    error { message } metricsUpdatedAt %METRICS%
    metadata { __typename ... on PinterestPostMetadata { url } }"""

_POSTS_Q = (
    """query Posts($after: String, $first: Int, $input: PostsInput!) {
  posts(after: $after, first: $first, input: $input) {
    edges { node { """
    + _POST_FIELDS
    + """ } }
    pageInfo { hasNextPage endCursor }
  }
}"""
)

_CREATE_Q = (
    """mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { """
    + _POST_FIELDS.replace("%METRICS%", "")
    + """ } }
    ... on NotFoundError { message }
    ... on UnauthorizedError { message }
    ... on UnexpectedError { message }
    ... on RestProxyError { message }
    ... on LimitReachedError { message }
    ... on InvalidInputError { message }
  }
}"""
)

_DELETE_Q = """mutation DeletePost($input: DeletePostInput!) {
  deletePost(input: $input) {
    __typename
    ... on DeletePostSuccess { id }
    ... on VoidMutationError { message }
  }
}"""

_SUCCESS_TYPES = {"PostActionSuccess", "DeletePostSuccess"}
_UNION_CODES = {"NotFoundError": "NOT_FOUND", "UnauthorizedError": "UNAUTHORIZED"}
_EXT_CODES = {
    "UNAUTHENTICATED": "UNAUTHORIZED",
    "UNAUTHORIZED": "UNAUTHORIZED",
    "FORBIDDEN": "FORBIDDEN",
    "NOT_FOUND": "NOT_FOUND",
    "RATE_LIMITED": "RATE_LIMITED",
}


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _retry_after_s(resp: httpx.Response) -> int | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        try:
            return max(0, int((parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError):
            return None


def _metric_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _parse_post(n: dict[str, Any]) -> BufferPost:
    meta = n.get("metadata") or {}
    # Absent != 0: only metrics Buffer actually reported with a value.
    # Buffer sends display labels ("Pin Clicks", "Eng. Rate"); key by snake_case.
    metrics = {
        _metric_key(m["name"]): float(m["value"])
        for m in (n.get("metrics") or [])
        if m.get("name") and m.get("value") is not None
    }
    return BufferPost(
        post_id=n["id"],
        channel_id=n.get("channelId") or "",
        status=str(n.get("status") or "").lower(),
        text=n.get("text") or "",
        due_at=_dt(n.get("dueAt")),
        sent_at=_dt(n.get("sentAt")),
        created_at=_dt(n.get("createdAt")),
        external_link=n.get("externalLink"),
        pin_url=meta.get("url"),
        error_message=(n.get("error") or {}).get("message"),
        metrics=metrics,
        metrics_updated_at=_dt(n.get("metricsUpdatedAt")),
    )


class LiveBufferAdapter:
    """Implements BufferAdapter (read) and BufferWriteAdapter (write)."""

    def __init__(
        self,
        api_key: str,
        *,
        max_retry_after_s: int = 60,
        timeout_s: float = 30.0,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client or httpx.Client(base_url=BASE_URL, timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._max_retry_after_s = max_retry_after_s
        self._sleep = sleep
        self._org_id: str | None = None

    def _gql(self, query: str, variables: dict[str, Any], *, mutation: bool = False) -> dict:
        for attempt in (0, 1):
            try:
                resp = self._client.post(
                    "/graphql", json={"query": query, "variables": variables}, headers=self._headers
                )
            except httpx.HTTPError as exc:
                # Never include str(exc): a request repr must not reach logs/events.
                cls = BufferUnknownOutcome if mutation else BufferError
                raise cls("TRANSPORT", type(exc).__name__) from None
            if resp.status_code == 429:
                wait = _retry_after_s(resp)
                # A 429 means the request was not processed, so a retry is safe, but a
                # long Retry-After must refuse, never block.
                if attempt == 0 and wait is not None and wait <= self._max_retry_after_s:
                    self._sleep(wait)
                    continue
                raise BufferError("RATE_LIMITED", "rate limited", retry_after_s=wait)
            break
        if resp.status_code in (401, 403):
            code = "UNAUTHORIZED" if resp.status_code == 401 else "FORBIDDEN"
            raise BufferError(code, f"HTTP {resp.status_code}")
        if resp.status_code >= 500:
            cls = BufferUnknownOutcome if mutation else BufferError
            raise cls("TRANSPORT", f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise BufferError("TRANSPORT", f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            cls = BufferUnknownOutcome if mutation else BufferError
            raise cls("TRANSPORT", "response was not JSON") from None
        errors = body.get("errors")
        if errors:
            first = errors[0]
            code = _EXT_CODES.get((first.get("extensions") or {}).get("code", ""))
            msg = "; ".join(str(e.get("message", "")) for e in errors)
            if code is None:
                # Unmapped (e.g. INTERNAL_SERVER_ERROR): a mutation may have been applied.
                cls = BufferUnknownOutcome if mutation else BufferError
                raise cls("TRANSPORT", msg)
            raise BufferError(code, msg)
        return body.get("data") or {}

    def _mutation_result(self, data: dict, field: str) -> dict:
        result = data.get(field) or {}
        typename = result.get("__typename")
        if typename not in _SUCCESS_TYPES:
            raise BufferError(_UNION_CODES.get(typename, "MUTATION"), result.get("message") or "")
        return result

    def _org(self) -> str:
        if self._org_id is None:
            orgs = self._gql(_ACCOUNT_Q, {})["account"]["organizations"]
            if not orgs:
                raise BufferError("NOT_FOUND", "account has no organization")
            self._org_id = orgs[0]["id"]
        return self._org_id

    def list_channels(self) -> list[BufferChannel]:
        rows = self._gql(_CHANNELS_Q, {"input": {"organizationId": self._org()}})["channels"]
        out = []
        for r in rows:
            service = str(r.get("service") or "").lower()
            if service not in get_args(Service):
                continue  # channels we don't publish to (tiktok, x, ...) are ignored
            boards = {
                b["name"]: b["serviceId"]
                for b in ((r.get("metadata") or {}).get("boards") or [])
                if b.get("name") and b.get("serviceId")
            }
            out.append(
                BufferChannel(
                    channel_id=r["id"],
                    service=cast(Service, service),
                    name=r.get("name") or r.get("displayName") or "",
                    is_disconnected=bool(r.get("isDisconnected")),
                    is_locked=bool(r.get("isLocked")),
                    is_queue_paused=bool(r.get("isQueuePaused")),
                    boards=boards,
                )
            )
        return out

    def list_posts(
        self,
        *,
        channel_ids: list[str],
        created_since: datetime,
        statuses: list[str] | None = None,
        with_metrics: bool = False,
    ) -> list[BufferPost]:
        query = _POSTS_Q.replace("%METRICS%", "metrics { name value }" if with_metrics else "")
        flt: dict[str, Any] = {
            "channelIds": channel_ids,
            "createdAt": {"start": created_since.astimezone(UTC).isoformat()},
        }
        if statuses:
            flt["status"] = statuses
        out: list[BufferPost] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            data = self._gql(
                query,
                {
                    "after": after,
                    "first": _PAGE_SIZE,
                    "input": {"organizationId": self._org(), "filter": flt},
                },
            )["posts"]
            out += [_parse_post(e["node"]) for e in data.get("edges") or []]
            page = data.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            after = page.get("endCursor")
        return out

    def create_post(self, spec: BufferPostSpec) -> BufferPost:
        inp: dict[str, Any] = {
            "channelId": spec.channel_id,
            "mode": spec.mode,
            "schedulingType": spec.scheduling_type,
            "text": spec.text,
            "aiAssisted": spec.ai_assisted,
        }
        if spec.due_at is not None:
            inp["dueAt"] = spec.due_at.astimezone(UTC).isoformat()
        if spec.image_url:
            inp["assets"] = [{"image": {"url": spec.image_url}}]
        if spec.tag_ids:
            inp["tagIds"] = spec.tag_ids
        pin = {
            k: v
            for k, v in (
                ("boardServiceId", spec.pin_board_service_id),
                ("title", spec.pin_title),
                ("url", spec.pin_url),
            )
            if v
        }
        if pin:
            inp["metadata"] = {"pinterest": pin}
        # Schema (introspected live 2026-11-24): instagram metadata requires
        # type + shouldShareToFeed, facebook requires type. Send them explicitly
        # rather than rely on Buffer defaulting a missing block. Never isAiGenerated.
        elif spec.service == "instagram":
            inp["metadata"] = {"instagram": {"type": "post", "shouldShareToFeed": True}}
        elif spec.service == "facebook":
            inp["metadata"] = {"facebook": {"type": "post"}}
        data = self._gql(_CREATE_Q, {"input": inp}, mutation=True)
        return _parse_post(self._mutation_result(data, "createPost")["post"])

    def delete_post(self, post_id: str) -> None:
        data = self._gql(_DELETE_Q, {"input": {"id": post_id}}, mutation=True)
        self._mutation_result(data, "deletePost")
