"""Versioned SQLite storage for independently validated official datasets."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import zipfile
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from scripts.validate_official_snapshot import (
    MAX_DOWNLOAD_BYTES,
    _check_gov_url,
    _decode_text,
    _expected_match_count,
    _find_json_records,
    _local_name,
    validate_payload,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "official_sources.json"
DEFAULT_DB_PATH = ROOT / "data" / "official_data" / "official_reference.db"
MOCK_DB_PATH = ROOT / "data" / "official_data" / "p0_mock_tariff.db"
DB_RESERVED_COLUMNS = {"record_id", "source_version_id", "source_row_number"}
DELIMITERS = (",", "\t", ";", "|")
TABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
DATASET_ID = re.compile(r"^\d+$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FORMAT_CONTENT_TYPES = {
    "CSV": "text/csv",
    "JSON": "application/json",
    "XML": "application/xml",
    "ZIP": "application/zip",
}
REQUIRED_SOURCE_METADATA = (
    "id",
    "name",
    "table",
    "format",
    "authority",
    "official_dataset_url",
    "content_type",
    "update_frequency",
    "license",
    "effective_from",
    "effective_to",
    "validation_rules",
    "fields",
    "keys",
)
ALLOWED_VALIDATION_RULES = {
    "required_fields",
    "nonempty_payload",
    "duplicate_headers",
    "unique_match_keys",
    "nonblank_match_keys",
    "exact_duplicate_rows",
    "valid_ccc_format",
    "zip_member_format",
    "law_identity_requires_url_and_dates",
}
VALIDATION_STATUSES = frozenset({
    "PASS",
    "CANDIDATE_PASS",
    "REVIEW",
    "FAIL",
    "MISSING",
    "IMPORT_FAIL",
})
REGISTRY_STATUSES = VALIDATION_STATUSES | {"ROLLBACK"}
REPARSE_POINT_ATTRIBUTE = getattr(
    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
)
REGISTRY_METADATA_COLUMNS = {
    "active_sha256": "TEXT",
    "authority": "TEXT",
    "official_dataset_url": "TEXT",
    "format": "TEXT",
    "content_type": "TEXT",
    "license_code": "TEXT",
    "license_name": "TEXT",
    "license_url": "TEXT",
    "declared_update_frequency": "TEXT",
    "effective_from": "TEXT",
    "effective_to": "TEXT",
    "validation_rules": "TEXT",
    "metadata_modified": "TEXT",
}
VERSION_METADATA_COLUMNS = {
    "content_digest": "TEXT",
    "authority": "TEXT",
    "official_dataset_url": "TEXT",
    "format": "TEXT",
    "content_type": "TEXT",
    "license_code": "TEXT",
    "license_name": "TEXT",
    "license_url": "TEXT",
    "declared_update_frequency": "TEXT",
    "effective_from": "TEXT",
    "effective_to": "TEXT",
    "validation_rules": "TEXT",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sources() -> list[dict[str, Any]]:
    sources = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not isinstance(sources, list) or not sources:
        raise ValueError("official source manifest must contain a non-empty list")
    dataset_ids: set[str] = set()
    tables: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("every official source manifest entry must be an object")
        _validate_source(source)
        dataset_id = str(source["id"])
        table = str(source["table"])
        if dataset_id in dataset_ids:
            raise ValueError(f"duplicate official dataset id {dataset_id}")
        if table in tables:
            raise ValueError(f"duplicate official source table {table}")
        dataset_ids.add(dataset_id)
        tables.add(table)
    return sources


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _require_text(source: dict[str, Any], field: str) -> str:
    value = source.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"invalid {field} for dataset {source.get('id')}")
    return value.strip()


def _validate_optional_date(source: dict[str, Any], field: str) -> None:
    value = source.get(field)
    if value is None:
        return
    if not isinstance(value, str) or not ISO_DATE.fullmatch(value):
        raise ValueError(f"invalid {field} for dataset {source.get('id')}")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid {field} for dataset {source.get('id')}") from exc


def _validate_source(source: dict[str, Any]) -> None:
    missing = [field for field in REQUIRED_SOURCE_METADATA if field not in source]
    if missing:
        raise ValueError(
            f"missing metadata {','.join(missing)} for dataset {source.get('id')}"
        )
    dataset_id = _require_text(source, "id")
    if not DATASET_ID.fullmatch(dataset_id):
        raise ValueError(f"dataset id must contain digits only: {dataset_id}")
    _require_text(source, "name")
    _require_text(source, "authority")
    _require_text(source, "update_frequency")

    table = source.get("table", "")
    fields = source.get("fields", [])
    if not TABLE_NAME.fullmatch(table):
        raise ValueError(f"invalid table name for dataset {source.get('id')}")
    if (
        not isinstance(fields, list)
        or not fields
        or any(not isinstance(field, str) or not field for field in fields)
        or len(fields) != len(set(fields))
    ):
        raise ValueError(f"invalid or duplicate source fields for dataset {source.get('id')}")
    if DB_RESERVED_COLUMNS.intersection(fields):
        raise ValueError(f"source fields conflict with database metadata for dataset {source.get('id')}")

    keys = source.get("keys", [])
    if (
        not isinstance(keys, list)
        or not keys
        or any(not isinstance(field, str) or field not in fields for field in keys)
        or len(keys) != len(set(keys))
    ):
        raise ValueError(f"invalid match keys for dataset {source.get('id')}")
    ccc_fields = source.get("ccc_fields", [])
    if (
        not isinstance(ccc_fields, list)
        or any(field not in fields for field in ccc_fields)
        or len(ccc_fields) != len(set(ccc_fields))
    ):
        raise ValueError(f"invalid CCC fields for dataset {source.get('id')}")
    nested_fields = source.get("nested_fields", [])
    if (
        not isinstance(nested_fields, list)
        or any(not isinstance(field, str) or not field for field in nested_fields)
        or len(nested_fields) != len(set(nested_fields))
    ):
        raise ValueError(f"invalid nested fields for dataset {source.get('id')}")
    if "keys_unique" in source and not isinstance(source["keys_unique"], bool):
        raise ValueError(f"keys_unique must be boolean for dataset {source.get('id')}")

    source_format = _require_text(source, "format").upper()
    content_type = _require_text(source, "content_type").lower()
    if source_format not in FORMAT_CONTENT_TYPES:
        raise ValueError(f"unsupported source format for dataset {source.get('id')}")
    if content_type != FORMAT_CONTENT_TYPES[source_format]:
        raise ValueError(
            f"content type does not match format for dataset {source.get('id')}"
        )

    try:
        _check_gov_url(_require_text(source, "official_dataset_url"))
        if source.get("download_url"):
            _check_gov_url(_require_text(source, "download_url"))
    except ValueError as exc:
        raise ValueError(
            f"invalid official URL for dataset {source.get('id')}: {exc}"
        ) from exc

    license_info = source.get("license")
    if not isinstance(license_info, dict):
        raise ValueError(f"invalid license metadata for dataset {source.get('id')}")
    for field in ("code", "name", "url"):
        if not isinstance(license_info.get(field), str) or not license_info[field].strip():
            raise ValueError(
                f"invalid license {field} for dataset {source.get('id')}"
            )
    try:
        _check_gov_url(license_info["url"])
    except ValueError as exc:
        raise ValueError(
            f"invalid license URL for dataset {source.get('id')}: {exc}"
        ) from exc

    _validate_optional_date(source, "effective_from")
    _validate_optional_date(source, "effective_to")
    if (
        source.get("effective_from")
        and source.get("effective_to")
        and source["effective_from"] > source["effective_to"]
    ):
        raise ValueError(f"effective dates are reversed for dataset {source.get('id')}")

    rules = source.get("validation_rules")
    if (
        not isinstance(rules, list)
        or not rules
        or any(not isinstance(rule, str) or rule not in ALLOWED_VALIDATION_RULES for rule in rules)
        or len(rules) != len(set(rules))
    ):
        raise ValueError(f"invalid validation rules for dataset {source.get('id')}")
    required_rules = {
        "required_fields",
        "nonempty_payload",
        "duplicate_headers",
        "exact_duplicate_rows",
    }
    if not required_rules.issubset(rules):
        raise ValueError(f"incomplete validation rules for dataset {source.get('id')}")
    if source_format == "ZIP" and "zip_member_format" not in rules:
        raise ValueError(f"ZIP validation rule missing for dataset {source.get('id')}")
    if ccc_fields and "valid_ccc_format" not in rules:
        raise ValueError(f"CCC validation rule missing for dataset {source.get('id')}")
    uniqueness_rule = (
        "unique_match_keys" if source.get("keys_unique", True) else "nonblank_match_keys"
    )
    if uniqueness_rule not in rules:
        raise ValueError(
            f"{uniqueness_rule} validation rule missing for dataset {source.get('id')}"
        )


def _validation_rules_json(source: dict[str, Any]) -> str:
    return json.dumps(source["validation_rules"], ensure_ascii=False, separators=(",", ":"))


def _source_metadata(source: dict[str, Any]) -> dict[str, Any]:
    license_info = source["license"]
    return {
        "authority": source["authority"],
        "official_dataset_url": source["official_dataset_url"],
        "format": source["format"].upper(),
        "content_type": source["content_type"].lower(),
        "license_code": license_info["code"],
        "license_name": license_info["name"],
        "license_url": license_info["url"],
        "declared_update_frequency": source["update_frequency"],
        "effective_from": source.get("effective_from"),
        "effective_to": source.get("effective_to"),
        "validation_rules": _validation_rules_json(source),
    }


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    existing = {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
    }
    for name, column_type in columns.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE {_quote(table)} ADD COLUMN {_quote(name)} {column_type}"
            )


def _consistent_active_version(
    connection: sqlite3.Connection,
    registry: sqlite3.Row,
    source: dict[str, Any],
    *,
    require_active_sha: bool = False,
) -> sqlite3.Row | None:
    """Return the active version only when its complete contents are consistent."""
    active_version_id = registry["active_version_id"]
    if not active_version_id:
        return None

    selected = ", ".join(
        _quote(column)
        for column in (*_version_digest_columns(), "content_digest")
    )
    version = connection.execute(
        f"""SELECT {selected}
           FROM official_dataset_versions
           WHERE version_id = ? AND dataset_id = ?""",
        (active_version_id, registry["dataset_id"]),
    ).fetchone()
    if not version or version["validation_status"] != "PASS":
        return None
    if version["version_id"] != f"{registry['dataset_id']}:{version['sha256']}":
        return None
    if registry["active_sha256"] != version["sha256"]:
        return None
    if not isinstance(version["record_count"], int) or isinstance(
        version["record_count"], bool
    ):
        return None

    table = _quote(str(source["table"]))
    view_name = _quote("active_" + str(source["table"]))
    view = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'view' AND name = ?",
        (f"active_{source['table']}",),
    ).fetchone()
    if not view or view["sql"] != _active_view_sql(source):
        return None
    view_columns = tuple(
        str(row["name"]) for row in connection.execute(f"PRAGMA table_info({view_name})")
    )
    expected_columns = tuple(
        ["record_id", "source_version_id", "source_row_number", *source["fields"]]
    )
    if view_columns != expected_columns:
        return None

    rows = connection.execute(
        f"""SELECT {", ".join(_quote(column) for column in expected_columns[1:])}
            FROM {table}
            WHERE source_version_id = ?
            ORDER BY source_row_number""",
        (active_version_id,),
    ).fetchall()
    row_values = [list(row) for row in rows]
    if (
        len(row_values) != version["record_count"]
        or any(row[1] != index for index, row in enumerate(row_values, start=1))
        or version["content_digest"] != _content_digest(source, version, row_values)
    ):
        return None
    return version


def _migrate_existing_metadata(
    connection: sqlite3.Connection,
    sources: list[dict[str, Any]],
) -> None:
    """Backfill descriptive metadata after verifying all stored integrity data."""
    metadata_fields = tuple(
        field for field in VERSION_METADATA_COLUMNS if field != "content_digest"
    )
    version_assignments = ", ".join(
        f"{_quote(field)} = COALESCE({_quote(field)}, ?)"
        for field in metadata_fields
    )
    versions = connection.execute(
        f"""SELECT {", ".join(
            _quote(column)
            for column in (*_version_digest_columns(), "content_digest")
        )}
            FROM official_dataset_versions
            ORDER BY dataset_id, version_id"""
    ).fetchall()
    sources_by_id = {str(source["id"]): source for source in sources}
    for version in versions:
        source = sources_by_id.get(str(version["dataset_id"]))
        if source is None:
            raise ValueError(
                "official database contains a version for an unknown dataset"
            )
        if version["content_digest"] is None:
            continue
        rows = connection.execute(
            f"""SELECT source_version_id, source_row_number,
                       {", ".join(_quote(field) for field in source["fields"])}
                FROM {_quote(source["table"])}
                WHERE source_version_id = ?
                ORDER BY source_row_number""",
            (version["version_id"],),
        ).fetchall()
        values = [list(row) for row in rows]
        if (
            len(values) != version["record_count"]
            or any(row[1] != index for index, row in enumerate(values, start=1))
            or version["content_digest"] != _content_digest(source, version, values)
        ):
            raise ValueError(
                f"stored content digest is inconsistent for {version['version_id']}"
            )

    for source in sources:
        metadata = _source_metadata(source)
        connection.execute(
            f"""UPDATE official_dataset_versions
                SET {version_assignments}
                WHERE dataset_id = ?""",
            (*(metadata[field] for field in metadata_fields), str(source["id"])),
        )

    registry_rows = connection.execute(
        """SELECT dataset_id, source_table, active_version_id, active_sha256,
                  metadata_modified
           FROM official_dataset_registry
           WHERE active_version_id IS NOT NULL"""
    ).fetchall()
    for registry in registry_rows:
        source = sources_by_id.get(str(registry["dataset_id"]))
        if source is None or str(source["table"]) != registry["source_table"]:
            continue
        version = _consistent_active_version(connection, registry, source)
        if version is None:
            continue
        if registry["metadata_modified"] is None:
            connection.execute(
                """UPDATE official_dataset_registry
                   SET metadata_modified = (
                       SELECT metadata_modified
                       FROM official_dataset_versions
                       WHERE version_id = ?
                   )
                   WHERE dataset_id = ? AND metadata_modified IS NULL""",
                (registry["active_version_id"], registry["dataset_id"]),
            )


def _version_digest_columns() -> tuple[str, ...]:
    return (
        "version_id",
        "dataset_id",
        "sha256",
        "source_url",
        "metadata_modified",
        "update_frequency",
        "fetched_at",
        "validated_at",
        "record_count",
        "validation_status",
        *(
            field
            for field in VERSION_METADATA_COLUMNS
            if field != "content_digest"
        ),
    )


def _active_view_sql(source: dict[str, Any]) -> str:
    dataset_id = str(source["id"]).replace("'", "''")
    table = _quote(str(source["table"]))
    view = _quote("active_" + str(source["table"]))
    columns = ["record_id", "source_version_id", "source_row_number", *source["fields"]]
    selected = ", ".join(f"d.{_quote(column)}" for column in columns)
    return (
        f"CREATE VIEW {view} AS\n"
        f"                    SELECT {selected}\n"
        f"                    FROM {table} AS d\n"
        "                    JOIN official_dataset_registry AS r\n"
        f"                     ON r.dataset_id = '{dataset_id}'\n"
        "                     AND r.active_version_id = d.source_version_id"
    )


def _content_digest(
    source: dict[str, Any],
    version: sqlite3.Row | dict[str, Any],
    rows: list[list[Any]],
) -> str:
    """Hash complete version metadata plus ordered source rows."""
    canonical = {
        "schema_version": 3,
        "dataset_id": str(source["id"]),
        "fields": list(source["fields"]),
        "version": {
            column: version[column]
            for column in _version_digest_columns()
        },
        "rows": rows,
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _backfill_legacy_content_digest(
    connection: sqlite3.Connection,
    registry: sqlite3.Row,
    source: dict[str, Any],
    existing: sqlite3.Row,
    records: list[dict[str, Any]],
    sha256: str,
) -> sqlite3.Row | None:
    """Backfill a legacy digest only after proving the active rows are intact."""
    if existing["content_digest"] is not None:
        return None
    if (
        existing["validation_status"] != "PASS"
        or existing["dataset_id"] != registry["dataset_id"]
        or existing["sha256"] != sha256
        or registry["active_version_id"] != existing["version_id"]
        or registry["active_sha256"] != sha256
    ):
        return None

    fields = source["fields"]
    expected_rows = [
        [existing["version_id"], index, *(_as_text(row.get(field)) for field in fields)]
        for index, row in enumerate(records, start=1)
    ]
    stored_rows = connection.execute(
        f"""SELECT source_version_id, source_row_number,
                   {", ".join(_quote(field) for field in fields)}
            FROM {_quote(source['table'])}
            WHERE source_version_id = ?
            ORDER BY source_row_number""",
        (existing["version_id"],),
    ).fetchall()
    stored_values = [list(row) for row in stored_rows]
    if (
        stored_values != expected_rows
        or existing["record_count"] != len(expected_rows)
        or existing["version_id"] != f"{source['id']}:{sha256}"
    ):
        return None

    digest = _content_digest(source, existing, expected_rows)
    cursor = connection.execute(
        """UPDATE official_dataset_versions
           SET content_digest = ?
           WHERE version_id = ? AND content_digest IS NULL""",
        (digest, existing["version_id"]),
    )
    if cursor.rowcount != 1:
        return None
    return connection.execute(
        """SELECT version_id, dataset_id, sha256, source_url,
                  metadata_modified, update_frequency, fetched_at,
                  validated_at, record_count, validation_status,
                  content_digest, authority, official_dataset_url, format,
                  content_type, license_code, license_name, license_url,
                  declared_update_frequency, effective_from, effective_to,
                  validation_rules
           FROM official_dataset_versions
           WHERE version_id = ?""",
        (existing["version_id"],),
    ).fetchone()


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _older_pass_is_safe_to_apply(
    last_status: str | None,
    last_check_at: str | None,
    validated_at: str,
    *,
    has_active_version: bool = False,
) -> bool:
    if has_active_version:
        return False
    if last_status is None and last_check_at is None:
        return True
    if not last_status or not last_check_at:
        return False
    if last_status not in REGISTRY_STATUSES:
        return False
    last_check = _parse_timestamp(last_check_at)
    candidate = _parse_timestamp(validated_at)
    if last_check is None or candidate is None:
        return False
    return candidate > last_check


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_POINT_ATTRIBUTE
    )


def _file_identity(path: Path | str) -> tuple[int, int] | None:
    """Return the current file identity without following a final symlink."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if _is_link_or_reparse(info):
        return None
    return info.st_dev, info.st_ino


def _assert_distinct_file_identity(
    official_path: Path | str,
    mock_path: Path | str | None = None,
) -> None:
    """Reject path, symlink, junction, and hardlink aliases of the mock DB."""
    official = Path(official_path)
    reference = Path(mock_path if mock_path is not None else MOCK_DB_PATH)
    official_resolved = official.resolve(strict=False)
    reference_resolved = reference.resolve(strict=False)
    if os.path.normcase(str(official_resolved)) == os.path.normcase(
        str(reference_resolved)
    ):
        raise ValueError(
            "official reference database must not use the P0 mock database path"
        )

    try:
        official_info = os.lstat(official)
    except FileNotFoundError:
        return
    if _is_link_or_reparse(official_info):
        raise ValueError("official reference database must not be a link or junction")
    if not stat.S_ISREG(official_info.st_mode):
        raise ValueError("official reference database path must be a regular file")

    official_identity = _file_identity(official)
    mock_identity = _file_identity(reference)
    if official_identity is not None and official_identity == mock_identity:
        raise ValueError(
            "official reference database must not share a file with the P0 mock database"
        )


def _read_snapshot_file(
    path: Path | str,
    *,
    root: Path | str,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> bytes:
    """Read one regular snapshot exactly once while bounding path and size."""
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError("snapshot path must be absolute")
    try:
        root_path = Path(root)
        root_before = os.lstat(root_path)
        approved_root = root_path.resolve(strict=True)
        before = os.lstat(candidate)
    except OSError as exc:
        raise ValueError("snapshot file or approved source directory is unavailable") from exc
    if (
        _is_link_or_reparse(root_before)
        or not stat.S_ISDIR(root_before.st_mode)
        or _is_link_or_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError("snapshot path must be a regular file")
    try:
        if not os.path.samefile(root_path, approved_root):
            raise ValueError("approved source directory changed while being resolved")
    except OSError as exc:
        raise ValueError("approved source directory is unavailable") from exc

    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("snapshot file is unavailable") from exc
    if resolved != approved_root and approved_root not in resolved.parents:
        raise ValueError("snapshot file is outside the approved source directory")

    try:
        canonical = os.lstat(resolved)
    except OSError as exc:
        raise ValueError("snapshot file is unavailable") from exc
    if (
        _is_link_or_reparse(canonical)
        or not stat.S_ISREG(canonical.st_mode)
        or canonical.st_nlink != 1
    ):
        raise ValueError("snapshot path must be a regular file")
    if (canonical.st_dev, canonical.st_ino) != (before.st_dev, before.st_ino):
        raise ValueError("snapshot file changed while being resolved")
    before = canonical
    if before.st_size > max_bytes:
        raise ValueError("snapshot exceeds configured size limit")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ValueError("snapshot file could not be opened safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            _is_link_or_reparse(opened)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size != before.st_size
            or opened.st_mtime_ns != before.st_mtime_ns
        ):
            raise ValueError("snapshot file changed while being opened")
        if opened.st_size > max_bytes:
            raise ValueError("snapshot exceeds configured size limit")

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if len(raw) > max_bytes:
            raise ValueError("snapshot exceeds configured size limit")
        if (
            len(raw) != opened.st_size
            or after.st_size != opened.st_size
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ValueError("snapshot file changed while being read")
        return raw
    finally:
        os.close(descriptor)


def _reconcile_validation_report(
    source: dict[str, Any],
    raw: bytes,
    validation: dict[str, Any],
    *,
    validated_at: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Require a complete PASS receipt that matches the exact snapshot bytes."""
    if not isinstance(validation, dict):
        raise ValueError("validated snapshot receipt must be an object")
    dataset_id = str(source["id"])
    if str(validation.get("id")) != dataset_id:
        raise ValueError("validated snapshot dataset id does not match the source")
    if validation.get("dataset_id") != dataset_id:
        raise ValueError("validated snapshot dataset id does not match the source")
    if validation.get("snapshot_origin") != "source_snapshot":
        raise ValueError("only an approved source snapshot can be imported")
    if validation.get("validation_rules") != list(source["validation_rules"]):
        raise ValueError("validated snapshot rules do not match the source manifest")

    sha256 = hashlib.sha256(raw).hexdigest()
    if validation.get("sha256") != sha256:
        raise ValueError("validated snapshot digest does not match the imported bytes")
    size_bytes = validation.get("size_bytes")
    if (
        not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes != len(raw)
    ):
        raise ValueError("validated snapshot size does not match the imported bytes")

    receipt_time = validation.get("validated_at")
    receipt_timestamp = _parse_timestamp(receipt_time)
    if receipt_timestamp is None:
        raise ValueError("validated snapshot receipt has no valid timestamp")
    if validated_at is not None:
        requested_timestamp = _parse_timestamp(validated_at)
        if requested_timestamp is None or requested_timestamp != receipt_timestamp:
            raise ValueError("validated_at does not match the validation receipt")
    now = validated_at or receipt_time

    fresh = validate_payload(raw, source)
    if fresh.get("status") != "PASS":
        raise ValueError("only a freshly validated PASS snapshot can be imported")
    for key, expected in fresh.items():
        if key not in validation or validation.get(key) != expected:
            raise ValueError(
                f"validated snapshot receipt does not match fresh validation: {key}"
            )
    return fresh, now


def preflight_validated_snapshot(
    source: dict[str, Any],
    raw: bytes,
    validation: dict[str, Any],
    *,
    validated_at: str | None = None,
) -> dict[str, Any]:
    """Validate all import inputs without opening or mutating a database."""
    _validate_source(source)
    fresh, receipt_time = _reconcile_validation_report(
        source, raw, validation, validated_at=validated_at
    )
    records = _payload_records(raw, source)
    if len(records) != fresh["row_count"]:
        raise ValueError("parsed record count differs from validation result")
    sha256 = hashlib.sha256(raw).hexdigest()
    dataset_id = str(source["id"])
    return {
        "dataset_id": dataset_id,
        "version_id": f"{dataset_id}:{sha256}",
        "sha256": sha256,
        "validated_at": receipt_time,
        "records": records,
        "fresh_validation": fresh,
        "metadata": _source_metadata(source),
    }


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    requested = Path(db_path)
    _assert_distinct_file_identity(requested)
    path = requested.resolve(strict=False)
    _assert_distinct_file_identity(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, factory=_ClosingConnection)
    try:
        connection.row_factory = sqlite3.Row
        opened_path = connection.execute("PRAGMA database_list").fetchone()[2]
        if not opened_path or Path(opened_path).resolve(strict=False) != path:
            raise ValueError("SQLite opened an unexpected official database path")
        _assert_distinct_file_identity(path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection
    except Exception:
        connection.close()
        raise


def initialize_database(
    db_path: Path | str = DEFAULT_DB_PATH,
    sources: list[dict[str, Any]] | None = None,
) -> Path:
    """Create missing schema objects without dropping data or replacing files."""
    source_list = sources if sources is not None else load_sources()
    for source in source_list:
        _validate_source(source)

    with connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS official_dataset_registry (
                dataset_id TEXT PRIMARY KEY,
                dataset_name TEXT NOT NULL,
                source_table TEXT NOT NULL,
                active_version_id TEXT,
                last_check_at TEXT,
                last_status TEXT,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS official_dataset_versions (
                version_id TEXT PRIMARY KEY,
                dataset_id TEXT NOT NULL REFERENCES official_dataset_registry(dataset_id),
                sha256 TEXT NOT NULL,
                content_digest TEXT,
                source_url TEXT,
                metadata_modified TEXT,
                update_frequency TEXT,
                fetched_at TEXT NOT NULL,
                validated_at TEXT NOT NULL,
                record_count INTEGER NOT NULL CHECK (record_count > 0),
                validation_status TEXT NOT NULL CHECK (validation_status = 'PASS'),
                UNIQUE(dataset_id, sha256)
            );
            CREATE INDEX IF NOT EXISTS idx_official_versions_dataset
                ON official_dataset_versions(dataset_id, fetched_at);
            """
        )
        _ensure_columns(
            connection, "official_dataset_registry", REGISTRY_METADATA_COLUMNS
        )
        _ensure_columns(
            connection, "official_dataset_versions", VERSION_METADATA_COLUMNS
        )

        for source in source_list:
            dataset_id = str(source["id"])
            table = str(source["table"])
            metadata = _source_metadata(source)
            connection.execute(
                """INSERT INTO official_dataset_registry
                   (dataset_id, dataset_name, source_table, authority,
                    official_dataset_url, format, content_type, license_code,
                    license_name, license_url, declared_update_frequency,
                    effective_from, effective_to, validation_rules)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(dataset_id) DO UPDATE SET
                     dataset_name = excluded.dataset_name,
                     source_table = excluded.source_table,
                     authority = COALESCE(official_dataset_registry.authority, excluded.authority),
                     official_dataset_url = COALESCE(official_dataset_registry.official_dataset_url, excluded.official_dataset_url),
                     format = COALESCE(official_dataset_registry.format, excluded.format),
                     content_type = COALESCE(official_dataset_registry.content_type, excluded.content_type),
                     license_code = COALESCE(official_dataset_registry.license_code, excluded.license_code),
                     license_name = COALESCE(official_dataset_registry.license_name, excluded.license_name),
                     license_url = COALESCE(official_dataset_registry.license_url, excluded.license_url),
                     declared_update_frequency = COALESCE(official_dataset_registry.declared_update_frequency, excluded.declared_update_frequency),
                     effective_from = COALESCE(official_dataset_registry.effective_from, excluded.effective_from),
                     effective_to = COALESCE(official_dataset_registry.effective_to, excluded.effective_to),
                     validation_rules = COALESCE(official_dataset_registry.validation_rules, excluded.validation_rules)""",
                (
                    dataset_id,
                    source["name"],
                    table,
                    metadata["authority"],
                    metadata["official_dataset_url"],
                    metadata["format"],
                    metadata["content_type"],
                    metadata["license_code"],
                    metadata["license_name"],
                    metadata["license_url"],
                    metadata["declared_update_frequency"],
                    metadata["effective_from"],
                    metadata["effective_to"],
                    metadata["validation_rules"],
                ),
            )
            quoted_table = _quote(table)
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {quoted_table} (
                    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_version_id TEXT NOT NULL
                        REFERENCES official_dataset_versions(version_id),
                    source_row_number INTEGER NOT NULL,
                    UNIQUE(source_version_id, source_row_number)
                )"""
            )
            existing = {
                row["name"]
                for row in connection.execute(f"PRAGMA table_info({quoted_table})")
            }
            for field in source["fields"]:
                if field not in existing:
                    connection.execute(
                        f"ALTER TABLE {quoted_table} ADD COLUMN {_quote(field)} TEXT"
                    )
            connection.execute(
                f"CREATE INDEX IF NOT EXISTS {_quote('idx_' + table + '_version')} "
                f"ON {quoted_table}(source_version_id)"
            )
            for index, field in enumerate(source.get("keys", []), start=1):
                if field in source["fields"]:
                    connection.execute(
                        f"CREATE INDEX IF NOT EXISTS {_quote(f'idx_{table}_key_{index}')} "
                        f"ON {quoted_table}(source_version_id, {_quote(field)})"
                    )

            view = _quote("active_" + table)
            connection.execute(f"DROP VIEW IF EXISTS {view}")
            connection.execute(_active_view_sql(source))

        _migrate_existing_metadata(connection, source_list)
        connection.execute("PRAGMA user_version = 3")
    return Path(db_path).resolve()


def record_validation_result(
    db_path: Path | str,
    dataset_id: str,
    status: str,
    error: str | None = None,
) -> None:
    if status not in VALIDATION_STATUSES:
        raise ValueError(f"invalid validation status {status}")
    with connect(db_path) as connection:
        cursor = connection.execute(
            """UPDATE official_dataset_registry
               SET last_check_at = ?, last_status = ?, last_error = ?
               WHERE dataset_id = ?""",
            (utc_now(), status, error, str(dataset_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"unknown official dataset id {dataset_id}")


def _parse_xml_records(raw: bytes, source: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    root = ElementTree.fromstring(raw)
    groups: list[tuple[int, int, int, ElementTree.Element, ElementTree.Element]] = []
    for parent in root.iter():
        by_tag: dict[str, list[ElementTree.Element]] = {}
        for child in list(parent):
            by_tag.setdefault(child.tag, []).append(child)
        for siblings in by_tag.values():
            sample = siblings[0]
            if list(sample):
                header = [_local_name(child.tag) for child in list(sample)]
                groups.append((
                    _expected_match_count(header, source["fields"]),
                    len(header),
                    len(siblings),
                    parent,
                    sample,
                ))
    if groups:
        _, _, _, parent, sample = max(groups, key=lambda item: item[:3])
        header = [_local_name(child.tag) for child in list(sample)]
        records = []
        for record in list(parent):
            if record.tag != sample.tag:
                continue
            row: dict[str, Any] = {}
            for child in list(record):
                name = _local_name(child.tag)
                if name in source.get("nested_fields", []):
                    row[name] = ElementTree.tostring(child, encoding="unicode")
                else:
                    row[name] = "".join(child.itertext()).strip()
            records.append(row)
        return header, records

    header = [_local_name(child.tag) for child in list(root)]
    if not header:
        raise ValueError("XML contains no record fields")
    row = {}
    for child in list(root):
        name = _local_name(child.tag)
        row[name] = (
            ElementTree.tostring(child, encoding="unicode")
            if name in source.get("nested_fields", [])
            else "".join(child.itertext()).strip()
        )
    return header, [row]


def _parse_member(raw: bytes, source: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    probe = raw.lstrip(b"\xef\xbb\xbf\t\r\n ")
    if probe.startswith(b"{") or probe.startswith(b"["):
        records = _find_json_records(json.loads(_decode_text(raw)))
        if records is None:
            raise ValueError("JSON contains no list of record objects")
        return list(records[0].keys()), records
    if probe.startswith(b"<"):
        return _parse_xml_records(raw, source)

    text = _decode_text(raw)
    choices = []
    for delimiter in DELIMITERS:
        header = next(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), [])
        choices.append((
            _expected_match_count(header, source["fields"]),
            len(header),
            delimiter,
        ))
    _, _, delimiter = max(choices)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    records = [
        row
        for row in reader
        if row and any(value is not None and str(value).strip() for value in row.values())
    ]
    return list(reader.fieldnames or []), records


def _payload_records(raw: bytes, source: dict[str, Any]) -> list[dict[str, Any]]:
    if raw.startswith(b"PK\x03\x04"):
        candidates = []
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = [item for item in archive.infolist() if not item.is_dir()]
            if sum(item.file_size for item in infos) > 512 * 1024 * 1024:
                raise ValueError("archive expands beyond configured size limit")
            for info in infos:
                if Path(info.filename).suffix.lower() not in {".csv", ".json", ".xml"}:
                    continue
                try:
                    header, rows = _parse_member(archive.read(info), source)
                except (UnicodeDecodeError, ValueError, json.JSONDecodeError, ElementTree.ParseError):
                    continue
                candidates.append((
                    _expected_match_count(header, source["fields"]),
                    len(rows),
                    info.file_size,
                    rows,
                ))
        if not candidates:
            raise ValueError("ZIP contains no supported data members")
        return max(candidates, key=lambda item: item[:3])[3]
    return _parse_member(raw, source)[1]


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def import_validated_snapshot(
    db_path: Path | str,
    source: dict[str, Any],
    raw: bytes,
    validation: dict[str, Any],
    *,
    source_url: str | None = None,
    metadata_modified: str | None = None,
    update_frequency: str | None = None,
    validated_at: str | None = None,
) -> dict[str, Any]:
    """Import one PASS snapshot atomically and activate its immutable version."""
    prepared = preflight_validated_snapshot(
        source, raw, validation, validated_at=validated_at
    )
    dataset_id = prepared["dataset_id"]
    table = _quote(source["table"])
    sha256 = prepared["sha256"]
    version_id = prepared["version_id"]
    records = prepared["records"]
    now = prepared["validated_at"]
    metadata = prepared["metadata"]

    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            """SELECT version_id, dataset_id, sha256, source_url,
                      metadata_modified, update_frequency, fetched_at,
                      validated_at, record_count, validation_status,
                      content_digest, authority, official_dataset_url, format,
                      content_type, license_code, license_name, license_url,
                      declared_update_frequency, effective_from, effective_to,
                      validation_rules
               FROM official_dataset_versions
               WHERE dataset_id = ? AND sha256 = ?""",
            (dataset_id, sha256),
        ).fetchone()
        if existing:
            if existing["version_id"] != version_id:
                raise ValueError("stored version id is inconsistent with its digest")
            registry = connection.execute(
                """SELECT dataset_id, source_table, active_version_id,
                          active_sha256, metadata_modified
                   FROM official_dataset_registry WHERE dataset_id = ?""",
                (dataset_id,),
            ).fetchone()
            if registry and existing["content_digest"] is None:
                existing = (
                    _backfill_legacy_content_digest(
                        connection,
                        registry,
                        source,
                        existing,
                        records,
                        sha256,
                    )
                    or existing
                )
            active = (
                _consistent_active_version(connection, registry, source)
                if registry
                else None
            )
            if (
                active is None
                or active["version_id"] != version_id
                or existing["content_digest"] != active["content_digest"]
            ):
                raise ValueError("existing snapshot is not the verified active version")
            checked_at = utc_now()
            cursor = connection.execute(
                """UPDATE official_dataset_registry
                   SET last_check_at = ?, last_status = 'PASS', last_error = NULL
                   WHERE dataset_id = ?""",
                (checked_at, dataset_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"official database schema is missing dataset {dataset_id}")
            return {
                "dataset_id": dataset_id,
                "version_id": version_id,
                "sha256": sha256,
                "row_count": len(records),
                "new_version": False,
                "activated": False,
                "reason": "same_snapshot_immutable_noop",
            }
        registry = connection.execute(
            """SELECT last_status, last_check_at
               FROM official_dataset_registry WHERE dataset_id = ?""",
            (dataset_id,),
        ).fetchone()
        if not registry:
            raise ValueError(f"official database schema is missing dataset {dataset_id}")
        if not _older_pass_is_safe_to_apply(
            registry["last_status"], registry["last_check_at"], now
        ):
            return {
                "dataset_id": dataset_id,
                "version_id": version_id,
                "sha256": sha256,
                "row_count": len(records),
                "new_version": False,
                "activated": False,
                "reason": "newer_validation_result_preserved",
            }
        fields = source["fields"]
        row_values = [
            [version_id, index, *(_as_text(row.get(field)) for field in fields)]
            for index, row in enumerate(records, start=1)
        ]
        version_record = {
            "version_id": version_id,
            "dataset_id": dataset_id,
            "sha256": sha256,
            "source_url": source_url,
            "metadata_modified": metadata_modified,
            "update_frequency": update_frequency,
            "fetched_at": now,
            "validated_at": now,
            "record_count": len(records),
            "validation_status": "PASS",
            **metadata,
            "content_digest": None,
        }
        digest = _content_digest(source, version_record, row_values)
        connection.execute(
            """INSERT INTO official_dataset_versions
               (version_id, dataset_id, sha256, content_digest, source_url, metadata_modified,
                update_frequency, fetched_at, validated_at, record_count,
                validation_status, authority, official_dataset_url, format,
                content_type, license_code, license_name, license_url,
                declared_update_frequency, effective_from, effective_to,
                validation_rules)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PASS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                version_id, dataset_id, sha256, digest, source_url, metadata_modified,
                update_frequency, now, now, len(records),
                metadata["authority"],
                metadata["official_dataset_url"],
                metadata["format"],
                metadata["content_type"],
                metadata["license_code"],
                metadata["license_name"],
                metadata["license_url"],
                metadata["declared_update_frequency"],
                metadata["effective_from"],
                metadata["effective_to"],
                metadata["validation_rules"],
            ),
        )
        columns = ["source_version_id", "source_row_number", *fields]
        insert_sql = (
            f"INSERT INTO {table} ({', '.join(_quote(column) for column in columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})"
        )
        connection.executemany(insert_sql, row_values)
        cursor = connection.execute(
            """UPDATE official_dataset_registry
               SET active_version_id = ?, active_sha256 = ?,
                   metadata_modified = ?, last_check_at = ?,
                   last_status = 'PASS', last_error = NULL
               WHERE dataset_id = ?""",
            (version_id, sha256, metadata_modified, now, dataset_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"official database schema is missing dataset {dataset_id}")
        activated = connection.execute(
            """SELECT dataset_id, source_table, active_version_id,
                      active_sha256, metadata_modified
               FROM official_dataset_registry WHERE dataset_id = ?""",
            (dataset_id,),
        ).fetchone()
        verified = (
            _consistent_active_version(connection, activated, source)
            if activated
            else None
        )
        if verified is None or verified["version_id"] != version_id:
            raise ValueError("imported version failed active-content verification")
    return {"dataset_id": dataset_id, "version_id": version_id, "sha256": sha256,
            "row_count": len(records), "new_version": True, "activated": True}


def rollback_dataset(
    db_path: Path | str, dataset_id: str, version_id: str
) -> None:
    sources = {str(source["id"]): source for source in load_sources()}
    with connect(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        registry = connection.execute(
            """SELECT dataset_id, source_table, active_version_id, active_sha256
               FROM official_dataset_registry WHERE dataset_id = ?""",
            (str(dataset_id),),
        ).fetchone()
        if not registry:
            raise ValueError(f"unknown official dataset id {dataset_id}")
        source = sources.get(str(dataset_id))
        if source is None or registry["source_table"] != str(source["table"]):
            raise ValueError("registry source table does not match the official manifest")
        selected = ", ".join(
            _quote(column) for column in _version_digest_columns()
        )
        version = connection.execute(
            f"""SELECT {selected}, content_digest
                FROM official_dataset_versions
                WHERE dataset_id = ? AND version_id = ?
                  AND validation_status = 'PASS'""",
            (str(dataset_id), version_id),
        ).fetchone()
        if not version:
            raise ValueError("version is not a PASS version of the requested dataset")
        if (
            version["dataset_id"] != str(dataset_id)
            or version["version_id"] != f"{dataset_id}:{version['sha256']}"
            or not isinstance(version["record_count"], int)
            or isinstance(version["record_count"], bool)
            or version["record_count"] <= 0
        ):
            raise ValueError("PASS version metadata is inconsistent")
        target_rows = connection.execute(
            f"""SELECT source_version_id, source_row_number,
                       {", ".join(_quote(field) for field in source["fields"])}
                FROM {_quote(source['table'])}
                WHERE source_version_id = ?
                ORDER BY source_row_number""",
            (version_id,),
        ).fetchall()
        target_values = [list(row) for row in target_rows]
        if (
            len(target_values) != version["record_count"]
            or any(
                row[1] != index
                for index, row in enumerate(target_values, start=1)
            )
            or version["content_digest"] != _content_digest(source, version, target_values)
        ):
            raise ValueError("target version contents are inconsistent")
        current = _consistent_active_version(
            connection,
            registry,
            source,
            require_active_sha=True,
        )
        if current is None:
            raise ValueError("current active version or view is inconsistent")
        cursor = connection.execute(
            """UPDATE official_dataset_registry
               SET active_version_id = ?, active_sha256 = ?, last_check_at = ?,
                   last_status = 'ROLLBACK', last_error = NULL
               WHERE dataset_id = ?""",
            (version_id, version["sha256"], utc_now(), str(dataset_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"unknown official dataset id {dataset_id}")
        activated = connection.execute(
            """SELECT dataset_id, source_table, active_version_id,
                      active_sha256, metadata_modified
               FROM official_dataset_registry WHERE dataset_id = ?""",
            (str(dataset_id),),
        ).fetchone()
        verified = (
            _consistent_active_version(connection, activated, source)
            if activated
            else None
        )
        if verified is None or verified["version_id"] != version_id:
            raise ValueError("rollback target failed active-content verification")
