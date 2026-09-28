#!/usr/bin/env python3
"""External popularity of published places, 0–100 (spec 19, D41, D42).

Signals, each over all published places of Crimea and
scaled by log1p against the most famous one:
  - ru.wikipedia pageviews over the last 12 full months (Wikimedia REST);
  - number of language editions of the article (Wikidata sitelinks);
  - files in the place's Commons category (Wikidata P373);
  - OSM tags: tourism=attraction, heritage=*, historic=*.

Trust in the Wikipedia link (D42): links from OSM tags and links named by
hand count fully; links found by name and distance count half, and such a
place landing in the top 10% is listed for the owner to check. Articles
about settlements and districts, and articles whose Wikidata item has no
coordinates (a band, a tank model: what monuments and exhibits link to),
never count.

Reads published.jsonl, writes the "popularity" step and popularity.jsonl.

  uv run python scripts/editorial/popularity.py
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402
import wiki_match  # noqa: E402

WIKIDATA = "https://www.wikidata.org/w/api.php"
COMMONS = "https://commons.wikimedia.org/w/api.php"
PAGEVIEWS = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "ru.wikipedia/all-access/user/{title}/monthly/{start}/{end}"
)
FORMULA_VERSION = 1
WEIGHTS = {"pageviews": 0.45, "sitelinks": 0.2, "commons": 0.2, "osm": 0.15}
NAME_MATCH_TRUST = 0.5
MIN_TITLE_SCORE = 0.5
# Wikidata classes of settlements and districts: such an article describes
# the town around the place, not the place (D42).
SETTLEMENT_CLASSES = {
    "Q486972",  # human settlement
    "Q515",  # city
    "Q3957",  # town
    "Q532",  # village
    "Q1549591",  # big city
    "Q7930989",  # city or town
    "Q2514025",  # posyolok
    "Q15078955",  # resort town
    "Q4286337",  # city district
    "Q2983893",  # quarter
    "Q1637706",  # city with over a million inhabitants
}


def _months() -> tuple[str, str]:
    today = date.today()
    end = date(today.year, today.month, 1)
    start = date(end.year - 1, end.month, 1)
    return start.strftime("%Y%m%d00"), end.strftime("%Y%m%d00")


def _links(place: dict[str, Any], match: tuple[str, dict[str, Any]] | None) -> dict[str, Any]:
    """Which article or Wikidata item stands for the place, and how far to trust it."""
    tags = place.get("tags") or {}
    wikipedia = tags.get("wikipedia") or ""
    if wikipedia.startswith("ru:"):
        return {"title": wikipedia[3:], "qid": tags.get("wikidata"), "trust": 1.0, "via": "osm"}
    if tags.get("wikidata"):
        return {"title": None, "qid": tags["wikidata"], "trust": 1.0, "via": "osm"}
    if match and match[0] == "done":
        method = match[1].get("method")
        trust = 1.0 if method in ("curated", "osm_wikipedia", "osm_wikidata") else NAME_MATCH_TRUST
        return {"title": match[1]["title"], "qid": None, "trust": trust, "via": method}
    if place.get("old_wiki"):
        return {"title": place["old_wiki"], "qid": None, "trust": NAME_MATCH_TRUST, "via": "old"}
    return {"title": None, "qid": None, "trust": 0.0, "via": None}


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    return await wiki_match._get(client, url, params)


async def _qids(client: httpx.AsyncClient, titles: list[str]) -> dict[str, str]:
    """ru.wikipedia title (after redirects) → Wikidata id."""
    out: dict[str, str] = {}
    for start in range(0, len(titles), 50):
        chunk = titles[start : start + 50]
        data = await _get(
            client,
            wiki_match.API,
            {
                "action": "query",
                "prop": "pageprops",
                "ppprop": "wikibase_item",
                "redirects": 1,
                "titles": "|".join(chunk),
            },
        )
        query = data.get("query") or {}
        alias = {r["from"]: r["to"] for r in query.get("redirects") or []}
        alias.update({n["from"]: n["to"] for n in query.get("normalized") or []})
        by_title = {
            page.get("title"): (page.get("pageprops") or {}).get("wikibase_item")
            for page in (query.get("pages") or {}).values()
        }
        for title in chunk:
            target = alias.get(alias.get(title, title), alias.get(title, title))
            if by_title.get(target):
                out[title] = by_title[target]
    return out


async def _entities(client: httpx.AsyncClient, qids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(qids), 50):
        data = await _get(
            client,
            WIKIDATA,
            {
                "action": "wbgetentities",
                "ids": "|".join(qids[start : start + 50]),
                "props": "sitelinks|claims",
            },
        )
        for qid, entity in (data.get("entities") or {}).items():
            claims = entity.get("claims") or {}

            def values(prop: str, claims: dict[str, Any] = claims) -> list[Any]:
                return [
                    ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
                    for c in claims.get(prop) or []
                ]

            classes = {v.get("id") for v in values("P31") if isinstance(v, dict)}
            category = next((v for v in values("P373") if isinstance(v, str)), None)
            out[qid] = {
                "sitelinks": len(entity.get("sitelinks") or {}),
                "settlement": bool(classes & SETTLEMENT_CLASSES),
                # A place has coordinates; a band, a tank model or an aircraft
                # type (what exhibits and monuments often link to) has none.
                "located": bool(claims.get("P625")),
                "ru_title": ((entity.get("sitelinks") or {}).get("ruwiki") or {}).get("title"),
                "category": category,
            }
    return out


async def _commons_counts(client: httpx.AsyncClient, categories: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for start in range(0, len(categories), 50):
        chunk = categories[start : start + 50]
        data = await _get(
            client,
            COMMONS,
            {
                "action": "query",
                "prop": "categoryinfo",
                "titles": "|".join(f"Category:{c}" for c in chunk),
            },
        )
        query = data.get("query") or {}
        alias = {n["from"]: n["to"] for n in query.get("normalized") or []}
        info = {
            page.get("title"): (page.get("categoryinfo") or {}).get("files", 0)
            for page in (query.get("pages") or {}).values()
        }
        for category in chunk:
            title = f"Category:{category}"
            out[category] = int(info.get(alias.get(title, title)) or 0)
    return out


async def _pageviews(client: httpx.AsyncClient, titles: list[str]) -> dict[str, int]:
    start, end = _months()
    gate = asyncio.Semaphore(5)
    out: dict[str, int] = {}

    async def one(title: str) -> None:
        url = PAGEVIEWS.format(title=quote(title.replace(" ", "_"), safe=""), start=start, end=end)
        async with gate:
            for attempt in range(5):
                try:
                    response = await client.get(url)
                except httpx.HTTPError:
                    await asyncio.sleep(3 * (attempt + 1))
                    continue
                if response.status_code == 429:
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                if response.status_code == 404:
                    out[title] = 0
                    return
                response.raise_for_status()
                out[title] = sum(i.get("views", 0) for i in response.json().get("items") or [])
                return
        out[title] = 0

    await asyncio.gather(*(one(t) for t in titles))
    return out


def _osm_score(tags: dict[str, Any]) -> float:
    score = 0.0
    if tags.get("tourism") == "attraction":
        score += 0.5
    if tags.get("heritage"):
        score += 0.3
    if tags.get("historic"):
        score += 0.2
    return score


def _scaled(values: dict[str, float]) -> dict[str, float]:
    """log1p(value) / log1p(max): keeps how much more famous a place is.

    Percentiles were tried first and flattened the top: Херсонес (142k views)
    and Царский курган (6.6k) came out equal.
    """
    top = math.log1p(max(values.values(), default=0))
    return {k: (math.log1p(v) / top if top else 0.0) for k, v in values.items()}


async def run() -> None:
    places = state.published()
    with state.connect() as db:
        matches = state.load(db, "wiki_match")
    links = {p["id"]: _links(p, matches.get(p["id"])) for p in places}
    async with httpx.AsyncClient(headers=wiki_match.HEADERS, timeout=30) as client:
        titles = sorted({v["title"] for v in links.values() if v["title"]})
        title_qids = await _qids(client, titles)
        for link in links.values():
            if not link["qid"] and link["title"]:
                link["qid"] = title_qids.get(link["title"])
        entities = await _entities(client, sorted({v["qid"] for v in links.values() if v["qid"]}))
        for link in links.values():
            entity = entities.get(link["qid"] or "") or {}
            if not link["title"] and entity.get("ru_title"):
                link["title"] = entity["ru_title"]
        categories = sorted({e["category"] for e in entities.values() if e.get("category")})
        commons = await _commons_counts(client, categories)
        views = await _pageviews(client, sorted({v["title"] for v in links.values() if v["title"]}))

    raw: dict[str, dict[str, float]] = {name: {} for name in WEIGHTS}
    detail: dict[str, dict[str, Any]] = {}
    for place in places:
        key = place["id"]
        link = links[key]
        entity = entities.get(link["qid"] or "") or {}
        excluded = bool(entity.get("settlement")) or (bool(entity) and not entity.get("located"))
        trust = 0.0 if excluded else link["trust"]
        # A monument's OSM link often names its subject («Памятник рок-группе
        # "Кино"» → the band's article, 360k views). A title unlike the
        # place's name is trusted only as far as a name match.
        if (
            trust > NAME_MATCH_TRUST
            and link["via"] != "curated"
            and link["title"]
            and wiki_match.name_score(place["name"], link["title"]) < MIN_TITLE_SCORE
        ):
            trust = NAME_MATCH_TRUST
        pv = views.get(link["title"] or "", 0) if trust else 0
        sl = entity.get("sitelinks", 0) if trust else 0
        cm = commons.get(entity.get("category") or "", 0) if trust else 0
        raw["pageviews"][key] = pv
        raw["sitelinks"][key] = sl
        raw["commons"][key] = cm
        raw["osm"][key] = _osm_score(place.get("tags") or {})
        detail[key] = {
            "title": link["title"],
            "qid": link["qid"],
            "via": link["via"],
            "trust": trust,
            "article_not_about_place": excluded,
            "pageviews": pv,
            "sitelinks": sl,
            "commons_files": cm,
        }
    pct = {name: _scaled(values) for name, values in raw.items() if name != "osm"}
    scores: dict[str, int] = {}
    for place in places:
        key = place["id"]
        trust = detail[key]["trust"]
        wiki = sum(WEIGHTS[n] * pct[n][key] for n in ("pageviews", "sitelinks", "commons"))
        scores[key] = round(100 * (trust * wiki + WEIGHTS["osm"] * raw["osm"][key]))
    ranked = sorted(places, key=lambda p: -scores[p["id"]])
    top10 = {p["id"] for p in ranked[: max(1, len(ranked) // 10)]}
    now = datetime.now(UTC).isoformat()
    with state.connect() as db, (state.WORK_DIR / "popularity.jsonl").open("w") as out:
        for place in ranked:
            key = place["id"]
            review = detail[key]["trust"] == NAME_MATCH_TRUST and key in top10
            payload = {
                **detail[key],
                "score": scores[key],
                "formula_version": FORMULA_VERSION,
                "computed_at": now,
                "owner_review": review,
            }
            state.save(db, "popularity", key, "needs_review" if review else "done", payload)
            out.write(
                json.dumps({"id": key, "name": place["name"], **payload}, ensure_ascii=False) + "\n"
            )
    print(f"{len(places)} places scored; top 30:")
    for place in ranked[:30]:
        d = detail[place["id"]]
        flag = (
            " [проверить привязку]"
            if place["id"] in top10 and d["trust"] == NAME_MATCH_TRUST
            else ""
        )
        print(
            f"  {scores[place['id']]:3} {place['name']} ({place.get('locality')}) "
            f"views={d['pageviews']} langs={d['sitelinks']} "
            f"commons={d['commons_files']} via={d['via']}{flag}"
        )


if __name__ == "__main__":
    asyncio.run(run())
