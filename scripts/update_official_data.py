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


def _validated_snapshot_path(result: dict, source_dir: Path) -> Path:
    """Resolve the validator's selected file without trusting its extension."""
    raw_path = result.get("file")
    if not raw_path:
        raise ValueError("PASS result does not identify a validated snapshot file")
    try:
        snapshot = Path(raw_path).resolve(strict=True)
    except OSError as exc:
        raise ValueError("validated snapshot file is unavailable") from exc
    root = Path(source_dir).resolve()
    if snapshot != root and root not in snapshot.parents:
        raise ValueError("validated snapshot file is outside the source snapshot directory")
    if not snapshot.is_file():
        raise ValueError("validated snapshot path is not a regular file")
    return snapshot


def run_update(
    download: bool,
    db_path: Path = DEFAULT_DB_PATH,
    *,
    source_dir: Path = DEFAULT_SOURCE_DIR,
    candidate_dir: Path = DEFAULT_CANDIDATE_DIR,
    report_dir: Path = DEFAULT_REPORT_DIR,
) -> dict:
    sources_list = load_sources()
    initialize_database(db_path, sources_list)
    report = run(
        download=download,
        source_dir=source_dir,
        candidate_dir=candidate_dir,
        report_dir=report_dir,
    )
    sources = {str(source["id"]): source for source in sources_list}
    updates = []
    import_errors = 0
    for result in report["results"]:
        dataset_id = str(result.get("id", ""))
        status = result.get("status", "FAIL")
        try:
            if status != "PASS":
                record_validation_result(db_path, dataset_id, status, result.get("error"))
                updates.append({"id": dataset_id, "status": status, "database_action": "kept_previous"})
                continue
            if dataset_id not in sources:
                raise ValueError(f"PASS result has unknown dataset id {dataset_id}")
            snapshot = _validated_snapshot_path(result, source_dir)
            imported = import_validated_snapshot(
                db_path,
                sources[dataset_id],
                snapshot.read_bytes(),
                result,
                source_url=result.get("source_url"),
                metadata_modified=result.get("metadata_modified"),
                update_frequency=result.get("update_frequency"),
            )
            updates.append({
                "id": dataset_id,
                "status": "PASS",
                "database_action": "activated" if imported["activated"] else "kept_previous",
                **imported,
            })
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
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "official_data_update.json").write_text(
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
