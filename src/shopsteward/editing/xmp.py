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
    for key, value in sorted(look.hsl.items()):
        attrs.append(f'crs:{_xml_name(key)}="{_clamp(int(value), -100, 100)}"')
    for key, value in sorted(look.split_toning.items()):
        attrs.append(f'crs:{_xml_name(key)}="{int(value)}"')

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
        f"     <rdf:li>{int(x)}, {int(y)}</rdf:li>\n" for x, y in points
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
    # Keys come from our own config/LLM schema; strip anything not a safe XML name char.
    return "".join(c for c in key if c.isalnum())


def write_sidecar(raw_path: Path, xmp: str, *, overwrite: bool) -> bool:
    target = sidecar_path(raw_path)
    if target.exists() and not overwrite:
        return False
    target.write_text(xmp, encoding="utf-8")
    return True
