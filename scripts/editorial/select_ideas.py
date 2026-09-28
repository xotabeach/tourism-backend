#!/usr/bin/env python3
"""Pick about 140 route ideas for the owner's idea page (spec 16a, D26, D35).

Every idea gets a quality score: popularity of its wow point and the
model's grade of that point's photo, a find, a story that says something,
a sensible number of stops. Ideas are then taken greedily, best first, with
a bonus for axes still short of their minimum (D27: at least 8–10 routes
per ability, 10–15 multi-day) and a quota per region: Kerch and the North
take what passes, up to 12 each (D35), the rest is split by how many places
a region has. An idea sharing more than 60% of its stops with one already
taken is skipped (D10).

Writes the "idea_selection" step and ideas-selected.json.

  uv run python scripts/editorial/select_ideas.py --total 140
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import route_ideas  # noqa: E402
import state  # noqa: E402

SMALL_REGIONS = {"Север и Запад": 12, "Керчь": 12}
MAX_SHARED = 0.6
# Minimum count per axis value (D27, D9); ideas that fill a short axis
# get a bonus while it stays short.
MINIMUMS: dict[tuple[str, str], int] = {
    ("abilities", "children"): 10,
    ("abilities", "no_car"): 10,
    ("abilities", "dog"): 10,
    ("abilities", "no_steep"): 10,
    ("duration", "2_days"): 7,
    ("duration", "3_days"): 5,
    ("difficulty", "1"): 6,
    ("difficulty", "5"): 6,
    ("type", "hike"): 20,
    ("type", "car_trip"): 20,
    ("type", "family"): 12,
    ("type", "sea"): 12,
    ("type", "history"): 15,
    ("type", "city_walk"): 15,
    ("time_of_day", "dawn"): 5,
    ("time_of_day", "sunset"): 8,
}


def _axes(idea: dict[str, Any]) -> list[tuple[str, str]]:
    out = [
        ("type", idea["type"]),
        ("duration", idea["duration"]),
        ("difficulty", str(idea.get("difficulty"))),
        ("time_of_day", idea.get("time_of_day") or "any"),
    ]
    return out + [("abilities", a) for a in idea.get("abilities") or []]


def _shared(a: list[str], b: list[str]) -> float:
    return len(set(a) & set(b)) / min(len(a), len(b))


def _quality(
    idea: dict[str, Any], places: dict[str, dict[str, Any]], photo_grade: dict[str, int]
) -> float:
    wow = places[idea["wow"]]
    grade = photo_grade.get(idea["wow"], 3)
    story = idea.get("story") or ""
    stops = len(idea["stops"])
    return (
        0.4 * wow["popularity"] / 100
        + 0.2 * grade / 5
        + (0.2 if idea.get("find") else 0)
        + (0.1 if 50 <= len(story) <= 220 else 0)
        + (0.1 if 3 <= stops <= 6 else 0)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--total", type=int, default=140)
    args = parser.parse_args()

    by_region = route_ideas.regions()
    places = {p["id"]: {**p, "region": r} for r, items in by_region.items() for p in items}
    with state.connect() as db:
        raw = state.load(db, "route_ideas")
        photos = state.load(db, "place_photo")
    photo_grade = {
        key: max(int(p["verdict"].get("quality") or 0) for p in payload["photos"])
        for key, (status, payload) in photos.items()
        if status == "done" and payload.get("photos")
    }

    # Duplicates merged after the ideas were written: a stop that became a
    # twin points at its surviving card (merge_places.py groups).
    survivor: dict[str, str] = {}
    for groups in sorted((state.WORK_DIR / "batches").glob("merge-groups*.jsonl")):
        for line in groups.read_text().splitlines():
            group = json.loads(line)
            survivor.update(dict.fromkeys(group["twins"], group["survivor"]))

    def remap(idea: dict[str, Any]) -> dict[str, Any]:
        stops = list(dict.fromkeys(survivor.get(s, s) for s in idea["stops"]))
        find = survivor.get(idea.get("find") or "", idea.get("find"))
        return {
            **idea,
            "stops": stops,
            "wow": survivor.get(idea["wow"], idea["wow"]),
            "find": find if find in stops else None,
        }

    ideas: list[dict[str, Any]] = []
    regions = list(route_ideas.REGIONS)
    for key, (status, payload) in sorted(raw.items()):
        if status != "done":
            continue
        region, round_number = key.rsplit(":", 1)
        for n, raw_idea in enumerate(payload["ideas"], start=1):
            idea = remap(raw_idea)
            if len(idea["stops"]) < 2 or not all(s in places for s in idea["stops"]):
                continue  # a stop was unpublished or merged since
            ideas.append(
                {
                    **idea,
                    "id": f"r{regions.index(region) + 1}-{round_number}-{n}",
                    "region": region,
                    "quality": round(_quality(idea, places, photo_grade), 3),
                }
            )

    counts = Counter(p["region"] for p in places.values())
    quota = {r: min(cap, counts[r]) for r, cap in SMALL_REGIONS.items()}
    rest = args.total - sum(quota.values())
    big = [r for r in regions if r not in SMALL_REGIONS]
    big_places = sum(counts[r] for r in big)
    for r in big:
        quota[r] = round(rest * counts[r] / big_places)

    chosen: list[dict[str, Any]] = []
    axis_count: Counter[tuple[str, str]] = Counter()
    taken: Counter[str] = Counter()
    pool = sorted(ideas, key=lambda i: -i["quality"])
    while pool and len(chosen) < args.total:

        def score(idea: dict[str, Any]) -> float:
            short = sum(1 for axis in _axes(idea) if axis_count[axis] < MINIMUMS.get(axis, 0))
            return idea["quality"] + 0.15 * short

        best = None
        for idea in sorted(pool, key=score, reverse=True):
            if taken[idea["region"]] >= quota[idea["region"]]:
                continue
            same_region = [c for c in chosen if c["region"] == idea["region"]]
            if any(_shared(idea["stops"], c["stops"]) > MAX_SHARED for c in same_region):
                continue
            best = idea
            break
        if best is None:
            break
        pool.remove(best)
        chosen.append(best)
        taken[best["region"]] += 1
        axis_count.update(_axes(best))

    short = {
        f"{axis}={value}": f"{axis_count[(axis, value)]} из {need}"
        for (axis, value), need in MINIMUMS.items()
        if axis_count[(axis, value)] < need
    }
    summary = {
        "ideas": len(ideas),
        "chosen": len(chosen),
        "by_region": {r: f"{taken[r]} из квоты {quota[r]}" for r in regions},
        "short_axes": short,
    }
    with state.connect() as db:
        state.save(db, "idea_selection", "current", "done", {"chosen": chosen, "summary": summary})
    (state.WORK_DIR / "ideas-selected.json").write_text(
        json.dumps({"chosen": chosen, "summary": summary}, ensure_ascii=False, indent=1)
    )
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
