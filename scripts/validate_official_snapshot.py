"""Download and validate official-source snapshots without importing them into P0."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "config" / "official_sources.json"
OFFICIAL_DATA_DIR = ROOT / "data" / "official_data"
DEFAULT_SOURCE_DIR = OFFICIAL_DATA_DIR / "source_snapshots"
DEFAULT_CANDIDATE_DIR = OFFICIAL_DATA_DIR / "candidate_snapshots"
DEFAULT_REPORT_DIR = OFFICIAL_DATA_DIR / "reports"
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
MAX_ZIP_BYTES = 512 * 1024 * 1024
DELIMITERS = (",", "\t", ";", "|")
csv.field_size_limit(50_000_000)


def _check_gov_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not (host == "gov.tw" or host.endswith(".gov.tw")):
        raise ValueError("refusing a non-HTTPS or non-.gov.tw source URL")


class GovRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_gov_url(newurl)
        newurl = urllib.parse.quote(newurl, safe=":/?&=%#[]@!$'()*+,;")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener() -> urllib.request.OpenerDirector:
    context = ssl.create_default_context()
    return urllib.request.build_opener(
        GovRedirectHandler(), urllib.request.HTTPSHandler(context=context)
    )


def _request_bytes(url: str, limit: int) -> bytes:
    _check_gov_url(url)
    url = urllib.parse.quote(url, safe=":/?&=%#[]@!$'()*+,;")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TaiwanImportCheck-source-audit/1.0", "Accept": "*/*"},
    )
    try:
        with _opener().open(request, timeout=45) as response:
            _check_gov_url(response.geturl())
            payload = response.read(limit + 1)
    except urllib.error.URLError as exc:
        if not (
            os.name == "nt"
            and isinstance(exc.reason, ssl.SSLCertVerificationError)
        ):
            raise
        payload = _request_bytes_with_windows_curl(url, limit)
    if len(payload) > limit:
        raise ValueError(f"source exceeds configured size limit ({limit} bytes)")
    return payload


def _request_bytes_with_windows_curl(url: str, limit: int) -> bytes:
    """Use Windows' certificate store when OpenSSL rejects a legacy gov.tw chain."""
    current_url = url
    for _ in range(6):
        _check_gov_url(current_url)
        with tempfile.TemporaryDirectory(prefix="tic-source-") as temp_dir:
            body_path = Path(temp_dir) / "body.bin"
            headers_path = Path(temp_dir) / "headers.txt"
            command = [
                "curl.exe", "--silent", "--show-error", "--max-time", "45",
                "--proto", "=https", "--max-filesize", str(limit),
                "--dump-header", str(headers_path), "--output", str(body_path),
                "--write-out", "%{http_code}", current_url,
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=50)
            if result.returncode != 0:
                detail = result.stderr.strip() or f"curl exited with {result.returncode}"
                raise RuntimeError(f"Windows curl request failed: {detail}")
            try:
                status = int(result.stdout.strip())
            except ValueError as exc:
                raise RuntimeError("Windows curl returned no HTTP status") from exc
            headers = headers_path.read_text(encoding="iso-8859-1") if headers_path.exists() else ""
            if 300 <= status < 400:
                locations = re.findall(r"(?im)^Location:\s*(.+?)\s*$", headers)
                if not locations:
                    raise ValueError(f"official source returned HTTP {status} without Location")
                current_url = urllib.parse.urljoin(current_url, locations[-1])
                _check_gov_url(current_url)
                continue
            if not 200 <= status < 300:
                raise ValueError(f"official source returned HTTP {status}")
            payload = body_path.read_bytes()
            if len(payload) > limit:
                raise ValueError(f"source exceeds configured size limit ({limit} bytes)")
            return payload
    raise ValueError("official source exceeded the redirect limit")


def _metadata(dataset_id: str) -> dict[str, Any]:
    url = f"https://data.gov.tw/api/v2/rest/dataset/{dataset_id}"
    raw = _request_bytes(url, 8 * 1024 * 1024)
    result = json.loads(raw.decode("utf-8-sig"))["result"]
    return result


def _choose_distribution(metadata: dict[str, Any], preferred_format: str) -> dict[str, Any]:
    distributions = metadata.get("distribution") or []
    preferred = preferred_format.upper()
    exact = [d for d in distributions if str(d.get("resourceFormat", "")).upper() == preferred]
    candidates = exact or distributions
    for item in candidates:
        if item.get("resourceDownloadUrl"):
            return item
    raise ValueError("data.gov.tw metadata has no downloadable resource")


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp950", "big5"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("text encoding is not UTF-8, UTF-16, CP950, or Big5")


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lstrip("\ufeff")).casefold()


def _expected_match_count(header: list[str], expected: list[str]) -> int:
    actual = {_norm(value) for value in header}
    return sum(_norm(value) in actual for value in expected)


def _parse_csv(
    raw: bytes,
    expected: list[str],
    keys: list[str],
    ccc_fields: list[str],
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = _decode_text(raw)
    choices = []
    for delimiter in DELIMITERS:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        header = next(reader, [])
        choices.append((_expected_match_count(header, expected), len(header), delimiter))
    _, _, delimiter = max(choices)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    header = next(reader, [])
    rows = (
        dict(zip(header, row))
        for row in reader
        if row and any(cell.strip() for cell in row)
    )
    return _summarize_rows(header, rows, expected, keys, ccc_fields, source)


def _find_json_records(value: Any) -> list[dict[str, Any]] | None:
    if isinstance(value, list):
        dicts = [item for item in value if isinstance(item, dict)]
        if dicts:
            return dicts
        for item in value:
            found = _find_json_records(item)
            if found:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            found = _find_json_records(item)
            if found:
                return found
    return None


def _parse_json(
    raw: bytes,
    expected: list[str],
    keys: list[str],
    ccc_fields: list[str],
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    value = json.loads(_decode_text(raw))
    records = _find_json_records(value)
    if records is None:
        raise ValueError("JSON contains no list of record objects")
    header = list(records[0].keys())
    return _summarize_rows(header, records, expected, keys, ccc_fields, source)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_xml(
    raw: bytes,
    expected: list[str],
    keys: list[str],
    ccc_fields: list[str],
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = ElementTree.fromstring(raw)
    groups: list[tuple[int, ElementTree.Element]] = []
    for parent in root.iter():
        children = list(parent)
        by_tag: dict[str, list[ElementTree.Element]] = {}
        for child in children:
            by_tag.setdefault(child.tag, []).append(child)
        for siblings in by_tag.values():
            if siblings and list(siblings[0]):
                groups.append((len(siblings), siblings[0]))

    repeated = []
    for count, sample in groups:
        if count == 0:
            continue
        header = [_local_name(child.tag) for child in list(sample)]
        repeated.append((
            _expected_match_count(header, expected), len(header), count, sample
        ))
    if repeated:
        _, _, row_count, sample = max(
            repeated, key=lambda item: (item[0], item[1], item[2])
        )
        header = [_local_name(child.tag) for child in list(sample)]
        parent = next(parent for parent in root.iter() if sample in list(parent))
        records = [
            {
                _local_name(child.tag): "".join(child.itertext()).strip()
                for child in list(record)
            }
            for record in list(parent)
            if record.tag == sample.tag
        ]
        return _summarize_rows(header, records, expected, keys, ccc_fields, source)

    children = list(root)
    header = [_local_name(child.tag) for child in children]
    if not header:
        raise ValueError("XML contains no record fields")
    row = {_local_name(child.tag): (child.text or "").strip() for child in children}
    return _summarize_rows(header, [row], expected, keys, ccc_fields, source)


def _is_parseable_source_date(value: Any) -> bool:
    """Accept the date forms used by the official datasets without guessing values."""
    text = str(value or "").strip()
    if not text:
        return False
    candidates = [text]
    if "T" in text:
        candidates.append(text.split("T", 1)[0])
    for candidate in candidates:
        try:
            datetime.fromisoformat(candidate)
            return True
        except ValueError:
            pass
    for date_format in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            datetime.strptime(text, date_format)
            return True
        except ValueError:
            continue
    return False


def _law_identity_issues(
    row: dict[str, Any],
    field_by_norm: dict[str, str],
) -> list[str]:
    """Return row-level law identity violations without filling missing values."""
    issues: list[str] = []
    name = str(row.get(field_by_norm.get(_norm("法規名稱"), ""), "") or "").strip()
    url = str(row.get(field_by_norm.get(_norm("法規網址"), ""), "") or "").strip()
    modified = str(
        row.get(field_by_norm.get(_norm("最新異動日期"), ""), "") or ""
    ).strip()
    effective = str(
        row.get(field_by_norm.get(_norm("生效日期"), ""), "") or ""
    ).strip()

    if not name:
        issues.append("blank_law_name")
    if not url:
        issues.append("blank_law_url")
    else:
        try:
            _check_gov_url(url)
        except ValueError:
            issues.append("invalid_law_url")
    if not modified:
        issues.append("blank_latest_modified_date")
    elif not _is_parseable_source_date(modified):
        issues.append("invalid_latest_modified_date")
    if effective and not _is_parseable_source_date(effective):
        issues.append("invalid_effective_date")
    return issues


def _summarize_rows(
    header: list[str], rows, expected: list[str], keys: list[str],
    ccc_fields: list[str], source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    field_by_norm = {_norm(field): field for field in header}
    key_columns = [field_by_norm[_norm(field)] for field in keys if _norm(field) in field_by_norm]
    ccc_columns = [field_by_norm[_norm(field)] for field in ccc_fields if _norm(field) in field_by_norm]
    seen: set[tuple[str, ...]] = set()
    seen_records: set[tuple[str, ...]] = set()
    duplicate_keys = 0
    duplicate_rows = 0
    blank_key_rows = 0
    invalid_ccc_values = 0
    law_identity_invalid_rows = 0
    law_identity_issue_counts: dict[str, int] = {}
    check_law_identity = bool(
        source
        and "law_identity_requires_url_and_dates"
        in source.get("validation_rules", [])
    )
    count = 0
    for row in rows:
        count += 1
        record = tuple(str(row.get(column, "") or "").strip() for column in header)
        if record in seen_records:
            duplicate_rows += 1
        else:
            seen_records.add(record)
        if key_columns:
            key = tuple(str(row.get(column, "") or "").strip() for column in key_columns)
            if any(not value for value in key):
                blank_key_rows += 1
            elif key in seen:
                duplicate_keys += 1
            else:
                seen.add(key)
        for column in ccc_columns:
            value = str(row.get(column, "") or "").strip()
            normalized = re.sub(r"[.\-\s]", "", value)
            if value and not re.fullmatch(r"\d{8,12}[A-Z]?", normalized.upper()):
                invalid_ccc_values += 1
        if check_law_identity:
            identity_issues = _law_identity_issues(row, field_by_norm)
            if identity_issues:
                law_identity_invalid_rows += 1
                for issue in identity_issues:
                    law_identity_issue_counts[issue] = (
                        law_identity_issue_counts.get(issue, 0) + 1
                    )
    return {
        "header": header,
        "row_count": count,
        "duplicate_keys": duplicate_keys,
        "duplicate_rows": duplicate_rows,
        "blank_key_rows": blank_key_rows,
        "invalid_ccc_values": invalid_ccc_values,
        "law_identity_invalid_rows": law_identity_invalid_rows,
        "law_identity_issue_counts": law_identity_issue_counts,
    }


def _parse_zip(
    raw: bytes,
    expected: list[str],
    keys: list[str],
    ccc_fields: list[str],
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if sum(info.file_size for info in infos) > MAX_ZIP_BYTES:
            raise ValueError("archive expands beyond configured size limit")
        supported = [
            info for info in infos
            if Path(info.filename).suffix.lower() in {".csv", ".json", ".xml"}
        ]
        if not supported:
            raise ValueError("ZIP contains no CSV, JSON, or XML data files")
        candidates = []
        for info in supported:
            inner = archive.read(info)
            suffix = Path(info.filename).suffix.lower()
            try:
                if suffix == ".json":
                    parsed = _parse_json(inner, expected, keys, ccc_fields, source)
                elif suffix == ".csv":
                    parsed = _parse_csv(inner, expected, keys, ccc_fields, source)
                else:
                    parsed = _parse_xml(inner, expected, keys, ccc_fields, source)
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError, ElementTree.ParseError):
                continue
            overlap = _expected_match_count(parsed["header"], expected)
            candidates.append((overlap, parsed["row_count"], info, parsed))
        if candidates:
            overlap, _, selected, parsed = max(
                candidates, key=lambda item: (item[0], item[1], item[2].file_size)
            )
            parsed["archive_members"] = len(infos)
            parsed["parsed_members"] = 1
            parsed["selected_member"] = selected.filename
            parsed["matched_expected_fields"] = overlap
            return parsed
        raise ValueError("ZIP contains no supported data members")


def validate_payload(raw: bytes, source: dict[str, Any]) -> dict[str, Any]:
    expected = list(dict.fromkeys(
        source["fields"] + source.get("keys", []) + source.get("ccc_fields", [])
    ))
    keys = source.get("keys", [])
    ccc_fields = source.get("ccc_fields", [])
    probe = raw.lstrip(b"\xef\xbb\xbf\t\r\n ")
    if raw.startswith(b"PK\x03\x04"):
        parsed = _parse_zip(raw, expected, keys, ccc_fields, source)
        parse_format = "ZIP"
    elif probe.startswith(b"{") or probe.startswith(b"["):
        parsed = _parse_json(raw, expected, keys, ccc_fields, source)
        parse_format = "JSON"
    elif probe.startswith(b"<"):
        parsed = _parse_xml(raw, expected, keys, ccc_fields, source)
        parse_format = "XML"
    else:
        parsed = _parse_csv(raw, expected, keys, ccc_fields, source)
        parse_format = "CSV"

    actual = {_norm(field) for field in parsed["header"]}
    missing = [field for field in expected if _norm(field) not in actual]
    duplicate_headers = len(parsed["header"]) - len(actual)
    quality_flags = []
    keys_unique = source.get("keys_unique", True)
    if parsed["duplicate_keys"] and keys_unique:
        quality_flags.append("duplicate_keys")
    if parsed["duplicate_rows"]:
        quality_flags.append("duplicate_rows")
    if parsed["blank_key_rows"]:
        quality_flags.append("blank_key_rows")
    if parsed["invalid_ccc_values"]:
        quality_flags.append("invalid_ccc_values")
    if parsed["law_identity_invalid_rows"]:
        quality_flags.append("invalid_law_identity")
    parsed.update({
        "parse_format": parse_format,
        "keys_unique": keys_unique,
        "missing_fields": missing,
        "duplicate_headers": duplicate_headers,
        "quality_flags": quality_flags,
    })
    if missing or duplicate_headers or parsed["row_count"] == 0:
        parsed["status"] = "FAIL"
    elif quality_flags:
        parsed["status"] = "REVIEW"
    else:
        parsed["status"] = "PASS"
    return parsed


def _filename(dataset_id: str, fmt: str) -> str:
    extension = {"CSV": ".csv", "JSON": ".json", "XML": ".xml", "ZIP": ".zip"}.get(fmt.upper(), ".bin")
    return dataset_id + extension


def _load_sources() -> list[dict[str, Any]]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _format_frequency(value: Any) -> str:
    if not isinstance(value, dict):
        return "未提供"
    if value.get("unittime") and value.get("Frequency"):
        return f"每 {value['Frequency']} {value['unittime']}"
    if value.get("regularupdate") == "2":
        return "不定期"
    if value.get("regularupdate") == "1":
        return "定期（間隔未提供）"
    return "未提供"


def _atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(raw)
    temporary.replace(path)


def run(
    download: bool,
    source_dir: Path,
    report_dir: Path,
    candidate_dir: Path | None = None,
) -> dict[str, Any]:
    sources = _load_sources()
    results = []
    for source in sources:
        dataset_id = source["id"]
        item: dict[str, Any] = {"id": dataset_id, "name": source["name"]}
        source_path: Path | None = None
        try:
            if download:
                metadata = _metadata(dataset_id)
                distribution = _choose_distribution(metadata, source["format"])
                source_url = source.get("download_url", distribution["resourceDownloadUrl"])
                item.update({
                    "metadata_modified": metadata.get("modifiedDate"),
                    "update_frequency": _format_frequency(metadata.get("updateFrequency")),
                    "resource_amount": distribution.get("resourceAmount"),
                    "source_url": source_url,
                })
                source_path = source_dir / _filename(dataset_id, source["format"])
                raw = _request_bytes(source_url, MAX_DOWNLOAD_BYTES)
                candidate_path = (candidate_dir or report_dir / "candidates") / source_path.name
                _atomic_write(candidate_path, raw)
                item["candidate_file"] = str(candidate_path)
            else:
                matches = sorted(source_dir.glob(dataset_id + ".*"))
                if not matches:
                    item.update({"status": "MISSING", "error": "no local snapshot; rerun with --download"})
                    results.append(item)
                    continue
                source_path = matches[0]
                raw = source_path.read_bytes()
            validation = validate_payload(raw, source)
            item.update(validation)
            item.update({
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            })
            if download and validation["status"] == "PASS":
                _atomic_write(source_path, raw)
                item["file"] = str(source_path)
                item["snapshot_promoted"] = True
            elif download:
                item["snapshot_promoted"] = False
                item["last_good_snapshot_preserved"] = source_path.exists()
            else:
                item["file"] = str(source_path)
        except Exception as exc:  # report one failed source without skipping the rest
            item.update({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
        results.append(item)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "download_and_validate" if download else "validate_local_snapshots",
        "results": results,
        "summary": {
            "total": len(results),
            "pass": sum(item.get("status") == "PASS" for item in results),
            "review": sum(item.get("status") == "REVIEW" for item in results),
            "fail": sum(item.get("status") == "FAIL" for item in results),
            "missing": sum(item.get("status") == "MISSING" for item in results),
        },
    }
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "official_source_validation.json"
    md_path = report_dir / "official_source_validation.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, md_path)
    return report


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Official source snapshot validation",
        "",
        f"Generated: {report['generated_at']}",
        f"Mode: {report['mode']}",
        "",
        "| ID | Dataset | Status | Rows | Missing fields | Quality flags | Duplicate keys | Exact duplicate rows | Key uniqueness | Blank keys | Invalid CCC | Metadata modified | Update frequency | SHA-256 |",
        "|---|---|---:|---:|---|---|---:|---:|---|---:|---:|---|---|---|",
    ]
    for item in report["results"]:
        lines.append(
            "| {id} | {name} | {status} | {rows} | {missing} | {quality} | {duplicates} | {duplicate_rows} | {key_uniqueness} | {blank} | {invalid_ccc} | {modified} | {frequency} | {sha} |".format(
                id=item.get("id", ""), name=item.get("name", "").replace("|", "\\|"),
                status=item.get("status", "FAIL"), rows=item.get("row_count", ""),
                missing=", ".join(item.get("missing_fields", [])) or "-",
                quality=", ".join(item.get("quality_flags", [])) or "-",
                duplicates=item.get("duplicate_keys", ""),
                duplicate_rows=item.get("duplicate_rows", ""),
                key_uniqueness="unique" if item.get("keys_unique", True) else "non-unique allowed",
                blank=item.get("blank_key_rows", ""),
                invalid_ccc=item.get("invalid_ccc_values", ""),
                modified=item.get("metadata_modified", "-"),
                frequency=item.get("update_frequency", "-"), sha=item.get("sha256", "-"),
            )
        )
    summary = report["summary"]
    lines.extend([
        "",
        "Summary: " + ", ".join(f"{key}={summary[key]}" for key in ("total", "pass", "review", "fail", "missing")),
        "",
    ])
    for item in report["results"]:
        if item.get("error"):
            lines.append(f"- {item.get('id')} {item.get('name')}: {item['error']}")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="download current official snapshots, then validate")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--candidate-dir", type=Path, default=DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args()
    report = run(args.download, args.source_dir, args.report_dir, args.candidate_dir)
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"Report: {args.report_dir / 'official_source_validation.md'}")
    print(f"Details: {args.report_dir / 'official_source_validation.json'}")
    return 0 if all(report["summary"][key] == 0 for key in ("review", "fail", "missing")) else 1


if __name__ == "__main__":
    sys.exit(main())
