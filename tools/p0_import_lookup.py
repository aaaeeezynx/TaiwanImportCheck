#!/usr/bin/env python3
"""
TaiwanImportCheck - P0 Local Python Lookup Tool (`p0_import_lookup`)

Responsibilities (P0 Technical Verification):
1. Enforce Telegram user allowlist (`TELEGRAM_ALLOWED_USERS` & `ADMIN_TELEGRAM_USERS`, fail-closed).
2. Accept text (`query`), product image (`image_path` / `image_description`), and product URL (`product_url`).
3. Validate product URL with strict SSRF protection and safe fallback if unreachable or blocked.
4. Query local mock SQLite database (`p0_mock_tariff.db`) and return structured JSON to Hermes.
5. Clearly mark all outputs as P0 mock data and enforce "BSMI not found != exempt".
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "official_data" / "p0_mock_tariff.db"
DATASET_VERSION = "P0-MOCK-2026.10-TEST"
P0_DISCLAIMER = (
    "[P0測試假資料] 本結果來自本地測試用假資料庫（Mock SQLite），"
    "所有 CCC Code、稅率、輸入規定與檢驗狀態皆為技術驗證用虛構測試值，"
    "不構成正式報關、稅則歸列或法規免驗建議。"
)

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

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


def _parse_id_set(raw: Optional[str]) -> Set[str]:
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def get_allowed_users() -> Tuple[Set[str], Set[str]]:
    """Return (allowed_users, admin_users) from environment variables."""
    admin_users = _parse_id_set(os.getenv("ADMIN_TELEGRAM_USERS", ""))
    allowed_users = _parse_id_set(os.getenv("TELEGRAM_ALLOWED_USERS", "")) | admin_users
    return allowed_users, admin_users


def check_authorization(user_id: Optional[str], action: str = "lookup") -> Tuple[bool, str, str]:
    """
    Fail-closed authorization check against TELEGRAM_ALLOWED_USERS and ADMIN_TELEGRAM_USERS.
    Returns (is_authorized, error_code, error_message).
    """
    uid = str(user_id or "").strip()
    allowed_users, admin_users = get_allowed_users()

    if not allowed_users:
        return (
            False,
            "ALLOWLIST_NOT_CONFIGURED",
            "系統尚未設定 TELEGRAM_ALLOWED_USERS 白名單（預設拒絕所有請求）。",
        )

    if not uid:
        return (
            False,
            "MISSING_USER_ID",
            "目前無法取得授權身分，查詢未執行；請從已授權的 Telegram 私聊使用本服務。",
        )

    if uid not in allowed_users:
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
        p = Path(raw)
        if not p.is_absolute():
            p = (PROJECT_ROOT / p).resolve()
        return p
    return DEFAULT_DB_PATH


def ensure_mock_db(db_path: Optional[Path] = None, force_reset: bool = False) -> Path:
    """Create and seed the P0 mock SQLite database if not already initialized."""
    target_path = db_path or resolve_db_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target_path))
    try:
        cur = conn.cursor()
        if force_reset:
            cur.execute("DROP TABLE IF EXISTS dataset_meta")
            cur.execute("DROP TABLE IF EXISTS mock_tariffs")
            cur.execute("DROP TABLE IF EXISTS lookup_audit_log")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        cur.execute(
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
        cur.execute(
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

        cur.execute("SELECT COUNT(*) FROM mock_tariffs")
        count = cur.fetchone()[0]
        if count == 0 or force_reset:
            now_iso = datetime.now(timezone.utc).isoformat()
            meta_rows = [
                ("dataset_version", DATASET_VERSION),
                ("updated_at", now_iso),
                ("is_mock_data", "true"),
                ("notice", P0_DISCLAIMER),
            ]
            cur.executemany(
                "INSERT OR REPLACE INTO dataset_meta (key, value) VALUES (?, ?)",
                meta_rows,
            )
            for item in MOCK_SEED_RECORDS:
                cur.execute(
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
            conn.commit()
    finally:
        conn.close()

    return target_path


def _is_forbidden_ip(ip_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def validate_and_extract_url_info(product_url: Optional[str]) -> Dict[str, Any]:
    """
    Validate a product URL with strict SSRF protection and extract safe keyword hints.
    Never accesses private/loopback/link-local/metadata IPs.
    """
    if not product_url or not str(product_url).strip():
        return {"provided": False}

    raw_url = str(product_url).strip()
    parsed = urlparse(raw_url)
    scheme = (parsed.scheme or "").lower()

    if scheme not in ("http", "https"):
        return {
            "provided": True,
            "url": raw_url,
            "valid": False,
            "ssrf_blocked": True,
            "reason": f"僅允許 http/https 商品網址，已阻擋協定：{scheme or '無'}",
            "extracted_keywords": "",
        }

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        return {
            "provided": True,
            "url": raw_url,
            "valid": False,
            "ssrf_blocked": True,
            "reason": "網址缺少有效的主機名稱 (hostname)。",
            "extracted_keywords": "",
        }

    blocked_hostnames = {"localhost", "localhost.localdomain", "metadata.google.internal"}
    if (
        hostname in blocked_hostnames
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
        or hostname.endswith(".localhost")
    ):
        return {
            "provided": True,
            "url": raw_url,
            "url_host": hostname,
            "valid": False,
            "ssrf_blocked": True,
            "reason": f"安全規則阻擋內網或本機主機名稱：{hostname}",
            "extracted_keywords": "",
        }

    try:
        ipaddress.ip_address(hostname)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip and _is_forbidden_ip(hostname):
        return {
            "provided": True,
            "url": raw_url,
            "url_host": hostname,
            "valid": False,
            "ssrf_blocked": True,
            "reason": f"安全規則阻擋私網／本機／Metadata IP 位址：{hostname}",
            "extracted_keywords": "",
        }

    decoded_path = unquote(f"{parsed.path} {parsed.query}")
    tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", decoded_path)
    stop_tokens = {"http", "https", "www", "com", "tw", "html", "htm", "item", "product", "products", "shop", "dp", "gp"}
    filtered_tokens = [t for t in tokens if t.lower() not in stop_tokens][:15]
    extracted_keywords = " ".join(filtered_tokens)

    result: Dict[str, Any] = {
        "provided": True,
        "url": raw_url,
        "url_host": hostname,
        "valid": True,
        "ssrf_blocked": False,
        "extracted_keywords": extracted_keywords,
        "live_fetch_attempted": False,
    }

    if os.getenv("P0_ALLOW_LIVE_URL_FETCH", "0").strip() == "1":
        result["live_fetch_attempted"] = True
        try:
            addr_info = socket.getaddrinfo(hostname, parsed.port or (443 if scheme == "https" else 80))
            for entry in addr_info:
                resolved_ip = entry[4][0]
                if _is_forbidden_ip(resolved_ip):
                    result["valid"] = False
                    result["ssrf_blocked"] = True
                    result["reason"] = f"DNS 解析指向受限制的內部 IP ({resolved_ip})，已阻擋連線。"
                    result["extracted_keywords"] = ""
                    return result

            req = Request(
                raw_url,
                headers={"User-Agent": "TaiwanImportCheck-P0-Verifier/0.1"},
                method="GET",
            )
            with urlopen(req, timeout=4) as resp:
                raw_bytes = resp.read(131072)
                html_text = raw_bytes.decode("utf-8", errors="ignore")
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
                if title_match:
                    clean_title = re.sub(r"\s+", " ", title_match.group(1)).strip()[:120]
                    result["page_title"] = clean_title
                    result["extracted_keywords"] = f"{extracted_keywords} {clean_title}".strip()
        except Exception as exc:
            result["fetch_warning"] = f"商品網址連線失敗或逾時（已自動降級使用文字／圖片與網址關鍵字）：{type(exc).__name__}"

    return result


def inspect_image_input(
    image_path: Optional[str] = None,
    image_description: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate optional product image path and/or image description passed from Hermes."""
    has_path = bool(image_path and str(image_path).strip())
    has_desc = bool(image_description and str(image_description).strip())

    if not has_path and not has_desc:
        return {"provided": False}

    info: Dict[str, Any] = {
        "provided": True,
        "image_description": str(image_description or "").strip(),
        "extracted_keywords": str(image_description or "").strip(),
    }

    if has_path:
        raw_path = str(image_path).strip()
        p = Path(raw_path)
        info["file_name"] = p.name
        ext = p.suffix.lower()
        stem_tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", p.stem)
        if stem_tokens:
            info["extracted_keywords"] = f"{info['extracted_keywords']} {' '.join(stem_tokens)}".strip()

        if ext and ext not in ALLOWED_IMAGE_EXTENSIONS:
            info["file_valid"] = False
            info["warning"] = f"圖片副檔名 ({ext}) 非標準格式（建議使用 jpg/png/webp）。"
        elif p.exists() and p.is_file():
            size_bytes = p.stat().st_size
            info["file_exists"] = True
            info["file_size_bytes"] = size_bytes
            info["file_valid"] = size_bytes <= MAX_IMAGE_BYTES
            if size_bytes > MAX_IMAGE_BYTES:
                info["warning"] = "圖片檔案超過 10MB 上限，僅使用圖片描述與文字進行查詢。"
        else:
            info["file_exists"] = False
            info["file_valid"] = True
            info["note"] = "未直接讀取本地圖片實體檔，已採用多模態圖片描述與檔名特徵進行查詢。"

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
    authorized, err_code, err_msg = check_authorization(user_id=user_id, action=action)

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

    target_db = ensure_mock_db(resolve_db_path(db_path))

    if action == "admin_status":
        conn = sqlite3.connect(str(target_db))
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM dataset_meta")
            meta = {row["key"]: row["value"] for row in cur.fetchall()}
            cur.execute("SELECT COUNT(*) AS cnt FROM mock_tariffs")
            tariff_count = cur.fetchone()["cnt"]
            cur.execute("SELECT COUNT(*) AS cnt FROM lookup_audit_log")
            audit_count = cur.fetchone()["cnt"]
        finally:
            conn.close()

        return {
            "status": "ok",
            "action": "admin_status",
            "is_mock_data": True,
            "disclaimer": P0_DISCLAIMER,
            "dataset_version": meta.get("dataset_version", DATASET_VERSION),
            "retrieved_at": now_iso,
            "admin_summary": {
                "db_path": str(target_db.name),
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
        elif url_info.get("fetch_warning"):
            warnings.append(url_info["fetch_warning"])

    if image_info.get("provided") and image_info.get("warning"):
        warnings.append(image_info["warning"])

    combined_search_parts = [
        clean_query,
        image_info.get("extracted_keywords", "") if image_info.get("provided") else "",
        url_info.get("extracted_keywords", "") if url_info.get("provided") and url_info.get("valid") else "",
    ]
    combined_search_text = " ".join(part for part in combined_search_parts if part).strip()

    input_summary = {
        "has_text": bool(clean_query),
        "text_query": clean_query,
        "has_image": bool(image_info.get("provided")),
        "image_info": image_info,
        "has_url": bool(url_info.get("provided")),
        "url_info": url_info,
        "combined_search_keywords": combined_search_text,
    }

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
    conn = sqlite3.connect(str(target_db))
    conn.row_factory = sqlite3.Row
    try:
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

        cur.execute(
            """
            INSERT INTO lookup_audit_log (created_at, user_id, action, input_summary_json, matched_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                now_iso,
                str(user_id).strip(),
                action,
                json.dumps(input_summary, ensure_ascii=False),
                len(top_matches),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    if not top_matches:
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

    for rank, (score, matched_terms, row) in enumerate(top_matches, start=1):
        regs = json.loads(row["import_regs_json"])
        missing_fields = json.loads(row["missing_fields_json"])
        bsmi_label = (
            row["bsmi_note"]
            if row["bsmi_status"] == "hit"
            else f"待確認（未命中 P0 測試清單，不等於免驗：{row['bsmi_note']}）"
        )
        candidates.append(
            {
                "rank": rank,
                "ccc_code": row["ccc_code"],
                "title_zh": row["title_zh"],
                "title_en": row["title_en"],
                "category": row["category"],
                "match_reason": f"命中關鍵字：{', '.join(matched_terms)} (測試匹配分數 {score})",
                "tariff_rates_mock": {
                    "first_column": row["duty_first_col"],
                    "second_column": row["duty_second_col"],
                    "third_column": row["duty_third_col"],
                },
                "import_regulations": regs,
                "bsmi_check": {
                    "status": row["bsmi_status"],
                    "label": bsmi_label,
                },
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
    return 0 if result.get("status") != "unauthorized" else 3


if __name__ == "__main__":
    raise SystemExit(main())
