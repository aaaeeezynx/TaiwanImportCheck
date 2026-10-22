# Official database field map

Generated from `config/official_sources.json`. Source fields are stored as SQLite `TEXT` columns with the same names.
Each dataset table also has `record_id` (`INTEGER PRIMARY KEY`), `source_version_id` (`TEXT`), and `source_row_number` (`INTEGER`).

The active view is named `active_<table>`. Version and source metadata are in `official_dataset_versions`; current pointers, manifest metadata, and latest check statuses are in `official_dataset_registry`.

## Source metadata

| Dataset ID | Authority | Official dataset | Format / content type | Declared update frequency | License | Effective dates | Validation rules |
|---|---|---|---|---|---|---|---|
| 80871 | 財政部關務署 | https://data.gov.tw/dataset/80871 | CSV / `text/csv` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows、valid_ccc_format |
| 92103 | 經濟部國際貿易署 | https://data.gov.tw/dataset/92103 | JSON / `application/json` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows、valid_ccc_format |
| 16519 | 經濟部國際貿易署 | https://data.gov.tw/dataset/16519 | CSV / `text/csv` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows |
| 30171 | 經濟部國際貿易署 | https://data.gov.tw/dataset/30171 | CSV / `text/csv` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows |
| 5996 | 國家通訊傳播委員會 | https://data.gov.tw/dataset/5996 | CSV / `text/csv` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、nonblank_match_keys、exact_duplicate_rows |
| 171493 | 國家通訊傳播委員會 | https://data.gov.tw/dataset/171493 | CSV / `text/csv` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows |
| 9639 | 衛生福利部食品藥物管理署 | https://data.gov.tw/dataset/9639 | CSV / `text/csv` | 每年 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows |
| 9047 | 衛生福利部食品藥物管理署 | https://data.gov.tw/dataset/9047 | CSV / `text/csv` | 每 3 個月 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows |
| 6133 | 衛生福利部食品藥物管理署 | https://data.gov.tw/dataset/6133 | CSV / `text/csv` | 每 7 日 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、nonblank_match_keys、exact_duplicate_rows、valid_ccc_format |
| 9116 | 衛生福利部食品藥物管理署 | https://data.gov.tw/dataset/9116 | ZIP / `application/zip` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows、valid_ccc_format、zip_member_format |
| 9122 | 衛生福利部食品藥物管理署 | https://data.gov.tw/dataset/9122 | ZIP / `application/zip` | 每 7 日 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、nonblank_match_keys、exact_duplicate_rows、zip_member_format |
| 9576 | 衛生福利部食品藥物管理署 | https://data.gov.tw/dataset/9576 | ZIP / `application/zip` | 每 7 日 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、nonblank_match_keys、exact_duplicate_rows、zip_member_format |
| 7078 | 經濟部標準檢驗局 | https://data.gov.tw/dataset/7078 | XML / `application/xml` | 每年 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows、valid_ccc_format |
| 9714 | 經濟部標準檢驗局 | https://data.gov.tw/dataset/9714 | XML / `application/xml` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows、valid_ccc_format |
| 8876 | 經濟部標準檢驗局 | https://data.gov.tw/dataset/8876 | XML / `application/xml` | 每月 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、unique_match_keys、exact_duplicate_rows、valid_ccc_format |
| 9640 | 衛生福利部食品藥物管理署 | https://data.gov.tw/dataset/9640 | CSV / `text/csv` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、nonblank_match_keys、exact_duplicate_rows |
| 6713 | 財政部關務署 | https://data.gov.tw/dataset/6713 | CSV / `text/csv` | 不定期 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、nonblank_match_keys、exact_duplicate_rows、valid_ccc_format |
| 18289 | 法務部 | https://data.gov.tw/dataset/18289 | ZIP / `application/zip` | 每月 | [1 政府資料開放授權條款－第1版](https://data.gov.tw/license) | open - open | required_fields、nonempty_payload、duplicate_headers、nonblank_match_keys、exact_duplicate_rows、zip_member_format、law_identity_requires_url_and_dates |

## Source fields

| Dataset ID | Dataset | SQLite table | Source fields / same-named columns | Match fields | Match fields unique? |
|---|---|---|---|---|---|
| 80871 | 海關進口稅則 | `tariff_rates` | 貨品分類號列、中文貨名、英文貨名、第一欄稅率、第二欄稅率、第三欄稅率、統計數量單位、統計重量單位、稽徵規定、輸入規定、輸出規定 | 貨品分類號列 | 是 |
| 92103 | 貨品及規定生效資料 | `commodity_rules` | good_code、good_pre_code、good_c_name、good_e_name、effect_date、ireg、ereg | good_code、good_pre_code、effect_date、ireg | 是 |
| 16519 | 貨品輸入規定代號 | `import_requirement_codes` | 輸入規定代號、規定中文說明、規定英文說明、規定生效日、資料筆數 | 輸入規定代號 | 是 |
| 30171 | 規定與主管機關 | `requirement_authorities` | 序號、主管機關、輸入規定代號、輸出規定代號、單位物件識別碼OID | 序號 | 是 |
| 5996 | 電信設備審驗合格清單 | `telecom_certified_devices` | 設備種類、西元年度後二位、審定證明編號、填報貿易e網的認證號碼、設備名稱、廠牌、型號、申請廠商、製造廠商、驗證機構、SAR值、拍照功能、拍照時發出聲響功能、充電器、電池、審驗日期、換證日期、出具檢驗報告之測試實驗室、測試實驗室所屬國家、驗證明是否仍屬有效、廢止撤銷或註銷日期 | 設備種類、西元年度後二位、審定證明編號 | 否；可對應多筆不同記錄 |
| 171493 | 電信管制射頻器材逐步審驗合格清單 | `telecom_radio_equipment` | 設備種類、西元年度後二位、審定證明編號、填報貿易e網的認證號碼、設備名稱、廠牌、型號、申請廠商、製造廠商、驗證機構、SAR值、拍照功能、拍照時發出聲響功能、充電器、電池、審驗日期、換證日期、出具檢驗報告之測試實驗室、測試實驗室所屬國家、驗證明是否仍屬有效、廢止撤銷或註銷日期、序號、頻率、最大功率 | 填報貿易e網的認證號碼 | 是 |
| 9639 | 食品法規條文資料集 | `food_regulations` | 類別、法規性質、法規名稱、條文內容、發佈日期、附件下載 | 法規名稱、發佈日期 | 是 |
| 9047 | 輸入錠狀膠囊狀食品查驗登記 | `registered_food_products` | 核備函字號、中文品名、英文品名、有效日期、發證日期、申請商名稱、申請商電話、申請商地址、成分、簽審文件編號 | 核備函字號 | 是 |
| 6133 | 不符合食品資訊資料集 | `food_noncompliance` | 產地、主旨、原因、進口商名稱、進口商地址、貨品分類號列、檢驗方法、不合格原因暨檢出量詳細說明、法規限量標準、製造廠或出口商名稱、製造商代碼、牌名、重量、處置情形、發布日期、報驗受理日期、附圖 | 主旨、發布日期 | 否；可對應多筆不同記錄 |
| 9116 | 藥品CCC號列資料集 | `drug_ccc_mappings` | 許可證字號、CCC號列一、CCC號列一說明 | 許可證字號、CCC號列一 | 是 |
| 9122 | 全部藥品許可證資料 | `drug_licenses` | 許可證字號、註銷狀態、註銷日期、註銷理由、有效日期、發證日期、許可證種類、舊證字號、通關簽審文件編號、中文品名、英文品名、適應症、劑型、包裝、藥品類別、管制藥品分類級別、主成分略述、申請商名稱、申請商地址、申請商統一編號、製造商名稱、製造廠廠址、製造廠公司地址、製造廠國別、製程、異動日期、用法用量、包裝與國際條碼 | 許可證字號 | 否；可對應多筆不同記錄 |
| 9576 | 醫療器材許可證資料集 | `medical_device_licenses` | 許可證字號、註銷狀態、註銷日期、註銷理由、有效日期、發證日期、許可證種類、舊證字號、醫療器材級數、通關簽審文件編號、中文品名、英文品名、效能、劑型、包裝、醫器主類別一、醫器次類別一、醫器主類別二、醫器次類別二、醫器主類別三、醫器次類別三、主成分略述、醫器規格、限制項目、申請商名稱、申請商地址、申請商統一編號、製造商名稱、製造廠廠址、製造廠公司地址、製造廠國別、製程、異動日期、製造許可登錄編號 | 許可證字號 | 否；可對應多筆不同記錄 |
| 7078 | BSMI應施檢驗/查驗品目 | `bsmi_required_inspections` | ccccode、chiname、engname、localname、chinote | ccccode | 是 |
| 9714 | BSMI監視查驗品目 | `bsmi_surveillance_items` | ccccode、chiname、engname、localname、examimp、checkway、檢驗期限、sampid、計量單位、列檢日期 | ccccode、列檢日期 | 是 |
| 8876 | BSMI逐批檢驗品目 | `bsmi_batch_inspection_items` | ccccode、chiname、engname、localname、prodkindname、公告規定、examimp、檢驗期限、sampid、計量單位、列檢日期、取樣數量 | ccccode、列檢日期 | 是 |
| 9640 | 食品添加物使用範圍及限量暨規格標準 | `food_additive_standards` | 類別、中文品名、英文品名、使用食品範圍及限量、使用限制、規格、類別說明 | 中文品名、英文品名 | 否；可對應多筆不同記錄 |
| 6713 | 稅則預先審核案例 | `tariff_advance_rulings` | 稅則分類理由、稅則、稅則號別、統計號別、檢查碼、申報貨品名稱、材質成分、功能及用途、發文文號、發文日期、中文稅則名稱、英文稅則名稱、備註 | 發文文號 | 否；可對應多筆不同記錄 |
| 18289 | 中文法規法律資料檔 | `taiwan_laws` | 法規性質、法規名稱、法規網址、法規類別、最新異動日期、生效日期、生效內容、廢止註記、是否英譯註記、英文法規名稱、附件、沿革內容、前言、法規內容 | 法規名稱 | 否；可對應多筆不同記錄 |

All source-specific columns use `TEXT` to preserve the source's original value and formatting. Parse dates or rates in query code when needed; do not infer missing values.
Match fields help locate or relate records. A repeated match-field value is informational when uniqueness is marked `no`; exact duplicate rows are checked separately.
For dataset 18289, nested values declared in `nested_fields` are stored as XML text in their source column so article/chapter structure is retained.
Registry metadata includes `authority`, `official_dataset_url`, `format`, `content_type`, `license_code`, `license_name`, `license_url`, `declared_update_frequency`, `effective_from`, `effective_to`, `validation_rules`, and `metadata_modified`.
Version metadata preserves the same source provenance on each imported snapshot, along with `source_url`, `fetched_at`, `validated_at`, `record_count`, `sha256`, and `validation_status`.
