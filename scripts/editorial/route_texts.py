#!/usr/bin/env python3
"""Texts of editorial routes from a source pack (spec 16a, section 5, D39).

For every route the pack holds the idea, the calculation of the builder's
dry run (length, days, hours, nights) and, per stop, our checked text of the
place, a longer part of its Wikipedia article and the known facts (paid,
opening hours, season). The model writes by prompts/route-text.txt: facts
only from the pack, numbers only from the calculation or the pack. The
answer is then checked against the pack the same way place texts are (links,
phones, emoji, calls, numbers and words that are not in the pack); anything
it flags goes to the owner's review as needs_review.

  uv run python scripts/editorial/route_texts.py routes-v7.jsonl routes-v7-report.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemma  # noqa: E402
import place_texts  # noqa: E402
import state  # noqa: E402
import wiki_match  # noqa: E402

PROMPT_VERSION = "route-text-v3"
SYSTEM = (Path(__file__).resolve().parent / "prompts" / "route-text.txt").read_text()
WIKI_CHARS = 1800
PLACE_CHARS = 900
# Route texts carry impressions the pack does not word («вид на море»), so
# they may hold more words of their own than a retold place text.
MAX_NEW_WORDS = 0.65
LIMITS = {"title": 60, "short": 160, "description": 1400, "stop": 220}
TYPES = {
    "city_walk": "прогулка по городу",
    "hike": "пешая тропа",
    "car_trip": "на машине с пешими подходами",
    "family": "семейный",
    "history": "история",
    "sea": "море и берег",
}
ABILITIES = {
    "children": "с детьми",
    "no_car": "без машины",
    "dog": "с собакой",
    "no_steep": "без крутых подъёмов",
}


# Wikipedia stamps every Crimean article with a paragraph on the status of
# the peninsula; it is no fact about the place and must not reach a route.
_STATUS = re.compile(
    r"[^.\n]*(аннекс|признанных большинством|контролирующей территорию|"
    r"культурного наследия (национального|регионального|федерального) значения)"
    r"[^.\n]*[.\n]?",
    re.I,
)
_STRESS = "\u0301"


def _clean(text: str) -> str:
    return _STATUS.sub("", place_texts.clean_source(text)).replace(_STRESS, "").strip()


def _num(value: float) -> str:
    return f"{value:.1f}".replace(".", ",").replace(",0", "")


async def _wiki_texts(titles: list[str]) -> dict[str, str]:
    """A longer plain extract per article, cached in the wiki_full step."""
    with state.connect() as db:
        cached = {k: p["text"] for k, (s, p) in state.load(db, "wiki_full").items() if s == "done"}
    missing = [t for t in titles if t not in cached]
    async with httpx.AsyncClient(headers=wiki_match.HEADERS, timeout=30) as client:
        for title in missing:
            data = await wiki_match._get(
                client,
                wiki_match.API,
                {
                    "action": "query",
                    "prop": "extracts",
                    "explaintext": 1,
                    "exchars": WIKI_CHARS,
                    "redirects": 1,
                    "titles": title,
                },
            )
            pages = (data.get("query") or {}).get("pages") or {}
            text = next(iter(pages.values()), {}).get("extract") or ""
            cached[title] = text
            with state.connect() as db:
                state.save(db, "wiki_full", title, "done", {"text": text})
    return cached


def _facts(place: dict[str, Any]) -> list[str]:
    facts = []
    if place.get("is_paid") is True:
        facts.append("вход платный")
    if place.get("opening_hours"):
        facts.append(f"часы работы по данным OSM: {place['opening_hours']}")
    if place.get("seasonality"):
        facts.append("сезон: " + ", ".join(place["seasonality"]))
    return facts


def _pack(
    idea: dict[str, Any],
    report: dict[str, Any],
    places: dict[str, dict[str, Any]],
    our: dict[str, dict[str, Any]],
    wiki_title: dict[str, str | None],
    wiki: dict[str, str],
) -> str:
    axes = idea.get("axes") or {}
    lines = [
        "<замысел>",
        f"Название-черновик: {idea['title']}",
        f"Замысел: {idea.get('story') or ''}",
        f"Тип: {TYPES.get(axes.get('type'), axes.get('type') or '')}",
    ]
    abilities = [ABILITIES[a] for a in axes.get("abilities") or [] if a in ABILITIES]
    if abilities:
        lines.append("Подходит: " + ", ".join(abilities))
    lines += ["</замысел>", "<расчёт>"]
    lines.append(f"Длина пути: {_num(report.get('distance_km') or 0)} км")
    lines.append(f"Сложность по расчёту: {report.get('difficulty')} из 5")
    days = report.get("days") or []
    lines.append(f"Дней: {len(days)}")
    for day in days:
        parts = [f"День {day['day']}: точек {day['stops']}"]
        if day.get("walk_km"):
            parts.append(f"пешком {_num(day['walk_km'])} км")
        if day.get("drive_h"):
            parts.append(f"за рулём {_num(day['drive_h'])} ч")
        parts.append(f"весь день около {_num(day['day_h'])} ч с осмотром и обедом")
        if day.get("night"):
            parts.append(day["night"])
        lines.append(", ".join(parts))
    lines += ["</расчёт>", "<точки>"]
    for number, place_id in enumerate(idea["stops"], start=1):
        place = places[place_id]
        lines.append(f'<точка номер="{number}" название="{place["name"]}">')
        lines.append(f"Где: {place.get('where') or place.get('locality') or ''}")
        # Only texts this pipeline wrote and checked: the first seed's cards
        # often hold the raw article intro, which the article below repeats.
        text = our.get(place_id)
        if text:
            description = (text.get("description") or "")[:PLACE_CHARS]
            lines.append(f"Наш текст: {_clean(text.get('short') or '')} {_clean(description)}")
        title = wiki_title.get(place_id)
        if title and wiki.get(title):
            lines.append(f"Статья «{title}»: {_clean(wiki[title])}")
        elif not text:
            fallback = _clean(place.get("description") or place.get("short") or "")
            lines.append(f"Описание: {fallback[:PLACE_CHARS]}")
        facts = _facts(place)
        if facts:
            lines.append("Факты: " + "; ".join(facts))
        lines.append("</точка>")
    lines.append("</точки>")
    return "\n".join(lines)


def _check(pack: str, answer: dict[str, Any], stops: int) -> list[str]:
    problems: list[str] = []
    for field in ("title", "short", "description"):
        value = answer.get(field) or ""
        if not value.strip():
            problems.append(f"нет поля {field}")
        elif len(value) > LIMITS[field] * 1.15:
            problems.append(f"{field} длиннее {LIMITS[field]} символов")
    notes = answer.get("stops") or {}
    if sorted(notes) != [str(n) for n in range(1, stops + 1)]:
        problems.append("заметки не ко всем точкам или к лишним")
    text = " ".join(
        [answer.get("title") or "", answer.get("short") or "", answer.get("description") or ""]
        + [str(v) for v in notes.values()]
    )
    saved = place_texts.MAX_NEW_WORDS
    place_texts.MAX_NEW_WORDS = MAX_NEW_WORDS
    try:
        problems += place_texts.check(pack, "", text)
    finally:
        place_texts.MAX_NEW_WORDS = saved
    return problems


async def run(
    ideas: list[dict[str, Any]],
    reports: dict[str, dict[str, Any]],
    only: list[str] | None,
    force: bool,
) -> None:
    places = {p["id"]: p for p in state.published()}
    with state.connect() as db:
        our = {k: p for k, (s, p) in state.load(db, "place_text").items() if s == "done"}
        popularity = {k: p for k, (s, p) in state.load(db, "popularity").items()}
        done = set() if force else state.done_keys(db, "route_text")
    wiki_title = {k: (p.get("title") if p.get("trust") else None) for k, p in popularity.items()}
    todo = [
        i
        for i in ideas
        if i["key"] not in done
        and (not only or i["key"] in only)
        and not (reports.get(i["key"]) or {}).get("problems")
        and all(s in places for s in i["stops"])
    ]
    titles = sorted({wiki_title[s] for i in todo for s in i["stops"] if wiki_title.get(s)})
    wiki = await _wiki_texts(titles)
    print(f"{len(todo)} routes to write", flush=True)
    counts: dict[str, int] = {}
    async with httpx.AsyncClient() as client:
        for idea in todo:
            report = reports[idea["key"]]
            pack = _pack(idea, report, places, our, wiki_title, wiki)
            try:
                answer = await gemma.chat_json(
                    client,
                    system=SYSTEM,
                    user=pack,
                    max_tokens=5000,
                    temperature=0.4,
                    request_seconds=1200,
                    reasoning="low",
                )
            except gemma.ModelUnavailable as exc:
                print(f"model unavailable, stopping; rerun to resume ({exc})")
                return
            except ValueError as exc:
                counts["failed"] = counts.get("failed", 0) + 1
                with state.connect() as db:
                    state.save(db, "route_text", idea["key"], "failed", {"error": str(exc)[:300]})
                continue
            problems = _check(pack, answer, len(idea["stops"]))
            status = "needs_review" if problems else "done"
            counts[status] = counts.get(status, 0) + 1
            with state.connect() as db:
                state.save(
                    db,
                    "route_text",
                    idea["key"],
                    status,
                    {
                        **answer,
                        "problems": problems,
                        "pack": pack,
                        "prompt_version": PROMPT_VERSION,
                    },
                    model=gemma.MODEL,
                )
            print(f"  {idea['key']} {status} {problems[:2]}", flush=True)
    print(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ideas", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--only", nargs="*", help="route keys to write")
    parser.add_argument("--force", action="store_true", help="write again what is written")
    args = parser.parse_args()
    ideas = [json.loads(line) for line in args.ideas.read_text().splitlines() if line.strip()]
    reports = {
        r["key"]: r for r in (json.loads(x) for x in args.report.read_text().splitlines() if x)
    }
    asyncio.run(run(ideas, reports, args.only, args.force))


if __name__ == "__main__":
    main()
