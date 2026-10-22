"""Versioned SQLite storage for independently validated official datasets."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import zipfile
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from scripts.validate_official_snapshot import (
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
VALIDATION_STATUSES = frozenset({"PASS", "REVIEW", "FAIL", "MISSING", "IMPORT_FAIL"})
FAILURE_STATUSES = frozenset({"FAIL", "IMPORT_FAIL"})
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
) -> sqlite3.Row | None:
    """Return the active version only when all pointer and row-count checks pass."""
    active_version_id = registry["active_version_id"]
    if not active_version_id:
        return None

    version = connection.execute(
        """SELECT version_id, dataset_id, sha256, record_count, validation_status
           FROM official_dataset_versions
           WHERE version_id = ? AND dataset_id = ?""",
        (active_version_id, registry["dataset_id"]),
    ).fetchone()
    if not version or version["validation_status"] != "PASS":
        return None
    if version["version_id"] != f"{registry['dataset_id']}:{version['sha256']}":
        return None

    table = _quote(str(source["table"]))
    view_name = _quote("active_" + str(source["table"]))
    view_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = ?",
        (f"active_{source['table']}",),
    ).fetchone()
    if not view_exists:
        return None
    source_count = connection.execute(
        f"SELECT COUNT(*) FROM {table} WHERE source_version_id = ?",
        (active_version_id,),
    ).fetchone()[0]
    view_count = connection.execute(f"SELECT COUNT(*) FROM {view_name}").fetchone()[0]
    if not (
        source_count == version["record_count"]
        and view_count == version["record_count"]
    ):
        return None
    return version


def _migrate_existing_metadata(
    connection: sqlite3.Connection,
    sources: list[dict[str, Any]],
) -> None:
    """Backfill only NULL metadata and leave active pointers and statuses untouched."""
    metadata_fields = tuple(VERSION_METADATA_COLUMNS)
    version_assignments = ", ".join(
        f"{_quote(field)} = COALESCE({_quote(field)}, ?)"
        for field in metadata_fields
    )
    for source in sources:
        metadata = _source_metadata(source)
        connection.execute(
            f"""UPDATE official_dataset_versions
                SET {version_assignments}
                WHERE dataset_id = ?""",
            (*(metadata[field] for field in metadata_fields), str(source["id"])),
        )

    sources_by_id = {str(source["id"]): source for source in sources}
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
        if registry["active_sha256"] is None:
            connection.execute(
                """UPDATE official_dataset_registry
                   SET active_sha256 = ?
                   WHERE dataset_id = ? AND active_sha256 IS NULL""",
                (version["sha256"], registry["dataset_id"]),
            )
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


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
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
) -> bool:
    if last_status not in FAILURE_STATUSES or not last_check_at:
        return True
    last_check = _parse_timestamp(last_check_at)
    candidate = _parse_timestamp(validated_at)
    if last_check is None or candidate is None:
        return False
    return candidate >= last_check


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path).resolve()
    if path == MOCK_DB_PATH.resolve():
        raise ValueError("official reference database must not use the P0 mock database path")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30, factory=_ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


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
            columns = ["record_id", "source_version_id", "source_row_number", *source["fields"]]
            selected = ", ".join(f"d.{_quote(column)}" for column in columns)
            connection.execute(
                f"""CREATE VIEW {view} AS
                    SELECT {selected}
                    FROM {quoted_table} AS d
                    JOIN official_dataset_registry AS r
                     ON r.dataset_id = '{dataset_id.replace("'", "''")}'
                     AND r.active_version_id = d.source_version_id"""
            )

        _migrate_existing_metadata(connection, source_list)
        connection.execute("PRAGMA user_version = 2")
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
    _validate_source(source)
    fresh_validation = validate_payload(raw, source)
    if validation.get("status") != "PASS" or fresh_validation["status"] != "PASS":
        raise ValueError("only a freshly validated PASS snapshot can be imported")

    dataset_id = str(source["id"])
    table = _quote(source["table"])
    sha256 = hashlib.sha256(raw).hexdigest()
    version_id = f"{dataset_id}:{sha256}"
    records = _payload_records(raw, source)
    if len(records) != fresh_validation["row_count"]:
        raise ValueError("parsed record count differs from validation result")
    now = validated_at or utc_now()
    if _parse_timestamp(now) is None:
        raise ValueError("validated_at must be an ISO-8601 timestamp")
    metadata = _source_metadata(source)

    with connect(db_path) as connection:
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
        existing = connection.execute(
            "SELECT version_id FROM official_dataset_versions WHERE dataset_id = ? AND sha256 = ?",
            (dataset_id, sha256),
        ).fetchone()
        if not existing:
            connection.execute(
                """INSERT INTO official_dataset_versions
                   (version_id, dataset_id, sha256, source_url, metadata_modified,
                    update_frequency, fetched_at, validated_at, record_count,
                    validation_status, authority, official_dataset_url, format,
                    content_type, license_code, license_name, license_url,
                    declared_update_frequency, effective_from, effective_to,
                    validation_rules)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PASS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version_id, dataset_id, sha256, source_url, metadata_modified,
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
            fields = source["fields"]
            columns = ["source_version_id", "source_row_number", *fields]
            insert_sql = (
                f"INSERT INTO {table} ({', '.join(_quote(column) for column in columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})"
            )
            values = [
                (version_id, index, *(_as_text(row.get(field)) for field in fields))
                for index, row in enumerate(records, start=1)
            ]
            connection.executemany(insert_sql, values)
            imported = True
        else:
            version_id = existing["version_id"]
            connection.execute(
                """UPDATE official_dataset_versions
                   SET source_url = ?, metadata_modified = ?, update_frequency = ?,
                       validated_at = ?, validation_status = 'PASS',
                       authority = ?, official_dataset_url = ?, format = ?,
                       content_type = ?, license_code = ?, license_name = ?,
                       license_url = ?, declared_update_frequency = ?,
                       effective_from = ?, effective_to = ?, validation_rules = ?
                   WHERE version_id = ?""",
                (
                    source_url,
                    metadata_modified,
                    update_frequency,
                    now,
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
                    version_id,
                ),
            )
            imported = False
        connection.execute(
            """UPDATE official_dataset_registry
               SET active_version_id = ?, active_sha256 = ?,
                   metadata_modified = ?, last_check_at = ?,
                   last_status = 'PASS', last_error = NULL
               WHERE dataset_id = ?""",
            (version_id, sha256, metadata_modified, now, dataset_id),
        )
    return {"dataset_id": dataset_id, "version_id": version_id, "sha256": sha256,
            "row_count": len(records), "new_version": imported, "activated": True}


def rollback_dataset(
    db_path: Path | str, dataset_id: str, version_id: str
) -> None:
    with connect(db_path) as connection:
        version = connection.execute(
            """SELECT sha256 FROM official_dataset_versions
               WHERE dataset_id = ? AND version_id = ?
                 AND validation_status = 'PASS'""",
            (str(dataset_id), version_id),
        ).fetchone()
        if not version:
            raise ValueError("version is not a PASS version of the requested dataset")
        cursor = connection.execute(
            """UPDATE official_dataset_registry
               SET active_version_id = ?, active_sha256 = ?, last_check_at = ?,
                   last_status = 'ROLLBACK', last_error = NULL
               WHERE dataset_id = ?""",
            (version_id, version["sha256"], utc_now(), str(dataset_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"unknown official dataset id {dataset_id}")
