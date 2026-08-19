import io
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import UploadFile
from PIL import Image
from starlette.datastructures import Headers

from tourism_backend.api.errors import AppError
from tourism_backend.modules.routes.application import review_media


def _upload(payload: bytes, content_type: str = "image/jpeg") -> UploadFile:
    return UploadFile(
        io.BytesIO(payload),
        filename="../../original-with-location.jpg",
        headers=Headers({"content-type": content_type}),
    )


def _jpeg_with_exif() -> bytes:
    output = io.BytesIO()
    image = Image.new("RGB", (72, 48), color=(41, 94, 121))
    exif = Image.Exif()
    exif[0x010E] = "private metadata"
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


@pytest.mark.asyncio
async def test_review_image_is_reencoded_without_metadata_or_original_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_media, "_MEDIA_ROOT", tmp_path)
    review_id = uuid4()

    saved = await review_media.save_review_image(
        _upload(_jpeg_with_exif()),
        review_id=review_id,
    )

    assert saved.content_type == "image/webp"
    assert saved.public_path.startswith(f"/media/reviews/{review_id}/")
    assert "original" not in saved.storage_key
    stored = tmp_path / saved.storage_key
    assert stored.is_file()
    with Image.open(stored) as image:
        assert image.format == "WEBP"
        assert not image.getexif()


@pytest.mark.asyncio
async def test_review_image_rejects_fake_and_oversized_uploads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_media, "_MEDIA_ROOT", tmp_path)
    with pytest.raises(AppError) as fake_error:
        await review_media.save_review_image(
            _upload(b"not an image", "image/png"),
            review_id=uuid4(),
        )
    assert fake_error.value.code == "invalid_review_image"

    monkeypatch.setattr(review_media, "_MAX_BYTES", 4)
    with pytest.raises(AppError) as large_error:
        await review_media.save_review_image(
            _upload(b"12345", "image/png"),
            review_id=uuid4(),
        )
    assert large_error.value.code == "invalid_review_image"


def test_review_image_delete_cannot_escape_its_review_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_media, "_MEDIA_ROOT", tmp_path)
    review_id = uuid4()
    outside = tmp_path / "outside.webp"
    outside.write_bytes(b"keep")

    review_media.delete_review_image("../outside.webp", review_id=review_id)

    assert outside.read_bytes() == b"keep"
