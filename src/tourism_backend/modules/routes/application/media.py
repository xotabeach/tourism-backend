"""Validated storage for user-created route media."""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from tourism_backend.api.errors import AppError

_DEFAULT_MEDIA_ROOT = Path(__file__).resolve().parents[5] / "data" / "media"
_MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", str(_DEFAULT_MEDIA_ROOT)))
_MAX_BYTES = 50 * 1024 * 1024
_MAX_IMAGE_PIXELS = 20_000_000
_MAX_IMAGE_EDGE = 2560


@dataclass(frozen=True, slots=True)
class SavedRouteMedia:
    storage_key: str
    public_path: str
    content_type: str
    byte_size: int
    checksum_sha256: str
    kind: Literal["image", "video"]
    width: int | None = None
    height: int | None = None


def _root() -> Path:
    _MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    return _MEDIA_ROOT


def _save(payload: bytes, *, route_id: UUID, extension: str) -> tuple[str, str]:
    relative_dir = Path("routes") / str(route_id)
    target_dir = _root() / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}.{extension}"
    target = target_dir / filename
    target.write_bytes(payload)
    storage_key = f"{relative_dir.as_posix()}/{filename}"
    return storage_key, f"/media/{storage_key}"


def _prepare_image(raw: bytes) -> tuple[bytes, int, int]:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            if (image.format or "").upper() not in {"JPEG", "PNG", "WEBP"}:
                raise AppError(
                    code="invalid_route_media",
                    message="Only JPEG, PNG, or WebP images are allowed",
                    status_code=400,
                )
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
                raise AppError(
                    code="invalid_route_media",
                    message="Image dimensions are invalid",
                    status_code=400,
                )
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
        raise AppError(
            code="invalid_route_media",
            message="Unrecognized image",
            status_code=400,
        ) from exc
    except AppError:
        raise
    except OSError as exc:
        raise AppError(
            code="invalid_route_media",
            message="Unable to process image",
            status_code=400,
        ) from exc


def _video_extension(raw: bytes) -> str | None:
    if len(raw) >= 12 and raw[4:8] == b"ftyp":
        return "mp4"
    if raw.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    return None


async def save_route_media(upload: UploadFile, *, route_id: UUID) -> SavedRouteMedia:
    raw = await upload.read(_MAX_BYTES + 1)
    if not raw:
        raise AppError(code="invalid_route_media", message="Empty upload", status_code=400)
    if len(raw) > _MAX_BYTES:
        raise AppError(
            code="invalid_route_media",
            message="Route media is too large",
            status_code=400,
        )

    content_type = (upload.content_type or "").lower()
    width: int | None = None
    height: int | None = None
    if content_type.startswith("image/"):
        payload, width, height = _prepare_image(raw)
        extension = "webp"
        stored_content_type = "image/webp"
        kind: Literal["image", "video"] = "image"
    else:
        detected_extension = _video_extension(raw)
        if detected_extension is None:
            raise AppError(
                code="invalid_route_media",
                message="Only MP4 or WebM videos are allowed",
                status_code=400,
            )
        extension = detected_extension
        payload = raw
        stored_content_type = "video/mp4" if extension == "mp4" else "video/webm"
        kind = "video"

    storage_key, public_path = _save(payload, route_id=route_id, extension=extension)
    return SavedRouteMedia(
        storage_key=storage_key,
        public_path=public_path,
        content_type=stored_content_type,
        byte_size=len(payload),
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
        kind=kind,
        width=width,
        height=height,
    )
