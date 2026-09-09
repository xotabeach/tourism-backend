"""Validate the explicit help pack; optionally print its bundled Dart FAQ.

No database, embeddings, external services, publication or file writes.
"""

import argparse
import sys
from pathlib import Path

from tourism_backend.modules.support.infrastructure.help_catalog import (
    load_help_catalog,
    render_mobile_faq,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data/support_help"))
    parser.add_argument("--dart", action="store_true", help="Print generated FAQ source")
    parser.add_argument("--check-mobile", type=Path, help="Compare an existing generated FAQ file")
    args = parser.parse_args()
    catalog = load_help_catalog(args.root)
    rendered = render_mobile_faq(catalog)
    if args.check_mobile is not None:
        actual = args.check_mobile.read_text(encoding="utf-8")
        if actual != rendered:
            print("Bundled mobile FAQ differs from the reviewed source pack", file=sys.stderr)
            return 1
    if args.dart:
        sys.stdout.write(rendered)
    else:
        print(
            f"Validated {len(catalog.articles)} help articles; "
            f"status={catalog.manifest.status}; "
            f"release_verified={catalog.manifest.release_verified}. Nothing published."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
