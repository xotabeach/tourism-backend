"""Our OSM static map renderer: projection, overlays and credit (spec 12a)."""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from tourism_backend.modules.maps.infrastructure.osm_static import (
    FIT_PADDING,
    MapFrame,
    StaticMapError,
    draw_overlays,
    fetch_basemap,
    fit_frame,
    to_pixel,
)

# Livadia Palace -> Swallow's Nest, the leg from the FRONTEND-40 bug.
_LIVADIA = (34.1436, 44.4678)
_SWALLOW = (34.1235, 44.4307)
_MIDDLE = (34.1335, 44.4492)


def _blank(frame: MapFrame) -> Image.Image:
    return Image.new("RGBA", (frame.width * frame.scale, frame.height * frame.scale), "white")


def test_fit_frame_matches_the_app_rule():
    """Same center (mid of the bbox) and zoom as the app's MapProjection.fit."""
    frame = fit_frame([_LIVADIA, _SWALLOW], width=377, height=600, scale=2)
    assert frame.zoom == 13
    assert frame.center_lng == pytest.approx(34.13355)
    assert frame.center_lat == pytest.approx(44.44925)
    for point in (_LIVADIA, _SWALLOW):
        x, y = to_pixel(frame, *point)
        assert FIT_PADDING <= x <= 377 - FIT_PADDING
        assert FIT_PADDING <= y <= 600 - FIT_PADDING


def test_pin_lands_on_the_route_line():
    """Golden check (D21): where the app puts a pin, our line is drawn."""
    frame = fit_frame([_LIVADIA, _SWALLOW], width=377, height=600, scale=2)
    png = draw_overlays(_blank(frame), frame, line=[_LIVADIA, _MIDDLE, _SWALLOW])
    image = Image.open(io.BytesIO(png)).convert("RGB")
    for point in (_LIVADIA, _MIDDLE, _SWALLOW):
        x, y = to_pixel(frame, *point)
        r, g, b = image.getpixel((round(x * 2), round(y * 2)))
        assert g > 140 > r
        assert b < 110


def test_numbered_pins_and_credit():
    frame = MapFrame(44.44925, 34.13355, 13, 377, 600, 2)
    png = draw_overlays(_blank(frame), frame, numbered_pins=[_LIVADIA, _SWALLOW])
    image = Image.open(io.BytesIO(png)).convert("RGB")
    assert image.size == (754, 1200)
    x, y = to_pixel(frame, *_LIVADIA)
    # The pin rim is white, its body green around the number.
    r, g, b = image.getpixel((round(x * 2 - 14), round(y * 2)))
    assert g > r
    # The credit sits in the bottom right corner on a light plate with text.
    corner = image.crop((754 - 200, 1200 - 30, 754, 1200))
    assert corner.getextrema()[0][0] < 120


@pytest.mark.asyncio
async def test_basemap_request_uses_the_same_zoom():
    seen: list[str] = []
    buffer = io.BytesIO()
    Image.new("RGB", (754, 1200), "white").save(buffer, format="PNG")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=buffer.getvalue())

    frame = MapFrame(44.44925, 34.13355, 13, 377, 600, 2)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    image = await fetch_basemap(
        frame, base_url="http://tiles:8080/", timeout_seconds=5, client=client
    )
    assert image.size == (754, 1200)
    assert seen == [
        "http://tiles:8080/styles/crimeatrip/static/34.133550,44.449250,13/377x600@2x.png"
    ]


@pytest.mark.asyncio
async def test_basemap_failures_raise():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    with pytest.raises(StaticMapError):
        await fetch_basemap(
            MapFrame(44.4, 34.1, 13, 100, 100, 1),
            base_url="http://tiles:8080",
            timeout_seconds=5,
            client=client,
        )
