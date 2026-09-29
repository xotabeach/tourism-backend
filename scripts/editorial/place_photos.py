#!/usr/bin/env python3
"""Photos for places from Wikimedia Commons, chosen by the home-lab model (spec 16, D5).

Candidates: the article's lead image, the Wikidata image and Commons
category, and files taken within 300 m. Only free licences a travel app may
show with credit (CC0, public domain, CC BY, CC BY-SA). The model looks at
the thumbnails and keeps up to three colour photos of the place itself: no
maps, plans, coats of arms, black-and-white archive shots or close-ups of
people. Author, licence and source travel with every photo.

  uv run python scripts/editorial/place_photos.py --limit 10
"""

from __future__ import annotations

import argparse
import asyncio
import html
import re
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemma  # noqa: E402
import state  # noqa: E402

COMMONS = "https://commons.wikimedia.org/w/api.php"
WIKIDATA = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "CrimeaTripEditorial/1.0 (https://xn--h1adgncbn4e.xn--p1ai)"}
MAX_CANDIDATES = 8
THUMB_FOR_MODEL = 480
THUMB_FOR_APP = 1600
NEARBY_M = 300
PROMPT_VERSION = "place-photo-v2"
_FREE = re.compile(r"^(cc0|public domain|pd|cc by(-sa)? ?[0-9.]*)", re.I)
_TAG = re.compile(r"<[^>]+>")

SYSTEM = (
    "Ты фоторедактор путеводителя по Крыму. Тебе показывают одну фотографию-"
    "кандидата для карточки места. Оцени её честно.\n"
    "shows_place: видно ли на фото само это место (а не соседний объект, "
    "не только табличку, не интерьер случайного здания).\n"
    "kind: photo — обычная фотография; map — карта или схема; emblem — герб, "
    "логотип, марка; text — табличка или документ; people — главное люди крупным "
    "планом; other — что-то иное.\n"
    "color: цветное ли фото (не чёрно-белое и не сепия).\n"
    "quality: от 1 до 5, насколько фото чёткое и красивое для обложки.\n"
    'Ответ строго JSON: {"shows_place": true|false, "kind": "...", '
    '"color": true|false, "quality": 1-5}'
)


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    for attempt in range(6):
        try:
            response = await client.get(url, params={**params, "format": "json"})
            if response.status_code in (429, 503):
                await asyncio.sleep(5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError(f"request failed: {url}")


def _plain(value: str | None) -> str:
    return html.unescape(_TAG.sub("", value or "")).strip()


async def _wikidata_media(client: httpx.AsyncClient, qid: str) -> tuple[list[str], str | None]:
    data = await _get(client, WIKIDATA, {"action": "wbgetentities", "ids": qid, "props": "claims"})
    claims = ((data.get("entities") or {}).get(qid) or {}).get("claims") or {}

    def values(prop: str) -> list[str]:
        out = []
        for claim in claims.get(prop) or []:
            value = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value")
            if isinstance(value, str):
                out.append(value)
        return out

    category = values("P373")
    return [f"File:{v}" for v in values("P18")], (category[0] if category else None)


async def _category_files(client: httpx.AsyncClient, category: str) -> list[str]:
    data = await _get(
        client,
        COMMONS,
        {
            "action": "query",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmtype": "file",
            "cmlimit": 12,
        },
    )
    return [m["title"] for m in (data.get("query") or {}).get("categorymembers") or []]


async def _nearby_files(client: httpx.AsyncClient, lat: float, lng: float) -> list[str]:
    data = await _get(
        client,
        COMMONS,
        {
            "action": "query",
            "list": "geosearch",
            "gsnamespace": 6,
            "gscoord": f"{lat}|{lng}",
            "gsradius": NEARBY_M,
            "gslimit": 12,
        },
    )
    return [g["title"] for g in (data.get("query") or {}).get("geosearch") or []]


async def _file_info(client: httpx.AsyncClient, titles: list[str]) -> list[dict[str, Any]]:
    if not titles:
        return []
    data = await _get(
        client,
        COMMONS,
        {
            "action": "query",
            "titles": "|".join(titles[:40]),
            "prop": "imageinfo",
            "iiprop": "url|mime|size|extmetadata",
            "iiurlwidth": THUMB_FOR_APP,
        },
    )
    files = []
    for page in ((data.get("query") or {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [None])[0]
        if not info or info.get("mime") not in ("image/jpeg", "image/png", "image/webp"):
            continue
        if min(info.get("width") or 0, info.get("height") or 0) < 600:
            continue
        meta = info.get("extmetadata") or {}
        licence = _plain((meta.get("LicenseShortName") or {}).get("value"))
        if not _FREE.match(licence):
            continue
        files.append(
            {
                "title": page["title"],
                "thumb": info.get("thumburl") or info.get("url"),
                "original": info.get("url"),
                "page": info.get("descriptionurl"),
                "author": _plain((meta.get("Artist") or {}).get("value"))[:255] or None,
                "license": licence[:128],
                "license_url": (meta.get("LicenseUrl") or {}).get("value"),
            }
        )
    return files


async def _thumb(client: httpx.AsyncClient, title: str) -> bytes | None:
    """A small rendering for the model; never the multi-megabyte original."""
    name = title.removeprefix("File:")
    url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{name}"
    try:
        response = await client.get(
            url, params={"width": THUMB_FOR_MODEL}, follow_redirects=True, timeout=60
        )
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    data = response.content
    is_image = data.startswith((b"\xff\xd8", b"\x89PNG")) or data[8:12] == b"WEBP"
    return data if is_image and len(data) < 1_500_000 else None


async def run(limit: int | None, concurrency: int, published_without_photos: bool) -> None:
    places = {p["id"]: p for p in state.places()}
    with state.connect() as db:
        texts = state.load(db, "place_text")
        extracts = state.load(db, "wiki_extract")
        if published_without_photos:
            # Published places that never went through this pipeline (the
            # first hand-made seed) and have no photo at all.
            wanted = [p["id"] for p in state.published() if not p.get("photos")]
        else:
            wanted = [k for k, (status, _p) in texts.items() if status in ("done", "needs_review")]
        done = state.done_keys(db, "place_photo")
        pending = [k for k in wanted if k not in done and k in places][: limit or None]
        print(f"{len(wanted)} places with text, {len(pending)} to find photos for")
        gate = asyncio.Semaphore(concurrency)
        counts: dict[str, int] = {}

        async def one(client: httpx.AsyncClient, key: str) -> None:
            place = places[key]
            extract = (extracts.get(key) or ("", {}))[1]
            titles: list[str] = []
            if extract.get("image"):
                titles.append(f"File:{extract['image']}")
            if extract.get("wikidata"):
                images, category = await _wikidata_media(client, extract["wikidata"])
                titles += images
                if category:
                    titles += await _category_files(client, category)
            titles += await _nearby_files(client, place["lat"], place["lng"])
            files = (await _file_info(client, list(dict.fromkeys(titles))))[:MAX_CANDIDATES]
            scored = []
            for file in files:
                image = await _thumb(client, file["title"])
                if image is None:
                    continue
                user = (
                    f"Место: {place['name']} ({', '.join(place.get('categories') or [])}), "
                    f"{place.get('locality')}. Файл: {file['title']}"
                )
                async with gate:
                    try:
                        verdict = await gemma.chat_json(
                            client, system=SYSTEM, user=user, images=[image], max_tokens=120
                        )
                    except ValueError:
                        continue
                scored.append({**file, "verdict": verdict})
            if not scored:
                counts["no_candidates"] = counts.get("no_candidates", 0) + 1
                state.save(db, "place_photo", key, "skipped", {"photos": [], "reason": "нет"})
                return
            good = [
                f
                for f in scored
                if f["verdict"].get("shows_place") is True
                and f["verdict"].get("color") is True
                and f["verdict"].get("kind") == "photo"
                and int(f["verdict"].get("quality") or 0) >= 3
            ]
            good.sort(key=lambda f: -int(f["verdict"].get("quality") or 0))
            chosen = good[:3]
            answer = {"reason": f"подошло {len(good)} из {len(scored)}"}
            usable = scored
            status = "done" if chosen else "skipped"
            counts[status] = counts.get(status, 0) + 1
            state.save(
                db,
                "place_photo",
                key,
                status,
                {
                    "photos": chosen,
                    "reason": answer.get("reason"),
                    "candidates": len(usable),
                    "prompt_version": PROMPT_VERSION,
                },
                model=gemma.MODEL,
            )

        async with httpx.AsyncClient(headers=HEADERS, timeout=60) as client:
            for start in range(0, len(pending), concurrency * 3):
                batch = pending[start : start + concurrency * 3]
                try:
                    await asyncio.gather(*(one(client, key) for key in batch))
                except gemma.ModelUnavailable as exc:
                    db.commit()
                    print(f"model unavailable, stopping; rerun to resume ({exc})")
                    return
                db.commit()
                print(
                    f"  {min(start + len(batch), len(pending))}/{len(pending)} {counts}", flush=True
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument(
        "--published-without-photos",
        action="store_true",
        help="published places (published.jsonl) that have no photo, instead of new texts",
    )
    args = parser.parse_args()
    asyncio.run(run(args.limit, args.concurrency, args.published_without_photos))


if __name__ == "__main__":
    main()
