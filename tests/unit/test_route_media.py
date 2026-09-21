import asyncio
import io
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import UploadFile
from PIL import Image
from starlette.datastructures import Headers

from tourism_backend.modules.routes.application import media as route_media


def _upload(payload: bytes) -> UploadFile:
    return UploadFile(
        io.BytesIO(payload),
        filename="photo.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )


def _jpeg(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), color=(41, 94, 121)).save(output, format="JPEG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_route_photo_is_stored_as_webp_within_the_edge_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(route_media, "_MEDIA_ROOT", tmp_path)
    route_id = uuid4()

    saved = await route_media.save_route_media(_upload(_jpeg(4000, 3000)), route_id=route_id)

    assert saved.content_type == "image/webp"
    assert (saved.width, saved.height) == (2560, 1920)
    with Image.open(tmp_path / saved.storage_key) as image:
        assert image.format == "WEBP"


@pytest.mark.asyncio
async def test_route_photo_is_processed_off_the_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(route_media, "_MEDIA_ROOT", tmp_path)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    original = route_media._prepare_image

    def spy(raw: bytes) -> tuple[bytes, int, int]:
        seen.append(threading.get_ident())
        return original(raw)

    monkeypatch.setattr(route_media, "_prepare_image", spy)

    await asyncio.wait_for(
        route_media.save_route_media(_upload(_jpeg(64, 48)), route_id=uuid4()),
        timeout=10,
    )

    assert seen
    assert seen[0] != loop_thread
