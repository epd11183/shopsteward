"""In-memory image prep for vision-API calls: downscale + full EXIF strip.
Never mutates the source file on disk; the print itself is never touched."""

from io import BytesIO
from pathlib import Path

from PIL import Image


def prep_vision_jpeg(jpeg_path: Path, max_long_edge: int = 1024) -> bytes:
    with Image.open(jpeg_path) as img:
        img = img.convert("RGB")
        img.thumbnail((max_long_edge, max_long_edge), Image.LANCZOS)
        buf = BytesIO()
        # Deliberately omit exif= here: Image.open()/convert()/thumbnail() all
        # carry the source EXIF blob forward in .info, but the JPEG encoder
        # only embeds it if explicitly passed. Leaving exif= unset re-encodes
        # a clean file with no metadata, no GPS, nothing.
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
