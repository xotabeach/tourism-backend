import io
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import UploadFile
from PIL import Image
from pydantic import ValidationError
from starlette.datastructures import Headers

from tourism_backend.api.errors import AppError
from tourism_backend.modules.routes.application import media as route_media
from tourism_backend.modules.routes.application.schemas import UserRouteDraftIn


def _image_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 40), color=(25, 80, 45)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_route_draft_rejects_duplicate_places_and_filters() -> None:
    place_id = uuid4()
    with pytest.raises(ValidationError):
        UserRouteDraftIn(
            name="Маршрут",
            place_ids=[place_id, place_id],
            filters=["Леса", "Леса"],
        )
    with pytest.raises(ValidationError):
        UserRouteDraftIn(
            name="   ",
            place_ids=[uuid4(), uuid4()],
        )


@pytest.mark.asyncio
async def test_route_image_is_validated_reencoded_and_randomized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(route_media, "_MEDIA_ROOT", tmp_path)
    route_id = uuid4()
    upload = UploadFile(
        io.BytesIO(_image_bytes()),
        filename="../../unsafe.png",
        headers=Headers({"content-type": "image/png"}),
    )
    saved = await route_media.save_route_media(upload, route_id=route_id)

    assert saved.kind == "image"
    assert saved.content_type == "image/webp"
    assert saved.public_path.startswith(f"/media/routes/{route_id}/")
    assert "unsafe" not in saved.storage_key
    assert (tmp_path / saved.storage_key).is_file()


@pytest.mark.asyncio
async def test_route_media_rejects_unrecognized_binary(tmp_path: Path) -> None:
    route_media._MEDIA_ROOT = tmp_path
    upload = UploadFile(
        io.BytesIO(b"not-media"),
        filename="payload.bin",
        headers=Headers({"content-type": "application/octet-stream"}),
    )
    with pytest.raises(AppError) as error:
        await route_media.save_route_media(upload, route_id=uuid4())
    assert error.value.code == "invalid_route_media"
