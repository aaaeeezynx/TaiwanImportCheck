import csv
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts.validate_official_snapshot import _write_markdown, run, validate_payload


class OfficialSnapshotValidatorTests(unittest.TestCase):
    def setUp(self):
        self.csv_source = {
            "fields": ["貨品分類號列", "中文貨名"],
            "keys": ["貨品分類號列"],
            "ccc_fields": ["貨品分類號列"],
        }

    def test_csv_required_fields_and_record_count(self):
        payload = "貨品分類號列,中文貨名\n01012100003,測試商品\n".encode("utf-8-sig")
        result = validate_payload(payload, self.csv_source)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["row_count"], 1)

    def test_csv_missing_required_field_fails(self):
        payload = "貨品分類號列\n01012100003\n".encode("utf-8")
        result = validate_payload(payload, self.csv_source)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["missing_fields"], ["中文貨名"])

    def test_json_record_list(self):
        payload = json.dumps([{"貨品分類號列": "01012100003", "中文貨名": "測試商品"}], ensure_ascii=False).encode("utf-8")
        result = validate_payload(payload, self.csv_source)
        self.assertEqual(result["parse_format"], "JSON")
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["status"], "PASS")

    def test_repeated_json_records_require_review(self):
        payload = json.dumps(
            [{"貨品分類號列": "01012100003", "中文貨名": "測試商品"}] * 3,
            ensure_ascii=False,
        ).encode("utf-8")
        result = validate_payload(payload, self.csv_source)
        self.assertEqual(result["duplicate_keys"], 2)
        self.assertEqual(result["duplicate_rows"], 2)
        self.assertEqual(result["status"], "REVIEW")

    def test_non_unique_match_fields_do_not_reject_distinct_records(self):
        source = {
            "fields": ["證書字號", "型號"],
            "keys": ["證書字號"],
            "keys_unique": False,
        }
        payload = "證書字號,型號\nA-1,型號甲\nA-1,型號乙\n".encode("utf-8")
        result = validate_payload(payload, source)
        self.assertEqual(result["duplicate_keys"], 1)
        self.assertEqual(result["duplicate_rows"], 0)
        self.assertEqual(result["status"], "PASS")

    def test_xml_record_list(self):
        payload = """<?xml version='1.0' encoding='utf-8'?>
        <records><record><貨品分類號列>01012100003</貨品分類號列><中文貨名>測試商品</中文貨名></record>
        <record><貨品分類號列>01012100004</貨品分類號列><中文貨名>另一商品</中文貨名></record></records>""".encode("utf-8")
        result = validate_payload(payload, self.csv_source)
        self.assertEqual(result["parse_format"], "XML")
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["status"], "PASS")

    def test_xml_with_utf8_bom_is_detected_as_xml(self):
        source = {"fields": ["法規名稱", "法規內容"], "keys": ["法規名稱"]}
        payload = "\ufeff<LAWS><法規><法規名稱>測試法規</法規名稱><法規內容>內容</法規內容></法規></LAWS>".encode("utf-8")
        result = validate_payload(payload, source)
        self.assertEqual(result["parse_format"], "XML")
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["status"], "PASS")

    def test_nested_law_xml_validates_law_records_not_article_rows(self):
        source = {
            "fields": ["法規名稱", "法規內容"],
            "keys": ["法規名稱"],
        }
        payload = """<LAWS>
        <法規><法規名稱>測試法規一</法規名稱><法規內容><編章節>第一章</編章節>
        <條文><條號>第一條</條號><條文內容>內容一</條文內容></條文>
        <條文><條號>第二條</條號><條文內容>內容二</條文內容></條文></法規內容></法規>
        <法規><法規名稱>測試法規二</法規名稱><法規內容><編章節>第一章</編章節>
        <條文><條號>第一條</條號><條文內容>內容三</條文內容></條文></法規內容></法規>
        </LAWS>""".encode("utf-8")
        result = validate_payload(payload, source)
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["missing_fields"], [])
        self.assertEqual(result["status"], "PASS")

    def test_zip_wrapping_csv(self):
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("data.csv", "貨品分類號列,中文貨名\n01012100003,測試商品\n")
        result = validate_payload(memory.getvalue(), self.csv_source)
        self.assertEqual(result["parse_format"], "ZIP")
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["status"], "PASS")

    def test_ccc_punctuation_is_normalized_for_format_check(self):
        source = {
            "fields": ["貨品分類號列", "中文貨名"],
            "ccc_fields": ["貨品分類號列"],
        }
        payload = "貨品分類號列,中文貨名\n3924.10.00.90-6,測試商品\n".encode("utf-8")
        result = validate_payload(payload, source)
        self.assertEqual(result["invalid_ccc_values"], 0)

    def test_bsmi_inspection_suffix_is_allowed(self):
        source = {"fields": ["ccccode", "chiname"], "ccc_fields": ["ccccode"]}
        payload = "ccccode,chiname\n39181090905B,測試品目\n".encode("utf-8")
        result = validate_payload(payload, source)
        self.assertEqual(result["invalid_ccc_values"], 0)
        self.assertEqual(result["status"], "PASS")

    def test_report_places_quality_flags_in_their_own_column(self):
        report = {
            "generated_at": "2026-01-01T00:00:00+00:00",
            "mode": "validate_local_snapshots",
            "summary": {"total": 1, "pass": 0, "review": 1, "fail": 0, "missing": 0},
            "results": [{
                "id": "1", "name": "測試", "status": "REVIEW", "row_count": 3,
                "missing_fields": [], "quality_flags": ["duplicate_keys"],
                "duplicate_keys": 2, "duplicate_rows": 0, "keys_unique": True,
                "blank_key_rows": 0, "invalid_ccc_values": 0,
                "metadata_modified": "-", "update_frequency": "-", "sha256": "abc",
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "report.md"
            _write_markdown(report, path)
            report_text = path.read_text(encoding="utf-8")
            row = next(line for line in report_text.splitlines() if line.startswith("| 1 |"))
        self.assertEqual(row.split("|")[3].strip(), "-")
        self.assertEqual(row.split("|")[4].strip(), "REVIEW")
        self.assertEqual(row.split("|")[7].strip(), "duplicate_keys")
        self.assertEqual(row.split("|")[8].strip(), "2")
        self.assertIn("Exact duplicate rows", report_text)
        self.assertIn("pass=0, review=1, fail=0, missing=0", report_text)

    def test_local_validation_prefers_source_snapshot_over_candidate(self):
        source = {
            "id": "99991",
            "name": "測試官方資料",
            "fields": ["分類代碼", "品名"],
            "keys": ["分類代碼"],
        }
        source_payload = "分類代碼,品名\n12345678,來源版本\n".encode("utf-8")
        candidate_payload = "分類代碼,品名\n12345678,候選版本\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source_snapshots"
            candidate_dir = root / "candidate_snapshots"
            report_dir = root / "reports"
            source_dir.mkdir()
            candidate_dir.mkdir()
            (source_dir / "99991.csv").write_bytes(source_payload)
            (candidate_dir / "99991.csv").write_bytes(candidate_payload)
            with mock.patch(
                "scripts.validate_official_snapshot._load_sources",
                return_value=[source],
            ):
                report = run(False, source_dir, report_dir, candidate_dir)
        result = report["results"][0]
        self.assertEqual(result["snapshot_origin"], "source_snapshot")
        self.assertEqual(result["sha256"], hashlib.sha256(source_payload).hexdigest())
        self.assertNotIn("snapshot_promoted", result)

    def test_local_validation_falls_back_to_candidate_without_promoting(self):
        source = {
            "id": "99991",
            "name": "測試官方資料",
            "fields": ["分類代碼", "品名"],
            "keys": ["分類代碼"],
        }
        payload = "分類代碼,品名\n12345678,候選版本\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source_snapshots"
            candidate_dir = root / "candidate_snapshots"
            report_dir = root / "reports"
            source_dir.mkdir()
            candidate_dir.mkdir()
            candidate = candidate_dir / "99991.csv"
            candidate.write_bytes(payload)
            with mock.patch(
                "scripts.validate_official_snapshot._load_sources",
                return_value=[source],
            ):
                report = run(False, source_dir, report_dir, candidate_dir)
        result = report["results"][0]
        self.assertEqual(result["snapshot_origin"], "candidate_snapshot")
        self.assertEqual(result["file"], str(candidate))
        self.assertFalse(result["snapshot_promoted"])
        self.assertFalse((source_dir / "99991.csv").exists())

    def test_local_validation_reports_missing_when_no_snapshot_exists(self):
        source = {
            "id": "99991",
            "name": "測試官方資料",
            "fields": ["分類代碼", "品名"],
            "keys": ["分類代碼"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            with mock.patch(
                "scripts.validate_official_snapshot._load_sources",
                return_value=[source],
            ):
                report = run(
                    False,
                    root / "source_snapshots",
                    root / "reports",
                    root / "candidate_snapshots",
                )
        result = report["results"][0]
        self.assertEqual(result["status"], "MISSING")
        self.assertNotIn("snapshot_origin", result)

    def test_zip_prefers_larger_xml_data_over_schema_csv(self):
        memory = io.BytesIO()
        xml = """<records><record><貨品分類號列>01012100003</貨品分類號列><中文貨名>測試商品</中文貨名></record>
        <record><貨品分類號列>01012100004</貨品分類號列><中文貨名>另一商品</中文貨名></record></records>"""
        with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("schema.csv", "field\nname\n")
            archive.writestr("laws.xml", xml)
        result = validate_payload(memory.getvalue(), self.csv_source)
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["status"], "PASS")

    def test_zip_uses_field_match_even_when_schema_csv_is_larger(self):
        memory = io.BytesIO()
        xml = """<records><record><貨品分類號列>01012100003</貨品分類號列><中文貨名>測試商品</中文貨名></record></records>"""
        schema = "field\n" + ("schema metadata,not a tariff row\n" * 200)
        with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("schema.csv", schema)
            archive.writestr("data.xml", xml)
        result = validate_payload(memory.getvalue(), self.csv_source)
        self.assertEqual(result["selected_member"], "data.xml")
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["status"], "PASS")

    def test_rejects_non_official_source_url(self):
        from scripts.validate_official_snapshot import _check_gov_url

        with self.assertRaises(ValueError):
            _check_gov_url("http://127.0.0.1/data.csv")

    def test_manifest_includes_drug_license_and_bsmi_batch_inspection_sources(self):
        from tools.official_database import load_sources

        sources = {source["id"]: source for source in load_sources()}
        self.assertEqual(sources["9122"]["table"], "drug_licenses")
        self.assertEqual(sources["9122"]["keys"], ["許可證字號"])
        self.assertFalse(sources["9122"]["keys_unique"])
        self.assertIn("有效日期", sources["9122"]["fields"])
        self.assertEqual(sources["8876"]["table"], "bsmi_batch_inspection_items")
        self.assertEqual(sources["8876"]["keys"], ["ccccode", "列檢日期"])
        self.assertIn("取樣數量", sources["8876"]["fields"])

    def test_law_identity_requires_name_official_url_and_valid_dates(self):
        source = {
            "fields": ["法規名稱", "法規網址", "最新異動日期", "生效日期"],
            "keys": ["法規名稱"],
            "validation_rules": ["law_identity_requires_url_and_dates"],
        }
        valid = (
            "法規名稱,法規網址,最新異動日期,生效日期\n"
            "測試法規,https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=X0000001,"
            "2026-01-02,20260103\n"
        ).encode("utf-8")
        invalid = (
            "法規名稱,法規網址,最新異動日期,生效日期\n"
            ",https://example.com/law,not-a-date,bad-date\n"
        ).encode("utf-8")

        valid_result = validate_payload(valid, source)
        invalid_result = validate_payload(invalid, source)

        self.assertEqual(valid_result["status"], "PASS")
        self.assertEqual(valid_result["law_identity_invalid_rows"], 0)
        self.assertEqual(invalid_result["status"], "REVIEW")
        self.assertEqual(invalid_result["law_identity_invalid_rows"], 1)
        self.assertEqual(
            invalid_result["law_identity_issue_counts"],
            {
                "blank_law_name": 1,
                "invalid_law_url": 1,
                "invalid_latest_modified_date": 1,
                "invalid_effective_date": 1,
            },
        )

    def test_law_identity_allows_blank_effective_date(self):
        source = {
            "fields": ["法規名稱", "法規網址", "最新異動日期", "生效日期"],
            "keys": ["法規名稱"],
            "validation_rules": ["law_identity_requires_url_and_dates"],
        }
        payload = (
            "法規名稱,法規網址,最新異動日期,生效日期\n"
            "測試法規,https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=X0000001,"
            "20260102,\n"
        ).encode("utf-8")
        result = validate_payload(payload, source)
        self.assertEqual(result["status"], "PASS")

    def test_manifest_fixtures_produce_14_pass_3_review_1_missing(self):
        from tools.official_database import load_sources

        sources = load_sources()
        review_ids = {"16519", "30171", "171493"}
        missing_id = "5996"
        pass_ids = {
            str(source["id"])
            for source in sources
            if str(source["id"]) not in review_ids | {missing_id}
        }
        self.assertEqual(len(sources), 18)
        self.assertEqual(len(pass_ids), 14)

        def fixture_payload(source):
            fields = list(source["fields"])
            ccc_fields = set(source.get("ccc_fields", []))
            special_values = {
                "法規名稱": "測試法規",
                "法規網址": "https://law.moj.gov.tw/LawClass/LawAll.aspx?pcode=X0000001",
                "最新異動日期": "2026-01-02",
                "生效日期": "2026-01-03",
            }
            row = {
                field: (
                    "01012100003"
                    if field in ccc_fields
                    else special_values.get(field, f"tests-{source['id']}-{field}")
                )
                for field in fields
            }
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)
            if str(source["id"]) in review_ids:
                writer.writerow(row)
            return output.getvalue().encode("utf-8")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "source_snapshots"
            candidate_dir = root / "candidate_snapshots"
            report_dir = root / "reports"
            source_dir.mkdir()
            fixture_bytes = {}
            for source in sources:
                dataset_id = str(source["id"])
                if dataset_id == missing_id:
                    continue
                payload = fixture_payload(source)
                fixture_bytes[dataset_id] = payload
                (source_dir / f"{dataset_id}.csv").write_bytes(payload)

            evidence = {}
            with mock.patch(
                "scripts.validate_official_snapshot._load_sources",
                return_value=sources,
            ):
                report = run(
                    False,
                    source_dir,
                    report_dir,
                    candidate_dir,
                    evidence=evidence,
                )

        self.assertEqual(
            report["summary"],
            {"total": 18, "pass": 14, "review": 3, "fail": 0, "missing": 1},
        )
        results = {str(item["id"]): item for item in report["results"]}
        self.assertEqual(
            {
                dataset_id
                for dataset_id, item in results.items()
                if item["status"] == "PASS"
            },
            pass_ids,
        )
        self.assertEqual(
            {
                dataset_id
                for dataset_id, item in results.items()
                if item["status"] == "REVIEW"
            },
            review_ids,
        )
        self.assertEqual(results[missing_id]["status"], "MISSING")
        self.assertNotIn(missing_id, evidence)

        sources_by_id = {str(source["id"]): source for source in sources}
        for dataset_id, payload in fixture_bytes.items():
            with self.subTest(dataset_id=dataset_id):
                item = results[dataset_id]
                self.assertEqual(evidence[dataset_id], payload)
                self.assertEqual(item["dataset_id"], dataset_id)
                self.assertEqual(item["snapshot_origin"], "source_snapshot")
                self.assertEqual(
                    item["validation_rules"],
                    sources_by_id[dataset_id]["validation_rules"],
                )
                self.assertTrue(item["validated_at"])
                self.assertEqual(
                    item["sha256"],
                    hashlib.sha256(payload).hexdigest(),
                )
                self.assertEqual(item["size_bytes"], len(payload))


if __name__ == "__main__":
    unittest.main()
