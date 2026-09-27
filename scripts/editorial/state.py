"""Local state of the editorial pipeline (spec 16, D20).

The pipeline runs on an editor's machine, not on the server: it reads a
places export, asks Wikipedia and the home-lab model, and keeps every step's
result here so a run that stops (the home PC went to sleep, the tunnel
dropped) resumes where it stopped. Nothing here touches the production
database; results reach it only through the reviewed import.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

WORK_DIR = Path(os.environ.get("EDITORIAL_WORK_DIR", Path.home() / "crimeatrip-editorial"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results (
    step TEXT NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL,          -- done / failed / needs_review / skipped
    payload TEXT NOT NULL,         -- JSON
    model TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (step, key)
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(WORK_DIR / "state.sqlite", timeout=120)
    # Several steps run side by side (matching while texts are written).
    # Another step may hold the file; WAL then switches on the next start.
    with contextlib.suppress(sqlite3.OperationalError):
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(_SCHEMA)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def done_keys(connection: sqlite3.Connection, step: str) -> set[str]:
    rows = connection.execute(
        "SELECT key FROM results WHERE step = ? AND status != 'failed'", (step,)
    )
    return {row[0] for row in rows}


def save(
    connection: sqlite3.Connection,
    step: str,
    key: str,
    status: str,
    payload: dict[str, Any],
    model: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO results(step, key, status, payload, model) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(step, key) DO UPDATE SET status = excluded.status, "
        "payload = excluded.payload, model = excluded.model, updated_at = datetime('now')",
        (step, key, status, json.dumps(payload, ensure_ascii=False), model),
    )


def load(connection: sqlite3.Connection, step: str) -> dict[str, tuple[str, dict[str, Any]]]:
    rows = connection.execute("SELECT key, status, payload FROM results WHERE step = ?", (step,))
    return {key: (status, json.loads(payload)) for key, status, payload in rows}


def places() -> list[dict[str, Any]]:
    path = WORK_DIR / "places.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
