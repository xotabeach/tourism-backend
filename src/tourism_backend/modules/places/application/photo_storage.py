"""Local-disk storage for imported place photos (mirrors routes media)."""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

_DEFAULT_MEDIA_ROOT = Path(__file__).resolve().parents[5] / "data" / "media"
_MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", str(_DEFAULT_MEDIA_ROOT)))
_MAX_IMAGE_PIXELS = 20_000_000
_MAX_IMAGE_EDGE = 2560


class InvalidPlacePhoto(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SavedPlacePhoto:
    storage_key: str
    public_path: str
    content_type: str
    byte_size: int
    checksum_sha256: str
    width: int
    height: int


def _root() -> Path:
    _MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    return _MEDIA_ROOT


def _prepare_image(raw: bytes) -> tuple[bytes, int, int]:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if (image.format or "").upper() not in {"JPEG", "PNG", "WEBP"}:
                raise InvalidPlacePhoto(f"Unsupported image format: {image.format!r}")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
                raise InvalidPlacePhoto("Image dimensions are invalid")
            prepared = ImageOps.exif_transpose(image)
            prepared.thumbnail((_MAX_IMAGE_EDGE, _MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
            prepared = prepared.convert("RGBA" if prepared.mode in {"RGBA", "LA"} else "RGB")
            output = io.BytesIO()
            prepared.save(
                output,
                format="WEBP",
                quality=90,
                method=6,
                lossless=prepared.mode == "RGBA",
            )
            return output.getvalue(), prepared.width, prepared.height
    except UnidentifiedImageError as exc:
        raise InvalidPlacePhoto("Unrecognized image") from exc
    except InvalidPlacePhoto:
        raise
    except OSError as exc:
        raise InvalidPlacePhoto("Unable to process image") from exc


def save_place_photo(raw: bytes, *, place_id: UUID) -> SavedPlacePhoto:
    """Re-encode to WebP (capped size) and write under `places/<place_id>/`."""
    payload, width, height = _prepare_image(raw)
    relative_dir = Path("places") / str(place_id)
    target_dir = _root() / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}.webp"
    target = target_dir / filename
    target.write_bytes(payload)
    storage_key = f"{relative_dir.as_posix()}/{filename}"
    return SavedPlacePhoto(
        storage_key=storage_key,
        public_path=f"/media/{storage_key}",
        content_type="image/webp",
        byte_size=len(payload),
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
        width=width,
        height=height,
    )
