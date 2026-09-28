#!/usr/bin/env python3
"""Route ideas from the home-lab model, region by region (spec 16a, D26, D30, D38).

Every published place of a region goes to the model as one numbered list
(name, where it lies from the town centre, categories, popularity, photo,
one line of text); copies of one sight are shown once. After reasoning, the
model answers 10 ideas per round: a title, a one-line story, the stops by
number, the wow point, the find, and the axes it aims at. Each round is told the ideas
already proposed, so it does not repeat them, and is saved on its own, so a
dropped tunnel loses one round, not the region. An idea naming a number
outside the list, or a wow point without a photo, is dropped.

  uv run python scripts/editorial/route_ideas.py --rounds 3
  uv run python scripts/editorial/route_ideas.py --region Керчь --rounds 1
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemma  # noqa: E402
import state  # noqa: E402

PROMPT_VERSION = "route-ideas-v2"
IDEAS_PER_ROUND = 10
# Six regions of D9; a place's town decides its region.
REGIONS: dict[str, list[str]] = {
    "Юго-Запад": ["Севастополь"],
    "ЮБК": ["Ялта", "Алушта"],
    "Горный и центральный Крым": ["Бахчисарай", "Симферополь"],
    "Восток": ["Судак", "Феодосия"],
    "Север и Запад": ["Евпатория", "Саки"],
    "Керчь": ["Керчь"],
}
TYPES = ("city_walk", "hike", "car_trip", "family", "history", "sea")
DURATIONS = ("2h", "half_day", "day", "2_days", "3_days")
TRANSPORT = ("walk", "car", "mixed")
ABILITIES = ("children", "no_car", "dog", "no_steep")

# The rules live in a text file: long Russian prose reads better there.
SYSTEM = (Path(__file__).resolve().parent / "prompts" / "route-ideas.txt").read_text()


def _km(a: dict[str, Any], b: dict[str, Any]) -> float:
    dlat, dlng = math.radians(b["lat"] - a["lat"]), math.radians(b["lng"] - a["lng"])
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(a["lat"]))
        * math.cos(math.radians(b["lat"]))
        * math.sin(dlng / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(h))


def _region_of(place: dict[str, Any], centres: dict[str, dict[str, float]]) -> str | None:
    town = place.get("locality")
    if not town:
        town = min(centres, key=lambda t: _km(place, centres[t]))
    return next((region for region, towns in REGIONS.items() if town in towns), None)


def regions() -> dict[str, list[dict[str, Any]]]:
    """Published places by region, most popular first, with popularity."""
    published = state.published()
    with state.connect() as db:
        popularity = state.load(db, "popularity")
    towns: dict[str, list[dict[str, Any]]] = {}
    for place in published:
        if place.get("locality"):
            towns.setdefault(place["locality"], []).append(place)
    centres = {
        town: {
            "lat": sum(p["lat"] for p in items) / len(items),
            "lng": sum(p["lng"] for p in items) / len(items),
        }
        for town, items in towns.items()
    }
    out: dict[str, list[dict[str, Any]]] = {region: [] for region in REGIONS}
    for place in published:
        region = _region_of(place, centres)
        if region is None:
            continue
        detail = (popularity.get(place["id"]) or ("", {}))[1]
        town = place.get("locality") or min(centres, key=lambda t: _km(place, centres[t]))
        out[region].append(
            {
                **place,
                "popularity": detail.get("score", 0),
                "qid": detail.get("qid"),
                "where": _where(place, town, centres[town]),
            }
        )
    for region, items in out.items():
        items.sort(key=lambda p: (-p["popularity"], not p.get("photos")))
        out[region] = _one_per_object(items)
    return out


def _where(place: dict[str, Any], town: str, centre: dict[str, float]) -> str:
    """«Ялта, 12 км на СВ» so the model sees which stops lie together."""
    distance = _km(centre, place)
    if distance < 2:
        return f"{town}, центр"
    bearing = math.degrees(
        math.atan2(
            math.radians(place["lng"] - centre["lng"]) * math.cos(math.radians(centre["lat"])),
            math.radians(place["lat"] - centre["lat"]),
        )
    )
    side = ("С", "СВ", "В", "ЮВ", "Ю", "ЮЗ", "З", "СЗ")[round(bearing / 45) % 8]
    return f"{town}, {distance:.0f} км на {side}"


def _one_per_object(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per sight: the same Wikidata item, or one name inside the
    other within 300 m («Пантикапей» and «Городище Пантикапей»), is the same
    place for a route. The most popular copy stays."""
    kept: list[dict[str, Any]] = []
    for place in items:
        name = place["name"].casefold()
        twin = next(
            (
                other
                for other in kept
                if (place.get("qid") and place.get("qid") == other.get("qid"))
                or (
                    _km(place, other) < 0.3
                    and (name in other["name"].casefold() or other["name"].casefold() in name)
                )
            ),
            None,
        )
        if twin is None:
            kept.append(place)
    return kept


def _listing(places: list[dict[str, Any]]) -> str:
    lines = []
    for number, place in enumerate(places, start=1):
        short = (place.get("short") or "").replace("\n", " ")[:110]
        photo = "да" if place.get("photos") else "нет"
        lines.append(
            f"{number}. {place['name']} ({place['where']}) "
            f"[{', '.join(place.get('categories') or [])}] "
            f"популярность {place['popularity']}, фото: {photo}. {short}"
        )
    return "\n".join(lines)


def _valid(idea: dict[str, Any], places: list[dict[str, Any]]) -> str | None:
    """Why the idea is rejected, or None."""
    stops = idea.get("stops")
    least = 2 if idea.get("type") in ("hike", "sea") else 3
    if not isinstance(stops, list) or len(stops) < least:
        return f"меньше {least} точек"
    if any(not isinstance(n, int) or not 1 <= n <= len(places) for n in stops):
        return "номер вне списка"
    if len(set(stops)) != len(stops):
        return "точка повторяется"
    wow = idea.get("wow")
    if wow not in stops:
        return "вау-точка не из маршрута"
    if not places[wow - 1].get("photos"):
        return "у вау-точки нет фото"
    if idea.get("type") not in TYPES or idea.get("duration") not in DURATIONS:
        return "неизвестный тип или длительность"
    if idea.get("transport") not in TRANSPORT:
        return "неизвестный транспорт"
    return None


def _as_ids(idea: dict[str, Any], places: list[dict[str, Any]]) -> dict[str, Any]:
    def pid(n: int | None) -> str | None:
        return places[n - 1]["id"] if isinstance(n, int) else None

    return {
        **idea,
        "stops": [pid(n) for n in idea["stops"]],
        "wow": pid(idea["wow"]),
        # A find outside the route is a slip, not a broken idea.
        "find": pid(idea["find"]) if idea.get("find") in idea["stops"] else None,
        # "Without a car" holds only for a walk; the rest is recomputed by
        # code later (D28, D36), the model's list is a hint.
        "abilities": [
            a
            for a in idea.get("abilities") or []
            if a in ABILITIES and (a != "no_car" or idea.get("transport") == "walk")
        ],
    }


async def run(only: str | None, rounds: int) -> None:
    by_region = regions()
    async with httpx.AsyncClient() as client:
        for region, places in by_region.items():
            if only and region != only:
                continue
            listing = _listing(places)
            with state.connect() as db:
                saved = {
                    key: payload
                    for key, (status, payload) in state.load(db, "route_ideas").items()
                    if key.startswith(f"{region}:") and status == "done"
                }
            ideas = [idea for payload in saved.values() for idea in payload["ideas"]]
            names = {p["id"]: p["name"] for p in places}
            for number in range(1, rounds + 1):
                key = f"{region}:{number}"
                if key in saved:
                    continue
                proposed = "\n".join(
                    f"- {i['title']}: {', '.join(names.get(s, '?') for s in i['stops'])}"
                    for i in ideas
                )
                user = (
                    f"Район: {region}. Мест: {len(places)}.\n\n{listing}\n\n"
                    f"Уже предложены ({len(ideas)}):\n{proposed or '— пока ничего'}\n\n"
                    f"Предложи {IDEAS_PER_ROUND} новых идей."
                )
                try:
                    answer = await gemma.chat_json(
                        client,
                        system=SYSTEM,
                        user=user,
                        max_tokens=9000,
                        temperature=0.7,
                        request_seconds=1800,
                        reasoning="medium",
                    )
                except gemma.ModelUnavailable as exc:
                    print(f"model unavailable, stopping; rerun to resume ({exc})")
                    return
                except ValueError as exc:
                    print(f"{key}: unreadable answer, will retry on the next run ({exc})")
                    continue
                kept, rejected = [], []
                for idea in answer.get("ideas") or []:
                    reason = _valid(idea, places)
                    if reason:
                        rejected.append({"title": idea.get("title"), "reason": reason})
                    else:
                        kept.append(_as_ids(idea, places))
                with state.connect() as db:
                    state.save(
                        db,
                        "route_ideas",
                        key,
                        "done",
                        {
                            "ideas": kept,
                            "rejected": rejected,
                            "places": len(places),
                            "prompt_version": PROMPT_VERSION,
                        },
                        model=gemma.MODEL,
                    )
                ideas += kept
                print(f"{key}: kept {len(kept)}, rejected {len(rejected)}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", choices=list(REGIONS))
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(run(args.region, args.rounds))


if __name__ == "__main__":
    main()
