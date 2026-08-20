"""Optional HEIC→JPEG conversion, backed by pillow-heif.
Installed via the `heic` extra: `pip install dcimport[heic]`."""

from pathlib import Path
from typing import BinaryIO


class MissingHeicSupportError(Exception):
    """HEIC conversion was requested but pillow-heif is not installed."""

    def __init__(self):
        super().__init__(
            "HEIC conversion requires the 'heic' extra. Install it with: pip install dcimport[heic]"
        )


def heic_support_available() -> bool:
    """Return whether pillow-heif is installed."""

    try:
        import pillow_heif  # noqa: F401
    except ImportError:
        return False
    else:
        return True


def convert_heic_to_jpeg(src: Path, dst: BinaryIO) -> None:
    """Convert the HEIC file at `src` to a JPEG at `dst`, carrying over EXIF data."""

    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()

    with Image.open(src) as img:
        exif = img.info.get("exif")

        if exif:
            img.save(dst, format="JPEG", quality=95, exif=exif)
        else:
            img.save(dst, format="JPEG", quality=95)
