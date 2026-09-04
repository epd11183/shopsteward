from PIL import Image
from typer.testing import CliRunner

import shopsteward.pipeline.listings.push as push_mod
from shopsteward.adapters.etsy.fake import FakeEtsyWriteAdapter
from shopsteward.cli import app
from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append, read_all
from shopsteward.pipeline.listings.projections import rebuild_listings
from shopsteward.pipeline.projections import rebuild_pipeline

USER_ID = 1
runner = CliRunner()


def _seed_landing_file(db_path, tmp_path) -> None:
    conn = connect(db_path)
    migrate(conn)
    path = tmp_path / "hero.jpg"
    Image.new("RGB", (100, 100), (1, 2, 3)).save(path, "JPEG")
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="landing.file_observed",
            payload={
                "file_id": "f1",
                "path": str(path),
                "base_name": "hero",
                "format": "JPEG",
                "width": 6000,
                "height": 4000,
                "color_space": "sRGB",
                "photo_id": "p1",
            },
        ),
    )
    rebuild_pipeline(conn)
    conn.close()


def test_pod_build_dry_run_prints_and_appends_nothing(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    _seed_landing_file(db, tmp_path)

    result = runner.invoke(app, ["pod", "build", "--dry-run"])

    assert result.exit_code == 0
    assert "KEEP acrylic/acrylic_16x24" in result.output
    assert "price=149.00" in result.output

    conn = connect(db)
    migrate(conn)
    n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE type LIKE 'listingdraft.%'").fetchone()
    assert n["n"] == 0  # dry-run appended nothing
    conn.close()


def test_pod_build_appends_and_reports(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    _seed_landing_file(db, tmp_path)

    result = runner.invoke(app, ["pod", "build"])

    assert result.exit_code == 0
    assert "drafts_built" in result.output

    conn = connect(db)
    migrate(conn)
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE type='listingdraft.print_file_hosted'"
    ).fetchone()
    assert n["n"] == 3  # acrylic, poster, canvas
    conn.close()


def test_pod_build_live_printfile_without_gate_refuses(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_PRINTFILE", raising=False)
    _seed_landing_file(db, tmp_path)

    result = runner.invoke(app, ["pod", "build", "--live-printfile"])

    assert result.exit_code == 1
    assert "gated on operator approval" in result.output


# --- pod publish (design: pod-publish) --------------------------------


def _seed_pod_draft(
    db_path, *, draft_id="pod-draft-1", format_="acrylic", etsy_listing_id=9001, pod_status
) -> None:
    """Same seeding shape as test_gate3.py's _seed_pod_draft, against a
    fresh connection to the CLI's own db (mirrors _seed_landing_file
    above)."""
    from shopsteward.pipeline.listings import config as listing_config

    conn = connect(db_path)
    migrate(conn)
    listing_config.seed(conn, USER_ID)
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.created",
            payload={
                "draft_id": draft_id,
                "landing_file_id": None,
                "photo_id": None,
                "set_key": None,
                "provider": "gelato",
                "format": format_,
                "sku_source": "provider",
                "listing_type": "physical",
                "config_hash": None,
                "pod_config_hash": "pod-cfg-hash-1",
            },
        ),
    )
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listingdraft.provider_linked",
            payload={
                "draft_id": draft_id,
                "etsy_listing_id": etsy_listing_id,
                "etsy_listing_state": "draft",
            },
        ),
    )
    if pod_status == "enriched":
        append(
            conn,
            Event(
                user_id=USER_ID,
                type="listingdraft.enriched",
                payload={"draft_id": draft_id, "etsy_listing_id": etsy_listing_id},
            ),
        )
    rebuild_listings(conn)
    conn.close()


def test_pod_publish_dry_run_prints_plan_and_writes_nothing(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    _seed_pod_draft(db, draft_id="pod-1", etsy_listing_id=9001, pod_status="enriched")

    result = runner.invoke(app, ["pod", "publish", "acrylic"])

    assert result.exit_code == 0
    assert "eligible drafts: 1" in result.output
    assert "9001" in result.output
    assert "Dry-run: nothing written" in result.output

    conn = connect(db)
    migrate(conn)
    assert read_all(conn, "gate3.published") == []
    conn.close()


def test_pod_publish_rejects_unknown_format(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))

    result = runner.invoke(app, ["pod", "publish", "mug"])

    assert result.exit_code == 1
    assert "must be one of" in result.output


def test_pod_publish_apply_without_live_etsy_write_flag_refuses(tmp_path, monkeypatch):
    """--apply alone (no --live-etsy-write at all) must refuse before ever
    touching the DB -- omitting the flag would otherwise silently run the
    real event-append path against the FAKE adapter, permanently mutating a
    real POD draft's state with nothing actually published on Etsy."""
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    _seed_pod_draft(db, draft_id="pod-1", etsy_listing_id=9001, pod_status="enriched")

    result = runner.invoke(app, ["pod", "publish", "acrylic", "--apply"])

    assert result.exit_code == 1
    assert "--live-etsy-write" in result.output

    conn = connect(db)
    migrate(conn)
    assert read_all(conn, "gate3.approved") == []
    assert read_all(conn, "gate3.published") == []
    assert read_all(conn, "gate3.publish_failed") == []
    conn.close()


def test_pod_publish_apply_without_live_gate_refuses(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    monkeypatch.delenv("SHOPSTEWARD_LIVE_ETSY_WRITE", raising=False)
    _seed_pod_draft(db, draft_id="pod-1", etsy_listing_id=9001, pod_status="enriched")

    result = runner.invoke(app, ["pod", "publish", "acrylic", "--apply", "--live-etsy-write"])

    assert result.exit_code == 1

    conn = connect(db)
    migrate(conn)
    assert read_all(conn, "gate3.published") == []
    conn.close()


def test_pod_publish_apply_publishes_only_the_named_format(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    _seed_pod_draft(
        db, draft_id="pod-acrylic", format_="acrylic", etsy_listing_id=9001, pod_status="enriched"
    )
    _seed_pod_draft(
        db, draft_id="pod-poster", format_="poster", etsy_listing_id=9002, pod_status="enriched"
    )
    _seed_pod_draft(
        db, draft_id="pod-acrylic-2", format_="acrylic", etsy_listing_id=9003, pod_status="linked"
    )

    fake = FakeEtsyWriteAdapter()
    for listing_id in (9001, 9002, 9003):
        fake.listings[listing_id] = {
            "title": "t",
            "description": "d",
            "price": 149.0,
            "quantity": 1,
            "tags": [],
            "state": "draft",
            "images": [{"listing_image_id": 1, "rank": 1}],
            "files": [{"listing_file_id": 1, "rank": 1, "name": "print.png"}],
        }
    monkeypatch.setattr(push_mod, "build_etsy_write_adapter", lambda *, live: fake)
    monkeypatch.setattr("shopsteward.pipeline.live_gate.live_etsy_write_open", lambda: True)

    result = runner.invoke(app, ["pod", "publish", "acrylic", "--apply", "--live-etsy-write"])

    assert result.exit_code == 0
    assert "published: 1 failed: 0 total: 1" in result.output

    conn = connect(db)
    migrate(conn)
    rebuild_listings(conn)
    rows = {
        r["draft_id"]: r["state"]
        for r in conn.execute(
            "SELECT draft_id, state FROM proj_listing_drafts WHERE user_id=?", (USER_ID,)
        ).fetchall()
    }
    assert rows["pod-acrylic"] == "published"
    assert rows["pod-poster"] == "built"  # different format -- untouched
    assert rows["pod-acrylic-2"] == "built"  # not yet enriched -- untouched
    conn.close()
