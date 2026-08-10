import xml.etree.ElementTree as ET
from pathlib import Path

from shopsteward.adapters.look.interface import LookProfile
from shopsteward.editing.models import CorrectionSettings
from shopsteward.editing.xmp import compose, sidecar_path, write_sidecar

CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"


def _parse(xmp: str) -> ET.Element:
    return ET.fromstring(xmp)  # raises on malformed XML


def test_compose_is_wellformed_and_wb_as_shot():
    xmp = compose(CorrectionSettings(exposure=0.5), LookProfile(name="x"))
    root = _parse(xmp)
    desc = root.find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    assert desc.get(f"{{{CRS}}}WhiteBalance") == "As Shot"
    assert f"{{{CRS}}}Temperature" not in desc.attrib
    assert desc.get(f"{{{CRS}}}Exposure2012") == "0.50"


def test_look_owns_contrast_not_correction():
    xmp = compose(CorrectionSettings(), LookProfile(name="x", contrast=18, vibrance=14))
    desc = _parse(xmp).find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    assert desc.get(f"{{{CRS}}}Contrast2012") == "18"
    assert desc.get(f"{{{CRS}}}Vibrance") == "14"


def test_shadow_lift_emits_local_range_mask():
    xmp = compose(CorrectionSettings(shadow_lift=0.8, shadow_range_high=45), LookProfile(name="x"))
    assert "MaskGroupBasedCorrections" in xmp
    assert "RangeMask" in xmp or "Luminance" in xmp


def test_no_shadow_lift_omits_mask():
    xmp = compose(CorrectionSettings(shadow_lift=0.0), LookProfile(name="x"))
    assert "MaskGroupBasedCorrections" not in xmp


def test_contrast_is_clamped():
    xmp = compose(CorrectionSettings(), LookProfile.model_construct(name="x", contrast=999,
        tone_curve=[], hsl={}, split_toning={}, vibrance=0, saturation=0))
    desc = _parse(xmp).find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    assert desc.get(f"{{{CRS}}}Contrast2012") == "100"


def test_write_sidecar_creates_file_and_skips_existing(tmp_path: Path):
    raw = tmp_path / "IMG_1.CR3"
    raw.write_bytes(b"stub")
    xmp = compose(CorrectionSettings(), LookProfile(name="x"))
    assert write_sidecar(raw, xmp, overwrite=False) is True
    assert sidecar_path(raw).exists()
    assert write_sidecar(raw, xmp, overwrite=False) is False  # already exists
    assert write_sidecar(raw, xmp, overwrite=True) is True
