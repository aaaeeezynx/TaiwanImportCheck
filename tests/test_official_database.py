import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from tools.official_database import (
    import_validated_snapshot,
    initialize_database,
    rollback_dataset,
)
from scripts.validate_official_snapshot import validate_payload


class OfficialDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.source = {
            "id": "test-1",
            "name": "測試官方資料",
            "table": "test_official_data",
            "format": "CSV",
            "fields": ["分類代碼", "品名"],
            "keys": ["分類代碼"],
        }
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "official_reference.db"
        initialize_database(self.db_path, [self.source])

    def _import(self, payload):
        validation = validate_payload(payload, self.source)
        return import_validated_snapshot(
            self.db_path, self.source, payload, validation,
            source_url="https://data.gov.tw/test.csv",
            metadata_modified="2026-01-01",
            update_frequency="每週",
        )

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
        self.assertTrue({"分類代碼", "品名", "source_version_id", "source_row_number"}.issubset(columns))

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
        validation = validate_payload(duplicate, self.source)
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
        rollback_dataset(self.db_path, "test-1", first["version_id"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute('SELECT "品名" FROM active_test_official_data').fetchone()
            versions = connection.execute("SELECT COUNT(*) FROM official_dataset_versions").fetchone()[0]
        self.assertEqual(row, ("第一版",))
        self.assertEqual(versions, 2)

    def test_mock_database_path_is_rejected(self):
        from tools.official_database import MOCK_DB_PATH

        with self.assertRaises(ValueError):
            initialize_database(MOCK_DB_PATH, [self.source])


if __name__ == "__main__":
    unittest.main()
