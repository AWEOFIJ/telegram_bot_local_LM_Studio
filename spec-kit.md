# spec-kit.md — Telegram + LM Studio + Brave 搜尋對話機器人

## 1. 專案概述
本專案是一個 Telegram 對話機器人（Python 3.10+），透過 LM Studio（OpenAI-compatible API）呼叫本機大語言模型，並由 Bot 端主動使用 Brave Search 進行網路檢索，將搜尋結果與擷取的網頁內容提供給模型產生來源可追溯的回答。

專案核心目標：
- 提供在 Telegram 私聊/群組可用的對話式助理
- 面對「時效性/需要驗證」問題（例如新聞、天氣）優先採用 web_search 以提高正確性與可追溯性
- 維持長對話的可用性（短期記憶 + 長期偏好/摘要）
- 新增「AI Architect + Senior Engineer」工作流：由規格書 spec-kit.md 解析出模組與任務，並可做技術檢索與模組代碼生成

## 2. 使用者情境（User Stories）
### 2.1 一般對話
- 使用者在私聊直接輸入問題，Bot 依照問題決策是否需要 web_search
- 使用者可要求附來源連結，否則預設不直接貼 URL（僅用 [n] 引用）

### 2.2 群組互動
- 群組/超級群組中，Bot 只在訊息開頭 `@BotUsername` 被點名時回覆

### 2.3 新聞查詢（重視時效與來源）
- 使用者輸入「今天新聞 / 今天國際新聞 / 最近…新聞」
- Bot 執行 Brave Search，擷取部分網頁內容
- 模型輸出需：
  - 條列多則新聞
  - 每則以日期開頭（YYYY-MM-DD 或 [未提供日期]）
  - 每則附 [n] 引用
- 若模型不遵守（日期/引用/來源對齊），Bot 會重試或用 deterministic fallback 逐來源摘要產生穩定輸出

### 2.4 天氣查詢（重視即時性）
- 使用者詢問某地天氣
- Bot 強制 web_search 並擷取內容
- 模型不得拒答「無法提供即時資訊」；若來源沒有數字，必須明確說明來源未提供

### 2.5 規格驅動開發（AI Architect + Senior Engineer）
- 使用者 `/read_spec` 後上傳 `spec-kit.md`
- Bot 將規格轉成結構化 `spec_index.json`
- 使用者可：
  - `/spec_status` 查看模組/里程碑/待釐清問題
  - `/search_tech <keyword>` 進行技術檢索並保存
  - `/gen_module <module>` 讓模型依 spec + tech notes 生成模組代碼檔案

## 3. 非功能性需求（NFR）
### 3.1 正確性與可追溯性
- 對時效性問題優先 web_search
- 新聞輸出強制多來源引用與日期提示；必要時 deterministic fallback

### 3.2 安全性
- 生成檔案寫入必須防止 path traversal（拒絕 `..`）
- URL 擷取需避免 SSRF（拒絕 localhost/私網 IP）
- 不在 repo 中硬寫入任何 API Key

### 3.3 可靠性
- Brave Search 支援：
  - HTTP API
  - MCP stdio（可選）
- MCP 子程序需避免 deadlock（stderr drain）

### 3.4 可維運性
- 以 `.env` 控制模型、搜尋、抓取、記憶策略、新聞輸出數量
- debug logging 可輸出結構化 JSON 方便追查

## 4. 技術架構
### 4.1 主要元件
- Telegram Bot：python-telegram-bot (async)
- LLM：LM Studio OpenAI-compatible server
- Web Search：Brave Search（HTTP 或 MCP）
- Web Fetch：httpx 擷取 HTML 並轉純文字
- Memory：
  - 短期：Markdown append log
  - 長期：profile.json（語言偏好、連結偏好、預設地點、對話摘要、狀態）

### 4.2 資料流（簡化）
1) 接收訊息
2) 群組點名判斷
3) deterministic time/date 快捷處理（僅明確問日期/星期/年份）
4) LLM 產生 tool plan（web_search 或 none）
5) 若 web_search：Brave 搜尋 → 擷取 top N 頁面純文字 →（可選）逐來源摘要
6) 構建 messages → 呼叫 LM Studio → 後處理（語言、新聞日期/引用/來源對齊）
7) 回覆 Telegram + 寫入記憶

## 5. 指令規格
### 5.1 /memory
- 目的：顯示目前聊天室長期偏好（profile）

### 5.2 /forget
- 目的：清除目前聊天室長期偏好（profile），不刪除 markdown 對話

### 5.3 /read_spec
- 目的：進入等待上傳 spec-kit.md 的狀態
- 行為：提示使用者上傳文件

### 5.4 /spec_status
- 目的：顯示 `spec_index.json` 的摘要（模組 + milestones + open questions）

### 5.5 /search_tech <keyword>
- 目的：技術檢索
- 儲存：`memory/chat_<chat_id>/spec/tech/*.json`
- 回覆：前幾筆來源（title + url）

### 5.6 /gen_module <module_name>
- 目的：為指定模組生成代碼檔案（JSON: files[]）
- 讀取：
  - `memory/chat_<chat_id>/spec/spec_index.json`
  - 最近 tech notes（預設 3 份）
- 寫入：`memory/chat_<chat_id>/spec/generated/<module_slug>/...`
- 安全：寫入路徑必須是相對路徑且不可含 `..`

## 6. 設定（.env）
### 6.1 必填
- `TELEGRAM_BOT_TOKEN`
- `BRAVE_API_KEY`

### 6.2 LLM
- `LMSTUDIO_BASE_URL`（預設 `http://localhost:1234/v1`）
- `LMSTUDIO_CHAT_MODEL`

### 6.3 Brave Search
- `BRAVE_COUNTRY`（預設 `TW`）
- `BRAVE_LANG`（預設 `zh-hant`）
- `BRAVE_COUNT`（預設 `10`）

### 6.4 Web Fetch
- `FETCH_TOP_N`（預設 `10`）
- `FETCH_MAX_CHARS`（預設 `8000`）

### 6.5 Memory
- `MEMORY_DIR`（預設 `memory`）
- `MEMORY_MODE`（`daily`/`per_chat_daily`/`per_chat`）
- `MEMORY_DAYS`（預設 `1`）
- `RECENT_TURNS`（預設 `6`）

### 6.6 News controls
- `NEWS_FOLLOWUP_DEFAULT_COUNT`（預設 `5`）
- `NEWS_MAX_ITEMS`（預設 `8`）

### 6.7 （選用）MCP Brave
- `MCP_BRAVE_ENABLED=1`
- `MCP_BRAVE_COMMAND=npx`
- `MCP_BRAVE_ARGS=-y @modelcontextprotocol/server-brave-search`

## 7. 模組拆分（Modules）
### 7.1 telegram_bot_core
- Telegram handler 註冊
- 群組點名策略
- 訊息路由與回覆

### 7.2 llm_orchestration
- tool planning（web_search/none）
- prompt 組裝與角色交替保護
- 語言偏好（繁中/簡中/英文）重寫

### 7.3 web_search_and_fetch
- BraveSearchClient（HTTP / MCP）
- Web fetch 純文字擷取（SSRF 防護）

### 7.4 news_pipeline
- 今日/近期新聞查詢策略（query + 過去 24 小時）
- 日期提示、引用多樣性
- deterministic fallback（逐來源摘要）
- 來源連結區塊重建（引用對齊）

### 7.5 weather_pipeline
- 強制 web_search
- 回答格式化（概況/溫度/降雨/注意）
- 拒答檢測與重試

### 7.6 memory_and_profile
- markdown 記錄
- profile.json 長期偏好與狀態
- conversation_summary 生成與注入

### 7.7 spec_workflow
- spec-kit.md 上傳與解析
- spec_index.json 儲存與摘要
- tech notes 保存
- module code generation 與安全寫檔

## 8. 里程碑（Milestones）
- M1：Bot 基礎對話 + LM Studio 串接
- M2：Brave web_search + fetch + 引用
- M3：Memory（短期 + 長期偏好）
- M4：新聞/天氣強化（日期、引用一致、fallback）
- M5：群組點名模式
- M6：Spec workflow（/read_spec /spec_status /search_tech /gen_module）

## 9. Open Questions
- 新聞「今天」的定義：是否固定採「過去 24 小時」？是否需可配置（例如 12h/48h）
- 新聞來源品質過濾：是否要剔除分類頁（如 /news/world）並偏好單篇文章 URL
- 是否要為不同類別新聞（國際/科技/體育/財經）加入更精準的 query 模板與來源白名單
- codegen 產物是否要直接寫入 repo（目前寫入 memory），以及是否要提供 `/apply_patch` 類型的更新流程
