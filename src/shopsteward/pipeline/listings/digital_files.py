"""PURE multi-ratio digital-file planner + encoder (design
2026-11-21-multi-ratio-digital-files, slice 1): no DB/event I/O. Same
input + config always yields the same plan and the same bytes."""

import hashlib
import io
from pathlib import Path
from typing import Any

from PIL import Image, ImageCms, ImageOps
from pydantic import BaseModel, model_validator

from shopsteward.pipeline.listings.images import encode_jpeg_under_cap

_EXIF_ARTIST = 0x013B
_EXIF_COPYRIGHT = 0x8298
_EXIF_IFD = 0x8769
_EXIF_DATETIME_ORIGINAL = 0x9003
_FULL_KEY = "full"


class SizeSpec(BaseModel):
    """One printable size as (short, long); exactly one of inches/mm."""

    label: str
    inches: tuple[float, float] | None = None
    mm: tuple[float, float] | None = None

    @model_validator(mode="after")
    def _one_unit(self) -> "SizeSpec":
        if (self.inches is None) == (self.mm is None):
            raise ValueError("size needs exactly one of inches or mm")
        return self

    def px(self, ppi: int) -> tuple[int, int]:
        """Minimum (short, long) pixels at ppi."""
        if self.inches is not None:
            short, long_ = self.inches
        else:
            assert self.mm is not None
            short, long_ = self.mm[0] / 25.4, self.mm[1] / 25.4
        return round(short * ppi), round(long_ * ppi)


class RatioSpec(BaseModel):
    key: str
    short: float
    long: float
    sizes: list[SizeSpec]


class DigitalFilesConfig(BaseModel):
    ratios: list[RatioSpec]
    ppi: int = 300
    min_retained_fraction: float = 0.6
    full_ratio_tolerance: float = 0.01
    max_files: int = 5
    max_bytes: int = 20_000_000
    copyright_holder: str | None = None


class FileEntry(BaseModel):
    key: str
    rank: int
    crop_box: tuple[int, int, int, int]  # left, top, right, bottom
    sizes: list[str]


class FilePlan(BaseModel):
    source_dims: tuple[int, int]
    focus: tuple[float, float]
    files: list[FileEntry]


def photo_dims(path: str | Path) -> tuple[int, int]:
    """(width, height) after EXIF orientation is applied."""
    with Image.open(path) as img:
        return ImageOps.exif_transpose(img).size


def _deliverable(spec: RatioSpec, crop_w: int, crop_h: int, ppi: int) -> list[str]:
    cs, cl = sorted((crop_w, crop_h))
    out = []
    for size in spec.sizes:
        ps, pl = size.px(ppi)
        if cs >= ps and cl >= pl:
            out.append(size.label)
    return out


def _window(
    dims: tuple[int, int], focus: tuple[float, float], spec: RatioSpec
) -> tuple[int, int, int, int]:
    w_src, h_src = dims
    # Square counts as portrait.
    aspect = spec.long / spec.short if w_src > h_src else spec.short / spec.long
    if w_src / h_src >= aspect:
        h, w = h_src, round(h_src * aspect)
    else:
        w, h = w_src, round(w_src / aspect)
    left = min(max(round(focus[0] * w_src - w / 2), 0), w_src - w)
    top = min(max(round(focus[1] * h_src - h / 2), 0), h_src - h)
    return left, top, left + w, top + h


def plan_files(
    dims: tuple[int, int],
    focus: tuple[float, float] | None,
    cfg: DigitalFilesConfig,
) -> FilePlan:
    fx, fy = focus or (0.5, 0.5)
    focus = (min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0))
    w_src, h_src = dims
    full_spec = next((r for r in cfg.ratios if r.key == _FULL_KEY), None)

    # `full` always ships, always first; its 2:3 sizes only if native is ~2:3.
    sizes: list[str] = []
    if full_spec is not None:
        target = full_spec.long / full_spec.short
        if abs(max(dims) / min(dims) - target) / target <= cfg.full_ratio_tolerance:
            sizes = _deliverable(full_spec, w_src, h_src, cfg.ppi)
    entries = [FileEntry(key=_FULL_KEY, rank=1, crop_box=(0, 0, w_src, h_src), sizes=sizes)]

    for spec in cfg.ratios:
        if spec.key == _FULL_KEY:
            continue
        box = _window(dims, focus, spec)
        cw, ch = box[2] - box[0], box[3] - box[1]
        if cw * ch < cfg.min_retained_fraction * w_src * h_src:
            continue
        deliver = _deliverable(spec, cw, ch, cfg.ppi)
        if deliver:
            entries.append(FileEntry(key=spec.key, rank=0, crop_box=box, sizes=deliver))

    entries = entries[: cfg.max_files]
    for rank, e in enumerate(entries, start=1):
        e.rank = rank
    return FilePlan(source_dims=dims, focus=focus, files=entries)


def _srgb_icc_bytes() -> bytes:
    """sRGB ICC with the header creation date (bytes 24-36) zeroed: lcms stamps
    the current time there, which would make every encode differ by second.
    The profile ID (84-100) is already zero, so no checksum to redo."""
    raw = bytearray(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
    raw[24:36] = bytes(12)
    return bytes(raw)


_SRGB_ICC = _srgb_icc_bytes()


def _is_srgb(profile: ImageCms.ImageCmsProfile) -> bool:
    return "srgb" in (ImageCms.getProfileDescription(profile) or "").lower()


def _capture_year(img: Image.Image) -> str | None:
    stamp = img.getexif().get_ifd(_EXIF_IFD).get(_EXIF_DATETIME_ORIGINAL)
    if isinstance(stamp, str) and len(stamp) >= 4 and stamp[:4].isdigit():
        return stamp[:4]
    return None


def encode_file(path: str | Path, entry: FileEntry, cfg: DigitalFilesConfig) -> tuple[bytes, str]:
    """Deterministic sRGB JPEG of entry.crop_box: sRGB ICC, dpi=ppi, fresh
    EXIF (Artist/Copyright only, never GPS/serials/Orientation). Copyright uses "(c)":
    EXIF ASCII is 7-bit and Pillow turns the copyright symbol into "?"."""
    with Image.open(path) as src:
        icc = src.info.get("icc_profile")
        year = _capture_year(src)
        img = ImageOps.exif_transpose(src)
        if icc:
            try:
                profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
                needs_convert = not _is_srgb(profile)
            except (ImageCms.PyCMSError, OSError) as exc:
                raise ValueError(f"{path}: unreadable embedded ICC profile") from exc
        else:
            needs_convert = False
        if needs_convert:
            srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB"))
            img = ImageCms.profileToProfile(img, profile, srgb, outputMode="RGB")
        else:
            img = img.convert("RGB")
        img = img.crop(entry.crop_box)

    save_kw: dict[str, Any] = {
        "icc_profile": _SRGB_ICC,
        "dpi": (cfg.ppi, cfg.ppi),
        "comment": b"",  # source JPEG COM markers can carry serials
    }
    if cfg.copyright_holder:
        exif = Image.Exif()
        exif[_EXIF_ARTIST] = cfg.copyright_holder
        stamp = f"{year} " if year else ""
        exif[_EXIF_COPYRIGHT] = f"(c) {stamp}{cfg.copyright_holder}"
        save_kw["exif"] = exif.tobytes()

    data = encode_jpeg_under_cap(
        img, cfg.max_bytes, label=str(path), cap_name="digital_files.max_bytes", **save_kw
    )
    return data, hashlib.sha256(data).hexdigest()
