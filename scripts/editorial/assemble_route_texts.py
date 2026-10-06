#!/usr/bin/env python3
"""Assemble hand-written route texts with the calculation and check them (spec 16a, D39).

The editor writes a short line and the body of each route by the facts of
its stops (route-texts-*.json: {key: {"short", "body"}}); stop notes come
from the checked short texts of the places (stop-notes.json). This script
adds the closing paragraph from the builder's report, so no number in it is
typed by hand, runs the same checks as for model texts against the route's
facts, and writes the builder's input with a "text" per route.

  uv run python scripts/editorial/assemble_route_texts.py routes-final.jsonl \\
      routes-final-report.jsonl routes-with-text.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import place_texts  # noqa: E402
import state  # noqa: E402

DAYS = {1: "Один день", 2: "Два дня", 3: "Три дня"}
LEVEL = {
    1: "лёгкий",
    2: "несложный",
    3: "средней сложности",
    4: "трудный",
    5: "очень трудный",
}
MAX_NEW_WORDS = 0.65
HALF_DAY_HOURS = 4.5
LIMITS = {"title": 60, "short": 160, "description": 1400, "note": 220}
# Hike nights are named after the stop the day ends at; elsewhere after its town.
OUTDOORS = {"mountain", "nature", "cave", "viewpoint"}


def _num(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def _night(raw: str, idea: dict[str, Any], places: dict[str, dict[str, Any]]) -> str:
    if raw.startswith("Ночлег: "):
        return raw.removeprefix("Ночлег: ")
    name = raw.removeprefix("Ночлег в районе: ")
    place = next((places[s] for s in idea["stops"] if places[s]["name"] == name), None)
    if place and place.get("locality") and not OUTDOORS & set(place.get("categories") or []):
        return place["locality"]
    return f"в районе точки «{name}»"


def closing(idea: dict[str, Any], report: dict[str, Any], places: dict[str, Any]) -> str:
    days = report["days"]
    walked = sum(d["walk_km"] for d in days)
    on_foot = idea.get("transport") == "walk"
    how = "пешком" if on_foot else ("на машине с пешими участками" if walked >= 1 else "на машине")
    span = DAYS.get(len(days), f"{len(days)} дней")
    if len(days) == 1 and days[0]["day_h"] <= HALF_DAY_HOURS:
        span = "Полдня"
    parts = [f"{span} {how}."]
    length = f"Путь {_num(report['distance_km'])} км"
    if not on_foot and walked >= 1:
        length += f", из них пешком {_num(walked)} км"
    parts.append(length + ".")
    parts.append(f"Маршрут {LEVEL.get(report['difficulty'], 'средней сложности')}.")
    nights = list(dict.fromkeys(_night(d["night"], idea, places) for d in days if d.get("night")))
    if nights:
        parts.append(("Ночёвка: " if len(nights) == 1 else "Ночёвки: ") + ", ".join(nights) + ".")
    if report.get("warnings"):
        longest = max(d["day_h"] for d in days)
        label = "День насыщенный" if len(days) == 1 else "Самый насыщенный день"
        parts.append(f"{label}: около {_num(longest)} ч с осмотром и обедом.")
    return " ".join(parts)


def main() -> None:
    source, report_path, target = (Path(a) for a in sys.argv[1:4])
    work = state.WORK_DIR
    texts: dict[str, dict[str, str]] = {}
    for path in sorted(work.glob("route-texts-*.json")):
        texts.update(json.loads(path.read_text()))
    notes = json.loads((work / "stop-notes.json").read_text())
    places = {p["id"]: p for p in state.published()}
    reports = {
        r["key"]: r for r in (json.loads(x) for x in report_path.read_text().splitlines() if x)
    }
    saved = place_texts.MAX_NEW_WORDS
    place_texts.MAX_NEW_WORDS = MAX_NEW_WORDS
    flagged = 0
    with target.open("w") as out:
        for line in source.read_text().splitlines():
            idea = json.loads(line)
            key, report = idea["key"], reports[idea["key"]]
            if key not in texts:
                print(f"NO TEXT {key}")
                continue
            tail = closing(idea, report, places)
            text = {
                "title": idea["title"],
                "short": texts[key]["short"],
                "description": texts[key]["body"].strip() + "\n\n" + tail,
                "stops": {str(n): notes[s] for n, s in enumerate(idea["stops"], start=1)},
            }
            # The facts a route text may use: its idea, its stops and the tail.
            pack = " ".join(
                [idea.get("story") or "", idea["title"], tail]
                + [f"{places[s]['name']} {notes[s]}" for s in idea["stops"]]
            )
            problems = place_texts.check(pack, "", f"{text['short']} {text['description']}")
            for field in ("title", "short", "description"):
                if len(text[field]) > LIMITS[field]:
                    problems.append(f"{field} длиннее {LIMITS[field]}: {len(text[field])}")
            problems += [
                f"заметка {n} длиннее {LIMITS['note']}"
                for n, note in text["stops"].items()
                if len(note) > LIMITS["note"]
            ]
            if problems:
                flagged += 1
                print(f"{key}: {'; '.join(problems)}")
            idea["text"] = text
            out.write(json.dumps(idea, ensure_ascii=False) + "\n")
    place_texts.MAX_NEW_WORDS = saved
    print(f"{len(texts)} texts, flagged {flagged}")


if __name__ == "__main__":
    main()
