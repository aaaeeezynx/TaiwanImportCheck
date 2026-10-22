# TaiwanImportCheck 專案現況與資料缺口

最後更新：2026-10-23（Asia/Taipei）

本文件是目前的專案狀態快照。產品需求仍以 `README.md`、`01` 至 `08` 編號
規格與 `AGENTS.md` 為準；本文只記錄已完成、未完成、已知限制與下一步。

## 1. 目前結論

- P0 的程式碼、權限控制、Mock SQLite 查詢與 Hermes plugin 整合已有自動測試；
  2026-10-23 實跑 36 項測試全部通過，Python 語法檢查通過。
- 官方參考資料庫已建立 18 個來源的 schema 與版本管理，但這不等於資料已可
  正式使用。最新隔離核驗結果是 14 PASS、2 REVIEW、2 FAIL、0 MISSING。
- 目前有 14 個來源具備 active version；`5996`、`92103`、`7078`、`8876`
  沒有可啟用版本。`9714` 已有 active PASS 版本，但 registry 的
  `last_status` 尚未寫回，仍是舊的 FAIL。
- Docker Client/Server 29.6.1 與 Compose v5.2.0 可用。2026-10-23 本輪已
  實際重建並啟動 `tic-p0-hermes`，s6 初始化與 gateway 均正常；容器內 smoke
  test 通過 plugin、SQLite、文字／圖片／URL 輸入與 allowlist 驗證。
- Telegram live E2E 尚未完成，因為目前沒有本次可使用的測試 Bot 憑證。
- 本次完成 Git 初始化、提交與推送後，專案會同步到
  `https://github.com/aaaeeezynx/TaiwanImportCheck.git`；資料庫、快照、報告、
  Hermes home 與 `.env` 不納入版本控制。

## 2. P0 驗證鏈與邊界

P0 只驗證以下鏈路：

1. 授權的 Telegram 使用者傳送商品文字、商品圖片或商品 URL。
2. Hermes 載入專案 plugin，呼叫唯一的本地工具 `p0_import_lookup`。
3. 工具以 session context 取得身分，查詢
   `data/official_data/p0_mock_tariff.db`。
4. 工具回傳結構化結果與 `[P0測試假資料]` disclaimer。
5. Hermes 將結果整理成適合 Telegram 的繁中回覆。

allowlist 採 fail-closed：`TELEGRAM_ALLOWED_USERS` 或
`ADMIN_TELEGRAM_USERS` 留空、使用者不在清單，或工具收到偽造的 `user_id`
參數時，查詢或管理動作都會被拒絕。工具 schema 不暴露 `user_id`，由 Hermes
session context 注入。

P0 不包含案件管理、網站、多 Agent、正式申報、正式法規判斷或官方資料查詢。
`not_found_in_mock` 只代表假資料沒有命中，不代表免稅、免驗、無輸入規定或可
合法銷售。

## 3. 兩個業務資料庫的隔離

目前只需要兩個業務 SQLite 資料庫，不需要第三個：

| 資料庫 | 用途 | 是否進 Git | Hermes P0 是否可讀 |
|---|---|---:|---:|
| `data/official_data/p0_mock_tariff.db` | P0 假資料與測試查詢 | 否 | 是，唯一查詢來源 |
| `data/official_data/official_reference.db` | 18 個官方來源的版本化參考庫 | 否 | 否 |

`data/hermes_home/` 是 Hermes 的 session、記憶與執行設定，不是業務查詢資料庫。

`official_reference.db` 內有 18 張來源表、2 張版本管理表
（`official_dataset_registry`、`official_dataset_versions`）與 18 個
`active_<table>` view。`P0_MOCK_DB_PATH` 必須維持指向 Mock DB，不能改成
`official_reference.db`。

## 4. 官方來源與最新核驗狀態

最新隔離核驗報告位於本機暫存目錄
`.tmp/live-validation-20261022/official_source_validation.json`；穩定報告
`data/official_data/reports/official_source_validation.json` 仍是較舊的
13 PASS、2 REVIEW、3 FAIL 結果。兩者不可混用。

| Dataset | SQLite 資料表 | 最新狀態 | Active version |
|---|---|---:|---:|
| 80871 | `tariff_rates` | PASS | 有 |
| 92103 | `commodity_rules` | REVIEW | 無 |
| 16519 | `import_requirement_codes` | PASS | 有 |
| 30171 | `requirement_authorities` | PASS | 有 |
| 5996 | `telecom_certified_devices` | REVIEW | 無 |
| 171493 | `telecom_radio_equipment` | PASS | 有 |
| 9639 | `food_regulations` | PASS | 有 |
| 9047 | `registered_food_products` | PASS | 有 |
| 6133 | `food_noncompliance` | PASS | 有 |
| 9116 | `drug_ccc_mappings` | PASS | 有 |
| 9122 | `drug_licenses` | PASS | 有 |
| 9576 | `medical_device_licenses` | PASS | 有 |
| 7078 | `bsmi_required_inspections` | FAIL | 無 |
| 9714 | `bsmi_surveillance_items` | PASS | 有 |
| 8876 | `bsmi_batch_inspection_items` | FAIL | 無 |
| 9640 | `food_additive_standards` | PASS | 有 |
| 6713 | `tariff_advance_rulings` | PASS | 有 |
| 18289 | `taiwan_laws` | PASS | 有 |

`9714` 的特殊情況：最新隔離核驗與對應版本表都是 PASS，且 active version
存在；但 registry 的 `last_status` 仍是舊的 FAIL。這是狀態尚未寫回，不可
描述成 active 資料失效，也不能直接把舊錯誤清除後宣稱所有來源都正常。

## 5. 四個主要缺口與補足方式

### 5.1 `92103` 貨品及規定生效資料重複

目前下載 12,703 筆，但 12,702 筆被判為重複 key，且 12,702 筆是完全重複列；
官方 metadata 又標示 12,699 筆。這個結果不可直接去重後當成官方資料。

處理步驟：

1. 保留原始 JSON、SHA-256 與下載時間，不再把整份大檔直接輸出到終端。
2. 以串流或分批方式比對 JSON 結構、資料根節點、陣列層級、分頁與尾端截斷，
   確認是否為下載器或解析器誤把包裝層拆成多筆。
3. 用官方單筆查詢、條件查詢與 metadata 筆數交叉比對少量樣本。
4. 若來源本身允許一對多，重新定義比對鍵並增加列級 hash；若確認是真正重複，
   保留 snapshot、標記 REVIEW，不自動匯入。
5. 修正後重新計算 SHA-256，只有通過欄位、筆數、鍵值與內容抽樣核驗才啟用。

### 5.2 `5996` NCC 電信設備清單重複

目前 111,331 筆，有 516 組重複比對 key，另有 8 筆完全重複列。`設備種類 +
西元年度後二位 + 審定證明編號` 在 manifest 中已標示可一對多，因此不能只看
key 重複就刪除。

處理步驟：

1. 保留原始 CSV 與列級 hash，先確認 516 組 key 的差異欄位是否代表不同型號、
   頻率、審驗狀態或同一設備的多筆紀錄。
2. 將 8 筆完全重複列隔離並記錄列號；完全重複列不得靜默去重。
3. 依官方欄位定義確認複合鍵；必要時增加來源列號或列 hash 作為追蹤欄位。
4. 確認一對多合法後，更新唯一性規則並重跑核驗；未確認前維持 REVIEW 且不
   啟用。

### 5.3 `7078`、`8876` BSMI TLS 憑證失敗

這兩個來源目前是 Windows curl `SEC_E_UNTRUSTED_ROOT`。不得使用 `-k`、關閉
憑證驗證、忽略憑證鏈或把 HTTP 降級當成解法。

處理步驟：

1. 確認 Windows 根憑證、企業代理與 TLS 攔截憑證是否正確安裝。
2. 用瀏覽器、PowerShell 與另一個受信任的 TLS 用戶端交叉檢查憑證鏈。
3. 尋找同一個 BSMI 官方資料集的替代 HTTPS 入口或 API。
4. 修正後保留新的下載證據與 hash；仍失敗就保留舊 active version，明確標示
   資料無法更新。

### 5.4 法規涵蓋範圍與可追溯性

現有 `18289` 提供中文法規資料、`9639` 提供食品法規條文，但還沒有一份完整的
來源目錄，逐項記錄主管機關、法規層級、官方 URL、格式、更新頻率、生效日、
失效日、dataset ID 與核驗狀態。

最小補足方式是擴充來源 manifest，之後再視需求建立正規化來源目錄；欄位至少
要有：

- `dataset_id`、`authority`、`legal_level`、`title`
- `official_url`、`data_format`、`update_frequency`
- `effective_from`、`effective_to`、`status`（現行、廢止、待確認）
- `source_version_id`、`sha256`、`validated_at`、`validation_status`

法規文字可以保留在 `taiwan_laws` / `food_regulations` 的原始欄位，但「這條
法規是否現行有效、是否適用某商品」仍需人工覆核，不能只靠下載成功判定。

## 6. 更新頻率與自動核驗

目前 Windows 排程名稱為 `TaiwanImportCheck Official Data Weekly Update`，
每週日 03:00（本機時區）檢查 18 個來源。2026-10-22 上次執行結果為
`LastTaskResult = 1`，原因是存在 REVIEW/FAIL，不是任務完全沒有執行；下次
執行時間是 2026-10-25 03:00。

低維護成本建議：

- 法規與高風險來源每天做一次輕量 metadata、`ETag`、`Last-Modified` 或官方
  索引檢查；完整快照與內容核驗仍每週一次。
- 一般許可證、設備清單與案例資料每週檢查；有重大公告時手動立即執行
  `--download`。
- 相同 SHA-256 不新增版本；只有 PASS 才匯入並切換 active version。
- REVIEW、FAIL、MISSING 一律保留舊版本，不自動覆蓋；失敗時保留錯誤摘要，
  不把 secrets 寫進報告。
- 下一階段增加版本差異報告、CCC/規定/機關跨表關聯檢查、生效與失效日期檢查、
  人工覆核閘門與失敗通知。

目前自動核驗只涵蓋格式、欄位、重複欄名、非零筆數、空 key、唯一 key、完全
重複列與 CCC 格式；尚未證明法規內容正確、資料集完整、條文現行有效或商品
適用性。

## 7. 欄位放置位置

每個官方來源保留原始欄名，寫入同名 SQLite `TEXT` 欄位，另加：

- `record_id`：資料列主鍵
- `source_version_id`：來源版本
- `source_row_number`：原始列號

版本與來源資訊放 `official_dataset_versions`：

`version_id`、`dataset_id`、`sha256`、`source_url`、`metadata_modified`、
`update_frequency`、`fetched_at`、`validated_at`、`record_count`、
`validation_status`。

目前 active pointer 與最近一次檢查結果放 `official_dataset_registry`：

`dataset_id`、`dataset_name`、`source_table`、`active_version_id`、
`last_check_at`、`last_status`、`last_error`。

常見查詢欄位對應：

- CCC：稅則表的 `貨品分類號列`、貨品規定的 `good_code`、BSMI 的 `cccode`。
- 稅率：`第一欄稅率`、`第二欄稅率`、`第三欄稅率`。
- 輸入規定：稅則表的 `輸入規定` 與貨品規定的 `ireg`；代號說明放
  `import_requirement_codes`，主管機關放 `requirement_authorities`。
- BSMI/NCC/TFDA：使用各自來源表的許可證、審驗、檢驗、法規、品名與日期欄位，
  不把「查無資料」轉成免驗或合法。

完整 18 個來源的原始欄位、比對鍵與唯一性設定，請見
[`official_database_field_map.md`](official_database_field_map.md) 與
[`../config/official_sources.json`](../config/official_sources.json)。

## 8. 啟動與測試

初始化 P0 假資料並跑測試：

```powershell
& 'C:\Users\aaaee\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe' scripts\init_mock_db.py
& 'C:\Users\aaaee\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe' -m unittest discover -s tests -v
& 'C:\Users\aaaee\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe' -m compileall -q tools scripts tests .hermes
```

Compose 設定檢查（不需要把秘密貼到命令列）：

```powershell
docker compose config --quiet
```

Windows 本機 Hermes 與 Docker 的完整啟動步驟、allowlist 設定與 Telegram
測試內容，請見 [`../P0_驗證說明.md`](../P0_驗證說明.md)。不要把 `.env`、
Bot Token、模型 API key、真實 Telegram User ID 或資料庫提交到 Git。

## 9. 目前限制與下一步

- Telegram live E2E、圖片事件到 `image_path` 的實際傳遞、真實 gateway
  重啟與 health 行為尚未驗證。
- 本輪已通過 Docker 建置、容器啟動、gateway 與容器內 smoke test；但 `.env`
  仍是範例 Bot Token，Telegram 平台連線被拒絕，因此不能把 Telegram live E2E
  標示為完成。
- 18 個來源仍有 2 個 REVIEW、2 個 FAIL；`9714` registry 狀態需修正寫回。
- 正式資料庫尚未接入 Hermes，這是刻意的隔離，不是漏接。
- 下一步先處理 `92103`、`5996` 的可重現診斷與 `7078`、`8876` 的 TLS，
  再補來源目錄、版本差異與人工覆核；完成後才評估讓正式資料庫進入查詢鏈。
