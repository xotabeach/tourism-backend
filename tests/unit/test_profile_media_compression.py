"""Unit tests for profile media compression (no DB)."""

from __future__ import annotations

import io

from PIL import Image

from tourism_backend.modules.identity.application.media import compress_profile_image


def _png_bytes(size: tuple[int, int], *, with_alpha: bool = False) -> bytes:
    mode = "RGBA" if with_alpha else "RGB"
    color = (10, 20, 30, 128) if with_alpha else (10, 20, 30)
    buf = io.BytesIO()
    Image.new(mode, size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def test_avatar_is_downscaled_and_stored_as_webp() -> None:
    raw = _png_bytes((3000, 2000))
    with Image.open(io.BytesIO(raw)) as image:
        payload, ext = compress_profile_image(image, kind="avatar")
    assert ext == "webp"
    assert len(payload) < len(raw)
    with Image.open(io.BytesIO(payload)) as stored:
        assert stored.format == "WEBP"
        assert max(stored.size) <= 1024


def test_cover_cap_is_larger_than_avatar() -> None:
    raw = _png_bytes((4000, 3000))
    with Image.open(io.BytesIO(raw)) as image:
        payload, ext = compress_profile_image(image, kind="cover")
    assert ext == "webp"
    with Image.open(io.BytesIO(payload)) as stored:
        assert max(stored.size) <= 2048
        assert max(stored.size) > 1024


def test_alpha_uses_lossless_webp() -> None:
    raw = _png_bytes((128, 128), with_alpha=True)
    with Image.open(io.BytesIO(raw)) as image:
        payload, ext = compress_profile_image(image, kind="avatar")
    assert ext == "webp"
    with Image.open(io.BytesIO(payload)) as stored:
        assert stored.mode in {"RGBA", "RGB"}
        # Round-trip alpha channel present for lossless path.
        assert "A" in stored.getbands() or stored.mode == "RGBA"
