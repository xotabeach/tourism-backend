#!/usr/bin/env python3
"""Link places to Wikipedia articles by hand (spec 19, D42).

The hand-made places of the first seed (Воронцовский дворец, Ай-Петри,
Херсонес…) carry no OSM tags, so wiki_match cannot find their articles. The
editor names the article; the script checks that the article's coordinates
are near the place, stores the link as method "curated" (fully trusted, like
an OSM link) and fetches the article intro for the photo step.

  uv run python scripts/editorial/link_places.py links.json

links.json maps a place id to a ru.wikipedia title.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402
import wiki_match  # noqa: E402

MAX_DISTANCE_KM = 5.0


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat, dlng = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(a))


async def run(links: dict[str, str]) -> None:
    places = {p["id"]: p for p in state.places()}
    async with httpx.AsyncClient(headers=wiki_match.HEADERS, timeout=30) as client:
        data = await wiki_match._get(
            client,
            wiki_match.API,
            {
                "action": "query",
                "prop": "coordinates",
                "redirects": 1,
                "titles": "|".join(links.values()),
            },
        )
    query = data.get("query") or {}
    alias = {r["from"]: r["to"] for r in query.get("redirects") or []}
    alias.update({n["from"]: n["to"] for n in query.get("normalized") or []})
    pages = {page.get("title"): page for page in (query.get("pages") or {}).values()}
    with state.connect() as db:
        for key, title in links.items():
            place = places.get(key)
            target = alias.get(alias.get(title, title), alias.get(title, title))
            page = pages.get(target) or {}
            if place is None or "missing" in page:
                print(f"skip {key} {title}: {'no place' if place is None else 'no article'}")
                continue
            coords = (page.get("coordinates") or [None])[0]
            distance = (
                _km(place["lat"], place["lng"], coords["lat"], coords["lon"]) if coords else None
            )
            if distance is not None and distance > MAX_DISTANCE_KM:
                print(f"skip {place['name']} → {target}: {distance:.1f} km away")
                continue
            state.save(
                db,
                "wiki_match",
                key,
                "done",
                {"title": target, "method": "curated", "distance_km": distance},
            )
            # A new link replaces whatever extract an older link produced.
            db.execute("DELETE FROM results WHERE step = 'wiki_extract' AND key = ?", (key,))
            shown = "no coordinates" if distance is None else f"{distance:.1f} km"
            print(f"linked {place['name']} → {target} ({shown})")
    await wiki_match.extracts(concurrency=2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("links", type=Path)
    args = parser.parse_args()
    asyncio.run(run(json.loads(args.links.read_text())))


if __name__ == "__main__":
    main()
