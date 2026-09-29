#!/usr/bin/env python3
"""Cut finished places into review batches (spec 16, section 2, step 4).

Every place whose text passed the checks goes into a batch of about 100
with its chosen photos. For each batch a random 5% sample (at least five)
is written as an HTML page for the owner; texts the checks flagged are
listed separately and never enter a batch until read by hand. The batch
file is what import_places.py loads once the owner approves the sample.

  uv run python scripts/editorial/build_batches.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import state  # noqa: E402

BATCH_SIZE = 100
SAMPLE_SHARE = 0.05
SAMPLE_MIN = 5


def _row(
    key: str, place: dict[str, Any], text: dict[str, Any], photos: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "id": key,
        "name": place["name"],
        "locality": place.get("locality"),
        "short": text["short"],
        "description": text["description"],
        "source_title": text["source_title"],
        "source_url": text["source_url"],
        "photos": [
            {k: p.get(k) for k in ("title", "thumb", "page", "author", "license")} for p in photos
        ],
    }


def main() -> None:
    random.seed(20260927)
    places = {p["id"]: p for p in state.places()}
    out_dir = state.WORK_DIR / "batches"
    out_dir.mkdir(exist_ok=True)
    with state.connect() as db:
        texts = state.load(db, "place_text")
        photos = state.load(db, "place_photo")
        batched = {key for key, (status, _p) in state.load(db, "batch").items() if status == "done"}
    ready, flagged = [], []
    for key, (status, text) in sorted(texts.items()):
        if key in batched or key not in places:
            continue
        chosen = (photos.get(key) or ("", {"photos": []}))[1].get("photos") or []
        row = _row(key, places[key], text, chosen)
        if status == "done":
            ready.append(row)
        elif status == "needs_review":
            flagged.append({**row, "problems": text.get("problems")})
    existing = len(list(out_dir.glob("batch-*.jsonl")))
    for index in range(0, len(ready), BATCH_SIZE):
        number = existing + index // BATCH_SIZE + 1
        batch = ready[index : index + BATCH_SIZE]
        name = f"batch-{number:02d}"
        with (out_dir / f"{name}.jsonl").open("w") as fh:
            for row in batch:
                fh.write(
                    json.dumps(
                        {**row, "batch": name, "model": "gemma-4-26b-it"}, ensure_ascii=False
                    )
                    + "\n"
                )
        sample = random.sample(
            batch, min(len(batch), max(SAMPLE_MIN, round(len(batch) * SAMPLE_SHARE)))
        )
        with (out_dir / f"{name}-sample.json").open("w") as fh:
            json.dump(sample, fh, ensure_ascii=False, indent=1)
        print(f"{name}: {len(batch)} places, sample {len(sample)}")
    with (out_dir / "flagged.json").open("w") as fh:
        json.dump(flagged, fh, ensure_ascii=False, indent=1)
    print(f"flagged for reading by hand: {len(flagged)}")


if __name__ == "__main__":
    main()
