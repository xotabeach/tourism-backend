#!/usr/bin/env python3
"""Fix what the realism checks found, in code, before asking the model (spec 16a, D31).

Takes the builder's input and its dry-run report and writes the next input:
  - a zigzag is straightened: the stops go in the shortest order that keeps
    the first stop (a multi-day route is reordered inside each day only);
  - a one-day route longer than its day limit loses its weakest stops (not
    the wow point, not the find, least popular first) while it keeps at
    least three, one stop per round, so the next dry run measures again.
What code cannot fix stays in the report for the model or the editor.

  uv run python scripts/editorial/fix_routes.py in.jsonl in-report.jsonl out.jsonl
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402

MIN_STOPS = 3


def _km(a: dict[str, Any], b: dict[str, Any]) -> float:
    dlat, dlng = math.radians(b["lat"] - a["lat"]), math.radians(b["lng"] - a["lng"])
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(a["lat"]))
        * math.cos(math.radians(b["lat"]))
        * math.sin(dlng / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(h))


def _length(ids: list[str], at: dict[str, dict[str, Any]]) -> float:
    return sum(_km(at[a], at[b]) for a, b in zip(ids, ids[1:], strict=False))


def _shortest(ids: list[str], at: dict[str, dict[str, Any]]) -> list[str]:
    """Nearest neighbour from the fixed first stop, then 2-opt."""
    if len(ids) < 3:
        return ids
    order, rest = [ids[0]], ids[1:]
    while rest:
        nxt = min(rest, key=lambda i: _km(at[order[-1]], at[i]))
        order.append(nxt)
        rest.remove(nxt)
    improved = True
    while improved:
        improved = False
        for i in range(1, len(order) - 1):
            for j in range(i + 1, len(order)):
                candidate = order[:i] + order[i : j + 1][::-1] + order[j + 1 :]
                if _length(candidate, at) + 1e-9 < _length(order, at):
                    order, improved = candidate, True
    return order


def main() -> None:
    source, report_path, target = (Path(a) for a in sys.argv[1:4])
    at = {p["id"]: p for p in state.published()}
    with state.connect() as db:
        popularity = {k: p.get("score", 0) for k, (_s, p) in state.load(db, "popularity").items()}
    reports = {
        r["key"]: r for r in (json.loads(line) for line in report_path.read_text().splitlines())
    }
    fixed = {"reordered": 0, "trimmed": 0}
    with target.open("w") as out:
        for line in source.read_text().splitlines():
            idea = json.loads(line)
            problems = (reports.get(idea["key"]) or {}).get("problems") or []
            if any("зигзаг" in p for p in problems):
                if idea.get("day_breaks"):
                    days, current = [], []
                    for stop in idea["stops"]:
                        current.append(stop)
                        if stop in idea["day_breaks"]:
                            days.append(current)
                            current = []
                    days.append(current)
                    days = [_shortest(d, at) for d in days]
                    idea["stops"] = [s for d in days for s in d]
                    idea["day_breaks"] = [d[-1] for d in days[:-1]]
                else:
                    idea["stops"] = _shortest(idea["stops"], at)
                fixed["reordered"] += 1
            long_day = any(" ч, для сложности" in p or "пешком" in p for p in problems)
            if long_day and not idea.get("day_breaks") and len(idea["stops"]) > MIN_STOPS:
                keep = {idea.get("wow"), idea.get("find"), idea["stops"][0]}
                weakest = min(
                    (s for s in idea["stops"] if s not in keep),
                    key=lambda s: popularity.get(s, 0),
                    default=None,
                )
                if weakest:
                    idea["stops"] = [s for s in idea["stops"] if s != weakest]
                    fixed["trimmed"] += 1
            out.write(json.dumps(idea, ensure_ascii=False) + "\n")
    print(fixed)


if __name__ == "__main__":
    main()
