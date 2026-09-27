#!/usr/bin/env python3
"""Find the Russian Wikipedia article of each place (spec 16, section 2).

Order of trust: the OSM ``wikipedia`` tag, then ``wikidata`` resolved to
its ruwiki sitelink, then an article within 500 m whose title matches the
place name closely enough. A title that only names the town around the place
is not a match. Then the intro of every matched article is fetched.

  uv run python scripts/editorial/wiki_match.py            # all pending places
  uv run python scripts/editorial/wiki_match.py --limit 50 # a trial run
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402

API = "https://ru.wikipedia.org/w/api.php"
WIKIDATA = "https://www.wikidata.org/w/api.php"
HEADERS = {"User-Agent": "CrimeaTripEditorial/1.0 (https://xn--h1adgncbn4e.xn--p1ai)"}
RADIUS_M = 500
MIN_SCORE = 0.62
_WORD = re.compile(r"[\w-]+", re.UNICODE)
_NOISE = {"им", "имени", "св", "святого", "святой", "памятник", "храм", "церковь", "г"}


def _norm(text: str) -> str:
    words = [w for w in _WORD.findall(text.lower().replace("ё", "е")) if w not in _NOISE]
    return " ".join(words)


def name_score(name: str, title: str) -> float:
    a, b = _norm(name), _norm(re.sub(r"\s*\(.*?\)\s*", " ", title))
    if not a or not b:
        return 0.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return max(ratio, overlap * 0.9)


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
    raise RuntimeError(f"request failed: {url} {params}")


async def _wikidata_titles(client: httpx.AsyncClient, ids: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for start in range(0, len(ids), 50):
        data = await _get(
            client,
            WIKIDATA,
            {
                "action": "wbgetentities",
                "ids": "|".join(ids[start : start + 50]),
                "props": "sitelinks",
                "sitefilter": "ruwiki",
            },
        )
        for qid, entity in (data.get("entities") or {}).items():
            link = (entity.get("sitelinks") or {}).get("ruwiki")
            if link:
                found[qid] = link["title"]
    return found


async def _geo_match(
    client: httpx.AsyncClient, place: dict[str, Any], locality: str | None
) -> tuple[str | None, float]:
    data = await _get(
        client,
        API,
        {
            "action": "query",
            "list": "geosearch",
            "gscoord": f"{place['lat']}|{place['lng']}",
            "gsradius": RADIUS_M,
            "gslimit": 15,
        },
    )
    best: tuple[str | None, float] = (None, 0.0)
    for item in (data.get("query") or {}).get("geosearch") or []:
        title = item["title"]
        if locality and _norm(title) == _norm(locality):
            continue
        score = name_score(place["name"], title)
        if score > best[1]:
            best = (title, score)
    return best


async def match(limit: int | None, concurrency: int) -> None:
    rows = [
        p
        for p in state.places()
        if p["status"] != "published" and p.get("locality") and p.get("categories")
    ]
    with state.connect() as db:
        done = state.done_keys(db, "wiki_match")
        pending = [p for p in rows if p["id"] not in done][: limit or None]
        print(f"{len(rows)} candidates, {len(done)} matched before, {len(pending)} to go")
        async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
            qids = sorted(
                {
                    (p.get("tags") or {}).get("wikidata")
                    for p in pending
                    if (p.get("tags") or {}).get("wikidata")
                }
                - {None}
            )
            by_qid = await _wikidata_titles(client, qids) if qids else {}
            gate = asyncio.Semaphore(concurrency)

            async def one(place: dict[str, Any]) -> None:
                tags = place.get("tags") or {}
                wiki = tags.get("wikipedia") or ""
                title, method, score = None, None, 0.0
                if wiki.startswith("ru:"):
                    title, method, score = wiki[3:], "osm_wikipedia", 1.0
                elif tags.get("wikidata") in by_qid:
                    title, method, score = by_qid[tags["wikidata"]], "osm_wikidata", 1.0
                else:
                    async with gate:
                        try:
                            title, score = await _geo_match(client, place, place.get("locality"))
                        except RuntimeError as exc:
                            # Retried on the next run (failed rows are not «done»).
                            state.save(db, "wiki_match", place["id"], "failed", {"error": str(exc)})
                            return
                    method = "geosearch"
                    if score < MIN_SCORE:
                        title = None
                status = "done" if title else "skipped"
                state.save(
                    db,
                    "wiki_match",
                    place["id"],
                    status,
                    {"title": title, "method": method, "score": round(score, 3)},
                )

            for start in range(0, len(pending), 100):
                await asyncio.gather(*(one(p) for p in pending[start : start + 100]))
                db.commit()
                print(f"  {min(start + 100, len(pending))}/{len(pending)}", flush=True)


async def extracts(concurrency: int) -> None:
    with state.connect() as db:
        matched = {
            key: payload["title"]
            for key, (status, payload) in state.load(db, "wiki_match").items()
            if status == "done"
        }
        have = state.done_keys(db, "wiki_extract")
        titles = sorted({t for k, t in matched.items() if k not in have})
        print(f"{len(titles)} articles to fetch")
        pages: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
            for start in range(0, len(titles), 20):
                data = await _get(
                    client,
                    API,
                    {
                        "action": "query",
                        "prop": "extracts|pageprops|pageimages|info",
                        "exintro": 1,
                        "explaintext": 1,
                        "exlimit": 20,
                        "ppprop": "wikibase_item|disambiguation",
                        "piprop": "name",
                        "inprop": "url",
                        "redirects": 1,
                        "titles": "|".join(titles[start : start + 20]),
                    },
                )
                query = data.get("query") or {}
                alias = {r["from"]: r["to"] for r in query.get("redirects") or []}
                alias.update({n["from"]: n["to"] for n in query.get("normalized") or []})
                for page in (query.get("pages") or {}).values():
                    pages[page.get("title", "")] = page
                for title in titles[start : start + 20]:
                    target = alias.get(alias.get(title, title), alias.get(title, title))
                    page = pages.get(target)
                    for key, wanted in matched.items():
                        if wanted != title or key in have:
                            continue
                        text = (page or {}).get("extract") or ""
                        props = (page or {}).get("pageprops") or {}
                        ok = bool(text.strip()) and "disambiguation" not in props
                        state.save(
                            db,
                            "wiki_extract",
                            key,
                            "done" if ok else "skipped",
                            {
                                "title": target,
                                "url": (page or {}).get("fullurl"),
                                "text": text,
                                "wikidata": props.get("wikibase_item"),
                                "image": (page or {}).get("pageimage"),
                            },
                        )
                db.commit()
                print(f"  {min(start + 20, len(titles))}/{len(titles)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--skip-match", action="store_true")
    args = parser.parse_args()
    if not args.skip_match:
        asyncio.run(match(args.limit, args.concurrency))
    asyncio.run(extracts(args.concurrency))


if __name__ == "__main__":
    main()
