"""Compose an Adobe Camera Raw XMP sidecar from a correction + a look, and write
it next to the RAW. WB is trusted as-shot (Temp/Tint omitted). Correction owns
Exposure2012 + a local luminance-range shadow-lift mask; the look owns Contrast,
tone curve, HSL, split toning, vibrance, saturation.

ponytail: the local-mask (MaskGroupBasedCorrections) block is the fiddliest part
of the ACR schema; it is verified structurally in tests and MUST be confirmed by
opening one sidecar in Lightroom during the Task 1 smoke before trusting output.
"""

from pathlib import Path

from shopsteward.adapters.look.interface import LookProfile
from shopsteward.editing.models import CorrectionSettings

_CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def _clamp_split(key: str, value: int) -> int:
    if key.endswith("Hue"):
        return _clamp(value, 0, 360)
    if key.endswith("Saturation"):
        return _clamp(value, 0, 100)
    return _clamp(value, -100, 100)  # Balance and anything else


def sidecar_path(raw_path: Path) -> Path:
    return raw_path.with_suffix(".xmp")


def compose(correction: CorrectionSettings, look: LookProfile) -> str:
    attrs: list[str] = [
        'crs:Version="15.0"',
        'crs:WhiteBalance="As Shot"',
        f'crs:Exposure2012="{correction.exposure:.2f}"',
        f'crs:Contrast2012="{_clamp(look.contrast, -100, 100)}"',
        f'crs:Vibrance="{_clamp(look.vibrance, -100, 100)}"',
        f'crs:Saturation="{_clamp(look.saturation, -100, 100)}"',
    ]
    seen: set[str] = set()
    for key, value in sorted(look.hsl.items()):
        name = _xml_name(key)
        if name and name not in seen:
            attrs.append(f'crs:{name}="{_clamp(int(value), -100, 100)}"')
            seen.add(name)
    for key, value in sorted(look.split_toning.items()):
        name = _xml_name(key)
        if name and name not in seen:
            attrs.append(f'crs:{name}="{_clamp_split(key, int(value))}"')
            seen.add(name)

    children: list[str] = []
    if look.tone_curve:
        children.append(_tone_curve(look.tone_curve))
    if correction.shadow_lift > 0:
        children.append(_shadow_mask(correction))

    attr_block = "\n    ".join(attrs)
    child_block = "\n".join(children)
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="ShopSteward">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        f'    xmlns:crs="{_CRS}"\n'
        f"    {attr_block}>\n"
        f"{child_block}\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
    )


def _tone_curve(points: list[list[int]]) -> str:
    lis = "".join(
        f"     <rdf:li>{int(p[0])}, {int(p[1])}</rdf:li>\n"
        for p in points if len(p) == 2
    )
    return (
        "   <crs:ToneCurvePV2012>\n    <rdf:Seq>\n"
        f"{lis}"
        "    </rdf:Seq>\n   </crs:ToneCurvePV2012>"
    )


def _shadow_mask(correction: CorrectionSettings) -> str:
    lo = _clamp(correction.shadow_range_low, 0, 100)
    hi = _clamp(correction.shadow_range_high, 0, 100)
    return (
        "   <crs:MaskGroupBasedCorrections>\n"
        "    <rdf:Seq>\n"
        "     <rdf:li>\n"
        "      <rdf:Description\n"
        f'       crs:LocalExposure2012="{correction.shadow_lift:.2f}"\n'
        '       crs:CorrectionActive="true">\n'
        "       <crs:CorrectionMasks>\n        <rdf:Seq>\n"
        "         <rdf:li>\n"
        '          <rdf:Description crs:What="Mask/RangeMask" crs:MaskActive="true"\n'
        '           crs:MaskName="Shadows" crs:MaskBlendMode="0" crs:RangeType="Luminance"\n'
        f'           crs:LumRangeLower="{lo / 100:.3f}" crs:LumRangeUpper="{hi / 100:.3f}"/>\n'
        "         </rdf:li>\n"
        "        </rdf:Seq>\n       </crs:CorrectionMasks>\n"
        "      </rdf:Description>\n"
        "     </rdf:li>\n"
        "    </rdf:Seq>\n"
        "   </crs:MaskGroupBasedCorrections>"
    )


def _xml_name(key: str) -> str:
    # Keys may be LLM-supplied; produce a valid XML local name or "" (caller skips "").
    name = "".join(c for c in key if c.isalnum())
    if not name or name[0].isdigit():
        return ""
    return name


def write_sidecar(raw_path: Path, xmp: str, *, overwrite: bool) -> bool:
    target = sidecar_path(raw_path)
    if target.exists() and not overwrite:
        return False
    target.write_text(xmp, encoding="utf-8")
    return True
