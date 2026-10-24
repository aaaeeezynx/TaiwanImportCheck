import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from scripts import update_official_data
from tools import official_database
from tools.official_database import (
    DEFAULT_DB_PATH,
    MOCK_DB_PATH,
    import_validated_snapshot,
    initialize_database,
    load_sources,
    record_validation_result,
    rollback_dataset,
)
from scripts.validate_official_snapshot import validate_payload


class OfficialDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "id": "99991",
            "name": "測試官方資料",
            "table": "test_official_data",
            "format": "CSV",
            "authority": "測試主管機關",
            "official_dataset_url": "https://data.gov.tw/dataset/99991",
            "content_type": "text/csv",
            "update_frequency": "每週",
            "license": {
                "code": "1",
                "name": "政府資料開放授權條款－第1版",
                "url": "https://data.gov.tw/license",
            },
            "effective_from": None,
            "effective_to": None,
            "validation_rules": [
                "required_fields",
                "nonempty_payload",
                "duplicate_headers",
                "unique_match_keys",
                "exact_duplicate_rows",
            ],
            "fields": ["分類代碼", "品名"],
            "keys": ["分類代碼"],
        }
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "official_reference.db"
        initialize_database(self.db_path, [self.source])
        self._receipt_sequence = 0

    def _receipt(
        self,
        payload,
        *,
        validated_at=None,
        snapshot_origin="source_snapshot",
    ):
        if validated_at is None:
            self._receipt_sequence += 1
            validated_at = (
                datetime(2026, 4, 1, tzinfo=timezone.utc)
                + timedelta(seconds=self._receipt_sequence)
            ).isoformat()
        validation = validate_payload(payload, self.source)
        validation.update({
            "id": str(self.source["id"]),
            "dataset_id": str(self.source["id"]),
            "snapshot_origin": snapshot_origin,
            "validation_rules": list(self.source["validation_rules"]),
            "validated_at": validated_at,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        })
        return validation

    def _import(self, payload, *, validated_at=None, validation=None):
        if validation is None:
            validation = self._receipt(payload, validated_at=validated_at)
        return import_validated_snapshot(
            self.db_path, self.source, payload, validation,
            source_url="https://data.gov.tw/test.csv",
            metadata_modified="2026-01-01",
            update_frequency="每週",
            validated_at=validated_at,
        )

    def _database_state(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            registry = connection.execute(
                """SELECT dataset_id, active_version_id, active_sha256,
                          last_check_at, last_status, last_error,
                          metadata_modified
                   FROM official_dataset_registry
                   WHERE dataset_id = '99991'"""
            ).fetchall()
            versions = connection.execute(
                """SELECT version_id, dataset_id, sha256, source_url,
                          metadata_modified, update_frequency, fetched_at,
                          validated_at, record_count, validation_status
                   FROM official_dataset_versions
                   ORDER BY version_id"""
            ).fetchall()
            rows = connection.execute(
                """SELECT record_id, source_version_id, source_row_number,
                          "分類代碼", "品名"
                   FROM test_official_data
                   ORDER BY record_id"""
            ).fetchall()
            active_rows = connection.execute(
                """SELECT record_id, source_version_id, source_row_number,
                          "分類代碼", "品名"
                   FROM active_test_official_data
                   ORDER BY record_id"""
            ).fetchall()
        return registry, versions, rows, active_rows

    def test_schema_has_exact_source_fields_and_is_versioned_separately(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            self.assertIn("official_dataset_registry", names)
            self.assertIn("official_dataset_versions", names)
            columns = {row[1] for row in connection.execute(
                'PRAGMA table_info("test_official_data")'
            )}
            registry_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(official_dataset_registry)"
            )}
            version_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(official_dataset_versions)"
            )}
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue({"分類代碼", "品名", "source_version_id", "source_row_number"}.issubset(columns))
        self.assertTrue({
            "active_sha256", "authority", "official_dataset_url", "format",
            "content_type", "license_code", "license_name", "license_url",
            "declared_update_frequency", "effective_from", "effective_to",
            "validation_rules", "metadata_modified",
        }.issubset(registry_columns))
        self.assertTrue({
            "authority", "official_dataset_url", "format", "content_type",
            "license_code", "license_name", "license_url",
            "declared_update_frequency", "effective_from", "effective_to",
            "validation_rules", "content_digest",
        }.issubset(version_columns))
        self.assertEqual(user_version, 3)

    def test_only_pass_snapshot_is_activated_and_duplicate_version_is_idempotent(self):
        payload = "分類代碼,品名\n12345678,測試商品\n".encode("utf-8")
        first = self._import(payload)
        second = self._import(payload)
        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute('SELECT "分類代碼", "品名" FROM active_test_official_data').fetchall()
            count = connection.execute("SELECT COUNT(*) FROM test_official_data").fetchone()[0]
        self.assertEqual(first["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertTrue(first["new_version"])
        self.assertFalse(second["new_version"])
        self.assertEqual(rows, [("12345678", "測試商品")])
        self.assertEqual(count, 1)

    def test_same_snapshot_is_an_immutable_noop(self):
        payload = "分類代碼,品名\n12345678,不可變版本\n".encode("utf-8")
        first = self._import(
            payload,
            validated_at="2026-04-01T00:00:00+00:00",
        )
        before = self._database_state()

        repeated = import_validated_snapshot(
            self.db_path,
            self.source,
            payload,
            self._receipt(
                payload,
                validated_at="2026-04-02T00:00:00+00:00",
            ),
            source_url="https://data.gov.tw/replacement.csv",
            metadata_modified="2026-04-02",
            update_frequency="每日",
        )

        self.assertFalse(repeated["new_version"])
        self.assertFalse(repeated["activated"])
        self.assertEqual(repeated["reason"], "same_snapshot_immutable_noop")
        self.assertEqual(repeated["version_id"], first["version_id"])
        after = self._database_state()
        before_registry = list(before[0][0])
        after_registry = list(after[0][0])
        self.assertNotEqual(after_registry[3], before_registry[3])
        before_registry[3] = after_registry[3]
        self.assertEqual(tuple(after_registry), tuple(before_registry))
        self.assertEqual(after[1:], before[1:])

    def test_legacy_active_snapshot_is_reverified_and_digest_backfilled(self):
        payload = "分類代碼,品名\n12345678,舊版完整資料\n".encode("utf-8")
        first = self._import(
            payload,
            validated_at="2026-04-01T00:00:00+00:00",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_versions
                   SET content_digest = NULL
                   WHERE version_id = ?""",
                (first["version_id"],),
            )
            connection.execute(
                """UPDATE official_dataset_registry
                   SET last_status = 'IMPORT_FAIL', last_error = 'legacy import failure'
                   WHERE dataset_id = '99991'"""
            )
            connection.commit()

        repeated = import_validated_snapshot(
            self.db_path,
            self.source,
            payload,
            self._receipt(
                payload,
                validated_at="2026-04-02T00:00:00+00:00",
            ),
        )

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            registry = connection.execute(
                """SELECT active_version_id, active_sha256, last_status,
                          last_error, last_check_at
                   FROM official_dataset_registry WHERE dataset_id = '99991'"""
            ).fetchone()
            version = connection.execute(
                """SELECT content_digest FROM official_dataset_versions
                   WHERE version_id = ?""",
                (first["version_id"],),
            ).fetchone()
        self.assertFalse(repeated["new_version"])
        self.assertFalse(repeated["activated"])
        self.assertEqual(repeated["reason"], "same_snapshot_immutable_noop")
        self.assertEqual(registry["active_version_id"], first["version_id"])
        self.assertEqual(registry["active_sha256"], first["sha256"])
        self.assertEqual(registry["last_status"], "PASS")
        self.assertIsNone(registry["last_error"])
        self.assertIsNotNone(registry["last_check_at"])
        self.assertIsNotNone(version["content_digest"])

    def test_legacy_snapshot_with_tampered_rows_is_not_repaired(self):
        payload = "分類代碼,品名\n12345678,原始資料\n".encode("utf-8")
        first = self._import(
            payload,
            validated_at="2026-04-01T00:00:00+00:00",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_versions
                   SET content_digest = NULL
                   WHERE version_id = ?""",
                (first["version_id"],),
            )
            connection.execute(
                """UPDATE test_official_data
                   SET "品名" = '遭竄改資料'
                   WHERE source_version_id = ?""",
                (first["version_id"],),
            )
            connection.commit()

        with self.assertRaisesRegex(
            ValueError,
            "existing snapshot is not the verified active version",
        ):
            import_validated_snapshot(
                self.db_path,
                self.source,
                payload,
                self._receipt(
                    payload,
                    validated_at="2026-04-02T00:00:00+00:00",
                ),
            )

        with closing(sqlite3.connect(self.db_path)) as connection:
            digest = connection.execute(
                """SELECT content_digest FROM official_dataset_versions
                   WHERE version_id = ?""",
                (first["version_id"],),
            ).fetchone()[0]
            stored_name = connection.execute(
                """SELECT "品名" FROM test_official_data
                   WHERE source_version_id = ?""",
                (first["version_id"],),
            ).fetchone()[0]
        self.assertIsNone(digest)
        self.assertEqual(stored_name, "遭竄改資料")

    def test_blank_csv_rows_are_ignored_consistently_with_validation(self):
        payload = (
            "分類代碼,品名\n"
            "12345678,測試商品一\n"
            "\n"
            ",\n"
            "  ,  \n"
            "12345679,測試商品二\n"
        ).encode("utf-8")
        validation = validate_payload(payload, self.source)

        imported = self._import(payload)

        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                'SELECT "分類代碼", "品名" FROM active_test_official_data ORDER BY "分類代碼"'
            ).fetchall()
        self.assertEqual(validation["status"], "PASS")
        self.assertEqual(validation["row_count"], 2)
        self.assertEqual(imported["row_count"], 2)
        self.assertEqual(rows, [("12345678", "測試商品一"), ("12345679", "測試商品二")])

    def test_failed_or_review_snapshot_cannot_replace_active_data(self):
        good = "分類代碼,品名\n12345678,舊版本\n".encode("utf-8")
        self._import(good)
        duplicate = "分類代碼,品名\n12345678,新版本\n12345678,新版本二\n".encode("utf-8")
        validation = self._receipt(duplicate)
        self.assertEqual(validation["status"], "REVIEW")
        with self.assertRaises(ValueError):
            import_validated_snapshot(self.db_path, self.source, duplicate, validation)
        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute('SELECT "品名" FROM active_test_official_data').fetchall()
        self.assertEqual(rows, [("舊版本",)])

    def test_rollback_switches_active_view_without_deleting_versions(self):
        first_payload = "分類代碼,品名\n12345678,第一版\n".encode("utf-8")
        second_payload = "分類代碼,品名\n12345678,第二版\n".encode("utf-8")
        first = self._import(first_payload)
        self._import(second_payload)
        with mock.patch.object(
            official_database,
            "load_sources",
            return_value=[self.source],
        ):
            rollback_dataset(self.db_path, "99991", first["version_id"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute('SELECT "品名" FROM active_test_official_data').fetchone()
            versions = connection.execute("SELECT COUNT(*) FROM official_dataset_versions").fetchone()[0]
        self.assertEqual(row, ("第一版",))
        self.assertEqual(versions, 2)

    def test_mock_database_path_is_rejected(self):
        with self.assertRaises(ValueError):
            initialize_database(MOCK_DB_PATH, [self.source])

    def test_manifest_sources_are_complete_and_identifiers_are_unique(self):
        sources = load_sources()
        self.assertEqual(len(sources), 18)
        self.assertEqual(len({source["id"] for source in sources}), len(sources))
        self.assertEqual(len({source["table"] for source in sources}), len(sources))
        self.assertTrue(all(str(source["id"]).isdigit() for source in sources))

    def test_registry_contains_manifest_metadata(self):
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM official_dataset_registry WHERE dataset_id = '99991'"
            ).fetchone()
        self.assertEqual(row["authority"], self.source["authority"])
        self.assertEqual(row["official_dataset_url"], self.source["official_dataset_url"])
        self.assertEqual(row["format"], "CSV")
        self.assertEqual(row["content_type"], "text/csv")
        self.assertEqual(row["license_code"], "1")
        self.assertEqual(row["declared_update_frequency"], "每週")
        self.assertEqual(
            row["validation_rules"],
            '["required_fields","nonempty_payload","duplicate_headers",'
            '"unique_match_keys","exact_duplicate_rows"]',
        )

    def test_invalid_source_url_or_content_type_is_rejected(self):
        bad_url = deepcopy(self.source)
        bad_url["official_dataset_url"] = "http://127.0.0.1/dataset"
        with self.assertRaises(ValueError):
            initialize_database(self.db_path, [bad_url])

        bad_type = deepcopy(self.source)
        bad_type["content_type"] = "application/json"
        with self.assertRaises(ValueError):
            initialize_database(self.db_path, [bad_type])

    def test_invalid_validation_status_is_rejected(self):
        with self.assertRaises(ValueError):
            record_validation_result(self.db_path, "99991", "UNKNOWN")

    def test_rollback_rejects_unknown_dataset_version(self):
        first = self._import("分類代碼,品名\n12345678,第一版\n".encode("utf-8"))
        with self.assertRaises(ValueError):
            rollback_dataset(self.db_path, "99992", first["version_id"])

    def test_newer_pass_can_replace_older_failure(self):
        record_validation_result(self.db_path, "99991", "FAIL", "fixture failure")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_registry
                   SET last_check_at = '2026-01-01T00:00:00+00:00'
                   WHERE dataset_id = '99991'"""
            )
            connection.commit()
        imported = self._import(
            "分類代碼,品名\n12345678,修正版本\n".encode("utf-8"),
            validated_at="2026-01-02T00:00:00+00:00",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            status = connection.execute(
                "SELECT last_status FROM official_dataset_registry WHERE dataset_id = '99991'"
            ).fetchone()[0]
        self.assertTrue(imported["activated"])
        self.assertEqual(status, "PASS")

    def test_older_pass_cannot_replace_newer_failure(self):
        record_validation_result(self.db_path, "99991", "FAIL", "newer failure")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_registry
                   SET last_check_at = '2026-02-01T00:00:00+00:00'
                   WHERE dataset_id = '99991'"""
            )
            connection.commit()
        imported = self._import(
            "分類代碼,品名\n12345678,過期版本\n".encode("utf-8"),
            validated_at="2026-01-01T00:00:00+00:00",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            status = connection.execute(
                "SELECT last_status FROM official_dataset_registry WHERE dataset_id = '99991'"
            ).fetchone()[0]
            version_count = connection.execute(
                "SELECT COUNT(*) FROM official_dataset_versions"
            ).fetchone()[0]
        self.assertFalse(imported["activated"])
        self.assertEqual(imported["reason"], "newer_validation_result_preserved")
        self.assertEqual(status, "FAIL")
        self.assertEqual(version_count, 0)

    def test_equal_or_older_pass_timestamp_cannot_replace_active_data(self):
        initial_payload = "分類代碼,品名\n12345678,現行版本\n".encode("utf-8")
        self._import(
            initial_payload,
            validated_at="2026-06-01T00:00:00+00:00",
        )
        for index, validated_at in enumerate((
            "2026-06-01T00:00:00+00:00",
            "2026-05-31T23:59:59+00:00",
        )):
            with self.subTest(validated_at=validated_at):
                candidate = (
                    f"分類代碼,品名\n123456{index},過期版本{index}\n"
                ).encode("utf-8")
                result = self._import(candidate, validated_at=validated_at)
                self.assertFalse(result["activated"])
                self.assertEqual(
                    result["reason"],
                    "newer_validation_result_preserved",
                )
        with closing(sqlite3.connect(self.db_path)) as connection:
            active_rows = connection.execute(
                'SELECT "品名" FROM active_test_official_data'
            ).fetchall()
            version_count = connection.execute(
                "SELECT COUNT(*) FROM official_dataset_versions"
            ).fetchone()[0]
        self.assertEqual(active_rows, [("現行版本",)])
        self.assertEqual(version_count, 1)

    def test_receipt_must_bind_dataset_rules_bytes_and_timestamp(self):
        payload = "分類代碼,品名\n12345678,測試商品\n".encode("utf-8")
        base = self._receipt(
            payload,
            validated_at="2026-07-01T00:00:00+00:00",
        )
        malformed = []

        receipt = deepcopy(base)
        receipt["id"] = "99992"
        malformed.append(("id", receipt, {}))
        receipt = deepcopy(base)
        receipt["dataset_id"] = "99992"
        malformed.append(("dataset_id", receipt, {}))
        receipt = deepcopy(base)
        receipt["validation_rules"] = ["required_fields"]
        malformed.append(("validation_rules", receipt, {}))
        receipt = deepcopy(base)
        receipt["sha256"] = "0" * 64
        malformed.append(("sha256", receipt, {}))
        receipt = deepcopy(base)
        receipt["size_bytes"] += 1
        malformed.append(("size_bytes", receipt, {}))
        receipt = deepcopy(base)
        receipt["validated_at"] = "not-a-timestamp"
        malformed.append(("validated_at", receipt, {}))
        malformed.append((
            "validated_at_override",
            deepcopy(base),
            {"validated_at": "2026-07-02T00:00:00+00:00"},
        ))

        for name, receipt, kwargs in malformed:
            with self.subTest(field=name):
                with self.assertRaises(ValueError):
                    self._import(payload, validation=receipt, **kwargs)

        with closing(sqlite3.connect(self.db_path)) as connection:
            version_count = connection.execute(
                "SELECT COUNT(*) FROM official_dataset_versions"
            ).fetchone()[0]
        self.assertEqual(version_count, 0)

    def test_migration_backfills_descriptive_metadata_without_inventing_integrity(self):
        payload = "分類代碼,品名\n12345678,測試商品\n".encode("utf-8")
        imported = self._import(payload)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_versions
                   SET authority = NULL, official_dataset_url = NULL, format = NULL,
                       content_type = NULL, license_code = NULL, license_name = NULL,
                       license_url = NULL, declared_update_frequency = NULL,
                       effective_from = NULL, effective_to = NULL,
                       validation_rules = NULL, content_digest = NULL
                   WHERE version_id = ?""",
                (imported["version_id"],),
            )
            connection.execute(
                """UPDATE official_dataset_registry
                   SET active_sha256 = NULL, metadata_modified = NULL
                   WHERE dataset_id = '99991'"""
            )
            connection.commit()

        initialize_database(self.db_path, [self.source])

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            registry = connection.execute(
                """SELECT active_sha256, metadata_modified
                   FROM official_dataset_registry WHERE dataset_id = '99991'"""
            ).fetchone()
            version = connection.execute(
                """SELECT authority, official_dataset_url, format, content_type,
                          license_code, license_name, license_url,
                          declared_update_frequency, validation_rules,
                          content_digest
                   FROM official_dataset_versions WHERE version_id = ?""",
                (imported["version_id"],),
            ).fetchone()
            source_count = connection.execute(
                "SELECT COUNT(*) FROM test_official_data"
            ).fetchone()[0]
            view_count = connection.execute(
                "SELECT COUNT(*) FROM active_test_official_data"
            ).fetchone()[0]
        self.assertIsNone(registry["active_sha256"])
        self.assertIsNone(registry["metadata_modified"])
        self.assertEqual(version["authority"], self.source["authority"])
        self.assertEqual(
            version["official_dataset_url"], self.source["official_dataset_url"]
        )
        self.assertEqual(version["format"], "CSV")
        self.assertEqual(version["content_type"], "text/csv")
        self.assertEqual(version["license_code"], "1")
        self.assertEqual(version["declared_update_frequency"], "每週")
        self.assertIn('"unique_match_keys"', version["validation_rules"])
        self.assertIsNone(version["content_digest"])
        self.assertEqual(source_count, 1)
        self.assertEqual(view_count, 1)

    def test_migration_preserves_existing_content_digest(self):
        imported = self._import(
            "分類代碼,品名\n12345678,完整性測試\n".encode("utf-8")
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            original_digest = connection.execute(
                """SELECT content_digest FROM official_dataset_versions
                   WHERE version_id = ?""",
                (imported["version_id"],),
            ).fetchone()[0]
            self.assertIsNotNone(original_digest)
            connection.execute(
                """UPDATE official_dataset_registry
                   SET active_sha256 = NULL, metadata_modified = NULL
                   WHERE dataset_id = '99991'"""
            )
            connection.commit()

        initialize_database(self.db_path, [self.source])

        with closing(sqlite3.connect(self.db_path)) as connection:
            migrated_digest = connection.execute(
                """SELECT content_digest FROM official_dataset_versions
                   WHERE version_id = ?""",
                (imported["version_id"],),
            ).fetchone()[0]
        self.assertEqual(migrated_digest, original_digest)

    def test_migration_preserves_existing_non_null_metadata(self):
        imported = self._import(
            "分類代碼,品名\n12345678,測試商品\n".encode("utf-8")
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_versions
                   SET authority = '既有版本主管機關',
                       validation_rules = '["existing_rule"]'
                   WHERE version_id = ?""",
                (imported["version_id"],),
            )
            connection.execute(
                """UPDATE official_dataset_registry
                   SET authority = '既有清單主管機關',
                       active_sha256 = 'existing-active-sha',
                       metadata_modified = '既有修改日期'
                   WHERE dataset_id = '99991'"""
            )
            connection.commit()

        initialize_database(self.db_path, [self.source])

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            registry = connection.execute(
                """SELECT authority, active_sha256, metadata_modified
                   FROM official_dataset_registry WHERE dataset_id = '99991'"""
            ).fetchone()
            version = connection.execute(
                """SELECT authority, validation_rules
                   FROM official_dataset_versions WHERE version_id = ?""",
                (imported["version_id"],),
            ).fetchone()
        self.assertEqual(registry["authority"], "既有清單主管機關")
        self.assertEqual(registry["active_sha256"], "existing-active-sha")
        self.assertEqual(registry["metadata_modified"], "既有修改日期")
        self.assertEqual(version["authority"], "既有版本主管機關")
        self.assertEqual(version["validation_rules"], '["existing_rule"]')

    def test_migration_skips_pointer_with_unknown_active_version(self):
        imported = self._import(
            "分類代碼,品名\n12345678,測試商品\n".encode("utf-8")
        )
        unknown_version_id = "99991:" + ("0" * 64)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_registry
                   SET active_version_id = ?, active_sha256 = NULL,
                       metadata_modified = NULL
                   WHERE dataset_id = '99991'""",
                (unknown_version_id,),
            )
            connection.commit()

        initialize_database(self.db_path, [self.source])

        with closing(sqlite3.connect(self.db_path)) as connection:
            registry = connection.execute(
                """SELECT active_version_id, active_sha256, metadata_modified
                   FROM official_dataset_registry WHERE dataset_id = '99991'"""
            ).fetchone()
            version_count = connection.execute(
                "SELECT COUNT(*) FROM official_dataset_versions WHERE version_id = ?",
                (imported["version_id"],),
            ).fetchone()[0]
        self.assertEqual(registry[0], unknown_version_id)
        self.assertIsNone(registry[1])
        self.assertIsNone(registry[2])
        self.assertEqual(version_count, 1)

    def test_migration_skips_pointer_when_row_counts_disagree(self):
        imported = self._import(
            "分類代碼,品名\n12345678,測試商品\n".encode("utf-8")
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_registry
                   SET active_sha256 = NULL, metadata_modified = NULL
                   WHERE dataset_id = '99991'"""
            )
            connection.execute("DELETE FROM test_official_data")
            connection.commit()

        initialize_database(self.db_path, [self.source])

        with closing(sqlite3.connect(self.db_path)) as connection:
            registry = connection.execute(
                """SELECT active_version_id, active_sha256, metadata_modified
                   FROM official_dataset_registry WHERE dataset_id = '99991'"""
            ).fetchone()
        self.assertEqual(registry[0], imported["version_id"])
        self.assertIsNone(registry[1])
        self.assertIsNone(registry[2])

    def test_migration_preserves_newer_registry_failure(self):
        imported = self._import(
            "分類代碼,品名\n12345678,測試商品\n".encode("utf-8"),
            validated_at="2026-01-01T00:00:00+00:00",
        )
        record_validation_result(self.db_path, "99991", "FAIL", "newer download failure")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_registry
                   SET last_check_at = '2026-02-01T00:00:00+00:00',
                       active_sha256 = NULL, metadata_modified = NULL
                   WHERE dataset_id = '99991'"""
            )
            connection.commit()

        initialize_database(self.db_path, [self.source])
        initialize_database(self.db_path, [self.source])

        with closing(sqlite3.connect(self.db_path)) as connection:
            registry = connection.execute(
                """SELECT active_version_id, active_sha256, last_status,
                          last_check_at, metadata_modified
                   FROM official_dataset_registry WHERE dataset_id = '99991'"""
            ).fetchone()
            version_count = connection.execute(
                "SELECT COUNT(*) FROM official_dataset_versions"
            ).fetchone()[0]
        self.assertEqual(registry[0], imported["version_id"])
        self.assertIsNone(registry[1])
        self.assertEqual(registry[2], "FAIL")
        self.assertEqual(registry[3], "2026-02-01T00:00:00+00:00")
        self.assertIsNone(registry[4])
        self.assertEqual(version_count, 1)

    def test_official_and_mock_database_paths_are_separate(self):
        self.assertNotEqual(DEFAULT_DB_PATH.resolve(), MOCK_DB_PATH.resolve())

    def test_official_database_rejects_mock_exact_path_and_hardlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mock_db = root / "p0_mock.db"
            mock_db.write_bytes(b"mock database identity fixture")
            hardlink = root / "official_alias.db"
            try:
                os.link(mock_db, hardlink)
            except OSError as exc:
                self.skipTest(f"hardlink creation is unavailable: {exc}")

            with mock.patch.object(official_database, "MOCK_DB_PATH", mock_db):
                with self.assertRaises(ValueError):
                    official_database.connect(mock_db)
                with self.assertRaises(ValueError):
                    official_database.connect(hardlink)

    def test_update_imports_validator_selected_zip_with_csv_filename(self):
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "data.csv",
                "分類代碼,品名\n12345678,測試商品\n",
            )
        payload = memory.getvalue()

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source_snapshots"
            source_dir.mkdir()
            snapshot = source_dir / "99991.csv"
            snapshot.write_bytes(payload)
            db_path = root / "official_reference.db"
            report = {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "mode": "validate_local_snapshots",
                "results": [{
                    "id": "99991",
                    "status": "PASS",
                    "snapshot_origin": "source_snapshot",
                    "file": str(snapshot),
                    "source_url": "https://data.gov.tw/test.csv",
                    "metadata_modified": "2026-01-01",
                    "update_frequency": "每週",
                }],
                "summary": {
                    "total": 1,
                    "pass": 1,
                    "review": 0,
                    "fail": 0,
                    "missing": 0,
                },
            }
            def fake_run(*, evidence, **kwargs):
                evidence["99991"] = payload
                report["results"][0].update(self._receipt(payload))
                return report

            with (
                mock.patch.object(update_official_data, "load_sources", return_value=[self.source]),
                mock.patch.object(update_official_data, "run", side_effect=fake_run),
            ):
                result = update_official_data.run_update(
                    download=False,
                    db_path=db_path,
                    source_dir=source_dir,
                    candidate_dir=root / "candidate_snapshots",
                    report_dir=root / "reports",
                )

            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    'SELECT "分類代碼", "品名" FROM active_test_official_data'
                ).fetchall()
            update_report = json.loads(
                (root / "reports" / "official_data_update.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(result["summary"]["import_errors"], 0)
        self.assertEqual(result["summary"]["activated"], 1)
        self.assertEqual(rows, [("12345678", "測試商品")])
        self.assertEqual(update_report["datasets"][0]["database_action"], "activated")

    def test_candidate_pass_is_validated_but_not_activated(self):
        payload = "分類代碼,品名\n12345678,候選版本\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source_snapshots"
            candidate_dir = root / "candidate_snapshots"
            source_dir.mkdir()
            candidate_dir.mkdir()
            snapshot = candidate_dir / "99991.csv"
            snapshot.write_bytes(payload)
            db_path = root / "official_reference.db"
            report = {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "mode": "validate_local_snapshots",
                "results": [{
                    "id": "99991",
                    "status": "PASS",
                    "snapshot_origin": "candidate_snapshot",
                    "file": str(snapshot),
                }],
                "summary": {
                    "total": 1,
                    "pass": 1,
                    "review": 0,
                    "fail": 0,
                    "missing": 0,
                },
            }
            with (
                mock.patch.object(
                    update_official_data, "load_sources", return_value=[self.source]
                ),
                mock.patch.object(update_official_data, "run", return_value=report),
            ):
                result = update_official_data.run_update(
                    download=False,
                    db_path=db_path,
                    source_dir=source_dir,
                    candidate_dir=candidate_dir,
                    report_dir=root / "reports",
                )

            with closing(sqlite3.connect(db_path)) as connection:
                registry = connection.execute(
                    """SELECT active_version_id, last_status
                       FROM official_dataset_registry WHERE dataset_id = '99991'"""
                ).fetchone()
                version_count = connection.execute(
                    "SELECT COUNT(*) FROM official_dataset_versions"
                ).fetchone()[0]

        dataset = result["datasets"][0]
        self.assertEqual(result["summary"]["activated"], 0)
        self.assertEqual(result["summary"]["candidate_pass"], 1)
        self.assertEqual(result["summary"]["import_errors"], 0)
        self.assertEqual(dataset["database_action"], "kept_previous")
        self.assertTrue(dataset["not_activated"])
        self.assertEqual(dataset["reason"], "candidate_snapshot_requires_promotion")
        self.assertEqual(registry, (None, "CANDIDATE_PASS"))
        self.assertEqual(version_count, 0)

    def test_update_imports_validator_evidence_after_disk_replacement(self):
        payload = "分類代碼,品名\n12345678,驗證當下版本\n".encode("utf-8")
        replacement = "分類代碼,品名\n12345678,磁碟遭替換版本\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source_snapshots"
            source_dir.mkdir()
            snapshot = source_dir / "99991.csv"
            snapshot.write_bytes(payload)
            db_path = root / "official_reference.db"
            report = {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "mode": "validate_local_snapshots",
                "results": [],
                "summary": {
                    "total": 1,
                    "pass": 1,
                    "review": 0,
                    "fail": 0,
                    "missing": 0,
                },
            }

            def fake_run(*, evidence, **kwargs):
                evidence["99991"] = payload
                snapshot.write_bytes(replacement)
                report["results"] = [{
                    "id": "99991",
                    "snapshot_origin": "source_snapshot",
                    "file": str(snapshot),
                    **self._receipt(payload),
                }]
                return report

            with (
                mock.patch.object(
                    update_official_data,
                    "load_sources",
                    return_value=[self.source],
                ),
                mock.patch.object(
                    update_official_data,
                    "run",
                    side_effect=fake_run,
                ),
            ):
                result = update_official_data.run_update(
                    download=False,
                    db_path=db_path,
                    source_dir=source_dir,
                    candidate_dir=root / "candidate_snapshots",
                    report_dir=root / "reports",
                )

            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    'SELECT "品名" FROM active_test_official_data'
                ).fetchall()

        self.assertEqual(result["summary"]["import_errors"], 0)
        self.assertEqual(rows, [("驗證當下版本",)])

    def test_candidate_review_keeps_existing_active_version(self):
        good = "分類代碼,品名\n12345678,舊版本\n".encode("utf-8")
        self._import(good)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source_snapshots"
            candidate_dir = root / "candidate_snapshots"
            source_dir.mkdir()
            candidate_dir.mkdir()
            snapshot = candidate_dir / "99991.csv"
            snapshot.write_bytes(
                "分類代碼,品名\n12345678,新版本\n12345678,重複版本\n".encode(
                    "utf-8"
                )
            )
            report = {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "mode": "validate_local_snapshots",
                "results": [{
                    "id": "99991",
                    "status": "REVIEW",
                    "snapshot_origin": "candidate_snapshot",
                    "file": str(snapshot),
                }],
                "summary": {
                    "total": 1,
                    "pass": 0,
                    "review": 1,
                    "fail": 0,
                    "missing": 0,
                },
            }
            with (
                mock.patch.object(
                    update_official_data, "load_sources", return_value=[self.source]
                ),
                mock.patch.object(update_official_data, "run", return_value=report),
            ):
                result = update_official_data.run_update(
                    download=False,
                    db_path=self.db_path,
                    source_dir=source_dir,
                    candidate_dir=candidate_dir,
                    report_dir=root / "reports",
                )

            with closing(sqlite3.connect(self.db_path)) as connection:
                rows = connection.execute(
                    'SELECT "品名" FROM active_test_official_data'
                ).fetchall()
                registry_status = connection.execute(
                    """SELECT last_status FROM official_dataset_registry
                       WHERE dataset_id = '99991'"""
                ).fetchone()[0]

        self.assertEqual(result["datasets"][0]["database_action"], "kept_previous")
        self.assertEqual(rows, [("舊版本",)])
        self.assertEqual(registry_status, "REVIEW")

    def test_safe_snapshot_reader_rejects_file_outside_source_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source_snapshots"
            source_dir.mkdir()
            outside = root / "outside.csv"
            outside.write_text("分類代碼,品名\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                official_database._read_snapshot_file(
                    outside,
                    root=source_dir,
                )

    def test_rollback_preflight_rejects_inconsistent_active_sha_atomically(self):
        first = self._import(
            "分類代碼,品名\n12345678,第一版\n".encode("utf-8"),
            validated_at="2026-08-01T00:00:00+00:00",
        )
        self._import(
            "分類代碼,品名\n12345678,第二版\n".encode("utf-8"),
            validated_at="2026-08-02T00:00:00+00:00",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                """UPDATE official_dataset_registry
                   SET active_sha256 = 'inconsistent'
                   WHERE dataset_id = '99991'"""
            )
            connection.commit()
        before = self._database_state()

        with mock.patch.object(
            official_database,
            "load_sources",
            return_value=[self.source],
        ):
            with self.assertRaises(ValueError):
                rollback_dataset(self.db_path, "99991", first["version_id"])

        self.assertEqual(self._database_state(), before)

    def test_safe_snapshot_reader_rejects_relative_oversize_and_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            approved = root / "source_snapshots"
            approved.mkdir()
            regular = approved / "99991.csv"
            regular.write_bytes(b"12345")

            with self.assertRaises(ValueError):
                official_database._read_snapshot_file(
                    Path("99991.csv"),
                    root=approved,
                )
            with self.assertRaises(ValueError):
                official_database._read_snapshot_file(
                    regular,
                    root=approved,
                    max_bytes=4,
                )
            self.assertEqual(
                official_database._read_snapshot_file(
                    regular,
                    root=approved,
                    max_bytes=5,
                ),
                b"12345",
            )

            symlink = approved / "linked.csv"
            try:
                symlink.symlink_to(regular)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaises(ValueError):
                official_database._read_snapshot_file(
                    symlink,
                    root=approved,
                )


if __name__ == "__main__":
    unittest.main()
