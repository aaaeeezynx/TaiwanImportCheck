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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from scripts.validate_official_snapshot import (
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_sources() -> list[dict[str, Any]]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _validate_source(source: dict[str, Any]) -> None:
    table = source.get("table", "")
    fields = source.get("fields", [])
    if not TABLE_NAME.fullmatch(table):
        raise ValueError(f"invalid table name for dataset {source.get('id')}")
    if not fields or len(fields) != len(set(fields)):
        raise ValueError(f"invalid or duplicate source fields for dataset {source.get('id')}")
    if DB_RESERVED_COLUMNS.intersection(fields):
        raise ValueError(f"source fields conflict with database metadata for dataset {source.get('id')}")


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

        for source in source_list:
            dataset_id = str(source["id"])
            table = str(source["table"])
            connection.execute(
                """INSERT INTO official_dataset_registry
                   (dataset_id, dataset_name, source_table)
                   VALUES (?, ?, ?)
                   ON CONFLICT(dataset_id) DO UPDATE SET
                     dataset_name = excluded.dataset_name,
                     source_table = excluded.source_table""",
                (dataset_id, source["name"], table),
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

        connection.execute("PRAGMA user_version = 1")
    return Path(db_path).resolve()


def record_validation_result(
    db_path: Path | str,
    dataset_id: str,
    status: str,
    error: str | None = None,
) -> None:
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
    now = utc_now()

    with connect(db_path) as connection:
        registry = connection.execute(
            "SELECT 1 FROM official_dataset_registry WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()
        if not registry:
            raise ValueError(f"official database schema is missing dataset {dataset_id}")
        existing = connection.execute(
            "SELECT version_id FROM official_dataset_versions WHERE dataset_id = ? AND sha256 = ?",
            (dataset_id, sha256),
        ).fetchone()
        if not existing:
            connection.execute(
                """INSERT INTO official_dataset_versions
                   (version_id, dataset_id, sha256, source_url, metadata_modified,
                    update_frequency, fetched_at, validated_at, record_count, validation_status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PASS')""",
                (
                    version_id, dataset_id, sha256, source_url, metadata_modified,
                    update_frequency, now, now, len(records),
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
                       validated_at = ?, validation_status = 'PASS'
                   WHERE version_id = ?""",
                (source_url, metadata_modified, update_frequency, now, version_id),
            )
            imported = False
        connection.execute(
            """UPDATE official_dataset_registry
               SET active_version_id = ?, last_check_at = ?, last_status = 'PASS', last_error = NULL
               WHERE dataset_id = ?""",
            (version_id, now, dataset_id),
        )
    return {"dataset_id": dataset_id, "version_id": version_id, "sha256": sha256,
            "row_count": len(records), "new_version": imported}


def rollback_dataset(
    db_path: Path | str, dataset_id: str, version_id: str
) -> None:
    with connect(db_path) as connection:
        version = connection.execute(
            """SELECT 1 FROM official_dataset_versions
               WHERE dataset_id = ? AND version_id = ?""",
            (str(dataset_id), version_id),
        ).fetchone()
        if not version:
            raise ValueError("version does not belong to the requested dataset")
        connection.execute(
            """UPDATE official_dataset_registry
               SET active_version_id = ?, last_check_at = ?, last_status = 'ROLLBACK', last_error = NULL
               WHERE dataset_id = ?""",
            (version_id, utc_now(), str(dataset_id)),
        )
