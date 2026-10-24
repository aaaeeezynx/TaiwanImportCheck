#!/usr/bin/env python3
"""
TaiwanImportCheck - P0 Local Python Lookup Tool (`p0_import_lookup`)

Responsibilities (P0 Technical Verification):
1. Enforce Telegram user allowlist (`TELEGRAM_ALLOWED_USERS` & `ADMIN_TELEGRAM_USERS`, fail-closed).
2. Accept text (`query`), product image (`image_path` / `image_description`), and product URL (`product_url`).
3. Validate product URL syntax without fetching the product page.
4. Query local mock SQLite database (`p0_mock_tariff.db`) and return structured JSON to Hermes.
5. Clearly mark all outputs as P0 mock data and enforce "BSMI not found != exempt".
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "official_data" / "p0_mock_tariff.db"
OFFICIAL_DB_PATH = PROJECT_ROOT / "data" / "official_data" / "official_reference.db"
DATASET_VERSION = "P0-MOCK-2026.10-TEST"
P0_DISCLAIMER = (
    "[P0測試假資料] 本結果來自本地測試用假資料庫（Mock SQLite），"
    "所有 CCC Code、稅率、輸入規定與檢驗狀態皆為技術驗證用虛構測試值，"
    "不構成正式報關、稅則歸列或法規免驗建議。"
)

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_PAIRING_FILE_BYTES = 1024 * 1024
MAX_PAIRING_ENTRIES = 1000
MAX_QUERY_CHARS = 4000
MAX_URL_CHARS = 2048
MAX_IMAGE_DESCRIPTION_CHARS = 4000
MAX_IMAGE_PATH_CHARS = 4096
IMAGE_HEAD_BYTES = 16
MAX_IMPORT_REGULATIONS = 50
MAX_MISSING_FIELDS = 50
IMAGE_MAGIC = {
    ".png": lambda head: head.startswith(b"\x89PNG\r\n\x1a\n"),
    ".jpg": lambda head: head.startswith(b"\xff\xd8\xff"),
    ".jpeg": lambda head: head.startswith(b"\xff\xd8\xff"),
    ".webp": lambda head: (
        len(head) >= 12 and head.startswith(b"RIFF") and head[8:12] == b"WEBP"
    ),
}

MOCK_DB_REQUIRED_COLUMNS = {
    "dataset_meta": ("key", "value"),
    "mock_tariffs": (
        "ccc_code",
        "title_zh",
        "title_en",
        "category",
        "keywords",
        "duty_first_col",
        "duty_second_col",
        "duty_third_col",
        "import_regs_json",
        "bsmi_status",
        "bsmi_note",
        "missing_fields_json",
    ),
    "lookup_audit_log": (
        "id",
        "created_at",
        "user_id",
        "action",
        "input_summary_json",
        "matched_count",
    ),
}
MOCK_DB_EXPECTED_SCHEMA = {
    "dataset_meta": (
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 1, None, 0),
    ),
    "mock_tariffs": (
        ("ccc_code", "TEXT", 0, None, 1),
        ("title_zh", "TEXT", 1, None, 0),
        ("title_en", "TEXT", 1, None, 0),
        ("category", "TEXT", 1, None, 0),
        ("keywords", "TEXT", 1, None, 0),
        ("duty_first_col", "TEXT", 1, None, 0),
        ("duty_second_col", "TEXT", 1, None, 0),
        ("duty_third_col", "TEXT", 1, None, 0),
        ("import_regs_json", "TEXT", 1, None, 0),
        ("bsmi_status", "TEXT", 1, None, 0),
        ("bsmi_note", "TEXT", 1, None, 0),
        ("missing_fields_json", "TEXT", 1, None, 0),
    ),
    "lookup_audit_log": (
        ("id", "INTEGER", 0, None, 1),
        ("created_at", "TEXT", 1, None, 0),
        ("user_id", "TEXT", 1, None, 0),
        ("action", "TEXT", 1, None, 0),
        ("input_summary_json", "TEXT", 1, None, 0),
        ("matched_count", "INTEGER", 1, None, 0),
    ),
}
MOCK_DB_TABLE_NAMES = frozenset(MOCK_DB_EXPECTED_SCHEMA)
MOCK_DB_DROP_ORDER = (
    "lookup_audit_log",
    "mock_tariffs",
    "dataset_meta",
)
MOCK_TARIFF_FIELDS = tuple(MOCK_DB_REQUIRED_COLUMNS["mock_tariffs"])
MOCK_META_FIELDS = {
    "dataset_version",
    "updated_at",
    "is_mock_data",
    "notice",
    "content_digest",
}
ASCII_HOST_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

MOCK_SEED_RECORDS: List[Dict[str, Any]] = [
    {
        "ccc_code": "8516.71.00.00-6",
        "title_zh": "[P0測試假資料] 家用無線便攜式電熱咖啡機（含充電電池）",
        "title_en": "[MOCK] Portable Electric Coffee Maker",
        "category": "小家電／電熱器具",
        "keywords": "咖啡機,coffee,espresso,濃縮咖啡,電熱,無線,充電,小家電,加熱,保溫杯",
        "duty_first_col": "10% (P0測試假值)",
        "duty_second_col": "免稅 (P0測試假值)",
        "duty_third_col": "25% (P0測試假值)",
        "import_regs_json": json.dumps(
            [
                {
                    "code": "C02",
                    "authority": "經濟部標準檢驗局 (BSMI)",
                    "summary": "[測試假資料] 屬於標準檢驗局公告應施檢驗品目參考，進口時須依規定辦理檢驗。",
                },
                {
                    "code": "602",
                    "authority": "國家通訊傳播委員會 (NCC)",
                    "summary": "[測試假資料] 若含藍牙或 Wi-Fi 無線射頻模組，需確認電信管制射頻器材相關規定。",
                },
            ],
            ensure_ascii=False,
        ),
        "bsmi_status": "hit",
        "bsmi_note": "命中 P0 測試應施檢驗清單（模擬電熱家電類 C02，實際仍須以標檢局正式品目表為準）",
        "missing_fields_json": json.dumps(
            [
                "額定電壓與消耗功率（例如 110V / 5V USB 充電）",
                "是否具備藍牙或 Wi-Fi 無線連線功能",
                "是否內建鋰電池及其瓦時數 (Wh)",
            ],
            ensure_ascii=False,
        ),
    },
    {
        "ccc_code": "8518.30.10.00-3",
        "title_zh": "[P0測試假資料] 真無線藍牙耳機（附充電盒）",
        "title_en": "[MOCK] True Wireless Bluetooth Earbuds",
        "category": "消費性電子／音訊設備",
        "keywords": "藍牙耳機,耳機,earbuds,airpods,headphone,tws,bluetooth,音訊,無線耳機,喇叭",
        "duty_first_col": "0% (P0測試假值)",
        "duty_second_col": "免稅 (P0測試假值)",
        "duty_third_col": "10% (P0測試假值)",
        "import_regs_json": json.dumps(
            [
                {
                    "code": "C02",
                    "authority": "經濟部標準檢驗局 (BSMI)",
                    "summary": "[測試假資料] 附充電盒或特定資訊影音設備可能涉及標檢局應施檢驗項目。",
                },
                {
                    "code": "602",
                    "authority": "國家通訊傳播委員會 (NCC)",
                    "summary": "[測試假資料] 具藍牙無線射頻功能之器材須留意 NCC 型式認證或自用申報規定。",
                },
            ],
            ensure_ascii=False,
        ),
        "bsmi_status": "hit",
        "bsmi_note": "命中 P0 測試應施檢驗清單（模擬資訊影音／充電盒類 C02，實際仍須以標檢局正式品目表為準）",
        "missing_fields_json": json.dumps(
            [
                "是否含藍牙／2.4GHz 無線射頻功能",
                "充電盒輸入電壓與鋰電池容量",
                "進口數量與用途（個人自用或商業販售）",
            ],
            ensure_ascii=False,
        ),
    },
    {
        "ccc_code": "3304.99.10.10-5",
        "title_zh": "[P0測試假資料] 臉部保濕乳霜／一般護膚化粧品（零售包裝）",
        "title_en": "[MOCK] Facial Moisturizing Cream / Skincare Cosmetic",
        "category": "美妝保養／化粧品",
        "keywords": "乳霜,面霜,保濕,化妝品,化粧品,護膚,精華液,skincare,cream,cosmetic,lotion,洗面乳",
        "duty_first_col": "5% (P0測試假值)",
        "duty_second_col": "免稅 (P0測試假值)",
        "duty_third_col": "15% (P0測試假值)",
        "import_regs_json": json.dumps(
            [
                {
                    "code": "508",
                    "authority": "衛生福利部食品藥物管理署 (TFDA)",
                    "summary": "[測試假資料] 進口化粧品須符合化粧品衛生安全管理法及產品登錄／標示相關規範。",
                }
            ],
            ensure_ascii=False,
        ),
        "bsmi_status": "not_found",
        "bsmi_note": "待確認（未命中 P0 測試清單，不等於免驗；化粧品另須留意衛福部食藥署 TFDA 規定）",
        "missing_fields_json": json.dumps(
            [
                "產品完整成分與宣稱用途（一般保濕或防曬／染髮／特定用途）",
                "是否為零售包裝及單件淨重／容量",
            ],
            ensure_ascii=False,
        ),
    },
    {
        "ccc_code": "9503.00.31.00-2",
        "title_zh": "[P0測試假資料] 塑膠製組裝模型玩具（無電力驅動）",
        "title_en": "[MOCK] Plastic Scale Model Toy Kit",
        "category": "玩具／嗜好收藏品",
        "keywords": "玩具,模型,公仔,積木,玩偶,toy,figure,model,gundam,lego,plush",
        "duty_first_col": "0% (P0測試假值)",
        "duty_second_col": "免稅 (P0測試假值)",
        "duty_third_col": "12.5% (P0測試假值)",
        "import_regs_json": json.dumps(
            [
                {
                    "code": "C02",
                    "authority": "經濟部標準檢驗局 (BSMI)",
                    "summary": "[測試假資料] 供 14 歲以下兒童玩耍之玩具屬標檢局應施檢驗品目參考。",
                }
            ],
            ensure_ascii=False,
        ),
        "bsmi_status": "hit",
        "bsmi_note": "命中 P0 測試應施檢驗清單（模擬兒童玩具類 C02，實際仍須以標檢局正式品目表為準）",
        "missing_fields_json": json.dumps(
            [
                "適用年齡標示（供 14 歲以下兒童玩耍或成人收藏模型）",
                "主要材質（塑膠、金屬或布絨）",
                "是否含電池、馬達或發光元件",
            ],
            ensure_ascii=False,
        ),
    },
    {
        "ccc_code": "3926.90.90.90-8",
        "title_zh": "[P0測試假資料] 其他塑膠製桌面收納盒／一般生活雜貨",
        "title_en": "[MOCK] Other Articles of Plastics / Desk Organizer",
        "category": "一般塑膠製品／生活雜貨",
        "keywords": "收納盒,塑膠盒,掛鉤,桌面,雜貨,organizer,plastic,box,holder,塑膠製品",
        "duty_first_col": "5% (P0測試假值)",
        "duty_second_col": "免稅 (P0測試假值)",
        "duty_third_col": "15% (P0測試假值)",
        "import_regs_json": json.dumps(
            [
                {
                    "code": "NONE-MOCK",
                    "authority": "P0 測試資料庫",
                    "summary": "[測試假資料] 本測試項目未列特定輸入規定代碼，但若接觸食品可能改列 3924 並涉及 F01。",
                }
            ],
            ensure_ascii=False,
        ),
        "bsmi_status": "not_found",
        "bsmi_note": "待確認（未命中 P0 測試清單，不等於免驗；實際是否屬應施檢驗品目仍須由標檢局或報關行確認）",
        "missing_fields_json": json.dumps(
            [
                "主要材質是否為塑膠（或複合材質）",
                "是否作為食品接觸容器或餐具使用（若接觸食品可能涉及食藥署 F01）",
            ],
            ensure_ascii=False,
        ),
    },
]


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(reparse_flag and attributes & reparse_flag)


def _safe_file_identity(file_stat: os.stat_result) -> Tuple[int, int]:
    return (int(file_stat.st_dev), int(file_stat.st_ino))


def _read_regular_file_no_follow(
    path: Path,
    max_bytes: int,
    read_limit: Optional[int] = None,
) -> Tuple[bytes, os.stat_result]:
    """
    Read a regular file through one verified descriptor.

    The path must not be a symlink, junction/reparse point, or hard link. The
    caller receives fail-closed OSError/ValueError behavior and never a path or
    file content in an error message.
    """
    try:
        path_before = os.lstat(path)
    except OSError as exc:
        raise OSError(errno.ENOENT, "file is unavailable") from exc

    if (
        stat.S_ISLNK(path_before.st_mode)
        or not stat.S_ISREG(path_before.st_mode)
        or _is_reparse_point(path_before)
        or int(path_before.st_nlink) > 1
    ):
        raise OSError(errno.ELOOP, "file is not a safe regular file")
    if int(path_before.st_size) > max_bytes:
        raise ValueError("file exceeds the configured size limit")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OSError(errno.ELOOP, "file could not be opened safely") from exc

    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse_point(opened)
            or int(opened.st_nlink) > 1
        ):
            raise OSError(errno.ELOOP, "file is not a safe regular file")
        if _safe_file_identity(path_before) != _safe_file_identity(opened):
            raise OSError(errno.ELOOP, "file changed while it was being opened")
        if int(opened.st_size) > max_bytes:
            raise ValueError("file exceeds the configured size limit")

        expected_bytes = int(opened.st_size)
        remaining = expected_bytes
        if read_limit is not None:
            remaining = min(remaining, max(0, int(read_limit)))
        chunks: List[bytes] = []
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if read_limit is None and len(raw) != expected_bytes:
            raise OSError(errno.EIO, "file changed while it was being read")
        if (
            int(after.st_size) != expected_bytes
            or _safe_file_identity(after) != _safe_file_identity(opened)
            or int(after.st_mtime_ns) != int(opened.st_mtime_ns)
        ):
            raise OSError(errno.EIO, "file changed while it was being read")
        return raw, opened
    finally:
        os.close(fd)


def _is_valid_telegram_user_id(value: str) -> bool:
    return bool(re.fullmatch(r"[1-9][0-9]{0,19}", value))


def _parse_id_set(raw: Optional[str]) -> Set[str]:
    if not raw:
        return set()
    return {
        item.strip()
        for item in raw.split(",")
        if item.strip() and _is_valid_telegram_user_id(item.strip())
    }


def get_allowed_users() -> Tuple[Set[str], Set[str]]:
    """Return (allowed_users, admin_users) from environment variables."""
    admin_users = _parse_id_set(os.getenv("ADMIN_TELEGRAM_USERS", ""))
    allowed_users = _parse_id_set(os.getenv("TELEGRAM_ALLOWED_USERS", "")) | admin_users
    return allowed_users, admin_users


def _read_pairing_ids(path: Path) -> Optional[Set[str]]:
    """
    Read one pairing file.

    ``None`` means the file does not exist. Once a path exists, every format or
    safety error is represented by an empty set and must not trigger a fallback.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return set()

    try:
        raw_bytes, _ = _read_regular_file_no_follow(
            path,
            max_bytes=MAX_PAIRING_FILE_BYTES,
        )
        data = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(data, dict) or len(data) > MAX_PAIRING_ENTRIES:
            return set()

        approved: Set[str] = set()
        for key, value in data.items():
            candidate = str(key).strip()
            if not _is_valid_telegram_user_id(candidate) or not isinstance(value, dict):
                return set()
            approved.add(candidate)
        return approved
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        # Never surface or log potentially sensitive pairing content.
        return set()


def get_approved_pairing_users(platform: str = "telegram") -> Set[str]:
    """
    Read user IDs approved via Hermes DM pairing from the pairing store.

    The pairing store mirrors the gateway's own authorization grant, so a user
    who completed the pairing handshake (operator approved via the CLI) is
    authorized to use the lookup tool without being listed in
    TELEGRAM_ALLOWED_USERS. Admin grants never come from pairing.

    Fail-closed: any read/parse error returns an empty set and never raises.
    """
    home = os.getenv("HERMES_HOME", "").strip()
    if home:
        # HERMES_HOME is authoritative. Do not fall back to another profile,
        # because a stale grant in ~/.hermes must not authorize this instance.
        roots = [home]
    else:
        roots = [os.path.join(os.path.expanduser("~"), ".hermes")]

    root = roots[0]
    canonical = Path(root) / "platforms" / "pairing" / f"{platform}-approved.json"
    approved = _read_pairing_ids(canonical)
    if approved is not None:
        return approved

    # Legacy format is consulted only when the canonical file is truly absent.
    legacy = Path(root) / "pairing" / f"{platform}-approved.json"
    legacy_approved = _read_pairing_ids(legacy)
    return legacy_approved if legacy_approved is not None else set()


def check_authorization(user_id: Optional[str], action: str = "lookup") -> Tuple[bool, str, str]:
    """
    Fail-closed authorization check.

    Authorization sources (union, all fail-closed):
      - TELEGRAM_ALLOWED_USERS / ADMIN_TELEGRAM_USERS environment variables
      - Hermes DM pairing approved store (operator approved via the CLI)

    Admin grants come ONLY from ADMIN_TELEGRAM_USERS; pairing never grants admin.
    Returns (is_authorized, error_code, error_message).
    """
    uid = str(user_id or "").strip()
    if not uid:
        return (
            False,
            "MISSING_USER_ID",
            "目前無法取得授權身分，查詢未執行；請從已授權的 Telegram 私聊使用本服務。",
        )

    allowed_users, admin_users = get_allowed_users()
    paired_users = get_approved_pairing_users()
    effective_allowed = allowed_users | paired_users

    if not effective_allowed:
        return (
            False,
            "ALLOWLIST_NOT_CONFIGURED",
            "目前沒有任何授權來源（環境白名單或配對核准），預設拒絕所有請求。",
        )

    if uid not in effective_allowed:
        return (
            False,
            "UNAUTHORIZED_USER",
            "目前 Telegram 使用者不在授權白名單內，無法呼叫查詢或管理工具。",
        )

    if action == "admin_status" and uid not in admin_users:
        return (
            False,
            "UNAUTHORIZED_ADMIN",
            "目前 Telegram 使用者沒有管理員權限，無法執行管理操作。",
        )

    return True, "OK", "Authorized"


def resolve_db_path(custom_db_path: Optional[str] = None) -> Path:
    raw = custom_db_path or os.getenv("P0_MOCK_DB_PATH")
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return Path(os.path.abspath(p))
    return Path(os.path.abspath(DEFAULT_DB_PATH))


def _paths_are_equal(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _validate_database_path(path: Path, require_exists: bool) -> Path:
    """Reject aliases, links and non-regular files before SQLite sees them."""
    candidate = Path(os.path.abspath(path))
    parent = candidate.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent_resolved = parent.resolve(strict=True)
    if not _paths_are_equal(parent, parent_resolved):
        raise ValueError("Mock DB parent path must not contain links")

    candidate = parent_resolved / candidate.name
    official = Path(os.path.abspath(OFFICIAL_DB_PATH)).resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    if _paths_are_equal(candidate_resolved, official):
        raise ValueError("Mock DB path must not point to the official database")

    try:
        path_stat = os.lstat(candidate)
    except FileNotFoundError:
        if require_exists:
            raise ValueError("Mock DB file is unavailable")
        return candidate
    except OSError as exc:
        raise ValueError("Mock DB path could not be inspected") from exc

    if (
        stat.S_ISLNK(path_stat.st_mode)
        or not stat.S_ISREG(path_stat.st_mode)
        or _is_reparse_point(path_stat)
        or int(path_stat.st_nlink) > 1
    ):
        raise ValueError("Mock DB must be a regular, non-linked file")

    try:
        if official.exists() and os.path.samefile(candidate, official):
            raise ValueError("Mock DB path must not point to the official database")
    except FileNotFoundError:
        pass
    return candidate


def _directory_identity(path: Path) -> Tuple[int, int]:
    info = os.stat(path, follow_symlinks=False)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or _is_reparse_point(info)
    ):
        raise ValueError("Mock DB parent directory is unsafe")
    return info.st_dev, info.st_ino


def _file_identity(path: Path) -> Optional[Tuple[int, int]]:
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if _is_reparse_point(info) or not stat.S_ISREG(info.st_mode):
        raise ValueError("Mock DB file identity is unsafe")
    return info.st_dev, info.st_ino


def _mock_content_digest(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        f"SELECT {', '.join(MOCK_TARIFF_FIELDS)} "
        "FROM mock_tariffs ORDER BY ccc_code"
    ).fetchall()
    canonical = {
        "schema_version": 1,
        "dataset_version": DATASET_VERSION,
        "fields": list(MOCK_TARIFF_FIELDS),
        "rows": [list(row) for row in rows],
    }
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _inspect_mock_database(
    conn: sqlite3.Connection,
) -> Tuple[str, Dict[str, str], int]:
    """Validate the complete mock metadata/schema before reset or lookup."""
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if not tables:
        return "new", {}, 0

    if tables != set(MOCK_DB_EXPECTED_SCHEMA):
        raise ValueError("Mock DB has unexpected or missing tables")

    for object_type in ("view", "trigger", "index"):
        unexpected = conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type = ? AND name NOT LIKE 'sqlite_autoindex_%'
               LIMIT 1""",
            (object_type,),
        ).fetchone()
        if unexpected:
            raise ValueError(f"Mock DB has an unexpected {object_type}")

    for table_name, expected_columns in MOCK_DB_EXPECTED_SCHEMA.items():
        actual_columns = tuple(
            (
                str(row[1]),
                str(row[2]).upper(),
                int(row[3]),
                row[4],
                int(row[5]),
            )
            for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        )
        if actual_columns != expected_columns:
            raise ValueError("Mock DB has an incompatible schema")

    meta = {
        str(row[0]): str(row[1])
        for row in conn.execute("SELECT key, value FROM dataset_meta").fetchall()
    }
    required_meta = {"dataset_version", "updated_at", "is_mock_data", "notice"}
    if not required_meta.issubset(meta):
        raise ValueError("Mock DB is missing required metadata")
    if set(meta) - MOCK_META_FIELDS:
        raise ValueError("Mock DB has unexpected metadata")
    if meta["dataset_version"] != DATASET_VERSION:
        raise ValueError("Mock DB version is not supported")
    if meta["is_mock_data"].strip().lower() != "true":
        raise ValueError("Database is not marked as mock data")
    if "[P0測試假資料]" not in meta["notice"]:
        raise ValueError("Mock DB disclaimer metadata is missing")

    tariff_count = int(
        conn.execute("SELECT COUNT(*) FROM mock_tariffs").fetchone()[0]
    )
    stored_digest = meta.get("content_digest")
    if (
        stored_digest is None
        or len(stored_digest) != 64
        or any(char not in "0123456789abcdef" for char in stored_digest)
        or stored_digest != _mock_content_digest(conn)
    ):
        raise ValueError("Mock DB content digest is missing or does not match")
    return "mock", meta, tariff_count


def _validate_mock_reset_candidate(conn: sqlite3.Connection) -> None:
    """Allow reset only when the database is recognizably an existing P0 mock DB."""
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if not tables:
        raise ValueError("Existing Mock DB is empty or not initialized")
    if not tables.issubset(MOCK_DB_TABLE_NAMES):
        raise ValueError("Mock DB reset rejected because it contains unexpected tables")
    if not {"dataset_meta", "mock_tariffs"}.issubset(tables):
        raise ValueError("Mock DB reset rejected because it is not recognizable")

    for object_type in ("view", "trigger", "index"):
        unexpected = conn.execute(
            """SELECT name FROM sqlite_master
               WHERE type = ? AND name NOT LIKE 'sqlite_autoindex_%'
               LIMIT 1""",
            (object_type,),
        ).fetchone()
        if unexpected:
            raise ValueError(
                f"Mock DB reset rejected because it contains an unexpected {object_type}"
            )

    for table_name in tables:
        expected_columns = {
            column[0] for column in MOCK_DB_EXPECTED_SCHEMA[table_name]
        }
        actual_columns = tuple(
            str(row[1])
            for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        )
        if (
            not actual_columns
            or len(actual_columns) != len(set(actual_columns))
            or any(column not in expected_columns for column in actual_columns)
        ):
            raise ValueError("Mock DB reset rejected because its schema is incompatible")

    meta_columns = tuple(
        str(row[1])
        for row in conn.execute('PRAGMA table_info("dataset_meta")').fetchall()
    )
    if meta_columns != ("key", "value"):
        raise ValueError("Mock DB reset rejected because metadata schema is incompatible")

    meta = {
        str(row[0]): str(row[1])
        for row in conn.execute("SELECT key, value FROM dataset_meta").fetchall()
    }
    required_legacy_meta = {
        "dataset_version",
        "updated_at",
        "is_mock_data",
        "notice",
    }
    if not required_legacy_meta.issubset(meta):
        raise ValueError("Mock DB reset rejected because metadata is incomplete")
    if set(meta) - MOCK_META_FIELDS:
        raise ValueError("Mock DB reset rejected because metadata is unexpected")
    if not meta["dataset_version"].startswith("P0-MOCK-"):
        raise ValueError("Mock DB reset rejected because its version is not P0")
    if meta["is_mock_data"].strip().lower() != "true":
        raise ValueError("Mock DB reset rejected because it is not marked as mock data")
    if "[P0測試假資料]" not in meta["notice"]:
        raise ValueError("Mock DB reset rejected because its disclaimer is missing")


def _create_mock_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dataset_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mock_tariffs (
            ccc_code TEXT PRIMARY KEY,
            title_zh TEXT NOT NULL,
            title_en TEXT NOT NULL,
            category TEXT NOT NULL,
            keywords TEXT NOT NULL,
            duty_first_col TEXT NOT NULL,
            duty_second_col TEXT NOT NULL,
            duty_third_col TEXT NOT NULL,
            import_regs_json TEXT NOT NULL,
            bsmi_status TEXT NOT NULL,
            bsmi_note TEXT NOT NULL,
            missing_fields_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lookup_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            user_id TEXT NOT NULL,
            action TEXT NOT NULL,
            input_summary_json TEXT NOT NULL,
            matched_count INTEGER NOT NULL
        )
        """
    )


def _seed_mock_database(conn: sqlite3.Connection) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    meta_rows = [
        ("dataset_version", DATASET_VERSION),
        ("updated_at", now_iso),
        ("is_mock_data", "true"),
        ("notice", P0_DISCLAIMER),
    ]
    try:
        conn.execute("BEGIN IMMEDIATE")
        _create_mock_tables(conn)
        conn.executemany(
            "INSERT OR REPLACE INTO dataset_meta (key, value) VALUES (?, ?)",
            meta_rows,
        )
        for item in MOCK_SEED_RECORDS:
            conn.execute(
                """
                INSERT OR REPLACE INTO mock_tariffs (
                    ccc_code, title_zh, title_en, category, keywords,
                    duty_first_col, duty_second_col, duty_third_col,
                    import_regs_json, bsmi_status, bsmi_note, missing_fields_json
                ) VALUES (
                    :ccc_code, :title_zh, :title_en, :category, :keywords,
                    :duty_first_col, :duty_second_col, :duty_third_col,
                    :import_regs_json, :bsmi_status, :bsmi_note, :missing_fields_json
                )
                """,
                item,
            )
        conn.execute(
            "INSERT INTO dataset_meta (key, value) VALUES ('content_digest', ?)",
            (_mock_content_digest(conn),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _verify_open_mock_db(
    conn: sqlite3.Connection,
    target_path: Path,
    parent_identity: Tuple[int, int],
    file_identity: Tuple[int, int],
) -> Tuple[Dict[str, str], int]:
    validated_path = _validate_database_path(target_path, require_exists=True)
    if _directory_identity(validated_path.parent) != parent_identity:
        raise ValueError("Mock DB parent directory changed while open")
    if _file_identity(validated_path) != file_identity:
        raise ValueError("Mock DB file changed while open")
    database_path = conn.execute("PRAGMA database_list").fetchone()[2]
    if not _paths_are_equal(Path(os.path.abspath(database_path)), validated_path):
        raise ValueError("SQLite opened an unexpected Mock DB path")
    state, meta, tariff_count = _inspect_mock_database(conn)
    if state != "mock" or tariff_count <= 0:
        raise ValueError("Mock DB is not initialized")
    return meta, tariff_count


@contextmanager
def open_verified_mock_db(
    db_path: Optional[Path] = None,
    force_reset: bool = False,
):
    """Yield one verified connection and recheck its identity after use."""
    target_path = _validate_database_path(
        db_path or resolve_db_path(),
        require_exists=False,
    )
    parent_identity = _directory_identity(target_path.parent)
    original_file_identity = _file_identity(target_path)

    conn = sqlite3.connect(str(target_path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        target_path = _validate_database_path(target_path, require_exists=True)
        if _directory_identity(target_path.parent) != parent_identity:
            raise ValueError("Mock DB parent directory changed while opening")
        opened_file_identity = _file_identity(target_path)
        if (
            original_file_identity is not None
            and opened_file_identity != original_file_identity
        ):
            raise ValueError("Mock DB file changed while opening")
        database_path = conn.execute("PRAGMA database_list").fetchone()[2]
        if not _paths_are_equal(
            Path(os.path.abspath(database_path)),
            target_path,
        ):
            raise ValueError("SQLite opened an unexpected Mock DB path")

        existed_before_open = original_file_identity is not None
        if force_reset and existed_before_open:
            try:
                conn.execute("BEGIN IMMEDIATE")
                _validate_mock_reset_candidate(conn)
                for table_name in MOCK_DB_DROP_ORDER:
                    conn.execute(f'DROP TABLE IF EXISTS "{table_name}"')
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            state = "new"
            tariff_count = 0
        else:
            state, _, tariff_count = _inspect_mock_database(conn)

        if not existed_before_open and state == "new":
            _seed_mock_database(conn)
        elif force_reset and state == "new":
            _seed_mock_database(conn)
        elif state != "mock" or tariff_count <= 0:
            raise ValueError("Existing Mock DB is empty or not initialized")

        opened_file_identity = _file_identity(target_path)
        if opened_file_identity is None:
            raise ValueError("Mock DB file disappeared while opening")
        _verify_open_mock_db(
            conn,
            target_path,
            parent_identity,
            opened_file_identity,
        )
        yield conn
        _verify_open_mock_db(
            conn,
            target_path,
            parent_identity,
            opened_file_identity,
        )
    finally:
        conn.close()


def ensure_mock_db(db_path: Optional[Path] = None, force_reset: bool = False) -> Path:
    """Create/seed a verified P0 mock DB and fail closed for any other DB."""
    with open_verified_mock_db(db_path, force_reset=force_reset) as conn:
        database_path = conn.execute("PRAGMA database_list").fetchone()[2]
        return Path(os.path.abspath(database_path))


def _is_forbidden_ip(ip_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
        ip_obj = ip_obj.ipv4_mapped
    return not ip_obj.is_global


def _is_valid_dns_hostname(hostname: str) -> bool:
    if not hostname or len(hostname) > 253 or hostname.endswith("."):
        return False
    labels = hostname.split(".")
    if len(labels) < 2 or any(not ASCII_HOST_LABEL.fullmatch(label) for label in labels):
        return False
    top_level = labels[-1]
    return any(char.isalpha() for char in top_level)


def _url_rejection(reason: str) -> Dict[str, Any]:
    return {
        "provided": True,
        "valid": False,
        "ssrf_blocked": True,
        "reason": reason,
        "extracted_keywords": "",
    }


def validate_and_extract_url_info(product_url: Optional[str]) -> Dict[str, Any]:
    """
    Validate a product URL offline and extract safe keyword hints.

    The tool never resolves DNS, opens a network connection, or follows a
    redirect. Private/loopback/link-local/metadata literal hosts are rejected.
    """
    if not product_url or not str(product_url).strip():
        return {"provided": False}

    original_url = str(product_url)
    if any(
        char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F
        for char in original_url
    ):
        return _url_rejection("商品網址含有空白或控制字元，未進行解析。")

    raw_url = original_url.strip()
    if len(raw_url) > MAX_URL_CHARS:
        return _url_rejection("商品網址超過長度上限，未進行解析。")
    if "\\" in raw_url:
        return _url_rejection("商品網址含有反斜線，未進行解析。")

    try:
        parsed = urlparse(raw_url)
        hostname = (parsed.hostname or "").lower()
        explicit_port = parsed.port
    except ValueError:
        return _url_rejection("商品網址格式或連接埠不正確，未進行解析。")

    scheme = (parsed.scheme or "").lower()

    if scheme not in ("http", "https"):
        return _url_rejection(
            f"僅允許 http/https 商品網址，已阻擋協定：{scheme or '無'}"
        )

    if not hostname:
        return _url_rejection("網址缺少有效的主機名稱 (hostname)。")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return _url_rejection("商品網址不得包含使用者認證資訊。")
    if hostname.endswith("."):
        return _url_rejection("商品網址主機名稱不得以句點結尾。")
    if explicit_port is not None and not 1 <= explicit_port <= 65535:
        return _url_rejection("商品網址連接埠必須介於 1 到 65535。")

    netloc_without_user = parsed.netloc.rsplit("@", 1)[-1]
    if netloc_without_user.startswith("["):
        closing = netloc_without_user.find("]")
        if closing < 0:
            return _url_rejection("商品網址的 IPv6 主機格式不正確。")
        port_suffix = netloc_without_user[closing + 1 :]
        if port_suffix and not re.fullmatch(r":[0-9]+", port_suffix):
            return _url_rejection("商品網址的連接埠格式不正確。")
    elif netloc_without_user.count(":") > 1:
        return _url_rejection("商品網址的主機或連接埠格式不正確。")
    elif netloc_without_user.endswith(":"):
        return _url_rejection("商品網址的連接埠格式不正確。")

    blocked_hostnames = {"localhost", "localhost.localdomain", "metadata.google.internal"}
    if (
        hostname in blocked_hostnames
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
        or hostname.endswith(".localhost")
    ):
        return _url_rejection("安全規則阻擋內網、本機或 Metadata 主機名稱。")

    try:
        ipaddress.ip_address(hostname)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip and _is_forbidden_ip(hostname):
        return _url_rejection("安全規則阻擋私網、本機或 Metadata IP 位址。")
    if not is_literal_ip and not _is_valid_dns_hostname(hostname):
        return _url_rejection(
            "商品網址主機必須是完整公開 DNS 名稱或正規全域 IP 位址。"
        )

    decoded_path = unquote(f"{parsed.path} {parsed.query}")
    tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", decoded_path)
    stop_tokens = {"http", "https", "www", "com", "tw", "html", "htm", "item", "product", "products", "shop", "dp", "gp"}
    filtered_tokens = [t for t in tokens if t.lower() not in stop_tokens][:15]
    extracted_keywords = " ".join(filtered_tokens)

    return {
        "provided": True,
        "valid": True,
        "ssrf_blocked": False,
        "extracted_keywords": extracted_keywords,
        "extracted_keyword_count": len(filtered_tokens),
    }


def get_image_cache_root() -> Path:
    """Return the only directory from which product images may be read."""
    raw_cache_dir = os.getenv("IMAGE_CACHE_DIR", "").strip()
    if raw_cache_dir:
        root = Path(raw_cache_dir).expanduser()
    else:
        home = os.getenv("HERMES_HOME", "").strip()
        if not home:
            home = os.path.join(os.path.expanduser("~"), ".hermes")
        root = Path(home).expanduser() / "image_cache"

    if not root.is_absolute():
        root = PROJECT_ROOT / root
    # Do not resolve here. Resolving would hide a configured symlink or
    # junction and weaken the requirement that the cache root itself is real.
    return Path(os.path.abspath(root))


def _assert_safe_cache_components(candidate: Path, cache_root: Path) -> None:
    """Reject links/reparse points between the approved root and the file."""
    relative = candidate.relative_to(cache_root)
    current = cache_root
    for part in (None, *relative.parts[:-1]):
        if part is not None:
            current = current / part
        current_stat = os.lstat(current)
        if (
            stat.S_ISLNK(current_stat.st_mode)
            or not stat.S_ISDIR(current_stat.st_mode)
            or _is_reparse_point(current_stat)
        ):
            raise OSError(errno.ELOOP, "image path contains an unsafe directory")


def _resolve_image_path_in_cache(raw_path: str) -> Optional[Path]:
    """
    Resolve an image path and require it to remain below the image cache root.

    None means the path is outside the approved root or could not be resolved.
    The caller must not attempt to access an unresolved path.
    """
    try:
        cache_root = get_image_cache_root()
        cache_stat = os.lstat(cache_root)
        if (
            stat.S_ISLNK(cache_stat.st_mode)
            or not stat.S_ISDIR(cache_stat.st_mode)
            or _is_reparse_point(cache_stat)
        ):
            return None

        candidate = Path(os.path.abspath(Path(raw_path).expanduser()))
        candidate.relative_to(cache_root)
        _assert_safe_cache_components(candidate, cache_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def inspect_image_input(
    image_path: Optional[str] = None,
    image_description: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate optional product image path and/or image description passed from Hermes."""
    description = image_description.strip() if isinstance(image_description, str) else ""
    raw_path = image_path.strip() if isinstance(image_path, str) else ""
    has_path = bool(raw_path)
    has_desc = bool(description)

    if not has_path and not has_desc:
        return {"provided": False}

    info: Dict[str, Any] = {
        "provided": True,
        "extracted_keywords": description,
    }
    warnings: List[str] = []

    if len(description) > MAX_IMAGE_DESCRIPTION_CHARS:
        info["extracted_keywords"] = ""
        has_desc = False
        warnings.append("圖片描述超過長度上限，未使用該描述。")

    if has_path:
        if len(raw_path) > MAX_IMAGE_PATH_CHARS:
            has_path = False
            info["file_exists"] = False
            info["file_readable"] = False
            info["file_valid"] = False
            warnings.append("圖片路徑超過長度上限，未讀取該檔案。")
            info["warning"] = " ".join(warnings)
            if not has_desc:
                info["provided"] = True
            return info

        p = _resolve_image_path_in_cache(raw_path)
        if p is None:
            info["file_exists"] = False
            info["file_readable"] = False
            info["file_valid"] = False
            warnings.append(
                "圖片路徑不在允許的 Hermes 圖片快取目錄，未讀取該檔案；"
                "已降級使用圖片描述、文字或有效網址特徵進行查詢。"
            )
            info["warning"] = " ".join(warnings)
            return info

        ext = p.suffix.lower()
        info["file_exists"] = False
        info["file_readable"] = False
        info["file_valid"] = False
        try:
            path_stat = os.lstat(p)
        except FileNotFoundError:
            info["file_exists"] = False
            warnings.append("圖片路徑不存在，已降級使用圖片描述、文字或有效網址特徵進行查詢。")
        except OSError:
            info["file_exists"] = False
            warnings.append("圖片路徑無法安全檢查，已降級使用圖片描述、文字或有效網址特徵進行查詢。")
        else:
            info["file_exists"] = True
            if (
                stat.S_ISLNK(path_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or _is_reparse_point(path_stat)
                or int(path_stat.st_nlink) > 1
            ):
                info["file_exists"] = False
                warnings.append("圖片路徑不是安全的一般檔案，未讀取該檔案。")
            else:
                try:
                    head, opened = _read_regular_file_no_follow(
                        p,
                        max_bytes=MAX_IMAGE_BYTES,
                        read_limit=IMAGE_HEAD_BYTES,
                    )
                except ValueError:
                    warnings.append("圖片檔案超過 10MB 上限，僅使用圖片描述、文字或有效網址進行查詢。")
                except OSError as exc:
                    if exc.errno == errno.ENOENT:
                        info["file_exists"] = False
                        warnings.append("圖片路徑不存在，已降級使用圖片描述、文字或有效網址特徵進行查詢。")
                    else:
                        warnings.append("圖片檔案無法安全讀取，已降級使用圖片描述、文字或有效網址特徵進行查詢。")
                else:
                    info["file_readable"] = True
                    info["file_size_bytes"] = int(opened.st_size)
                    magic_matches = IMAGE_MAGIC.get(ext)
                    if ext not in ALLOWED_IMAGE_EXTENSIONS:
                        warnings.append("圖片副檔名不是支援的 jpg、jpeg、png 或 webp 格式。")
                    elif magic_matches is None or not magic_matches(head):
                        warnings.append("圖片內容與副檔名格式不符，未使用該圖片。")
                    else:
                        info["file_valid"] = True
                        info["detected_format"] = ext.lstrip(".")

        if not has_desc and not info["file_valid"]:
            # Keep the tool honest when neither a readable image nor a
            # description can provide product keywords.
            info["provided"] = True

        if warnings:
            info["warning"] = " ".join(warnings)
    else:
        if warnings:
            info["warning"] = " ".join(warnings)

    return info


def _score_candidate(search_text: str, row: sqlite3.Row) -> Tuple[int, List[str]]:
    text_lower = search_text.lower()
    matched_terms: List[str] = []
    score = 0

    ccc_plain = row["ccc_code"].replace(".", "").replace("-", "")
    if row["ccc_code"] in text_lower or (len(ccc_plain) >= 6 and ccc_plain[:6] in text_lower):
        score += 50
        matched_terms.append(row["ccc_code"])

    keywords = [k.strip() for k in row["keywords"].split(",") if k.strip()]
    for kw in keywords:
        if kw.lower() in text_lower:
            score += 15
            matched_terms.append(kw)

    for token in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", search_text):
        t_low = token.lower()
        if t_low in row["title_zh"].lower() or t_low in row["title_en"].lower() or t_low in row["category"].lower():
            if t_low not in [m.lower() for m in matched_terms]:
                score += 8
                matched_terms.append(token)

    return score, matched_terms


def _load_import_regulations(raw_value: str) -> List[Dict[str, str]]:
    value = json.loads(raw_value)
    if not isinstance(value, list) or len(value) > MAX_IMPORT_REGULATIONS:
        raise ValueError("invalid import regulations")

    regulations: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid import regulation entry")
        code = item.get("code")
        authority = item.get("authority")
        summary = item.get("summary")
        if not all(isinstance(part, str) and part.strip() for part in (code, authority, summary)):
            raise ValueError("invalid import regulation fields")
        regulations.append(
            {
                "code": code.strip(),
                "authority": authority.strip(),
                "summary": summary.strip(),
            }
        )
    return regulations


def _load_missing_fields(raw_value: str) -> List[str]:
    value = json.loads(raw_value)
    if not isinstance(value, list) or len(value) > MAX_MISSING_FIELDS:
        raise ValueError("invalid missing fields")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError("invalid missing field entry")
    return [item.strip() for item in value]


def _error_result(code: str, message: str, retrieved_at: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "is_mock_data": True,
        "disclaimer": P0_DISCLAIMER,
        "retrieved_at": retrieved_at,
        "error": {
            "code": code,
            "message": message,
        },
    }


def _safe_input_summary(
    clean_query: str,
    product_url: Optional[str],
    image_path: Optional[str],
    image_description: Optional[str],
    url_info: Dict[str, Any],
    image_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Build a response summary without raw user content or internal paths."""
    image_summary = {"provided": bool(image_info.get("provided"))}
    if image_summary["provided"]:
        image_summary.update(
            {
                "path_provided": bool(isinstance(image_path, str) and image_path.strip()),
                "description_provided": bool(
                    isinstance(image_description, str) and image_description.strip()
                ),
                "file_exists": bool(image_info.get("file_exists", False)),
                "file_readable": bool(image_info.get("file_readable", False)),
                "file_valid": bool(image_info.get("file_valid", False)),
            }
        )
        if "file_size_bytes" in image_info:
            image_summary["file_size_bytes"] = int(image_info["file_size_bytes"])
        if "detected_format" in image_info:
            image_summary["detected_format"] = str(image_info["detected_format"])

    url_summary = {"provided": bool(url_info.get("provided"))}
    if url_summary["provided"]:
        url_summary.update(
            {
                "url_provided": bool(isinstance(product_url, str) and product_url.strip()),
                "valid": bool(url_info.get("valid", False)),
                "ssrf_blocked": bool(url_info.get("ssrf_blocked", False)),
                "extracted_keyword_count": int(
                    url_info.get("extracted_keyword_count", 0)
                ),
            }
        )

    return {
        "has_text": bool(clean_query),
        "has_image": bool(image_info.get("provided")),
        "image_info": image_summary,
        "has_url": bool(url_info.get("provided")),
        "url_info": url_summary,
    }


def _safe_audit_summary(
    clean_query: str,
    product_url: Optional[str],
    image_path: Optional[str],
    image_description: Optional[str],
    url_info: Dict[str, Any],
    image_info: Dict[str, Any],
    matched_count: int,
) -> Dict[str, Any]:
    """Record only coarse flags, never raw input, paths, URLs, or identities."""
    return {
        "has_text": bool(clean_query),
        "has_image_path": bool(isinstance(image_path, str) and image_path.strip()),
        "has_image_description": bool(
            isinstance(image_description, str) and image_description.strip()
        ),
        "image_file_valid": bool(image_info.get("file_valid", False)),
        "has_url": bool(url_info.get("provided")),
        "url_valid": bool(url_info.get("valid", False)),
        "url_ssrf_blocked": bool(url_info.get("ssrf_blocked", False)),
        "matched_count": int(matched_count),
        "dataset_version": DATASET_VERSION,
    }


def run_p0_lookup(
    user_id: Optional[str],
    query: Optional[str] = None,
    product_url: Optional[str] = None,
    image_path: Optional[str] = None,
    image_description: Optional[str] = None,
    action: str = "lookup",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Core entry point for P0 lookup and admin status check.
    Returns a structured dict ready to be serialized as JSON for Hermes.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    normalized_action = action.strip().lower() if isinstance(action, str) else "lookup"
    authorized, err_code, err_msg = check_authorization(
        user_id=user_id,
        action=normalized_action,
    )

    if not authorized:
        return {
            "status": "unauthorized",
            "is_mock_data": True,
            "disclaimer": P0_DISCLAIMER,
            "retrieved_at": now_iso,
            "error": {
                "code": err_code,
                "message": err_msg,
            },
        }

    if not isinstance(action, str):
        return _error_result(
            "INVALID_ACTION",
            "不支援的工具動作。",
            now_iso,
        )
    action = normalized_action
    if action not in {"lookup", "admin_status"}:
        return _error_result(
            "INVALID_ACTION",
            "不支援的工具動作。",
            now_iso,
        )

    request_values = {
        "query": (query, MAX_QUERY_CHARS),
        "product_url": (product_url, MAX_URL_CHARS),
        "image_path": (image_path, MAX_IMAGE_PATH_CHARS),
        "image_description": (image_description, MAX_IMAGE_DESCRIPTION_CHARS),
    }
    for field_name, (value, max_chars) in request_values.items():
        if value is not None and not isinstance(value, str):
            return _error_result(
                "INVALID_ARGUMENTS",
                f"欄位 {field_name} 必須是字串。",
                now_iso,
            )
        if isinstance(value, str) and len(value) > max_chars:
            return _error_result(
                "INPUT_TOO_LONG",
                f"欄位 {field_name} 超過長度上限。",
                now_iso,
            )

    if action == "admin_status":
        try:
            with open_verified_mock_db(resolve_db_path(db_path)) as conn:
                cur = conn.cursor()
                cur.execute("SELECT key, value FROM dataset_meta")
                meta = {row["key"]: row["value"] for row in cur.fetchall()}
                cur.execute("SELECT COUNT(*) AS cnt FROM mock_tariffs")
                tariff_count = cur.fetchone()["cnt"]
                cur.execute("SELECT COUNT(*) AS cnt FROM lookup_audit_log")
                audit_count = cur.fetchone()["cnt"]
                database_path = conn.execute("PRAGMA database_list").fetchone()[2]
                database_name = Path(os.path.abspath(database_path)).name
        except (OSError, sqlite3.Error, ValueError):
            return _error_result(
                "MOCK_DB_UNAVAILABLE",
                "P0 測試資料庫無法安全開啟，查詢未完成。",
                now_iso,
            )

        return {
            "status": "ok",
            "action": "admin_status",
            "is_mock_data": True,
            "disclaimer": P0_DISCLAIMER,
            "dataset_version": meta.get("dataset_version", DATASET_VERSION),
            "retrieved_at": now_iso,
            "admin_summary": {
                "db_path": database_name,
                "mock_tariff_records": tariff_count,
                "audit_log_entries": audit_count,
                "updated_at": meta.get("updated_at", now_iso),
            },
        }

    clean_query = str(query or "").strip()
    url_info = validate_and_extract_url_info(product_url)
    image_info = inspect_image_input(image_path=image_path, image_description=image_description)

    warnings: List[str] = []
    if url_info.get("provided"):
        if not url_info.get("valid", False):
            warnings.append(url_info.get("reason", "商品網址無效或被安全規則阻擋。"))

    if image_info.get("provided") and image_info.get("warning"):
        warnings.append(image_info["warning"])

    combined_search_parts = [
        clean_query,
        image_info.get("extracted_keywords", "") if image_info.get("provided") else "",
        url_info.get("extracted_keywords", "") if url_info.get("provided") and url_info.get("valid") else "",
    ]
    combined_search_text = " ".join(part for part in combined_search_parts if part).strip()

    input_summary = _safe_input_summary(
        clean_query=clean_query,
        product_url=product_url,
        image_path=image_path,
        image_description=image_description,
        url_info=url_info,
        image_info=image_info,
    )

    if not combined_search_text:
        return {
            "status": "needs_more_info",
            "is_mock_data": True,
            "disclaimer": P0_DISCLAIMER,
            "dataset_version": DATASET_VERSION,
            "retrieved_at": now_iso,
            "input_summary": input_summary,
            "data": {
                "candidates": [],
                "import_regulations": [],
                "bsmi_check": {
                    "status": "pending_input",
                    "label": "待確認（請先提供商品名稱、材質、用途或清楚的商品圖片／有效網址）",
                },
                "missing_fields": [
                    "商品中文或英文名稱",
                    "主要材質與用途",
                    "是否插電、含電池或具藍牙／Wi-Fi 無線功能",
                ],
                "sources": [
                    {
                        "name": "P0 本地測試用 SQLite 資料庫 (p0_mock_tariff.db)",
                        "version": DATASET_VERSION,
                    }
                ],
                "warnings": warnings
                + ["目前無法從您提供的輸入取得足夠的商品關鍵字，請補充商品名稱、材質、用途或圖片說明。"],
            },
        }
    try:
        with open_verified_mock_db(resolve_db_path(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM mock_tariffs")
            all_rows = cur.fetchall()

            scored: List[Tuple[int, List[str], sqlite3.Row]] = []
            for row in all_rows:
                score, matched_terms = _score_candidate(combined_search_text, row)
                if score > 0:
                    scored.append((score, matched_terms, row))

            scored.sort(key=lambda item: item[0], reverse=True)
            top_matches = scored[:3]

            prepared_matches: List[Tuple[int, List[str], Dict[str, Any]]] = []
            try:
                for score, matched_terms, row in top_matches:
                    regs = _load_import_regulations(row["import_regs_json"])
                    missing_fields = _load_missing_fields(row["missing_fields_json"])
                    bsmi_label = (
                        row["bsmi_note"]
                        if row["bsmi_status"] == "hit"
                        else f"待確認（未命中 P0 測試清單，不等於免驗：{row['bsmi_note']}）"
                    )
                    prepared_matches.append(
                        (
                            score,
                            matched_terms,
                            {
                                "ccc_code": row["ccc_code"],
                                "title_zh": row["title_zh"],
                                "title_en": row["title_en"],
                                "category": row["category"],
                                "import_regulations": regs,
                                "required_clarifications": missing_fields,
                                "bsmi_check": {
                                    "status": row["bsmi_status"],
                                    "label": bsmi_label,
                                },
                                "tariff_rates_mock": {
                                    "first_column": row["duty_first_col"],
                                    "second_column": row["duty_second_col"],
                                    "third_column": row["duty_third_col"],
                                },
                            },
                        )
                    )
            except (json.JSONDecodeError, TypeError, ValueError):
                return _error_result(
                    "MOCK_DATA_INVALID",
                    "P0 測試資料格式不正確，查詢未完成。",
                    now_iso,
                )

            audit_summary = _safe_audit_summary(
                clean_query=clean_query,
                product_url=product_url,
                image_path=image_path,
                image_description=image_description,
                url_info=url_info,
                image_info=image_info,
                matched_count=len(prepared_matches),
            )
            cur.execute(
                """
                INSERT INTO lookup_audit_log (created_at, user_id, action, input_summary_json, matched_count)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    now_iso,
                    "authorized",
                    action,
                    json.dumps(audit_summary, ensure_ascii=False),
                    len(prepared_matches),
                ),
            )
            conn.commit()
    except (OSError, sqlite3.Error, ValueError):
        return _error_result(
            "MOCK_DB_UNAVAILABLE",
            "P0 測試資料庫無法安全開啟，查詢未完成。",
            now_iso,
        )

    if not prepared_matches:
        return {
            "status": "needs_more_info",
            "is_mock_data": True,
            "disclaimer": P0_DISCLAIMER,
            "dataset_version": DATASET_VERSION,
            "retrieved_at": now_iso,
            "input_summary": input_summary,
            "data": {
                "candidates": [],
                "import_regulations": [],
                "bsmi_check": {
                    "status": "not_found_in_mock",
                    "label": "待確認（未命中 P0 測試清單，絕不等於免驗；請補充商品細節或洽詢報關行／標檢局）",
                },
                "missing_fields": [
                    "商品具體材質（如塑膠、金屬、紡織、電子元件）",
                    "商品主要用途與工作原理（是否插電、含電池、具無線功能或接觸人體／食品）",
                ],
                "sources": [
                    {
                        "name": "P0 本地測試用 SQLite 資料庫 (p0_mock_tariff.db)",
                        "version": DATASET_VERSION,
                    }
                ],
                "warnings": warnings
                + [
                    "P0 測試資料庫僅收錄少量驗證用假資料（如：咖啡機、藍牙耳機、保養乳霜、模型玩具、塑膠收納盒），未命中測試資料不等於免稅或免驗。"
                ],
            },
        }

    candidates: List[Dict[str, Any]] = []
    aggregated_regs: List[Dict[str, str]] = []
    seen_reg_codes: Set[str] = set()
    aggregated_missing_fields: List[str] = []

    for rank, (score, matched_terms, prepared) in enumerate(prepared_matches, start=1):
        regs = prepared["import_regulations"]
        missing_fields = prepared["required_clarifications"]
        candidates.append(
            {
                "rank": rank,
                "ccc_code": prepared["ccc_code"],
                "title_zh": prepared["title_zh"],
                "title_en": prepared["title_en"],
                "category": prepared["category"],
                "match_reason": f"命中關鍵字：{', '.join(matched_terms)} (測試匹配分數 {score})",
                "tariff_rates_mock": prepared["tariff_rates_mock"],
                "import_regulations": regs,
                "bsmi_check": prepared["bsmi_check"],
                "required_clarifications": missing_fields,
            }
        )
        for reg in regs:
            if reg["code"] not in seen_reg_codes:
                seen_reg_codes.add(reg["code"])
                aggregated_regs.append(reg)
        for mf in missing_fields:
            if mf not in aggregated_missing_fields:
                aggregated_missing_fields.append(mf)

    primary_bsmi = candidates[0]["bsmi_check"]

    return {
        "status": "ok",
        "is_mock_data": True,
        "disclaimer": P0_DISCLAIMER,
        "dataset_version": DATASET_VERSION,
        "retrieved_at": now_iso,
        "input_summary": input_summary,
        "data": {
            "candidates": candidates,
            "import_regulations": aggregated_regs,
            "bsmi_check": primary_bsmi,
            "missing_fields": aggregated_missing_fields,
            "sources": [
                {
                    "name": "P0 本地測試用 SQLite 資料庫 (p0_mock_tariff.db)",
                    "version": DATASET_VERSION,
                    "retrieved_at": now_iso,
                }
            ],
            "warnings": warnings,
        },
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="TaiwanImportCheck P0 Local SQLite Lookup Tool"
    )
    parser.add_argument("--user-id", required=False, default="", help="Telegram User ID")
    parser.add_argument("--query", required=False, default="", help="Product text description or keywords")
    parser.add_argument("--product-url", required=False, default="", help="Product webpage URL")
    parser.add_argument("--image-path", required=False, default="", help="Local path to product image")
    parser.add_argument("--image-description", required=False, default="", help="Description of product image")
    parser.add_argument(
        "--action",
        choices=["lookup", "admin_status"],
        default="lookup",
        help="Tool action (lookup or admin_status)",
    )
    parser.add_argument("--db-path", required=False, default="", help="Optional custom SQLite DB path")

    args = parser.parse_args(argv)
    result = run_p0_lookup(
        user_id=args.user_id,
        query=args.query,
        product_url=args.product_url,
        image_path=args.image_path,
        image_description=args.image_description,
        action=args.action,
        db_path=args.db_path or None,
    )
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    status = result.get("status")
    if status in {"ok", "needs_more_info"}:
        return 0
    if status == "unauthorized":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
