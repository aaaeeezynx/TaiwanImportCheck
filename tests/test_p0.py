"""P0 tests for the local mock lookup and Hermes project plugin integration."""

from __future__ import annotations

import importlib.util
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = PROJECT_ROOT / "tools" / "p0_import_lookup.py"
TEST_TEMP_ROOT = Path(
    os.environ.get("P0_TEST_TEMP_ROOT", Path(tempfile.gettempdir()) / "tic-p0-tests")
)
TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
HERMES_ROOT = Path(
    os.environ.get("HERMES_INSTALL_ROOT", Path(sys.executable).resolve().parents[2])
)
ALLOWED_USER = "100000001"
ADMIN_USER = "100000001"
UNAUTHORIZED_USER = "100000009"
VALID_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
)


def _load_core_module():
    spec = importlib.util.spec_from_file_location("tic_p0_import_lookup_tests", TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load P0 lookup module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


core = _load_core_module()


class P0LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "p0_test_tariff.db"
        core.ensure_mock_db(self.db_path, force_reset=True)
        self.image_cache_dir = Path(self.temp_dir.name) / "image_cache"
        self.image_cache_dir.mkdir()

        self.env_patcher = patch.dict(
            os.environ,
            {
                "TELEGRAM_ALLOWED_USERS": ALLOWED_USER,
                "ADMIN_TELEGRAM_USERS": ADMIN_USER,
                "P0_MOCK_DB_PATH": str(self.db_path),
                "HERMES_HOME": str(Path(self.temp_dir.name) / "hermes_home"),
                "IMAGE_CACHE_DIR": str(self.image_cache_dir),
            },
            clear=False,
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def test_text_lookup_returns_structured_mock_result(self) -> None:
        result = core.run_p0_lookup(user_id=ALLOWED_USER, query="藍牙耳機")

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["is_mock_data"])
        self.assertIn("[P0測試假資料]", result["disclaimer"])
        self.assertIn("不構成正式", result["disclaimer"])
        self.assertGreaterEqual(len(result["data"]["candidates"]), 1)
        self.assertIn("ccc_code", result["data"]["candidates"][0])

    def test_image_path_is_accepted(self) -> None:
        image_path = self.image_cache_dir / "product-earbuds.png"
        image_path.write_bytes(VALID_PNG_BYTES)

        result = core.run_p0_lookup(
            user_id=ALLOWED_USER,
            image_path=str(image_path),
            image_description="白色真無線藍牙耳機與充電盒",
        )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["input_summary"]["has_image"])
        self.assertTrue(result["input_summary"]["image_info"]["file_valid"])
        self.assertEqual(
            result["input_summary"]["image_info"]["detected_format"],
            "png",
        )
        self.assertNotIn(str(image_path), json.dumps(result, ensure_ascii=False))

    def test_valid_image_without_description_needs_more_info(self) -> None:
        image_path = self.image_cache_dir / "product-earbuds.png"
        image_path.write_bytes(VALID_PNG_BYTES)

        result = core.run_p0_lookup(user_id=ALLOWED_USER, image_path=str(image_path))

        self.assertEqual(result["status"], "needs_more_info")
        self.assertTrue(result["input_summary"]["image_info"]["file_valid"])
        self.assertFalse(
            result["input_summary"]["image_info"]["description_provided"]
        )
        self.assertEqual(result["data"]["candidates"], [])

    def test_image_path_outside_cache_is_not_read(self) -> None:
        outside_path = Path(self.temp_dir.name) / "must-not-read.png"
        outside_path.write_bytes(b"P0 test image placeholder")

        info = core.inspect_image_input(
            image_path=str(outside_path),
            image_description="白色真無線藍牙耳機與充電盒",
        )

        self.assertFalse(info["file_exists"])
        self.assertFalse(info["file_readable"])
        self.assertFalse(info["file_valid"])
        self.assertNotIn("file_name", info)
        self.assertNotIn(str(outside_path), json.dumps(info, ensure_ascii=False))
        self.assertIn("不在允許", info["warning"])
        self.assertIn("藍牙耳機", info["extracted_keywords"])

    def test_image_path_traversal_escape_is_not_read(self) -> None:
        traversal_path = self.image_cache_dir / ".." / "must-not-read.png"

        info = core.inspect_image_input(image_path=str(traversal_path))

        self.assertFalse(info["file_exists"])
        self.assertFalse(info["file_readable"])
        self.assertFalse(info["file_valid"])
        self.assertNotIn("file_name", info)
        self.assertIn("warning", info)
        self.assertNotIn(str(traversal_path), json.dumps(info, ensure_ascii=False))

    def test_image_symlink_escape_is_not_read(self) -> None:
        outside_path = Path(self.temp_dir.name) / "outside-secret.png"
        outside_path.write_bytes(b"must not be read")
        symlink_path = self.image_cache_dir / "linked-secret.png"
        try:
            symlink_path.symlink_to(outside_path)
        except OSError as exc:
            self.skipTest(f"Symlink creation is unavailable: {type(exc).__name__}")

        info = core.inspect_image_input(image_path=str(symlink_path))

        self.assertFalse(info["file_exists"])
        self.assertFalse(info["file_readable"])
        self.assertFalse(info["file_valid"])
        self.assertNotIn("file_name", info)
        self.assertIn("warning", info)
        self.assertNotIn(str(symlink_path), json.dumps(info, ensure_ascii=False))

    def test_image_description_is_accepted(self) -> None:
        result = core.run_p0_lookup(
            user_id=ALLOWED_USER,
            image_description="白色真無線藍牙耳機與充電盒",
        )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["input_summary"]["has_image"])
        self.assertGreaterEqual(len(result["data"]["candidates"]), 1)

    def test_missing_image_path_falls_back_to_description(self) -> None:
        missing_path = self.image_cache_dir / "missing-product.png"

        result = core.run_p0_lookup(
            user_id=ALLOWED_USER,
            image_path=str(missing_path),
            image_description="白色真無線藍牙耳機與充電盒",
        )

        self.assertEqual(result["status"], "ok")
        image_info = result["input_summary"]["image_info"]
        self.assertTrue(image_info["provided"])
        self.assertFalse(image_info["file_exists"])
        self.assertFalse(image_info["file_readable"])
        self.assertFalse(image_info["file_valid"])
        self.assertIn("不存在", " ".join(result["data"]["warnings"]))
        self.assertGreaterEqual(len(result["data"]["candidates"]), 1)

    def test_product_url_is_parsed_without_live_fetch(self) -> None:
        with patch("socket.create_connection", side_effect=AssertionError("network used")):
            result = core.run_p0_lookup(
                user_id=ALLOWED_USER,
                product_url="https://shop.example.com/products/bluetooth-earbuds",
            )

        self.assertEqual(result["status"], "ok")
        url_info = result["input_summary"]["url_info"]
        self.assertTrue(url_info["valid"])
        self.assertNotIn("live_fetch_attempted", url_info)
        self.assertGreaterEqual(len(result["data"]["candidates"]), 1)

    def test_missing_user_id_is_rejected(self) -> None:
        result = core.run_p0_lookup(user_id="", query="藍牙耳機")

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["error"]["code"], "MISSING_USER_ID")
        self.assertNotIn("user_id", result["error"]["message"].lower())
        self.assertNotIn("Telegram ID", result["error"]["message"])

    def test_unauthorized_user_is_rejected_without_echoing_user_id(self) -> None:
        result = core.run_p0_lookup(user_id=UNAUTHORIZED_USER, query="藍牙耳機")

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["error"]["code"], "UNAUTHORIZED_USER")
        self.assertNotIn(UNAUTHORIZED_USER, json.dumps(result, ensure_ascii=False))

    def test_empty_allowlist_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"TELEGRAM_ALLOWED_USERS": "", "ADMIN_TELEGRAM_USERS": ""},
            clear=False,
        ):
            result = core.run_p0_lookup(user_id=ALLOWED_USER, query="藍牙耳機")

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["error"]["code"], "ALLOWLIST_NOT_CONFIGURED")

    def test_admin_status_allowed_for_admin_without_echoing_user_id(self) -> None:
        result = core.run_p0_lookup(user_id=ADMIN_USER, action="admin_status")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["action"], "admin_status")
        self.assertNotIn("authorized_admin_user_id", result["admin_summary"])
        self.assertNotIn(ADMIN_USER, json.dumps(result, ensure_ascii=False))

    def test_admin_status_rejected_for_non_admin(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_ALLOWED_USERS": f"{ALLOWED_USER},{UNAUTHORIZED_USER}",
                "ADMIN_TELEGRAM_USERS": ADMIN_USER,
            },
            clear=False,
        ):
            result = core.run_p0_lookup(user_id=UNAUTHORIZED_USER, action="admin_status")

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["error"]["code"], "UNAUTHORIZED_ADMIN")

    def test_lookup_and_admin_results_do_not_echo_authorized_user_id(self) -> None:
        lookup_result = core.run_p0_lookup(user_id=ALLOWED_USER, query="藍牙耳機")
        admin_result = core.run_p0_lookup(user_id=ADMIN_USER, action="admin_status")

        self.assertNotIn(ALLOWED_USER, json.dumps(lookup_result, ensure_ascii=False))
        self.assertNotIn(ADMIN_USER, json.dumps(admin_result, ensure_ascii=False))

    def test_ssrf_and_non_http_urls_are_blocked(self) -> None:
        blocked_urls = [
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://172.16.0.1/",
            "http://192.168.0.1/",
            "http://100.64.0.1/",
            "http://[::1]/",
            "http://[fc00::1]/",
            "http://[fe80::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://169.254.169.254/latest/meta-data/",
            "http://metadata.google.internal/",
            "http://localhost/",
            "file:///etc/passwd",
        ]
        for url in blocked_urls:
            with self.subTest(url=url):
                info = core.validate_and_extract_url_info(url)
                self.assertTrue(info["provided"])
                self.assertFalse(info["valid"])
                self.assertTrue(info["ssrf_blocked"])

    def test_normal_public_ip_url_is_allowed_without_fetching(self) -> None:
        with patch("socket.create_connection", side_effect=AssertionError("network used")):
            info = core.validate_and_extract_url_info("https://8.8.8.8/product/widget")

        self.assertTrue(info["valid"])
        self.assertFalse(info["ssrf_blocked"])
        self.assertEqual(info["extracted_keywords"], "widget")

    def test_mock_miss_is_not_reported_as_exempt(self) -> None:
        result = core.run_p0_lookup(user_id=ALLOWED_USER, query="火星樣本XYZ987")

        self.assertEqual(result["status"], "needs_more_info")
        self.assertEqual(
            result["data"]["bsmi_check"]["status"],
            "not_found_in_mock",
        )
        self.assertIn("不等於免驗", result["data"]["bsmi_check"]["label"])
        self.assertNotIn("免驗：是", result["data"]["bsmi_check"]["label"])

    def test_cli_exits_3_for_unauthorized_user(self) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOL_PATH),
                "--user-id",
                UNAUTHORIZED_USER,
                "--query",
                "藍牙耳機",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 3)
        self.assertEqual(json.loads(completed.stdout)["status"], "unauthorized")
        self.assertNotIn(UNAUTHORIZED_USER, completed.stdout + completed.stderr)

    def test_cli_exits_0_for_successful_lookup(self) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOL_PATH),
                "--user-id",
                ALLOWED_USER,
                "--query",
                "藍牙耳機",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["status"], "ok")

    def test_cli_exits_2_for_database_error(self) -> None:
        broken_db = Path(self.temp_dir.name) / "broken.db"
        sqlite3.connect(broken_db).close()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOL_PATH),
                "--user-id",
                ALLOWED_USER,
                "--query",
                "藍牙耳機",
                "--db-path",
                str(broken_db),
            ],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(
            json.loads(completed.stdout)["error"]["code"],
            "MOCK_DB_UNAVAILABLE",
        )

    def test_unauthorized_lookup_does_not_create_database(self) -> None:
        missing_db = Path(self.temp_dir.name) / "must-not-exist.db"
        with patch.dict(
            os.environ,
            {
                "P0_MOCK_DB_PATH": str(missing_db),
                "TELEGRAM_ALLOWED_USERS": ALLOWED_USER,
                "ADMIN_TELEGRAM_USERS": ADMIN_USER,
            },
            clear=False,
        ):
            result = core.run_p0_lookup(
                user_id=UNAUTHORIZED_USER,
                query="藍牙耳機",
            )

        self.assertEqual(result["status"], "unauthorized")
        self.assertFalse(missing_db.exists())

    def test_unauthorized_admin_does_not_create_audit_row(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            before = conn.execute("SELECT COUNT(*) FROM lookup_audit_log").fetchone()[0]
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_ALLOWED_USERS": f"{ALLOWED_USER},{UNAUTHORIZED_USER}",
                "ADMIN_TELEGRAM_USERS": ADMIN_USER,
            },
            clear=False,
        ):
            result = core.run_p0_lookup(
                user_id=UNAUTHORIZED_USER,
                action="admin_status",
            )
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            after = conn.execute("SELECT COUNT(*) FROM lookup_audit_log").fetchone()[0]

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(before, after)

    def test_legacy_mock_db_without_digest_can_be_explicitly_reset(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM dataset_meta WHERE key = 'content_digest'")

        with self.assertRaises(ValueError):
            core.ensure_mock_db(self.db_path)

        core.ensure_mock_db(self.db_path, force_reset=True)
        with core.open_verified_mock_db(self.db_path) as conn:
            meta = dict(conn.execute("SELECT key, value FROM dataset_meta"))
            tariff_count = conn.execute(
                "SELECT COUNT(*) FROM mock_tariffs"
            ).fetchone()[0]

        self.assertEqual(meta["dataset_version"], core.DATASET_VERSION)
        self.assertEqual(len(meta["content_digest"]), 64)
        self.assertEqual(tariff_count, len(core.MOCK_SEED_RECORDS))

    def test_arbitrary_database_with_unexpected_table_cannot_be_reset(self) -> None:
        arbitrary_db = Path(self.temp_dir.name) / "arbitrary.db"
        with closing(sqlite3.connect(arbitrary_db)) as conn, conn:
            conn.execute("CREATE TABLE customer_data (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO customer_data DEFAULT VALUES")
            conn.execute("CREATE TABLE dataset_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT INTO dataset_meta VALUES (?, ?)",
                [
                    ("dataset_version", core.DATASET_VERSION),
                    ("updated_at", "2026-01-01T00:00:00+00:00"),
                    ("is_mock_data", "true"),
                    ("notice", core.P0_DISCLAIMER),
                ],
            )
            conn.execute(
                "CREATE TABLE mock_tariffs (ccc_code TEXT PRIMARY KEY)"
            )

        with self.assertRaises(ValueError):
            core.ensure_mock_db(arbitrary_db, force_reset=True)

        with closing(sqlite3.connect(arbitrary_db)) as conn, conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            row_count = conn.execute("SELECT COUNT(*) FROM customer_data").fetchone()[0]
        self.assertIn("customer_data", tables)
        self.assertEqual(row_count, 1)

    def test_existing_empty_database_is_not_silently_seeded(self) -> None:
        empty_db = Path(self.temp_dir.name) / "empty.db"
        sqlite3.connect(empty_db).close()

        with self.assertRaises(ValueError):
            core.ensure_mock_db(empty_db)
        with self.assertRaises(ValueError):
            core.ensure_mock_db(empty_db, force_reset=True)

        with closing(sqlite3.connect(empty_db)) as conn, conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        self.assertEqual(tables, [])

    def test_modified_row_fails_content_digest_validation(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE mock_tariffs SET title_zh = ? WHERE ccc_code = ?",
                ("[P0測試假資料] 未經授權的內容變更", "8518.30.10.00-3"),
            )

        with self.assertRaises(ValueError):
            core.ensure_mock_db(self.db_path)

        result = core.run_p0_lookup(user_id=ALLOWED_USER, query="藍牙耳機")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "MOCK_DB_UNAVAILABLE")

    def test_missing_or_invalid_digest_fails_closed(self) -> None:
        for operation in (
            "DELETE FROM dataset_meta WHERE key = 'content_digest'",
            "UPDATE dataset_meta SET value = 'not-a-sha256' "
            "WHERE key = 'content_digest'",
        ):
            with self.subTest(operation=operation):
                core.ensure_mock_db(self.db_path, force_reset=True)
                with closing(sqlite3.connect(self.db_path)) as conn, conn:
                    conn.execute(operation)

                with self.assertRaises(ValueError):
                    core.ensure_mock_db(self.db_path)


class P0PairingAuthTests(unittest.TestCase):
    """Authorization must honor Hermes DM pairing in addition to the env allowlist."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "p0_test_tariff.db"
        core.ensure_mock_db(self.db_path, force_reset=True)
        self.pairing_dir = Path(self.temp_dir.name) / "pairing"
        self.pairing_dir.mkdir(parents=True, exist_ok=True)
        self.approved_path = self.pairing_dir / "platforms" / "pairing" / "telegram-approved.json"
        self.approved_path.parent.mkdir(parents=True, exist_ok=True)
        self.env_patcher = patch.dict(
            os.environ,
            {
                # Env allowlist intentionally empty; only pairing grants access.
                "TELEGRAM_ALLOWED_USERS": "",
                "ADMIN_TELEGRAM_USERS": "",
                "P0_MOCK_DB_PATH": str(self.db_path),
                "HERMES_HOME": str(self.pairing_dir),
            },
            clear=False,
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)

    def _write_approved(self, payload: dict) -> None:
        self.approved_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_paired_user_can_lookup_with_empty_env_allowlist(self) -> None:
        PAIRED = "200000002"
        self._write_approved({PAIRED: {"user_name": "tester", "approved_at": 1.0}})

        result = core.run_p0_lookup(user_id=PAIRED, query="藍牙耳機")

        self.assertEqual(result["status"], "ok")
        self.assertGreaterEqual(len(result["data"]["candidates"]), 1)
        self.assertNotIn(PAIRED, json.dumps(result, ensure_ascii=False))

    def test_non_paired_user_is_rejected_when_allowlist_empty(self) -> None:
        self._write_approved({"200000002": {"user_name": "tester", "approved_at": 1.0}})

        result = core.run_p0_lookup(user_id="200000099", query="藍牙耳機")

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["error"]["code"], "UNAUTHORIZED_USER")

    def test_paired_user_cannot_use_admin_status(self) -> None:
        PAIRED = "200000002"
        self._write_approved({PAIRED: {"user_name": "tester", "approved_at": 1.0}})
        # Admin list stays empty: pairing never grants admin.

        result = core.run_p0_lookup(user_id=PAIRED, action="admin_status")

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["error"]["code"], "UNAUTHORIZED_ADMIN")

    def test_malformed_approved_file_fails_closed(self) -> None:
        # Not a JSON object: must not grant or throw.
        self.approved_path.write_text("this is not json", encoding="utf-8")

        result = core.run_p0_lookup(user_id="200000002", query="藍牙耳機")

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["error"]["code"], "ALLOWLIST_NOT_CONFIGURED")

    def test_legacy_pairing_dir_is_also_read(self) -> None:
        PAIRED = "200000002"
        legacy_dir = Path(self.temp_dir.name) / "legacy_pairing"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        legacy_path = legacy_dir / "pairing" / "telegram-approved.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(
            json.dumps({PAIRED: {"user_name": "tester"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.environ["HERMES_HOME"] = str(legacy_dir)

        result = core.run_p0_lookup(user_id=PAIRED, query="藍牙耳機")

        self.assertEqual(result["status"], "ok")

    def test_external_default_hermes_home_is_not_read(self) -> None:
        external_home = Path(self.temp_dir.name) / "external_home"
        external_approved = (
            external_home / ".hermes" / "platforms" / "pairing" / "telegram-approved.json"
        )
        external_approved.parent.mkdir(parents=True, exist_ok=True)
        external_approved.write_text(
            json.dumps(
                {"200000003": {"user_name": "outside-profile", "approved_at": 1.0}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {"HOME": str(external_home)}, clear=False):
            result = core.run_p0_lookup(user_id="200000003", query="藍牙耳機")

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["error"]["code"], "ALLOWLIST_NOT_CONFIGURED")


@unittest.skipUnless(
    (HERMES_ROOT / "hermes_cli" / "plugins.py").is_file(),
    "Hermes installation is not available",
)
class HermesPluginIntegrationTests(unittest.TestCase):
    def test_real_hermes_discovers_plugin_and_dispatches_json(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            hermes_home = temp_path / "hermes_home"
            hermes_home.mkdir()
            db_path = temp_path / "p0_plugin_test.db"
            core.ensure_mock_db(db_path, force_reset=True)
            image_cache_dir = temp_path / "image_cache"
            image_cache_dir.mkdir()
            image_path = image_cache_dir / "product-earbuds.png"
            image_path.write_bytes(VALID_PNG_BYTES)
            (hermes_home / "config.yaml").write_text(
                "plugins:\n"
                "  enabled:\n"
                "    - taiwan_import_p0\n"
                "gateway:\n"
                "  unauthorized_dm_behavior: ignore\n",
                encoding="utf-8",
            )

            script = r'''
import json
import os
from pathlib import Path
import sys

project = Path(os.environ["TIC_PROJECT_ROOT"]).resolve()
hermes_root = Path(os.environ["HERMES_INSTALL_ROOT"]).resolve()
sys.path = [
    item for item in sys.path
    if Path(item or os.getcwd()).resolve() != project
]
sys.path.insert(0, str(hermes_root))

from hermes_cli.plugins import PluginManager
from gateway.session_context import clear_session_vars, set_session_vars
from tools.registry import registry

manager = PluginManager()
manager.discover_and_load(force=True)
loaded = manager._plugins.get("taiwan_import_p0")
if loaded is None:
    raise SystemExit("taiwan_import_p0 was not discovered")
if loaded.error:
    raise SystemExit(f"plugin load error: {loaded.error}")
if not loaded.enabled:
    raise SystemExit("taiwan_import_p0 was not enabled")

entry = registry.get_entry("p0_import_lookup")
if entry is None:
    raise SystemExit("p0_import_lookup was not registered")

schema = entry.schema
properties = schema.get("parameters", {}).get("properties", {})
if "user_id" in properties:
    raise SystemExit("schema must not expose user_id")

session_tokens = set_session_vars(
    platform="telegram",
    source="test",
    chat_id="test-chat",
    chat_type="dm",
    user_id=os.environ["HERMES_SESSION_USER_ID"],
)
try:
    raw_result = registry.dispatch("p0_import_lookup", {"query": "藍牙耳機"})
    result = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    if result.get("status") != "ok":
        raise SystemExit("registry dispatch did not return an ok lookup")

    raw_forbidden = registry.dispatch(
        "p0_import_lookup",
        {"query": "藍牙耳機", "user_id": os.environ["HERMES_SESSION_USER_ID"]},
    )
    forbidden = json.loads(raw_forbidden) if isinstance(raw_forbidden, str) else raw_forbidden
    if forbidden.get("error", {}).get("code") != "INVALID_ARGUMENTS":
        raise SystemExit("schema-external user_id was not rejected")

    raw_media = registry.dispatch(
        "p0_import_lookup",
        {
            "image_path": os.environ["TIC_TEST_IMAGE_PATH"],
            "image_description": "白色真無線藍牙耳機與充電盒",
            "product_url": "https://shop.example.com/products/bluetooth-earbuds",
        },
    )
    media_result = json.loads(raw_media) if isinstance(raw_media, str) else raw_media
    if media_result.get("status") != "ok":
        raise SystemExit("image and URL dispatch did not return an ok lookup")
    if not media_result.get("input_summary", {}).get("has_image"):
        raise SystemExit("image input was not preserved by plugin dispatch")
    if not media_result.get("input_summary", {}).get("has_url"):
        raise SystemExit("product URL was not preserved by plugin dispatch")
finally:
    clear_session_vars(session_tokens)

payload = {
    "loaded": True,
    "enabled": loaded.enabled,
    "tools_registered": loaded.tools_registered,
    "toolset": entry.toolset,
    "schema_has_user_id": "user_id" in properties,
    "lookup_status": result.get("status"),
    "media_lookup_status": media_result.get("status"),
    "media_has_image": media_result.get("input_summary", {}).get("has_image"),
    "media_has_url": media_result.get("input_summary", {}).get("has_url"),
    "user_id_echoed": os.environ["HERMES_SESSION_USER_ID"] in json.dumps(
        result, ensure_ascii=False
    ),
    "forbidden_args_code": forbidden.get("error", {}).get("code"),
}
print(json.dumps(payload, ensure_ascii=False))
'''

            env = {
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                "WINDIR": os.environ.get("WINDIR", ""),
                "PATH": os.environ.get("PATH", ""),
                "TEMP": temp_dir,
                "TMP": temp_dir,
                "HOME": temp_dir,
                "USERPROFILE": temp_dir,
                "HERMES_HOME": str(hermes_home),
                "HERMES_INSTALL_ROOT": str(HERMES_ROOT),
                "HERMES_ENABLE_PROJECT_PLUGINS": "1",
                "HERMES_SESSION_USER_ID": ALLOWED_USER,
                "TELEGRAM_ALLOWED_USERS": ALLOWED_USER,
                "ADMIN_TELEGRAM_USERS": ADMIN_USER,
                "P0_MOCK_DB_PATH": str(db_path),
                "IMAGE_CACHE_DIR": str(image_cache_dir),
                "TIC_TEST_IMAGE_PATH": str(image_path),
                "PYTHONDONTWRITEBYTECODE": "1",
                "TIC_PROJECT_ROOT": str(PROJECT_ROOT),
            }
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )

            self.assertEqual(
                completed.returncode,
                0,
                msg=f"Hermes discovery failed: {completed.stderr[-2000:]}",
            )
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertTrue(payload["loaded"])
            self.assertTrue(payload["enabled"])
            self.assertIn("p0_import_lookup", payload["tools_registered"])
            self.assertEqual(payload["toolset"], "taiwan_import_p0")
            self.assertFalse(payload["schema_has_user_id"])
            self.assertEqual(payload["lookup_status"], "ok")
            self.assertEqual(payload["media_lookup_status"], "ok")
            self.assertTrue(payload["media_has_image"])
            self.assertTrue(payload["media_has_url"])
            self.assertFalse(payload["user_id_echoed"])
            self.assertEqual(payload["forbidden_args_code"], "INVALID_ARGUMENTS")


if __name__ == "__main__":
    unittest.main()
