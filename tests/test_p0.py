"""P0 tests for the local mock lookup and Hermes project plugin integration."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = PROJECT_ROOT / "tools" / "p0_import_lookup.py"
TEST_TEMP_ROOT = Path(
    os.environ.get("P0_TEST_TEMP_ROOT", PROJECT_ROOT / ".tmp" / "tests")
)
TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
HERMES_ROOT = Path(
    os.environ.get("HERMES_INSTALL_ROOT", Path(sys.executable).resolve().parents[2])
)
ALLOWED_USER = "100000001"
ADMIN_USER = "100000001"
UNAUTHORIZED_USER = "100000009"


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

        self.env_patcher = patch.dict(
            os.environ,
            {
                "TELEGRAM_ALLOWED_USERS": ALLOWED_USER,
                "ADMIN_TELEGRAM_USERS": ADMIN_USER,
                "P0_MOCK_DB_PATH": str(self.db_path),
                "P0_ALLOW_LIVE_URL_FETCH": "0",
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
        image_path = Path(self.temp_dir.name) / "product-earbuds.png"
        image_path.write_bytes(b"P0 test image placeholder")

        result = core.run_p0_lookup(user_id=ALLOWED_USER, image_path=str(image_path))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["input_summary"]["has_image"])
        self.assertEqual(result["input_summary"]["image_info"]["file_name"], image_path.name)

    def test_image_description_is_accepted(self) -> None:
        result = core.run_p0_lookup(
            user_id=ALLOWED_USER,
            image_description="白色真無線藍牙耳機與充電盒",
        )

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["input_summary"]["has_image"])
        self.assertGreaterEqual(len(result["data"]["candidates"]), 1)

    def test_product_url_is_parsed_without_live_fetch(self) -> None:
        result = core.run_p0_lookup(
            user_id=ALLOWED_USER,
            product_url="https://shop.example.com/products/bluetooth-earbuds",
        )

        self.assertEqual(result["status"], "ok")
        url_info = result["input_summary"]["url_info"]
        self.assertTrue(url_info["valid"])
        self.assertFalse(url_info["live_fetch_attempted"])
        self.assertGreaterEqual(len(result["data"]["candidates"]), 1)

    def test_missing_user_id_is_rejected(self) -> None:
        result = core.run_p0_lookup(user_id="", query="藍牙耳機")

        self.assertEqual(result["status"], "unauthorized")
        self.assertEqual(result["error"]["code"], "MISSING_USER_ID")

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
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
        ]
        for url in blocked_urls:
            with self.subTest(url=url):
                info = core.validate_and_extract_url_info(url)
                self.assertTrue(info["provided"])
                self.assertFalse(info["valid"])
                self.assertTrue(info["ssrf_blocked"])

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
            image_path = temp_path / "product-earbuds.png"
            image_path.write_bytes(b"P0 test image placeholder")
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
                "TIC_TEST_IMAGE_PATH": str(image_path),
                "P0_ALLOW_LIVE_URL_FETCH": "0",
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
