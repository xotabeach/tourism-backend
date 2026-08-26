"""Validated, metadata-stripping storage for support-ticket photos."""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from tourism_backend.api.errors import AppError

_DEFAULT_MEDIA_ROOT = Path(__file__).resolve().parents[5] / "data" / "media"
_MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", str(_DEFAULT_MEDIA_ROOT)))
_MAX_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_PIXELS = 12_000_000
_MAX_IMAGE_EDGE = 2048


@dataclass(frozen=True, slots=True)
class SavedSupportAttachment:
    storage_key: str
    public_path: str
    content_type: str
    byte_size: int
    checksum_sha256: str
    width: int
    height: int


def _invalid(message: str) -> AppError:
    return AppError(code="invalid_support_attachment", message=message, status_code=400)


def _prepare(raw: bytes) -> tuple[bytes, int, int]:
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if (image.format or "").upper() not in {"JPEG", "PNG", "WEBP"}:
                raise _invalid("Only JPEG, PNG, or WebP images are allowed")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
                raise _invalid("Image dimensions are invalid")
            image.load()
            prepared = ImageOps.exif_transpose(image)
            prepared.thumbnail((_MAX_IMAGE_EDGE, _MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
            prepared = prepared.convert("RGBA" if prepared.mode in {"RGBA", "LA"} else "RGB")
            output = io.BytesIO()
            prepared.save(
                output,
                format="WEBP",
                quality=86,
                method=6,
                lossless=prepared.mode == "RGBA",
            )
            return output.getvalue(), prepared.width, prepared.height
    except (Image.DecompressionBombError, UnidentifiedImageError) as exc:
        raise _invalid("Unrecognized or unsafe image") from exc
    except AppError:
        raise
    except (OSError, ValueError) as exc:
        raise _invalid("Unable to process image") from exc


async def save_support_attachment(upload: UploadFile, *, ticket_id: UUID) -> SavedSupportAttachment:
    raw = await upload.read(_MAX_BYTES + 1)
    if not raw:
        raise _invalid("Empty upload")
    if len(raw) > _MAX_BYTES:
        raise _invalid("Attachment is too large")
    if not (upload.content_type or "").lower().startswith("image/"):
        raise _invalid("Only images are allowed")

    payload, width, height = _prepare(raw)
    relative_dir = Path("support") / str(ticket_id)
    target_dir = _MEDIA_ROOT / relative_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}.webp"
    target = target_dir / filename
    target.write_bytes(payload)
    storage_key = f"{relative_dir.as_posix()}/{filename}"
    return SavedSupportAttachment(
        storage_key=storage_key,
        public_path=f"/media/{storage_key}",
        content_type="image/webp",
        byte_size=len(payload),
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
        width=width,
        height=height,
    )


def delete_support_attachment(storage_key: str, *, ticket_id: UUID) -> None:
    """Delete only a file inside this ticket's storage directory."""
    root = _MEDIA_ROOT.resolve()
    allowed = (root / "support" / str(ticket_id)).resolve()
    target = (root / storage_key.lstrip("/")).resolve()
    if target.parent != allowed or allowed.parent.parent != root:
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        # Database archival remains authoritative; orphan cleanup can retry.
        return
