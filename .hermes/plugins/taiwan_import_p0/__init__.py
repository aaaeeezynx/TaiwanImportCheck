"""Hermes P0 plugin for the local Taiwan import mock lookup tool."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOOL_PATH = PROJECT_ROOT / "tools" / "p0_import_lookup.py"
ALLOWED_ARGUMENTS = {
    "query",
    "product_url",
    "image_path",
    "image_description",
    "action",
}
ALLOWED_ACTIONS = {"lookup", "admin_status"}
_core_module: Any = None


def _load_core() -> Any:
    """Load the project tool without importing the workspace ``tools`` package."""
    global _core_module
    if _core_module is not None:
        return _core_module

    spec = importlib.util.spec_from_file_location("tic_p0_import_lookup", TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("P0 tool module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _core_module = module
    return module


def _json_error(code: str, message: str) -> str:
    return json.dumps(
        {
            "status": "error",
            "is_mock_data": True,
            "error": {"code": code, "message": message},
        },
        ensure_ascii=False,
    )


def _optional_string(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _handle_p0_import_lookup(params: dict[str, Any], **kwargs: Any) -> str:
    """Run an authorized lookup using identity from the Hermes session context."""
    del kwargs
    if not isinstance(params, dict):
        return _json_error("INVALID_ARGUMENTS", "工具參數格式不正確。")
    if set(params) - ALLOWED_ARGUMENTS:
        return _json_error("INVALID_ARGUMENTS", "工具參數含不允許的欄位。")

    try:
        from gateway.session_context import get_session_env

        user_id = get_session_env("HERMES_SESSION_USER_ID", "")
        action = _optional_string(params, "action") or "lookup"
        action = action.strip().lower()
        if action not in ALLOWED_ACTIONS:
            return _json_error("INVALID_ACTION", "不支援的工具動作。")

        result = _load_core().run_p0_lookup(
            user_id=user_id,
            query=_optional_string(params, "query"),
            product_url=_optional_string(params, "product_url"),
            image_path=_optional_string(params, "image_path"),
            image_description=_optional_string(params, "image_description"),
            action=action,
        )
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return _json_error(
            "P0_TOOL_ERROR",
            "P0 查詢工具暫時無法執行；請稍後再試或聯絡管理者。",
        )


def register(ctx: Any) -> None:
    schema = {
        "name": "p0_import_lookup",
        "description": (
            "查詢 P0 本地 Mock SQLite 測試資料庫。當授權的 Telegram 使用者提供商品文字、"
            "商品圖片或商品 URL 時呼叫。身分由 Hermes session context 注入，不要要求使用者提供 ID。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "商品名稱、描述、材質、用途或使用者文字。",
                },
                "product_url": {
                    "type": "string",
                    "description": "使用者提供的公開 http/https 商品網址。",
                },
                "image_path": {
                    "type": "string",
                    "description": "Hermes 可存取的本地商品圖片路徑；沒有路徑時可省略。",
                },
                "image_description": {
                    "type": "string",
                    "description": "Hermes 對商品圖片的客觀描述，例如外觀、材質、用途與可見標示。",
                },
                "action": {
                    "type": "string",
                    "enum": ["lookup", "admin_status"],
                    "default": "lookup",
                    "description": "一般商品查詢使用 lookup；只有管理員狀態檢查使用 admin_status。",
                },
            },
            "additionalProperties": False,
        },
    }

    handler: Callable[..., str] = _handle_p0_import_lookup
    ctx.register_tool(
        name="p0_import_lookup",
        toolset="taiwan_import_p0",
        schema=schema,
        handler=handler,
        description="P0 本地 Mock SQLite 商品查詢工具（非正式稅則或法規建議）。",
    )
