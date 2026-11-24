"""One-shot "what you get" panel render for an existing listing (slice 5b,
`listing.panel_replace`): the mockup job's renderer, fed sizes by the caller.
Lives here so the pipeline needs a single mockups import."""

import io
from pathlib import Path

from shopsteward.mockups import compositor
from shopsteward.mockups.config import load_mockup_defaults
from shopsteward.mockups.intents import render_digital_whatyougot
from shopsteward.mockups.jobs import _MASTER_MAX_LONG_EDGE_PX


def render_panel_jpeg(photo_path: str, sizes: list[str]) -> bytes:
    cfg = load_mockup_defaults()
    photo_cfg = cfg.model_copy(
        update={"whatyougot": cfg.whatyougot.model_copy(update={"sizes": sizes})}
    )
    panel, _ = render_digital_whatyougot(
        compositor.prep_master(Path(photo_path), _MASTER_MAX_LONG_EDGE_PX), photo_cfg
    )
    buf = io.BytesIO()
    panel.save(buf, "JPEG", quality=cfg.render.jpeg_quality)
    return buf.getvalue()
