# TaiwanImportCheck 專案現況與資料缺口

最後更新：2026-10-23（Asia/Taipei）

本文件是目前的專案狀態快照。產品需求仍以 `README.md`、`01` 至 `08` 編號
規格與 `AGENTS.md` 為準；本文只記錄已完成、未完成、已知限制與下一步。

## 1. 目前結論

- P0 的程式碼、權限控制、Mock SQLite 查詢與 Hermes plugin 整合已有自動測試；
  2026-10-23 實跑 58 項測試全部通過。
- 官方參考資料庫已建立 18 個來源的 schema 與版本管理，但這不等於資料已可
  正式使用。最新本機核驗結果是 14 PASS、3 REVIEW、0 FAIL、1 MISSING，
  0 import errors。
- 目前有 14 個來源具備 active version；`92103`、`5996`、`7078` 已有候選快照，
  但核驗均為 `REVIEW`；`8876` 尚無候選快照。這 4 個來源都沒有可啟用版本。
  `9714` 的最新核驗、registry 與 active version 均為 PASS。
- Docker Client/Server 29.6.1 與 Compose v5.2.0 可用。2026-10-23 本輪已
  實際重建並啟動 `tic-p0-hermes`，s6 初始化與 gateway 均正常；容器內 smoke
  test 通過 plugin、SQLite、文字／圖片／URL 輸入與 allowlist 驗證。
- Telegram Bot 憑證已配置且 `getMe` 實測成功；目前 gateway 日誌已記錄
  `Connected to Telegram (polling mode)`、`telegram connected` 與 1 個平台
  已上線。目前尚無 allowlist 帳號的真實訊息事件處理證據，因此 Telegram
  live E2E 尚未完成。
- Hermes 已設定 NVIDIA NIM 測試模型
  `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`，基本 one-shot 回覆已通過；
  CLI 工具呼叫也已實際進入 `p0_import_lookup`。先前 CLI 缺少 Telegram session
  身分時，模型曾誤要求使用者提供 ID；該錯誤訊息與 Agent 規則已修正，回歸測試
  確認現在只回覆無法取得授權身分。真實 Telegram 回覆鏈路仍未驗證。
- 專案已初始化 Git 並設定
  `https://github.com/aaaeeezynx/TaiwanImportCheck.git` 為 origin；資料庫、快照、
  報告、Hermes home 與 `.env` 不納入版本控制。

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

最新本機核驗報告位於
`data/official_data/reports/official_source_validation.json`，結果為
14 PASS、3 REVIEW、1 FAIL、0 MISSING；更新報告
`data/official_data/reports/official_data_update.json` 顯示 14 個 active version、
0 個新版本與 0 個匯入錯誤。

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
| 7078 | `bsmi_required_inspections` | REVIEW | 無 |
| 9714 | `bsmi_surveillance_items` | PASS | 有 |
| 8876 | `bsmi_batch_inspection_items` | FAIL | 無 |
| 9640 | `food_additive_standards` | PASS | 有 |
| 6713 | `tariff_advance_rulings` | PASS | 有 |
| 18289 | `taiwan_laws` | PASS | 有 |

表中沒有 active version 的來源，代表目前沒有可啟用快照，不等於
2026-10-23 另行直接核驗 `candidate_snapshots/` 的現有候選檔，結果如下：

| Dataset | 候選檔 | 候選核驗 | 筆數 | 重複情況 |
|---|---|---:|---:|---|
| 92103 | `92103.json` | REVIEW | 12,703 | 12,702 組重複鍵；12,702 筆完全重複列 |
| 5996 | `5996.csv` | REVIEW | 111,331 | 516 組重複複合鍵；8 筆完全重複列 |
| 7078 | `7078.xml` | REVIEW | 4,914 | 3,302 組重複鍵；3,280 筆完全重複列 |
| 8876 | 無 | 無候選檔 | - | - |

上述三個候選檔的缺失欄位、重複欄名、空白鍵與無效 CCC 數量均為 0；阻擋啟用
的問題是重複資料語意尚未確認。候選快照不能直接供查詢；候選檔即使格式核驗
通過，也只會記為 `CANDIDATE_PASS`，必須先明確提升至 `source_snapshots/`
並通過正式核驗，才可能以 `PASS` 匯入並切換 active version。

`9714` 的最新核驗、`official_dataset_registry.last_status` 與 active version
均為 PASS，先前舊 FAIL 狀態已修正寫回；這仍不表示其資料內容已具備正式法律效力。

## 5. 四個主要缺口與補足方式

### 5.1 `92103` 貨品及規定生效資料重複

目前有候選快照 `candidate_snapshots/92103.json`，大小 4,662,002 bytes；核驗為
12,703 筆、12,702 組重複 key 與 12,702 筆完全重複列，官方 metadata 又標示
12,699 筆。該候選快照狀態是 `REVIEW`，不可直接去重後當成官方資料，也不可
直接啟用。

處理步驟：

1. 保留原始 JSON、SHA-256 與下載時間，不再把整份大檔直接輸出到終端。
2. 以串流或分批方式比對 JSON 結構、資料根節點、陣列層級、分頁與尾端截斷，
   確認是否為下載器或解析器誤把包裝層拆成多筆。
3. 用官方單筆查詢、條件查詢與 metadata 筆數交叉比對少量樣本。
4. 若來源本身允許一對多，重新定義比對鍵並增加列級 hash；若確認是真正重複，
   保留 snapshot、標記 REVIEW，不自動匯入。
5. 修正後重新計算 SHA-256，只有通過欄位、筆數、鍵值與內容抽樣核驗才啟用。

### 5.2 `5996` NCC 電信設備清單重複

目前有候選快照 `candidate_snapshots/5996.csv`，大小 22,716,643 bytes；核驗為
111,331 筆、516 組重複複合鍵與 8 筆完全重複列，狀態是 `REVIEW`。
`設備種類 + 西元年度後二位 + 審定證明編號` 在 manifest 中已標示可一對多，
因此不能只看 key 重複就刪除或啟用。

處理步驟：

1. 保留原始 CSV 與列級 hash，先確認 516 組 key 的差異欄位是否代表不同型號、
   頻率、審驗狀態或同一設備的多筆紀錄。
2. 將 8 筆完全重複列隔離並記錄列號；完全重複列不得靜默去重。
3. 依官方欄位定義確認複合鍵；必要時增加來源列號或列 hash 作為追蹤欄位。
4. 確認一對多合法後，更新唯一性規則並重跑核驗；未確認前維持 REVIEW 且不
   啟用。

### 5.3 `7078` BSMI 候選快照重複

目前有候選快照 `candidate_snapshots/7078.xml`，大小 2,244,255 bytes；核驗為
4,914 筆、3,302 組重複鍵與 3,280 筆完全重複列，狀態是 `REVIEW`。缺失欄位、
重複欄名、空白鍵與無效 CCC 均為 0，問題集中在重複資料的來源語意與解析結果。
必須保留原始 XML 與列級證據，確認重複是否來自合法一對多、XML 包裝層重複或
解析器展開錯誤；未確認前不得去重後啟用。

### 5.4 `8876` BSMI 候選快照缺失

`8876` 目前沒有 `candidate_snapshots/` 檔案，核驗結果為 `MISSING`。下載曾出現 Windows curl
`SEC_E_UNTRUSTED_ROOT`，但沒有足夠證據斷定所有失敗都來自 TLS 憑證；應保留
失敗日誌與下載證據再判斷。不得使用 `-k`、關閉憑證驗證、忽略憑證鏈或把 HTTP
降級當成解法。

處理步驟：

1. 先重跑下載並保存錯誤類別、HTTP 狀態、TLS 階段與來源 URL 的非秘密摘要，
   確認是 DNS、連線、憑證、HTTP 錯誤、格式或解析失敗。
2. 若確認是 TLS，再檢查 Windows 根憑證、企業代理與 TLS 攔截憑證是否正確安裝，
   並用瀏覽器、PowerShell 與另一個受信任的 TLS 用戶端交叉檢查憑證鏈。
3. 尋找同一個 BSMI 官方資料集的替代 HTTPS 入口或 API。
4. 修正後保留新的下載證據與 hash；仍失敗就明確標示資料無法更新，不得把
   無候選快照描述成全部來源已更新成功。

### 5.5 法規涵蓋範圍與可追溯性

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

Windows 排程名稱為 `TaiwanImportCheck Official Data Weekly Update`，已建立、
啟用並設定為每週日 03:00（本機時區）檢查 18 個來源。2026-10-23 曾手動觸發
排程，`LastRunTime` 已有紀錄，`LastTaskResult` 為 `1`，下次執行時間為
2026-10-25 03:00。結果碼 `1` 不是程式崩潰，而是更新器在仍有 REVIEW／FAIL
來源時刻意回傳非零；報告已產生，但不可把所有來源描述成更新成功。

低維護成本建議：

- 法規與高風險來源每天做一次輕量 metadata、`ETag`、`Last-Modified` 或官方
  索引檢查；完整快照與內容核驗仍每週一次。
- 一般許可證、設備清單與案例資料每週檢查；有重大公告時手動立即執行
  `--download`。
- 相同 SHA-256 不新增版本；只有已提升的來源快照核驗為 `PASS` 才匯入並切換
  active version，候選快照通過只記為 `CANDIDATE_PASS`。
- REVIEW、FAIL、MISSING 一律保留舊版本，不自動覆蓋；失敗時保留錯誤摘要，
  不把 secrets 寫進報告。
- 下一階段增加版本差異報告、CCC/規定/機關跨表關聯檢查、生效與失效日期檢查、
  人工覆核閘門與失敗通知。

目前自動核驗涵蓋格式、欄位、重複欄名、非零筆數、空 key、唯一 key、完全
重複列、CCC 格式，以及法規名稱、官方 HTTPS `.gov.tw` 網址、最新異動日期與
可解析的生效日期；尚未證明法規內容正確、資料集完整、條文現行有效或商品
適用性。

## 7. 欄位放置位置

每個官方來源保留原始欄名，寫入同名 SQLite `TEXT` 欄位，另加：

- `record_id`：資料列主鍵
- `source_version_id`：來源版本
- `source_row_number`：原始列號

版本與來源資訊放 `official_dataset_versions`：

`version_id`、`dataset_id`、`sha256`、`source_url`、`metadata_modified`、
`update_frequency`、`fetched_at`、`validated_at`、`record_count`、
`validation_status`，以及主管機關、格式、授權、有效期間與核驗規則等來源
metadata。

目前 active pointer 與最近一次檢查結果放 `official_dataset_registry`：

`dataset_id`、`dataset_name`、`source_table`、`active_version_id`、
`active_sha256`、`last_check_at`、`last_status`、`last_error`、`metadata_modified`
與來源 manifest metadata。

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

容器內可執行的 unittest（必須帶入 `PYTHONPATH`）：

```powershell
docker exec -e PYTHONPATH=/opt/hermes -e P0_TEST_TEMP_ROOT=/tmp/tic-p0-tests tic-p0-hermes /opt/hermes/.venv/bin/python -m unittest discover -s tests -v
```

## 9. 目前限制與下一步

- Telegram 真實訊息 live E2E、圖片事件到 `image_path` 的實際傳遞，以及
  Docker 重啟後的自動恢復行為尚未驗證。
- 本輪已通過 Docker 建置、容器啟動、gateway 與容器內 smoke test；Bot Token
  也通過 `getMe` 驗證。目前日誌已顯示 Telegram polling 連線；但尚無 allowlist
  帳號的真實訊息事件，因此不能把 Telegram live E2E 或完整回覆鏈路標示為完成。
- Compose 已加入 gateway process healthcheck，但這只確認 Hermes gateway
  程序可回應，不代表 Telegram polling 已連線，也不代表平台事件已成功送達。
- NVIDIA NIM 測試模型基本回覆與 CLI plugin 呼叫已通過；先前曾遇到
  `ResourceExhausted: Worker local total request limit reached (64/16)`，
  這是暫時性的供應商限流，不代表模型能力已完整驗證。
- 18 個來源仍有 4 個沒有 active version：`92103`、`5996`、`7078` 的候選快照
  核驗為 `REVIEW`，`8876` 尚無候選快照且核驗為 `MISSING`。
- 正式資料庫尚未接入 Hermes，這是刻意的隔離，不是漏接。
- 下一步先處理 `92103`、`5996`、`7078` 的重複資料語意與來源品質，並補抓或
  診斷 `8876`；再補來源目錄、版本差異與人工覆核，完成後才評估讓正式資料庫
  進入查詢鏈。
- 目前 live 阻塞的最小處理順序：
  1. 由 allowlist 與非 allowlist 帳號送出真實私聊訊息，確認事件送達與拒絕
     行為；gateway process 健康與 Telegram polling 狀態需分開檢查。
  2. NVIDIA 模型基本回覆與 CLI 工具呼叫已通過；若再次遇到限流，等待冷卻或
     改用同供應商可用模型。接著確認 vision 能力，必要時暫以
     `image_description` 降級驗證。
  3. 由 allowlist 帳號完成文字、圖片、URL 三種 Telegram 實測，再檢查
     `image_path` 是否由圖片事件傳入。
  4. 利用已加入的 gateway healthcheck 驗證容器重啟後能恢復，並另外觀察
     Telegram polling 狀態。
  5. 最後才進行 `92103`、`5996`、`7078` 的重複資料診斷與 `8876` 的補抓診斷。
