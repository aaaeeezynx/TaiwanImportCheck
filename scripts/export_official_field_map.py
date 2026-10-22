#!/usr/bin/env python3
"""Generate the human-readable SQLite field map from the source manifest."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.official_database import load_sources


def main() -> int:
    output = ROOT / "docs" / "official_database_field_map.md"
    lines = [
        "# Official database field map",
        "",
        "Generated from `config/official_sources.json`. Source fields are stored as SQLite `TEXT` columns with the same names.",
        "Each dataset table also has `record_id` (`INTEGER PRIMARY KEY`), `source_version_id` (`TEXT`), and `source_row_number` (`INTEGER`).",
        "",
        "The active view is named `active_<table>`. Version and source metadata are in `official_dataset_versions`; current pointers, manifest metadata, and latest check statuses are in `official_dataset_registry`.",
        "",
        "## Source metadata",
        "",
        "| Dataset ID | Authority | Official dataset | Format / content type | Declared update frequency | License | Effective dates | Validation rules |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for source in load_sources():
        effective = " - ".join(
            value or "open"
            for value in (source.get("effective_from"), source.get("effective_to"))
        )
        lines.append(
            "| {id} | {authority} | {url} | {format} / `{content_type}` | {frequency} | [{code} {name}]({license_url}) | {effective} | {rules} |".format(
                id=source["id"],
                authority=source["authority"].replace("|", "\\|"),
                url=source["official_dataset_url"],
                format=source["format"].upper(),
                content_type=source["content_type"],
                frequency=source["update_frequency"].replace("|", "\\|"),
                code=source["license"]["code"],
                name=source["license"]["name"].replace("|", "\\|"),
                license_url=source["license"]["url"],
                effective=effective,
                rules="、".join(source["validation_rules"]),
            )
        )
    lines.extend([
        "",
        "## Source fields",
        "",
        "| Dataset ID | Dataset | SQLite table | Source fields / same-named columns | Match fields | Match fields unique? |",
        "|---|---|---|---|---|---|",
    ])
    for source in load_sources():
        lines.append(
            "| {id} | {name} | `{table}` | {fields} | {keys} | {unique} |".format(
                id=source["id"],
                name=source["name"].replace("|", "\\|"),
                table=source["table"],
                fields="、".join(source["fields"]).replace("|", "\\|"),
                keys="、".join(source.get("keys", [])) or "未定義",
                unique="是" if source.get("keys_unique", True) else "否；可對應多筆不同記錄",
            )
        )
    lines.extend([
        "",
        "All source-specific columns use `TEXT` to preserve the source's original value and formatting. Parse dates or rates in query code when needed; do not infer missing values.",
        "Match fields help locate or relate records. A repeated match-field value is informational when uniqueness is marked `no`; exact duplicate rows are checked separately.",
        "For dataset 18289, nested values declared in `nested_fields` are stored as XML text in their source column so article/chapter structure is retained.",
        "Registry metadata includes `authority`, `official_dataset_url`, `format`, `content_type`, `license_code`, `license_name`, `license_url`, `declared_update_frequency`, `effective_from`, `effective_to`, `validation_rules`, and `metadata_modified`.",
        "Version metadata preserves the same source provenance on each imported snapshot, along with `source_url`, `fetched_at`, `validated_at`, `record_count`, `sha256`, and `validation_status`.",
        "",
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(f"Generated field map for {len(load_sources())} datasets: {output.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
