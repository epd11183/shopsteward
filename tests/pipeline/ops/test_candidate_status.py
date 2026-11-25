"""`shopsteward listings candidate-status`: each base name lands in the right
bucket, `needed` is arithmetic over unfinished candidates, nothing is written.
Images are generated with Pillow (public repo: no photo fixtures)."""

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from typer.testing import CliRunner

from shopsteward.core.db import connect, migrate
from shopsteward.core.events import Event, append
from shopsteward.pipeline import tuning
from shopsteward.pipeline.config import TUNING_PROFILE_PATH
from shopsteward.pipeline.listings import adopt
from shopsteward.pipeline.ops.capabilities.catalog_expand import candidate_status

from .test_catalog_expand import (
    USER_ID,
    _apply_ops_cfg,
    _enable_asset_store,
    _fail,
    _propose_for,
)

SIZE = 3000  # landing's own floor (tuning_profile.json)
QUADS = [(0, 0), (1, 0), (0, 1), (1, 1)]


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    return c


def _photo(folder: Path, name: str, quad: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (SIZE, SIZE), (10, 40, 90))
    qx, qy = QUADS[quad]
    h = SIZE // 2
    ImageDraw.Draw(img).rectangle((qx * h, qy * h, qx * h + h, qy * h + h), fill=(200, 40, 10))
    path = folder / name
    img.save(path, "JPEG")
    return path


def _setup(conn, tmp_path):
    _enable_asset_store(conn, tmp_path)
    folder = tmp_path / "candidates"
    paths = {n: _photo(folder, f"{n}.jpg", i) for i, n in enumerate(["listed", "failed", "fresh"])}
    cfg = _apply_ops_cfg(conn, tmp_path, source_folder=folder, min_long_edge_px=SIZE)
    return cfg, paths


def _with_min(cfg, n: int):
    ce = cfg.catalog_expansion.model_copy(update={"min_candidates": n})
    return cfg.model_copy(update={"catalog_expansion": ce})


def test_each_base_name_lands_in_its_bucket(conn, tmp_path):
    cfg, paths = _setup(conn, tmp_path)
    ids = {n: hashlib.sha256(p.read_bytes()).hexdigest() for n, p in paths.items()}

    # `listed`: ingested, then adopted onto a live Etsy listing (no title).
    tuning.seed(conn, USER_ID, TUNING_PROFILE_PATH)
    assert adopt.ingest_one_file(conn, USER_ID, paths["listed"]) == ids["listed"]
    append(
        conn,
        Event(
            user_id=USER_ID,
            type="listing.source_adopted",
            payload={
                "draft_id": "adopted-777",
                "landing_file_id": ids["listed"],
                "photo_id": "p",
                "etsy_listing_id": 777,
            },
        ),
    )
    # `failed`: three failed catalog_expand attempts.
    _propose_for(conn, cfg, ids["failed"], "a1")
    for kind in ("failed", "refused", "failed"):
        _fail(conn, "a1", kind)

    events_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    report = candidate_status(conn, USER_ID, cfg)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before

    assert report["listed"] == ["listed"]
    assert report["in_folder"] == ["failed", "fresh", "listed"]
    assert report["failed"] == ["failed"]
    assert report["ingested"] == []  # the only landing file is already `listed`
    # one unfinished, untried file (`fresh`) of min_candidates=15
    assert report["needed"] == 14


def test_needed_floors_at_zero(conn, tmp_path):
    cfg, _ = _setup(conn, tmp_path)  # 3 untried candidates
    assert candidate_status(conn, USER_ID, _with_min(cfg, 2))["needed"] == 0
    assert candidate_status(conn, USER_ID, _with_min(cfg, 5))["needed"] == 2


def test_cli_json_is_read_only(tmp_path, monkeypatch):
    from shopsteward.cli import app

    db = tmp_path / "cli.db"
    monkeypatch.setenv("SHOPSTEWARD_DB", str(db))
    # Never scan the operator's real candidates folder from a test.
    from shopsteward.pipeline.ops import config as ops_config

    shipped = ops_config.load_ops_config()
    isolated = shipped.model_copy(
        update={
            "catalog_expansion": shipped.catalog_expansion.model_copy(
                update={"source_folder": str(tmp_path / "absent")}
            )
        }
    )
    monkeypatch.setattr(ops_config, "load_ops_config", lambda *a, **k: isolated)
    result = CliRunner().invoke(app, ["listings", "candidate-status", "--json"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert set(out) == {"listed", "ingested", "in_folder", "failed", "needed"}
    assert out["needed"] == 15  # shipped min_candidates; source folder absent here
    c = connect(db)
    assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
