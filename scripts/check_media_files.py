#!/usr/bin/env python3
"""Read-only audit: media_attachments rows whose file is missing on disk.

Compares every active `media_attachments.storage_key` against the files
actually present under `MEDIA_ROOT`, and separately lists files on disk that
no active row points to (orphans left behind by an interrupted re-import or a
storage_key that drifted from the real filename, as happened once for the
«Долина привидений» place cover — BACKEND-23).

This never touches the database or the filesystem. It only reports; a fix is
a manual, reviewed UPDATE after checking the byte content matches (see
BACKEND-23 for the pattern: compare checksum_sha256 against the orphan file
before repointing storage_key/public_path to it).

Examples:
  uv run python scripts/check_media_files.py
  MEDIA_ROOT=/app/data/media uv run python scripts/check_media_files.py --json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from tourism_backend.config import get_settings
from tourism_backend.modules.media.infrastructure.models import MediaAttachment

_DEFAULT_MEDIA_ROOT = Path(__file__).resolve().parents[1] / "data" / "media"
_MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", str(_DEFAULT_MEDIA_ROOT)))


def _files_on_disk(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    with Session(engine) as session:
        rows = session.scalars(
            select(MediaAttachment).where(MediaAttachment.status == "active")
        ).all()

    on_disk = _files_on_disk(_MEDIA_ROOT)
    row_keys = {row.storage_key for row in rows}
    missing = [row for row in rows if row.storage_key not in on_disk]
    orphans = sorted(on_disk - row_keys)

    if args.json:
        print(
            json.dumps(
                {
                    "media_root": str(_MEDIA_ROOT),
                    "active_rows": len(rows),
                    "files_on_disk": len(on_disk),
                    "missing": [
                        {
                            "id": str(row.id),
                            "entity_type": row.entity_type,
                            "entity_id": str(row.entity_id),
                            "storage_key": row.storage_key,
                            "checksum_sha256": row.checksum_sha256,
                            "byte_size": row.byte_size,
                        }
                        for row in missing
                    ],
                    "orphan_files": orphans,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(f"media root: {_MEDIA_ROOT}")
    print(f"active media_attachments rows: {len(rows)}")
    print(f"files on disk: {len(on_disk)}")
    print(f"missing (row has no file): {len(missing)}")
    for row in missing:
        print(
            f"  {row.entity_type} {row.entity_id} -> {row.storage_key} "
            f"(id={row.id}, sha256={row.checksum_sha256})"
        )
    print(f"orphan files (file has no active row): {len(orphans)}")
    for path in orphans:
        print(f"  {path}")


if __name__ == "__main__":
    main()
