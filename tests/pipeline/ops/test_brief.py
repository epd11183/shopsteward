from shopsteward.core.db import connect, migrate
from shopsteward.core.projections import rebuild as rebuild_core
from shopsteward.pipeline.ops import config as ops_config
from shopsteward.pipeline.ops.brief import generate_brief, render_text
from shopsteward.pipeline.ops.projections import rebuild_ops
from tests.pipeline.ops.helpers import AS_OF, USER_ID, seed_two_year_shop


def _built_brief(tmp_path, as_of=AS_OF):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    seed_two_year_shop(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    return generate_brief(conn, USER_ID, cfg, as_of=as_of)


def test_generate_brief_assembles_every_section(tmp_path):
    report = _built_brief(tmp_path)
    assert report.revenue is not None
    assert len(report.top_sellers) == 1
    assert len(report.viewed_not_sold) == 1
    assert len(report.dead_listings) == 1
    assert len(report.trending) == 1
    assert report.product_type_breakdown
    assert report.size_breakdown


def test_render_text_contains_the_shop_section_and_the_shop_section_is_unaffected_by_chassis(
    tmp_path,
):
    # PR3 (M8a spec §8) added the NEEDS YOU/DONE/REFUSED/AUTONOMY chassis
    # sections on top of THE SHOP -- this scenario seeds no action.* events,
    # so those sections render "(0)"/empty, but THE SHOP's own wording is
    # byte-for-byte what slice 1 shipped.
    text = render_text(_built_brief(tmp_path))
    assert "THE SHOP" in text
    assert "Revenue" in text
    assert "Selling" in text
    assert "Dying" in text
    assert "Trending" in text
    assert "Shoot more" in text
    assert "NEEDS YOU (0)" in text
    assert "DONE (0)" in text
    assert "REFUSED (0)" in text
    assert "autonomy: ON" in text  # defaults: autonomy.enabled=True (2026-08-24)
    assert "$20.00 cap" in text
    # DONE OVERNIGHT was the draft mockup's literal label; the shipped
    # section header is "DONE" (see draft §6 vs the PR3 contract's §8).
    assert "DONE OVERNIGHT" not in text


def test_render_text_reflects_actual_titles(tmp_path):
    text = render_text(_built_brief(tmp_path))
    assert "Sandhill Cranes at Dawn Acrylic Print 16x24" in text
    assert "Foggy Pines Fine Art Poster" in text  # viewed but never sold


def test_disabled_section_toggle_is_respected(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    seed_two_year_shop(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    cfg = cfg.model_copy(
        update={"brief_sections": cfg.brief_sections.model_copy(update={"dying": False})}
    )

    report = generate_brief(conn, USER_ID, cfg, as_of=AS_OF)
    assert report.dead_listings == []
    assert report.trending == []


def test_render_text_reconciles_revenue_vs_item_revenue_lines(tmp_path):
    # F2: the top-line Revenue figure (receipt grandtotal) and the
    # Selling/Product-mix/By-size/Shoot-more lines (item price only) must
    # each say what they are, right where they're printed.
    text = render_text(_built_brief(tmp_path))
    assert "receipt total incl. shipping/tax" in text
    assert "Selling (item revenue only -- excludes shipping/tax)" in text
    assert "Product mix (item revenue only -- excludes shipping/tax)" in text
    assert "item revenue this window" in text  # the shoot-more lines


def test_render_text_shoot_more_is_labelled_as_product_type_not_a_photo_subject(tmp_path):
    # F3: "Shoot more of: acrylic" reads as a literal photography directive.
    text = render_text(_built_brief(tmp_path))
    assert "product types selling well on few listings, not photo subjects" in text


def test_render_text_on_an_empty_shop_does_not_crash(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    ops_config.seed(conn, USER_ID)
    rebuild_core(conn)
    rebuild_ops(conn)
    cfg = ops_config.get_ops_config(conn, USER_ID)
    report = generate_brief(conn, USER_ID, cfg, as_of=AS_OF)
    text = render_text(report)
    assert "n/a" in text  # pct_change undefined with $0 prior revenue
    assert "nothing sold" in text
