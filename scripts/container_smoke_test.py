#!/usr/bin/env python3
"""Run the P0 Hermes plugin smoke test inside the built Docker image."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(os.environ.get("TIC_PROJECT_ROOT", "/workspace/app")).resolve()
HERMES_ROOT = Path(os.environ.get("HERMES_INSTALL_ROOT", "/opt/hermes")).resolve()
ALLOWED_USER = os.environ.get("TIC_SMOKE_ALLOWED_USER", "100000001")
UNAUTHORIZED_USER = os.environ.get("TIC_SMOKE_UNAUTHORIZED_USER", "100000009")

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


def main() -> int:
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

    image_path = Path("/tmp/tic-p0-smoke-product.png")
    image_path.write_bytes(b"P0 smoke-test image placeholder")
    media_result = _dispatch_as(
        ALLOWED_USER,
        {
            "image_path": str(image_path),
            "image_description": "白色真無線藍牙耳機與充電盒",
            "product_url": "https://shop.example.com/products/bluetooth-earbuds",
        },
    )
    _ensure(media_result.get("status") == "ok", "media lookup was not ok")
    _ensure(
        media_result.get("input_summary", {}).get("has_image") is True,
        "image input was not preserved",
    )
    _ensure(
        media_result.get("input_summary", {}).get("has_url") is True,
        "URL input was not preserved",
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


if __name__ == "__main__":
    raise SystemExit(main())
