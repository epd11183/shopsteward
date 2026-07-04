# ShopSteward M0 Completion + M1 (Etsy Data Pull & Analytics) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Project rule overlay (PRD §8.1):** non-trivial tasks route through the
> `.claude/agents/` roster — `python-impl` implements, `test-author` writes
> tests, `reviewer` gates every diff before the operator sees it. Task 1
> (operator-confirmed cleanup) and Task 2 (git init) are orchestrator/operator
> actions, not sub-agent work. The first PR of the milestone requires operator
> review (PRD §8.2).

**Goal:** Close out M0 (repo safe to build in the open, CI green end-to-end) and deliver M1: pull Etsy shop data through a fixture-backed adapter into the event-sourced core and surface a local analytics dashboard.

**Architecture:** Event-sourced SQLite core (`core/`): immutable `events` table + rebuildable projections. Etsy sits behind an adapter protocol in `adapters/etsy/` with two implementations — a fixture-backed fake (default, used by tests and by `sync --fixtures`) and a live httpx client that stays un-wired until the operator approves the smoke test (PRD §8.4). A sync service turns adapter snapshots into events; projections turn events into `listings` / `sales` / daily-metrics tables; FastAPI serves `/api/analytics/*`; a minimal React+Vite page renders it.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLite (stdlib `sqlite3`), httpx + respx, Typer CLI, pytest, React + Vite + Tailwind (dashboard), uv, ruff.

---

## Phase 0 — M0 completion

### Task 1: Operator-confirmed cleanup of the nested duplicate scaffold

A stale full copy of the repo lives at `shopsteward/` (repo root → `shopsteward/CLAUDE.md`, `shopsteward/docs/…`, etc. — an unzip artifact). Its PRD predates the 2026-07-03 amendments. It must not be committed.

**Files:**
- Delete (after operator confirms): entire `shopsteward/` directory at repo root

- [ ] **Step 1: Operator confirms** the nested `shopsteward/` directory holds nothing unique (spot-check any file you personally edited there; every file was diffed as stale or identical during planning).
- [ ] **Step 2: Delete it** (operator or orchestrator; `rm -rf` is denied to agents by `.claude/settings.json`, deliberately):

```powershell
Remove-Item -Recurse -Force .\shopsteward
```

- [ ] **Step 3: Verify** `Get-ChildItem` at repo root no longer lists `shopsteward/` (the `src/shopsteward/` package is unaffected).

### Task 2: Initialize git and make the scaffold the first commit

The directory is not yet a git repository. CI, PR flow, and gitleaks all depend on this.

**Files:**
- Create: `.git/` (init), first commit of the existing scaffold

- [ ] **Step 1: Init + first commit**

```bash
git init -b main
git add .
git status   # VERIFY: no data/ contents beyond .gitkeep, no .env*, no photos
git commit -m "chore: M0 scaffold — repo structure, guardrails, CI, plugin, PRD v2.1"
```

- [ ] **Step 2: Verify guardrails held**

```bash
git ls-files | grep -Ei '\.env($|\.)|^data/.+|\.(cr3|jpg|jpeg|png)$' ; echo "exit=$?"
```

Expected: only `data/.gitkeep` and (if any) template placeholder images under `config/defaults/`; `.env.example` is allowed — everything else is a stop-and-fix.

- [ ] **Step 3: Operator creates the GitHub repo and pushes** (public is cleared — Workiva addendum confirmed 2026-07-03):

```bash
git remote add origin https://github.com/<owner>/shopsteward.git
git push -u origin main
```

### Task 3: Make the declared CLI entry point real

`pyproject.toml` declares `shopsteward = "shopsteward.cli:app"` but `src/shopsteward/cli.py` doesn't exist — `uv run shopsteward` crashes today.

**Files:**
- Create: `src/shopsteward/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py
from typer.testing import CliRunner

from shopsteward.cli import app

runner = CliRunner()


def test_cli_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    assert "ingest" in result.output


def test_ingest_requires_mode():
    result = runner.invoke(app, ["ingest", "some/path"])
    assert result.exit_code != 0  # --mode is required
```

- [ ] **Step 2: Run it — expect failure**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL / collection error, `ModuleNotFoundError: No module named 'shopsteward.cli'`

- [ ] **Step 3: Minimal implementation**

```python
# src/shopsteward/cli.py
"""ShopSteward CLI. UI is the primary surface; this is the scriptable path."""

from enum import Enum
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, help="ShopSteward — photography workflow tool.")


class IngestMode(str, Enum):
    hero = "hero"
    mass = "mass"


@app.command()
def serve() -> None:
    """Run the FastAPI backend + local UI."""
    import uvicorn

    uvicorn.run("shopsteward.api:create_app", factory=True, host="127.0.0.1", port=8321)


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    mode: Annotated[IngestMode, typer.Option(...)],
    preset: Annotated[str | None, typer.Option(help="Mass-mode preset family")] = None,
) -> None:
    """Folder-pointed ingestion (M2 — stub until then)."""
    raise typer.Exit(code=typer.secho(f"ingest is M2 scope ({mode.value=}, {preset=})", fg="yellow") or 1)
```

- [ ] **Step 4: Run tests — expect pass** (`uv run pytest tests/test_cli.py -v` → 2 passed; the `serve`/`api` import is lazy so no api module is needed yet)
- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check . && uv run ruff format .
git add src/shopsteward/cli.py tests/test_cli.py
git commit -m "feat: real CLI entry point with serve/ingest commands"
```

### Task 4: CI green end-to-end on a doc-only PR (Kickoff §3.4)

**Files:**
- Modify: none (Task 3's test already un-breaks `pytest` exit-5-on-empty)

- [ ] **Step 1: Branch, doc touch, PR**

```bash
git checkout -b m0/ci-smoke
printf '\n<!-- CI smoke: %s -->\n' "$(date -u +%F)" >> CONTRIBUTING.md
git add CONTRIBUTING.md && git commit -m "docs: CI smoke change" && git push -u origin m0/ci-smoke
gh pr create --title "M0: CI smoke test" --body "Doc-only change to prove gitleaks + ruff + pytest all run green."
```

- [ ] **Step 2: Verify all three CI jobs pass** (`gh pr checks --watch`). Expected: `Secret scan (gitleaks)` ✅, `Lint + tests` ✅.
- [ ] **Step 3: Operator merges** (first PR of the milestone — operator review required). M0 is now complete.

---

## Phase 1 — M1: Etsy data pull + analytics dashboard

**M1 boundary decisions (locked here):**
- Event types v1: `etsy.listing.observed`, `etsy.sale.observed`, `etsy.shop.observed`. Observations are snapshots; dedup/diffing is the projection's job, keeping events append-only and idempotent to re-sync.
- The live Etsy adapter is *implemented* in M1 but nothing calls it until the operator approves the recorded-fixture set + smoke-test plan (PRD §8.4). All tests and the default `sync` path use the fixture adapter.
- Dashboard v1 is one page: revenue/orders over time, top listings, catalog counts. No auth (localhost only).

### Task 5: Event store (core)

**Files:**
- Create: `src/shopsteward/core/__init__.py` (empty), `src/shopsteward/core/db.py`, `src/shopsteward/core/events.py`
- Test: `tests/core/test_events.py` (+ empty `tests/__init__.py`, `tests/core/__init__.py`)

- [ ] **Step 1: Write the failing tests**

```python
# tests/core/test_events.py
import sqlite3

import pytest

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    migrate(c)
    return c


def test_append_and_read_roundtrip(conn):
    e = append(conn, Event(user_id=1, type="etsy.listing.observed", payload={"listing_id": 42}))
    events = read_all(conn)
    assert [ev.id for ev in events] == [e.id]
    assert events[0].payload == {"listing_id": 42}
    assert events[0].user_id == 1


def test_events_are_immutable(conn):
    append(conn, Event(user_id=1, type="etsy.shop.observed", payload={}))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE events SET type = 'tampered'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM events")


def test_read_all_orders_by_id(conn):
    for i in range(3):
        append(conn, Event(user_id=1, type="t", payload={"i": i}))
    assert [e.payload["i"] for e in read_all(conn)] == [0, 1, 2]
```

- [ ] **Step 2: Run — expect failure** (`uv run pytest tests/core -v` → ModuleNotFoundError)
- [ ] **Step 3: Implement**

```python
# src/shopsteward/core/db.py
"""SQLite connection + schema. Events are append-only, enforced by triggers."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
```

```python
# src/shopsteward/core/events.py
"""Append-only event log. Projections rebuild derived state from here."""

import json
import sqlite3

from pydantic import BaseModel


class Event(BaseModel):
    id: int | None = None
    user_id: int
    type: str
    payload: dict
    created_at: str | None = None


def append(conn: sqlite3.Connection, event: Event) -> Event:
    cur = conn.execute(
        "INSERT INTO events (user_id, type, payload) VALUES (?, ?, ?)",
        (event.user_id, event.type, json.dumps(event.payload)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _from_row(row)


def read_all(conn: sqlite3.Connection, type_prefix: str | None = None) -> list[Event]:
    if type_prefix:
        rows = conn.execute(
            "SELECT * FROM events WHERE type LIKE ? ORDER BY id", (type_prefix + "%",)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY id").fetchall()
    return [_from_row(r) for r in rows]


def _from_row(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        user_id=row["user_id"],
        type=row["type"],
        payload=json.loads(row["payload"]),
        created_at=row["created_at"],
    )
```

- [ ] **Step 4: Run — expect 3 passed**
- [ ] **Step 5: Commit** (`git add src/shopsteward/core tests && git commit -m "feat(core): append-only event store with immutability triggers"`)

### Task 6: Etsy adapter protocol + Pydantic boundary models

**Files:**
- Create: `src/shopsteward/adapters/__init__.py` (empty), `src/shopsteward/adapters/etsy/__init__.py` (empty), `src/shopsteward/adapters/etsy/models.py`, `src/shopsteward/adapters/etsy/interface.py`
- Test: `tests/adapters/etsy/test_models.py` (+ `__init__.py` files)

- [ ] **Step 1: Failing test**

```python
# tests/adapters/etsy/test_models.py
from shopsteward.adapters.etsy.models import EtsyListing, EtsyReceipt, EtsyShop


def test_listing_parses_minimal_etsy_shape():
    listing = EtsyListing.model_validate(
        {
            "listing_id": 111,
            "title": "Misty Ridge Print",
            "state": "active",
            "quantity": 5,
            "views": 120,
            "num_favorers": 7,
            "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"},
            "tags": ["landscape", "wall art"],
        }
    )
    assert listing.price_usd == 25.0


def test_receipt_totals():
    receipt = EtsyReceipt.model_validate(
        {
            "receipt_id": 9,
            "created_timestamp": 1751500000,
            "grandtotal": {"amount": 4300, "divisor": 100, "currency_code": "USD"},
            "transactions": [
                {"transaction_id": 1, "listing_id": 111, "quantity": 1,
                 "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"}},
            ],
        }
    )
    assert receipt.total_usd == 43.0
    assert receipt.transactions[0].listing_id == 111
```

- [ ] **Step 2: Run — expect ModuleNotFoundError**
- [ ] **Step 3: Implement**

```python
# src/shopsteward/adapters/etsy/models.py
"""Pydantic models mirroring the Etsy Open API v3 shapes we consume."""

from pydantic import BaseModel


class Money(BaseModel):
    amount: int
    divisor: int
    currency_code: str

    @property
    def as_float(self) -> float:
        return self.amount / self.divisor


class EtsyShop(BaseModel):
    shop_id: int
    shop_name: str
    listing_active_count: int = 0
    transaction_sold_count: int = 0


class EtsyListing(BaseModel):
    listing_id: int
    title: str
    state: str
    quantity: int
    views: int = 0
    num_favorers: int = 0
    price: Money
    tags: list[str] = []

    @property
    def price_usd(self) -> float:
        return self.price.as_float


class EtsyTransaction(BaseModel):
    transaction_id: int
    listing_id: int
    quantity: int
    price: Money


class EtsyReceipt(BaseModel):
    receipt_id: int
    created_timestamp: int
    grandtotal: Money
    transactions: list[EtsyTransaction] = []

    @property
    def total_usd(self) -> float:
        return self.grandtotal.as_float
```

```python
# src/shopsteward/adapters/etsy/interface.py
"""Adapter protocol. Core code depends on this, never on an SDK/HTTP client."""

from typing import Protocol

from shopsteward.adapters.etsy.models import EtsyListing, EtsyReceipt, EtsyShop


class EtsyAdapter(Protocol):
    def get_shop(self) -> EtsyShop: ...
    def list_listings(self) -> list[EtsyListing]: ...
    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]: ...
```

- [ ] **Step 4: Run — expect 2 passed**  · **Step 5: Commit** (`feat(adapters): Etsy adapter protocol + boundary models`)

### Task 7: Fixture-backed fake adapter + scrubbed fixtures

**Files:**
- Create: `src/shopsteward/adapters/etsy/fake.py`, `tests/fixtures/etsy/shop.json`, `tests/fixtures/etsy/listings.json`, `tests/fixtures/etsy/receipts.json`
- Test: `tests/adapters/etsy/test_fake.py`

- [ ] **Step 1: Author scrubbed fixtures** (hand-written for now; replaced by recorded-then-scrubbed responses when the live smoke test is approved — same shapes either way)

```json
// tests/fixtures/etsy/shop.json
{"shop_id": 100001, "shop_name": "ExampleShop", "listing_active_count": 3, "transaction_sold_count": 12}
```

```json
// tests/fixtures/etsy/listings.json
{"count": 3, "results": [
  {"listing_id": 111, "title": "Misty Ridge Print", "state": "active", "quantity": 5, "views": 120,
   "num_favorers": 7, "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"},
   "tags": ["landscape", "wall art"]},
  {"listing_id": 222, "title": "Heron at Dawn", "state": "active", "quantity": 3, "views": 340,
   "num_favorers": 21, "price": {"amount": 3500, "divisor": 100, "currency_code": "USD"},
   "tags": ["bird", "wildlife"]},
  {"listing_id": 333, "title": "Digital Download Bundle", "state": "active", "quantity": 999, "views": 88,
   "num_favorers": 4, "price": {"amount": 1200, "divisor": 100, "currency_code": "USD"},
   "tags": ["digital", "printable"]}
]}
```

```json
// tests/fixtures/etsy/receipts.json
{"count": 2, "results": [
  {"receipt_id": 9001, "created_timestamp": 1751000000,
   "grandtotal": {"amount": 2500, "divisor": 100, "currency_code": "USD"},
   "transactions": [{"transaction_id": 1, "listing_id": 111, "quantity": 1,
                     "price": {"amount": 2500, "divisor": 100, "currency_code": "USD"}}]},
  {"receipt_id": 9002, "created_timestamp": 1751430000,
   "grandtotal": {"amount": 4700, "divisor": 100, "currency_code": "USD"},
   "transactions": [{"transaction_id": 2, "listing_id": 222, "quantity": 1,
                     "price": {"amount": 3500, "divisor": 100, "currency_code": "USD"}},
                    {"transaction_id": 3, "listing_id": 333, "quantity": 1,
                     "price": {"amount": 1200, "divisor": 100, "currency_code": "USD"}}]}
]}
```

- [ ] **Step 2: Failing test**

```python
# tests/adapters/etsy/test_fake.py
from pathlib import Path

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter

FIXTURES = Path(__file__).parents[2] / "fixtures" / "etsy"


def test_fake_adapter_serves_fixture_data():
    adapter = FixtureEtsyAdapter(FIXTURES)
    assert adapter.get_shop().shop_name == "ExampleShop"
    assert len(adapter.list_listings()) == 3
    receipts = adapter.list_receipts()
    assert len(receipts) == 2


def test_min_created_filters_receipts():
    adapter = FixtureEtsyAdapter(FIXTURES)
    assert [r.receipt_id for r in adapter.list_receipts(min_created=1751400000)] == [9002]
```

- [ ] **Step 3: Run — expect failure, then implement**

```python
# src/shopsteward/adapters/etsy/fake.py
"""Fixture-backed adapter: the default until live access is approved (PRD §8.4)."""

import json
from pathlib import Path

from shopsteward.adapters.etsy.models import EtsyListing, EtsyReceipt, EtsyShop


class FixtureEtsyAdapter:
    def __init__(self, fixture_dir: Path):
        self._dir = Path(fixture_dir)

    def _load(self, name: str) -> dict:
        return json.loads((self._dir / f"{name}.json").read_text())

    def get_shop(self) -> EtsyShop:
        return EtsyShop.model_validate(self._load("shop"))

    def list_listings(self) -> list[EtsyListing]:
        return [EtsyListing.model_validate(r) for r in self._load("listings")["results"]]

    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]:
        receipts = [EtsyReceipt.model_validate(r) for r in self._load("receipts")["results"]]
        if min_created is not None:
            receipts = [r for r in receipts if r.created_timestamp >= min_created]
        return receipts
```

- [ ] **Step 4: Run — expect 2 passed** · **Step 5: Commit** (`feat(adapters): fixture-backed Etsy fake + scrubbed fixtures`)

### Task 8: Sync service — adapter snapshot → events

**Files:**
- Create: `src/shopsteward/core/sync.py`
- Test: `tests/core/test_sync.py`

- [ ] **Step 1: Failing test**

```python
# tests/core/test_sync.py
from pathlib import Path

import pytest

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import read_all
from shopsteward.core.sync import sync_etsy

FIXTURES = Path(__file__).parents[1] / "fixtures" / "etsy"


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def test_sync_appends_observation_events(conn):
    result = sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    assert result.shops == 1 and result.listings == 3 and result.receipts == 2
    types = [e.type for e in read_all(conn)]
    assert types.count("etsy.shop.observed") == 1
    assert types.count("etsy.listing.observed") == 3
    assert types.count("etsy.sale.observed") == 2


def test_resync_is_incremental_on_receipts(conn):
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    sales = [e for e in read_all(conn, "etsy.sale") ]
    assert len(sales) == 2  # second sync passes min_created past both receipts
```

- [ ] **Step 2: Run — expect failure, then implement**

```python
# src/shopsteward/core/sync.py
"""Pull an Etsy snapshot through the adapter and append observation events."""

import sqlite3

from pydantic import BaseModel

from shopsteward.adapters.etsy.interface import EtsyAdapter
from shopsteward.core.events import Event, append, read_all


class SyncResult(BaseModel):
    shops: int = 0
    listings: int = 0
    receipts: int = 0


def _last_receipt_ts(conn: sqlite3.Connection) -> int | None:
    sales = read_all(conn, "etsy.sale.observed")
    return max((e.payload["created_timestamp"] for e in sales), default=None)


def sync_etsy(conn: sqlite3.Connection, adapter: EtsyAdapter, user_id: int) -> SyncResult:
    result = SyncResult()
    shop = adapter.get_shop()
    append(conn, Event(user_id=user_id, type="etsy.shop.observed", payload=shop.model_dump()))
    result.shops = 1
    for listing in adapter.list_listings():
        append(conn, Event(user_id=user_id, type="etsy.listing.observed",
                           payload=listing.model_dump()))
        result.listings += 1
    last_ts = _last_receipt_ts(conn)
    min_created = last_ts + 1 if last_ts is not None else None
    for receipt in adapter.list_receipts(min_created=min_created):
        append(conn, Event(user_id=user_id, type="etsy.sale.observed",
                           payload=receipt.model_dump()))
        result.receipts += 1
    return result
```

- [ ] **Step 3: Run — expect 2 passed** · **Step 4: Commit** (`feat(core): Etsy sync service appending observation events`)

### Task 9: Analytics projection

**Files:**
- Create: `src/shopsteward/core/projections.py`
- Test: `tests/core/test_projections.py`

- [ ] **Step 1: Failing test**

```python
# tests/core/test_projections.py
from pathlib import Path

import pytest

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import analytics_summary, rebuild
from shopsteward.core.sync import sync_etsy

FIXTURES = Path(__file__).parents[1] / "fixtures" / "etsy"


@pytest.fixture()
def synced(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    rebuild(conn)
    return conn


def test_summary_totals(synced):
    s = analytics_summary(synced, user_id=1)
    assert s.total_revenue_usd == pytest.approx(72.0)  # 25.00 + 47.00
    assert s.total_orders == 2
    assert s.active_listings == 3


def test_top_listings_by_views(synced):
    s = analytics_summary(synced, user_id=1)
    assert s.top_listings[0].listing_id == 222  # 340 views


def test_rebuild_is_idempotent(synced):
    rebuild(synced)
    rebuild(synced)
    assert analytics_summary(synced, user_id=1).total_orders == 2
```

- [ ] **Step 2: Run — expect failure, then implement**

```python
# src/shopsteward/core/projections.py
"""Derived read models, rebuilt from the event log. Safe to drop and rebuild."""

import sqlite3
from datetime import UTC, datetime

from pydantic import BaseModel

from shopsteward.core.events import read_all

PROJECTION_SCHEMA = """
DROP TABLE IF EXISTS proj_listings;
CREATE TABLE proj_listings (
    user_id INTEGER NOT NULL, listing_id INTEGER NOT NULL, title TEXT NOT NULL,
    state TEXT NOT NULL, views INTEGER, num_favorers INTEGER, price_usd REAL,
    PRIMARY KEY (user_id, listing_id)
);
DROP TABLE IF EXISTS proj_sales;
CREATE TABLE proj_sales (
    user_id INTEGER NOT NULL, receipt_id INTEGER NOT NULL, sale_date TEXT NOT NULL,
    total_usd REAL NOT NULL,
    PRIMARY KEY (user_id, receipt_id)
);
"""


class ListingRow(BaseModel):
    listing_id: int
    title: str
    views: int
    num_favorers: int
    price_usd: float


class Summary(BaseModel):
    total_revenue_usd: float
    total_orders: int
    active_listings: int
    revenue_by_day: dict[str, float]
    top_listings: list[ListingRow]


def rebuild(conn: sqlite3.Connection) -> None:
    conn.executescript(PROJECTION_SCHEMA)
    for e in read_all(conn, "etsy.listing.observed"):
        p = e.payload
        conn.execute(
            "INSERT OR REPLACE INTO proj_listings VALUES (?,?,?,?,?,?,?)",
            (e.user_id, p["listing_id"], p["title"], p["state"], p.get("views", 0),
             p.get("num_favorers", 0), p["price"]["amount"] / p["price"]["divisor"]),
        )
    for e in read_all(conn, "etsy.sale.observed"):
        p = e.payload
        day = datetime.fromtimestamp(p["created_timestamp"], tz=UTC).date().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO proj_sales VALUES (?,?,?,?)",
            (e.user_id, p["receipt_id"], day,
             p["grandtotal"]["amount"] / p["grandtotal"]["divisor"]),
        )
    conn.commit()


def analytics_summary(conn: sqlite3.Connection, user_id: int) -> Summary:
    revenue, orders = conn.execute(
        "SELECT COALESCE(SUM(total_usd),0), COUNT(*) FROM proj_sales WHERE user_id=?",
        (user_id,),
    ).fetchone()
    active = conn.execute(
        "SELECT COUNT(*) FROM proj_listings WHERE user_id=? AND state='active'", (user_id,)
    ).fetchone()[0]
    by_day = dict(conn.execute(
        "SELECT sale_date, SUM(total_usd) FROM proj_sales WHERE user_id=? "
        "GROUP BY sale_date ORDER BY sale_date", (user_id,),
    ).fetchall())
    top = [
        ListingRow(listing_id=r["listing_id"], title=r["title"], views=r["views"],
                   num_favorers=r["num_favorers"], price_usd=r["price_usd"])
        for r in conn.execute(
            "SELECT * FROM proj_listings WHERE user_id=? ORDER BY views DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    ]
    return Summary(total_revenue_usd=revenue, total_orders=orders, active_listings=active,
                   revenue_by_day=by_day, top_listings=top)
```

- [ ] **Step 3: Run — expect 3 passed** · **Step 4: Commit** (`feat(core): analytics projection rebuilt from event log`)

### Task 10: FastAPI app + analytics endpoints + sync CLI command

**Files:**
- Create: `src/shopsteward/api.py`, `src/shopsteward/settings.py`
- Modify: `src/shopsteward/cli.py` (add `sync` command)
- Test: `tests/test_api.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_api.py
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
from shopsteward.api import create_app
from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import rebuild
from shopsteward.core.sync import sync_etsy

FIXTURES = Path(__file__).parent / "fixtures" / "etsy"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    conn = connect(db)
    migrate(conn)
    sync_etsy(conn, FixtureEtsyAdapter(FIXTURES), user_id=1)
    rebuild(conn)
    conn.close()
    return TestClient(create_app())


def test_analytics_summary_endpoint(client):
    body = client.get("/api/analytics/summary").json()
    assert body["total_revenue_usd"] == 72.0
    assert body["active_listings"] == 3
    assert len(body["top_listings"]) == 3


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}
```

- [ ] **Step 2: Run — expect failure, then implement**

```python
# src/shopsteward/settings.py
"""Runtime settings. DB path via env; defaults to data/ (gitignored)."""

import os
from pathlib import Path


def db_path() -> Path:
    return Path(os.environ.get("SHOPSTEWARD_DB", "data/shopsteward.db"))
```

```python
# src/shopsteward/api.py
"""FastAPI backend serving the local dashboard and analytics API."""

from fastapi import FastAPI

from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import Summary, analytics_summary
from shopsteward.settings import db_path

DEFAULT_USER_ID = 1  # single-operator v1; schema stays multi-tenant-ready


def create_app() -> FastAPI:
    app = FastAPI(title="ShopSteward")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/analytics/summary")
    def summary() -> Summary:
        conn = connect(db_path())
        try:
            migrate(conn)
            return analytics_summary(conn, user_id=DEFAULT_USER_ID)
        finally:
            conn.close()

    return app
```

Add to `src/shopsteward/cli.py` (below `ingest`):

```python
@app.command()
def sync(
    fixtures: Annotated[Path | None, typer.Option(help="Fixture dir (default source until live approved)")] = None,
) -> None:
    """Pull Etsy data into the event store and rebuild projections."""
    from shopsteward.adapters.etsy.fake import FixtureEtsyAdapter
    from shopsteward.core.db import connect, migrate
    from shopsteward.core.projections import rebuild
    from shopsteward.core.sync import sync_etsy
    from shopsteward.settings import db_path

    if fixtures is None:
        typer.secho("Live Etsy sync is gated on operator approval (PRD §8.4); pass --fixtures.", fg="red")
        raise typer.Exit(1)
    conn = connect(db_path())
    migrate(conn)
    result = sync_etsy(conn, FixtureEtsyAdapter(fixtures), user_id=1)
    rebuild(conn)
    typer.echo(f"synced: {result.model_dump()}")
```

- [ ] **Step 3: Run full suite — expect all green** (`uv run pytest -v`)
- [ ] **Step 4: Commit** (`feat(api): analytics endpoints + fixture-gated sync command`)

### Task 11: Live Etsy adapter (implemented, not wired)

**Files:**
- Create: `src/shopsteward/adapters/etsy/live.py`, `docs/setup/etsy.md`
- Test: `tests/adapters/etsy/test_live.py` (respx-mocked — still no live calls)

- [ ] **Step 1: Failing test** (respx mocks the Etsy v3 endpoints; asserts auth header, pagination, and model parsing)

```python
# tests/adapters/etsy/test_live.py
import httpx
import respx

from shopsteward.adapters.etsy.live import LiveEtsyAdapter

BASE = "https://openapi.etsy.com/v3/application"


@respx.mock
def test_list_listings_paginates_and_parses():
    respx.get(f"{BASE}/shops/100001/listings/active", params={"limit": 100, "offset": 0}).mock(
        return_value=httpx.Response(200, json={"count": 1, "results": [
            {"listing_id": 111, "title": "T", "state": "active", "quantity": 1,
             "price": {"amount": 100, "divisor": 100, "currency_code": "USD"}}]})
    )
    adapter = LiveEtsyAdapter(api_key="k", shop_id=100001, access_token="tok")
    listings = adapter.list_listings()
    assert listings[0].listing_id == 111
    sent = respx.calls.last.request
    assert sent.headers["x-api-key"] == "k"
    assert sent.headers["authorization"] == "Bearer tok"
```

- [ ] **Step 2: Implement** — `LiveEtsyAdapter` with httpx.Client, `x-api-key` + OAuth bearer headers, offset pagination (limit 100) for listings and receipts, `min_created` passed to the receipts endpoint. Read credentials from constructor args only — callers load env; the adapter never reads `.env` itself.

```python
# src/shopsteward/adapters/etsy/live.py
"""Live Etsy Open API v3 client. NOT wired into any default path — the CLI
refuses live sync until the operator approves the smoke test (PRD §8.4)."""

import httpx

from shopsteward.adapters.etsy.models import EtsyListing, EtsyReceipt, EtsyShop

BASE = "https://openapi.etsy.com/v3/application"


class LiveEtsyAdapter:
    def __init__(self, api_key: str, shop_id: int, access_token: str):
        self._shop_id = shop_id
        self._client = httpx.Client(
            headers={"x-api-key": api_key, "authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )

    def _get(self, path: str, **params) -> dict:
        resp = self._client.get(f"{BASE}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, path: str, **params) -> list[dict]:
        results: list[dict] = []
        offset = 0
        while True:
            page = self._get(path, limit=100, offset=offset, **params)
            results.extend(page["results"])
            offset += 100
            if offset >= page["count"]:
                return results

    def get_shop(self) -> EtsyShop:
        return EtsyShop.model_validate(self._get(f"/shops/{self._shop_id}"))

    def list_listings(self) -> list[EtsyListing]:
        rows = self._paginate(f"/shops/{self._shop_id}/listings/active")
        return [EtsyListing.model_validate(r) for r in rows]

    def list_receipts(self, min_created: int | None = None) -> list[EtsyReceipt]:
        params = {"min_created": min_created} if min_created else {}
        rows = self._paginate(f"/shops/{self._shop_id}/receipts", **params)
        return [EtsyReceipt.model_validate(r) for r in rows]
```

- [ ] **Step 3: Write `docs/setup/etsy.md`** — how to register an Etsy app, get the personal OAuth token (authorization-code flow with `transactions_r listings_r shops_r` scopes), which env vars the CLI will read (`ETSY_API_KEY`, `ETSY_SHOP_ID`, `ETSY_ACCESS_TOKEN`, `ETSY_REFRESH_TOKEN`), and the smoke-test plan below.
- [ ] **Step 4: Run suite green, commit** (`feat(adapters): live Etsy client (unwired) + setup guide`)
- [ ] **Step 5: OPERATOR GATE — smoke-test approval (PRD §8.4).** Proposal to approve: one manual run of `shopsteward sync --live` (flag added only after approval) against PhotosByEricD with read-only scopes; record responses, scrub identifiers (shop id, listing ids ok to keep? → no: replace with fixture ids), commit scrubbed fixtures replacing the hand-written ones; then live sync stays operator-invoked only through M1.

### Task 12: Dashboard page (React + Vite)

**Files:**
- Create: `frontend/` scaffold (Vite react-ts template), `frontend/src/App.tsx`, `frontend/src/api.ts`
- Modify: `src/shopsteward/api.py` (serve built assets), `.github/workflows/ci.yml` (frontend build job)

- [ ] **Step 1: Scaffold** (`npm create vite@latest frontend -- --template react-ts && cd frontend && npm i && npm i -D tailwindcss @tailwindcss/vite`; add `/api` dev proxy to `vite.config.ts` → `http://127.0.0.1:8321`)
- [ ] **Step 2: `frontend/src/api.ts`**

```ts
export type ListingRow = {
  listing_id: number; title: string; views: number;
  num_favorers: number; price_usd: number;
};
export type Summary = {
  total_revenue_usd: number; total_orders: number; active_listings: number;
  revenue_by_day: Record<string, number>; top_listings: ListingRow[];
};
export const fetchSummary = async (): Promise<Summary> =>
  (await fetch("/api/analytics/summary")).json();
```

- [ ] **Step 3: `frontend/src/App.tsx`** — three stat cards (revenue, orders, active listings), a revenue-by-day bar list, a top-listings table. Plain Tailwind, no chart library yet (YAGNI — revisit if the bar list reads poorly with real data).

```tsx
import { useEffect, useState } from "react";
import { fetchSummary, type Summary } from "./api";

export default function App() {
  const [s, setS] = useState<Summary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { fetchSummary().then(setS).catch((e) => setErr(String(e))); }, []);
  if (err) return <p className="p-8 text-red-600">Failed to load: {err}</p>;
  if (!s) return <p className="p-8">Loading…</p>;
  const maxDay = Math.max(...Object.values(s.revenue_by_day), 1);
  return (
    <main className="mx-auto max-w-4xl p-8 space-y-8">
      <h1 className="text-2xl font-semibold">ShopSteward — Analytics</h1>
      <section className="grid grid-cols-3 gap-4">
        <Stat label="Revenue" value={`$${s.total_revenue_usd.toFixed(2)}`} />
        <Stat label="Orders" value={String(s.total_orders)} />
        <Stat label="Active listings" value={String(s.active_listings)} />
      </section>
      <section>
        <h2 className="mb-2 font-medium">Revenue by day</h2>
        {Object.entries(s.revenue_by_day).map(([day, usd]) => (
          <div key={day} className="flex items-center gap-2 text-sm">
            <span className="w-24 text-gray-500">{day}</span>
            <div className="h-4 bg-emerald-500" style={{ width: `${(usd / maxDay) * 100}%` }} />
            <span>${usd.toFixed(2)}</span>
          </div>
        ))}
      </section>
      <section>
        <h2 className="mb-2 font-medium">Top listings</h2>
        <table className="w-full text-sm">
          <thead><tr className="text-left text-gray-500">
            <th>Title</th><th>Views</th><th>Favorites</th><th>Price</th></tr></thead>
          <tbody>{s.top_listings.map((l) => (
            <tr key={l.listing_id} className="border-t">
              <td>{l.title}</td><td>{l.views}</td><td>{l.num_favorers}</td>
              <td>${l.price_usd.toFixed(2)}</td></tr>))}
          </tbody>
        </table>
      </section>
    </main>
  );
}
const Stat = ({ label, value }: { label: string; value: string }) => (
  <div className="rounded border p-4">
    <div className="text-sm text-gray-500">{label}</div>
    <div className="text-xl font-semibold">{value}</div>
  </div>
);
```

- [ ] **Step 4: Serve built assets from FastAPI** — in `create_app()`, mount `frontend/dist` if it exists:

```python
from pathlib import Path
from fastapi.staticfiles import StaticFiles

dist = Path("frontend/dist")
if dist.exists():
    app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
```

- [ ] **Step 5: CI** — add a `frontend` job (`npm ci && npm run build` in `frontend/`) to `.github/workflows/ci.yml`. Commit lockfile.
- [ ] **Step 6: Manual verify** — `shopsteward sync --fixtures tests/fixtures/etsy`, `npm run build`, `shopsteward serve`, open `http://127.0.0.1:8321`, see the three cards populated from fixture data. Commit (`feat(ui): M1 analytics dashboard`).

### Task 13: M1 wrap-up

- [ ] Reviewer sub-agent pass over the full M1 diff (guardrails + milestone scope).
- [ ] `uv run ruff check . && uv run pytest` green; `npm run build` green.
- [ ] PR "M1: Etsy data pull + analytics dashboard" — **operator review required** (first PR of milestone).
- [ ] After merge + operator smoke-test approval (Task 11 Step 5): record real fixtures, scrub, swap in, delete hand-written ones.

---

## Phase 2+ — M2–M7 outline (planned at their own milestone kickoffs, per §8.5 C-Suite cadence)

| Milestone | Key deliverables | Load-bearing decisions already made |
|---|---|---|
| **M2 — Editing module** | `editing/` package, folder-pointed ingestion (UI toggle + CLI `--mode`), RAW+JPEG pairing, EPD Edit Bridge queue processor (lua-impl), mass-mode `--preset` + confirm, LrC collection + JPEG export, import-linter boundary rule in CI | Q1–Q4; boundary rule from PRD §12 |
| **M3 — Hero scoring + Gate 1** | Gemini Flash triage + Pro borderline adapter (fixtures first), technical scoring (Laplacian sharpness etc.), curation UI, landing-folder watcher (no re-scoring, technical validation only) | Q5, Q6, Q10, §8.5 budget logging |
| **M4 — Templates + compositor** | ~15 AI-generated rooms (offline via Gemini/ChatGPT), JSON quad sidecars, annotation helper tool, OpenCV perspective composite, template-matching by palette/orientation | Q7–Q9, Q11 |
| **M5 — Listings** | Gelato + Printful adapters (POD-first), Etsy enrichment, Gate 3 UI | PRD §5.4 invariants |
| **M6 — Instagram** | Asset packs, captions via Gemini + style guide, scheduler | Q12 (style guide authored before first listing copy — pull into M5 kickoff) |
| **M7 — Feedback loop** | Tuning profiles from sales/engagement, weekly action queue | PRD §6 |

## Self-review notes

- Spec coverage: M0 items (Kickoff §3.3–3.4) → Tasks 1–4; M1 (PRD §10) → Tasks 5–13; §8.4 live-API gate → Tasks 10/11 refuse live paths without approval; multi-tenant rule → `user_id` on events and both projections.
- Style-guide artifact (Q12) is needed before the first generated listing copy (M5/M6), not M1 — noted in the outline so it isn't lost.
- Types are consistent across tasks (`Event`, `Summary`, `ListingRow`, adapter protocol signatures match all three implementations).
