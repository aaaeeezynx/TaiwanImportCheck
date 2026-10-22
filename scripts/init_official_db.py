#!/usr/bin/env python3
"""Create the independent official reference SQLite schema."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_database import DEFAULT_DB_PATH, initialize_database, load_sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    target = initialize_database(args.db_path)
    print(f"Official reference SQLite schema initialized ({len(load_sources())} source tables; no records imported).")
    print(f"Database file: {target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
