#!/usr/bin/env python3
"""Fetch, validate, and atomically import PASS official-source snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_official_snapshot import (
    DEFAULT_CANDIDATE_DIR,
    DEFAULT_REPORT_DIR,
    DEFAULT_SOURCE_DIR,
    _filename,
    run,
)
from tools.official_database import (
    DEFAULT_DB_PATH,
    import_validated_snapshot,
    initialize_database,
    load_sources,
    record_validation_result,
    rollback_dataset,
)


def run_update(download: bool, db_path: Path = DEFAULT_DB_PATH) -> dict:
    initialize_database(db_path)
    report = run(
        download=download,
        source_dir=DEFAULT_SOURCE_DIR,
        candidate_dir=DEFAULT_CANDIDATE_DIR,
        report_dir=DEFAULT_REPORT_DIR,
    )
    sources = {str(source["id"]): source for source in load_sources()}
    updates = []
    import_errors = 0
    for result in report["results"]:
        dataset_id = str(result.get("id", ""))
        status = result.get("status", "FAIL")
        try:
            record_validation_result(db_path, dataset_id, status, result.get("error"))
            if status != "PASS" or dataset_id not in sources:
                updates.append({"id": dataset_id, "status": status, "database_action": "kept_previous"})
                continue
            snapshot = DEFAULT_SOURCE_DIR / _filename(
                dataset_id, sources[dataset_id]["format"]
            )
            imported = import_validated_snapshot(
                db_path,
                sources[dataset_id],
                snapshot.read_bytes(),
                result,
                source_url=result.get("source_url"),
                metadata_modified=result.get("metadata_modified"),
                update_frequency=result.get("update_frequency"),
            )
            updates.append({"id": dataset_id, "status": "PASS", "database_action": "activated", **imported})
        except Exception as exc:
            import_errors += 1
            error = f"{type(exc).__name__}: {exc}"
            try:
                record_validation_result(db_path, dataset_id, "IMPORT_FAIL", error)
            except ValueError:
                pass
            updates.append({
                "id": dataset_id,
                "status": status,
                "database_action": "kept_previous",
                "error": error,
            })

    summary = {
        **report["summary"],
        "activated": sum(item["database_action"] == "activated" for item in updates),
        "new_versions": sum(
            item.get("database_action") == "activated" and item.get("new_version", False)
            for item in updates
        ),
        "import_errors": import_errors,
    }
    update_report = {
        "generated_at": report["generated_at"],
        "mode": report["mode"],
        "summary": summary,
        "datasets": updates,
    }
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (DEFAULT_REPORT_DIR / "official_data_update.json").write_text(
        json.dumps(update_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return update_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--download", action="store_true", help="download and validate current official snapshots")
    mode.add_argument("--local", action="store_true", help="validate local snapshots and retry PASS imports")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--rollback", nargs=2, metavar=("DATASET_ID", "VERSION_ID"))
    args = parser.parse_args()

    if args.rollback:
        initialize_database(args.db_path)
        rollback_dataset(args.db_path, args.rollback[0], args.rollback[1])
        print("Official dataset active version rolled back.")
        return 0

    report = run_update(download=not args.local, db_path=args.db_path)
    print(json.dumps(report["summary"], ensure_ascii=False))
    print("Report: data/official_data/reports/official_source_validation.md")
    print("Update details: data/official_data/reports/official_data_update.json")
    return 0 if not any(report["summary"][key] for key in ("review", "fail", "missing", "import_errors")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
