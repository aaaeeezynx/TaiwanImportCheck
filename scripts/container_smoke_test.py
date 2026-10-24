#!/usr/bin/env python3
"""Run the P0 Hermes plugin smoke test inside the built Docker image."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


PROJECT_ROOT = Path(os.environ.get("TIC_PROJECT_ROOT", "/workspace/app")).resolve()
HERMES_ROOT = Path(os.environ.get("HERMES_INSTALL_ROOT", "/opt/hermes")).resolve()
ALLOWED_USER = os.environ.get("TIC_SMOKE_ALLOWED_USER", "100000001")
UNAUTHORIZED_USER = os.environ.get("TIC_SMOKE_UNAUTHORIZED_USER", "100000009")
VALID_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
)

# Keep smoke tests isolated from the real Telegram allowlists.
os.environ["TELEGRAM_ALLOWED_USERS"] = ALLOWED_USER
os.environ["ADMIN_TELEGRAM_USERS"] = ALLOWED_USER

os.chdir(PROJECT_ROOT)
sys.path = [
    item
    for item in sys.path
    if Path(item or os.getcwd()).resolve() != PROJECT_ROOT
]
sys.path.insert(0, str(HERMES_ROOT))

from gateway.session_context import clear_session_vars, set_session_vars  # noqa: E402
from hermes_cli.plugins import PluginManager  # noqa: E402
from tools.registry import registry  # noqa: E402


def _ensure(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def _dispatch_as(user_id: str, params: dict[str, Any]) -> dict[str, Any]:
    tokens = set_session_vars(
        platform="telegram",
        source="smoke_test",
        chat_id="p0-smoke-chat",
        chat_type="private",
        user_id=user_id,
    )
    try:
        raw_result = registry.dispatch("p0_import_lookup", params)
    finally:
        clear_session_vars(tokens)

    _ensure(isinstance(raw_result, str), "tool dispatch did not return JSON text")
    result = json.loads(raw_result)
    _ensure(isinstance(result, dict), "tool dispatch did not return a JSON object")
    return result


@contextmanager
def _isolated_runtime():
    with tempfile.TemporaryDirectory(prefix="tic-p0-smoke-") as temp_dir:
        smoke_root = Path(temp_dir)
        db_path = smoke_root / "p0_smoke_tariff.db"
        image_cache_dir = smoke_root / "image_cache"
        hermes_home = smoke_root / "hermes_home"
        image_cache_dir.mkdir()
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text(
            "plugins:\n"
            "  enabled:\n"
            "    - taiwan_import_p0\n"
            "gateway:\n"
            "  unauthorized_dm_behavior: ignore\n",
            encoding="utf-8",
        )
        os.environ.update(
            {
                "P0_MOCK_DB_PATH": str(db_path),
                "IMAGE_CACHE_DIR": str(image_cache_dir),
                "HERMES_HOME": str(hermes_home),
                "HERMES_ENABLE_PROJECT_PLUGINS": "1",
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "scripts" / "init_mock_db.py"),
                "--db-path",
                str(db_path),
            ],
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit("isolated mock DB initialization failed")
        yield smoke_root


def _run_smoke() -> int:
    manager = PluginManager()
    manager.discover_and_load(force=True)

    loaded = manager._plugins.get("taiwan_import_p0")
    _ensure(loaded is not None, "taiwan_import_p0 was not discovered")
    _ensure(not loaded.error, f"plugin load error: {loaded.error}")
    _ensure(loaded.enabled, "taiwan_import_p0 was not enabled")
    _ensure(
        "p0_import_lookup" in loaded.tools_registered,
        "p0_import_lookup was not registered",
    )

    entry = registry.get_entry("p0_import_lookup")
    _ensure(entry is not None, "p0_import_lookup registry entry is missing")
    properties = entry.schema.get("parameters", {}).get("properties", {})
    _ensure("user_id" not in properties, "schema must not expose user_id")

    text_result = _dispatch_as(ALLOWED_USER, {"query": "藍牙耳機"})
    _ensure(text_result.get("status") == "ok", "text lookup was not ok")
    _ensure(
        "[P0測試假資料]" in text_result.get("disclaimer", ""),
        "text lookup did not include the P0 mock disclaimer",
    )
    _ensure(
        text_result.get("dataset_version") == "P0-MOCK-2026.10-TEST",
        "text lookup returned an unexpected dataset version",
    )

    image_cache_dir = Path(
        os.environ.get("IMAGE_CACHE_DIR", "/opt/data/image_cache")
    ).resolve()
    image_cache_dir.mkdir(parents=True, exist_ok=True)
    image_path = image_cache_dir / f"tic-p0-smoke-{os.getpid()}.png"
    image_path.write_bytes(VALID_PNG_BYTES)
    try:
        media_result = _dispatch_as(
            ALLOWED_USER,
            {
                "image_path": str(image_path),
                "image_description": "白色真無線藍牙耳機與充電盒",
                "product_url": "https://shop.example.com/products/bluetooth-earbuds",
            },
        )
    finally:
        image_path.unlink(missing_ok=True)
    _ensure(media_result.get("status") == "ok", "media lookup was not ok")
    _ensure(
        media_result.get("input_summary", {}).get("has_image") is True,
        "image input was not preserved",
    )
    _ensure(
        media_result.get("input_summary", {}).get("has_url") is True,
        "URL input was not preserved",
    )
    _ensure(
        media_result.get("input_summary", {}).get("image_info", {}).get(
            "file_valid"
        )
        is True,
        "image bytes were not validated",
    )

    forbidden_result = _dispatch_as(
        ALLOWED_USER,
        {"query": "藍牙耳機", "user_id": ALLOWED_USER},
    )
    _ensure(
        forbidden_result.get("error", {}).get("code") == "INVALID_ARGUMENTS",
        "schema-external user_id was not rejected",
    )

    unauthorized_result = _dispatch_as(UNAUTHORIZED_USER, {"query": "藍牙耳機"})
    _ensure(
        unauthorized_result.get("status") == "unauthorized",
        "unauthorized user was not rejected",
    )
    _ensure(
        UNAUTHORIZED_USER not in json.dumps(unauthorized_result, ensure_ascii=False),
        "unauthorized result echoed the user ID",
    )

    raw_missing_identity = registry.dispatch("p0_import_lookup", {"query": "藍牙耳機"})
    missing_identity = json.loads(raw_missing_identity)
    _ensure(
        missing_identity.get("error", {}).get("code") == "MISSING_USER_ID",
        "missing session identity did not fail closed",
    )
    _ensure(
        "[P0測試假資料]" in missing_identity.get("disclaimer", ""),
        "missing identity error omitted the P0 disclaimer",
    )

    admin_result = _dispatch_as(ALLOWED_USER, {"action": "admin_status"})
    _ensure(admin_result.get("status") == "ok", "admin status was not ok")
    _ensure(
        ALLOWED_USER not in json.dumps(admin_result, ensure_ascii=False),
        "admin result echoed the user ID",
    )

    print(
        json.dumps(
            {
                "status": "ok",
                "plugin_loaded": True,
                "plugin_enabled": loaded.enabled,
                "tool_registered": True,
                "schema_has_user_id": False,
                "text_lookup": text_result.get("status"),
                "media_lookup": media_result.get("status"),
                "image_input_preserved": True,
                "url_input_preserved": True,
                "forbidden_user_id_argument": forbidden_result.get("error", {}).get(
                    "code"
                ),
                "unauthorized_lookup": unauthorized_result.get("status"),
                "admin_status": admin_result.get("status"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    with _isolated_runtime():
        return _run_smoke()


if __name__ == "__main__":
    raise SystemExit(main())
