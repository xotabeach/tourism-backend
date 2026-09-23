"""Static map images on our own OSM tiles (spec 12a, section 3).

tileserver-gl renders only the basemap for an exact center and zoom; the
route line, pins and the «© участники OpenStreetMap» credit are drawn here.
That keeps the projection ours: the app places its tappable overlays with the
same 256 px Web Mercator math (MapProjection), so both must agree.

Zoom in the public contract is 256 px based (as with 2GIS, D21). The
tileserver-gl static API already speaks the same zoom: checked on Livadia and
Swallow's Nest, pins land on the places with no shift.
"""

from __future__ import annotations

import io
import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

TILE_SIZE = 256
MIN_ZOOM = 1
MAX_ZOOM = 18
# Breathing room around fitted points, as in the app's MapProjection.fit.
FIT_PADDING = 56
# OpenMapTiles schema (CC-BY) and OSM data (ODbL) both require a visible credit.
ATTRIBUTION = "© OpenMapTiles © участники OpenStreetMap"
_STYLE = "crimeatrip"
_LINE_COLOR = (22, 163, 74, 255)  # #16A34A, the route green of the old maps
_LINE_WIDTH = 5
# Walking is dashed and every vehicle solid (spec 14, D23); colours per
# mode are provisional until the designer's mockups (D15).
_MODE_COLORS: dict[str, tuple[int, int, int, int]] = {
    "walk": _LINE_COLOR,
    "car": (37, 99, 235, 255),  # #2563EB
}
_TRANSIT_COLOR = (245, 158, 11, 255)  # #F59E0B: bus, train, cable car, ferry
_DASH = 10  # in the image's points, like _LINE_WIDTH
_GAP = 7
_PIN_COLOR = (22, 163, 74, 255)
_PLACE_PIN_COLOR = (229, 57, 53, 255)
# Overlays are drawn this many times larger and scaled down: Pillow lines
# have no anti-aliasing of their own.
_SUPERSAMPLE = 3
_FONT = Path(__file__).resolve().parents[1] / "assets" / "Rubik-Medium.ttf"

Point = tuple[float, float]  # (lng, lat)


class StaticMapError(Exception):
    """The basemap could not be rendered."""


@dataclass(frozen=True, slots=True)
class MapFrame:
    center_lat: float
    center_lng: float
    zoom: int
    width: int
    height: int
    scale: int


def world_xy(lng: float, lat: float, zoom: int) -> tuple[float, float]:
    world = TILE_SIZE * 2**zoom
    lat = max(min(lat, 85.05112878), -85.05112878)
    rad = math.radians(lat)
    x = (lng + 180.0) / 360.0 * world
    y = (1 - math.log(math.tan(rad) + 1 / math.cos(rad)) / math.pi) / 2 * world
    return x, y


def fit_frame(points: Sequence[Point], *, width: int, height: int, scale: int) -> MapFrame:
    """Largest zoom that shows every point; same rule as the app's fit."""
    lngs = [p[0] for p in points]
    lats = [p[1] for p in points]
    center_lat = (min(lats) + max(lats)) / 2
    center_lng = (min(lngs) + max(lngs)) / 2
    usable_w = max(width - FIT_PADDING * 2, 1)
    usable_h = max(height - FIT_PADDING * 2, 1)
    best = MIN_ZOOM
    for zoom in range(MAX_ZOOM, MIN_ZOOM - 1, -1):
        x0, y0 = world_xy(min(lngs), max(lats), zoom)
        x1, y1 = world_xy(max(lngs), min(lats), zoom)
        if abs(x1 - x0) <= usable_w and abs(y1 - y0) <= usable_h:
            best = zoom
            break
    return MapFrame(center_lat, center_lng, best, width, height, scale)


def to_pixel(frame: MapFrame, lng: float, lat: float) -> tuple[float, float]:
    """Logical-pixel position of a point inside the frame."""
    cx, cy = world_xy(frame.center_lng, frame.center_lat, frame.zoom)
    x, y = world_xy(lng, lat, frame.zoom)
    return x - cx + frame.width / 2, y - cy + frame.height / 2


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(_FONT), size)


async def fetch_basemap(
    frame: MapFrame,
    *,
    base_url: str,
    timeout_seconds: float,
    client: httpx.AsyncClient | None = None,
) -> Image.Image:
    url = (
        f"{base_url.rstrip('/')}/styles/{_STYLE}/static/"
        f"{frame.center_lng:.6f},{frame.center_lat:.6f},{frame.zoom}/"
        f"{frame.width}x{frame.height}@{frame.scale}x.png"
    )
    try:
        if client is not None:
            response = await client.get(url, timeout=timeout_seconds)
        else:
            async with httpx.AsyncClient(timeout=timeout_seconds) as own:
                response = await own.get(url)
    except httpx.HTTPError as exc:
        raise StaticMapError("tileserver is unavailable") from exc
    if response.status_code != 200 or not response.content:
        raise StaticMapError(f"tileserver answered {response.status_code}")
    try:
        image = Image.open(io.BytesIO(response.content))
        image.load()
    except OSError as exc:
        raise StaticMapError("tileserver returned an unreadable image") from exc
    return image.convert("RGBA")


def draw_overlays(
    base: Image.Image,
    frame: MapFrame,
    *,
    line: Sequence[Point] = (),
    numbered_pins: Sequence[Point] = (),
    place_pin: Point | None = None,
    line_mode: str = "walk",
    pieces: Sequence[tuple[str, Sequence[Point]]] = (),
) -> bytes:
    """Route line, pins and the OSM credit on top of the basemap, as PNG.

    ``line_mode`` is the way the line is travelled: ``walk`` draws it dashed.
    ``pieces``, when given, replace ``line``: each (mode, points) part of a
    route is drawn in its own style, drives first so walks stay on top.
    """
    k = frame.scale * _SUPERSAMPLE
    size = (base.width * _SUPERSAMPLE, base.height * _SUPERSAMPLE)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    def px(point: Point) -> tuple[float, float]:
        x, y = to_pixel(frame, point[0], point[1])
        return x * k, y * k

    parts = list(pieces) or [(line_mode, line)]
    width = round(_LINE_WIDTH * k)
    for mode, points in sorted(parts, key=lambda part: part[0] == "walk"):
        if len(points) < 2:
            continue
        coords = [px(p) for p in points]
        color = _MODE_COLORS.get(mode, _TRANSIT_COLOR)
        if mode == "walk":
            _draw_dashed(draw, coords, color=color, width=width, dash=_DASH * k, gap=_GAP * k)
        else:
            draw.line(coords, fill=color, width=width, joint="curve")
            _round_cap(draw, coords[0], color, width)
            _round_cap(draw, coords[-1], color, width)
    number_font = _font(round(11 * k))
    for index, point in enumerate(numbered_pins[:8], start=1):
        x, y = px(point)
        r = 11 * k
        draw.ellipse(
            (x - r, y - r, x + r, y + r), fill=_PIN_COLOR, outline="white", width=round(2 * k)
        )
        draw.text((x, y), str(index), fill="white", font=number_font, anchor="mm")
    if place_pin is not None:
        x, y = px(place_pin)
        r = 9 * k
        draw.ellipse(
            (x - r, y - r, x + r, y + r), fill=_PLACE_PIN_COLOR, outline="white", width=round(3 * k)
        )
    overlay = layer.resize(base.size, Image.Resampling.LANCZOS)
    image = Image.alpha_composite(base, overlay)
    _draw_attribution(image, frame.scale)
    out = io.BytesIO()
    image.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _round_cap(
    draw: ImageDraw.ImageDraw,
    point: tuple[float, float],
    color: tuple[int, int, int, int],
    width: int,
) -> None:
    x, y = point
    r = width / 2
    draw.ellipse((x - r, y - r, x + r, y + r), fill=color)


def _draw_dashed(
    draw: ImageDraw.ImageDraw,
    coords: Sequence[tuple[float, float]],
    *,
    color: tuple[int, int, int, int],
    width: int,
    dash: float,
    gap: float,
) -> None:
    """A polyline cut into round-capped dashes that run on across its vertices."""
    period = dash + gap
    walked = 0.0
    current: list[tuple[float, float]] = []

    def flush() -> None:
        if len(current) >= 2:
            draw.line(current, fill=color, width=width, joint="curve")
            _round_cap(draw, current[0], color, width)
            _round_cap(draw, current[-1], color, width)
        current.clear()

    for (x0, y0), (x1, y1) in zip(coords, coords[1:], strict=False):
        length = math.hypot(x1 - x0, y1 - y0)
        if length == 0:
            continue
        start = walked
        end = walked + length
        position = start
        while position < end:
            phase = position % period
            in_dash = phase < dash
            boundary = position + ((dash - phase) if in_dash else (period - phase))
            stop = min(boundary, end)
            if in_dash:
                t0 = (position - start) / length
                t1 = (stop - start) / length
                p0 = (x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0)
                p1 = (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)
                if not current:
                    current.append(p0)
                current.append(p1)
                if stop == boundary:
                    flush()
            position = stop
        walked = end
    flush()


def _draw_attribution(image: Image.Image, scale: int) -> None:
    """ODbL credit in the bottom right corner (D16)."""
    font = _font(9 * scale)
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = draw.textbbox((0, 0), ATTRIBUTION, font=font)
    pad = 3 * scale
    w, h = round(right - left) + pad * 2, round(bottom - top) + pad * 2
    x0, y0 = image.width - w, image.height - h
    box = Image.new("RGBA", (w, h), (255, 255, 255, 190))
    image.alpha_composite(box, (x0, y0))
    draw.text((x0 + pad - left, y0 + pad - top), ATTRIBUTION, fill=(60, 60, 67, 255), font=font)
