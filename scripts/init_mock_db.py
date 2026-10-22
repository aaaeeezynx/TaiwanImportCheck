#!/usr/bin/env python3
"""Initialize or reset the P0 mock SQLite database (`p0_mock_tariff.db`)."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.p0_import_lookup import DATASET_VERSION, ensure_mock_db, resolve_db_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize P0 mock SQLite database")
    parser.add_argument("--db-path", default="", help="Custom SQLite DB path")
    parser.add_argument("--reset", action="store_true", help="Drop and recreate tables")
    args = parser.parse_args()

    target = ensure_mock_db(resolve_db_path(args.db_path or None), force_reset=args.reset)
    print(f"[OK] P0 mock SQLite DB initialized at: {target} (version={DATASET_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
