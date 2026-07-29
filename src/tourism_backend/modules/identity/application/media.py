"""Safe profile image upload to local MEDIA_DIR."""

from __future__ import annotations

import io
import os
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from tourism_backend.api.errors import AppError

_DEFAULT_MEDIA_ROOT = Path(__file__).resolve().parents[5] / "data" / "media"
_MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", str(_DEFAULT_MEDIA_ROOT)))
_MAX_BYTES = 5 * 1024 * 1024
_ALLOWED_FORMATS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
_MAX_PIXELS = 12_000_000

# UI caps: phone photos are often 12MP; UI never needs that much.
# Downscale is visually lossless for avatar/cover display sizes.
_MAX_EDGE = {
    "avatar": 1024,
    "cover": 2048,
}

# WebP q≈90 is typically smaller than JPEG q88 at equal or better look.
_WEBP_QUALITY = 90
_WEBP_METHOD = 6


def media_root() -> Path:
    root = _MEDIA_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prepare_image(image: Image.Image, *, kind: str) -> Image.Image:
    """Normalize orientation, strip EXIF via rebuild, and cap edge length."""
    # Honour camera orientation before stripping metadata.
    normalized = ImageOps.exif_transpose(image)

    max_edge = _MAX_EDGE[kind]
    width, height = normalized.size
    longest = max(width, height)
    if longest > max_edge:
        scale = max_edge / longest
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        normalized = normalized.resize(new_size, Image.Resampling.LANCZOS)

    if normalized.mode in {"RGBA", "LA"} or (
        normalized.mode == "P" and "transparency" in normalized.info
    ):
        return normalized.convert("RGBA")
    return normalized.convert("RGB")


def compress_profile_image(image: Image.Image, *, kind: str) -> tuple[bytes, str]:
    """Re-encode to WebP for storage savings without visible quality loss.

    Always returns WebP: high-quality lossy for photos (q=90), lossless when
    the image has an alpha channel so transparency stays exact.
    """
    prepared = _prepare_image(image, kind=kind)
    out = io.BytesIO()
    if prepared.mode == "RGBA":
        # Lossless keeps sharp edges / transparency exact for overlays.
        prepared.save(out, format="WEBP", lossless=True, method=_WEBP_METHOD)
    else:
        prepared.save(
            out,
            format="WEBP",
            quality=_WEBP_QUALITY,
            method=_WEBP_METHOD,
        )
    return out.getvalue(), "webp"


async def save_profile_image(
    upload: UploadFile,
    *,
    user_id: UUID,
    kind: str,
) -> str:
    if kind not in {"avatar", "cover"}:
        raise AppError(code="validation_error", message="Unknown media kind", status_code=400)

    raw = await upload.read(_MAX_BYTES + 1)
    if not raw:
        raise AppError(code="invalid_image", message="Empty upload", status_code=400)
    if len(raw) > _MAX_BYTES:
        raise AppError(code="invalid_image", message="Image too large", status_code=400)

    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            fmt = (image.format or "").upper()
            if fmt not in _ALLOWED_FORMATS:
                raise AppError(
                    code="invalid_image",
                    message="Only JPEG, PNG, or WebP are allowed",
                    status_code=400,
                )
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > _MAX_PIXELS:
                raise AppError(
                    code="invalid_image",
                    message="Image dimensions invalid",
                    status_code=400,
                )
            payload, ext = compress_profile_image(image, kind=kind)
    except UnidentifiedImageError as exc:
        raise AppError(
            code="invalid_image",
            message="Unrecognized image",
            status_code=400,
        ) from exc
    except AppError:
        raise
    except OSError as exc:
        raise AppError(
            code="invalid_image",
            message="Unable to process image",
            status_code=400,
        ) from exc

    rel_dir = Path("profiles") / str(user_id)
    abs_dir = media_root() / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{kind}-{uuid4().hex}.{ext}"
    abs_path = abs_dir / filename
    abs_path.write_bytes(payload)
    return f"/media/{rel_dir.as_posix()}/{filename}"
