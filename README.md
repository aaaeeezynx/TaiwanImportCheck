# 台灣進口商品法規與稅則查詢 MVP

## 專案目的

建立一個以 Telegram 為唯一使用入口、由 Hermes Agent 協調工具、以 Python
工具查詢官方資料庫的內部 MVP，協助使用者針對擬進口台灣的商品：

1.  提出有依據的 CCC 稅則號列候選與適用稅率。
2.  查核可能涉及的輸入規定、商品檢驗、許可或其他程序。
3.  查核進口及後續在台灣銷售可能涉及的限制。
4.  保存案件、證據來源、使用者選擇與後續實際結果，支援人工覆核及逐步改善。

本系統是決策輔助工具，不是海關、主管機關或正式法律意見的替代品。不得以虛假品名、錯誤用途或其他方式規避稅負、檢驗或法規。

## MVP 架構原則

-   Telegram Bot：唯一使用入口；不開發獨立網站前端。
-   Hermes
    Agent：理解問題、整理商品資訊、決定何時呼叫工具、整合結果與追問必要資訊。
-   Python
    工具：執行可重複、可測試的資料查詢、來源更新、案件保存等工作。
-   SQLite：保存案件與查詢所需的本地索引資料；案件資料庫與 Hermes 自身
    session/記憶資料分開。
-   Docker：開發與測試先在個人電腦執行；驗證通過後再部署至已確認可跑
    Docker 的 NAS。
-   官方資料優先：官方資料集、主管機關查詢頁、法規與公告；記錄來源、抓取時間、版本或生效日期。
-   先做單一 Agent，不先做多
    Agent、獨立前後端、複雜權限系統或完整報表網站。

## 建議閱讀順序

1.  `01_PM_MVP規格.md`
2.  `02_系統架構師.md`
3.  `03_Hermes_Agent工程師.md`
4.  `04_後端與資料工程師.md`
5.  `05_Telegram互動設計.md`
6.  `06_QA與資安.md`
7.  `07_待用戶確認事項.md`

## MVP 範圍

### 必須包含

-   Telegram 私聊入口及使用者 allowlist。
-   接收商品圖片、商品
    URL、出口國、數量；商品名稱、品牌型號、材質、規格、功能、用途、原產國與技術文件為可選補充資料。
-   只在缺少的資訊會實質影響分類或法規判定時，才追問使用者。
-   多個合理稅則候選的比較，不可只給單一看似確定的答案。
-   輸入規定、檢驗/認證及進口與銷售限制的查核結果。
-   證據來源、來源日期/資料版本、判定依據、信心與待確認事項。
-   保存案件及案件狀態，區分 AI
    建議、使用者選擇、實際通關結果、正式預先審核/歸列結果。
-   手動更新官方資料：先下載至
    staging、驗證、成功後切換；失敗保留舊版本並可回復。
-   URL 抓取基本 SSRF 防護與輸入驗證。
-   可測試、可備份、可復原的部署方式。

### 暫不包含

-   獨立 Web 前端或公開 API。
-   多 Agent 協作。
-   自動提交報關或代替使用者申請許可。
-   自動保證商品分類、免驗或可合法銷售。
-   複雜多層級角色權限、完整審核工作流。
-   大型分析儀表板、客製化 PDF 報告。
-   未經使用者確認的自動正式申報。

## 官方資料來源候選

-   財政部關務署進口稅則資料集：https://data.gov.tw/dataset/80871
-   關務署預先審核/稅則分類案例資料集：https://data.gov.tw/dataset/6713
-   經濟部國際貿易署 CCC
    貨品輸出入規定資料集：https://data.gov.tw/dataset/92103
-   標準檢驗局應施檢驗商品資料集：https://data.gov.tw/dataset/7078
-   法規資料相關資料集：https://data.gov.tw/dataset/18289
-   標準檢驗局官方網站：https://www.bsmi.gov.tw/SafetyGIP/wSite/np?ctNode=8799&mp=65
-   關務署稅則查詢：https://web.customs.gov.tw/singlehtml/1207?cntId=cus1_93361_1207
-   貿易署貨品輸出入規定查詢：https://fbfh.trade.gov.tw/fh/ap/queryCCCRegFormf.do

注意：資料集更新時間、涵蓋範圍與即時查詢頁可能不同。開發時須確認實際下載格式、授權、更新頻率、欄位及是否足以支援判定；不得假設資料集完整或即時。

## 開發驗收原則

-   先完成最小 PoC：Telegram 收到圖片與 URL → Hermes 呼叫一個 Python
    工具 → 工具查詢測試資料庫 → Telegram 回傳可讀結果。
-   PoC 通過後才擴展完整資料匯入、候選比較、案件保存及更新流程。
-   每個功能需有成功、失敗、缺資料、資料過期及不確定情境的測試。
-   不以「回答看起來合理」作為唯一驗收；需檢查來源、可追溯性、工具呼叫、資料版本與安全性。

## P0 技術驗證快速入口

目前只實作 P0：授權 Telegram 使用者可傳送商品文字、圖片與 URL，由單一
Hermes Agent 呼叫本地 Python 工具 `p0_import_lookup`，查詢測試用 SQLite
假資料後回覆 Telegram。所有 CCC、稅率與檢驗內容均為虛構測試值，不是正式
稅則、法規或免驗建議。

完整環境檢查、Windows 本機隔離啟動、Docker Compose、Telegram 測試步驟、
allowlist 行為與目前限制，請見
[`P0_驗證說明.md`](P0_驗證說明.md)。最小本機測試如下：

```powershell
C:\Users\aaaee\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe scripts\init_mock_db.py
C:\Users\aaaee\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -m unittest discover -s tests -v
C:\Users\aaaee\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe -m compileall tools scripts tests .hermes
```

官方資料來源盤點、正式資料庫欄位、更新/核驗/匯入與 Windows 每週排程，請見
[`08_資料庫欄位與資料維護.md`](08_資料庫欄位與資料維護.md)。官方 SQLite 與 P0
假資料庫分開；官方資料庫不會被 P0 Hermes 查詢。

目前已完成範圍、18 個來源的最新核驗狀態、資料缺口、更新頻率、Git 與
Telegram/Docker 限制，請見
[`docs/PROJECT_STATUS.md`](docs/PROJECT_STATUS.md)。
