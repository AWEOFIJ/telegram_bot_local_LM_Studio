# Telegram + LM Studio + Brave 搜尋對話機器人（Python）

## 功能
- Telegram 私聊/群組文字對話
- 透過 LM Studio（OpenAI-compatible）呼叫本機模型
- bot 端聯網：bot 呼叫 Brave Search API 取得資料，再交給 LM Studio 彙整答案
- 對話記憶（短期）寫入 `memory/chat_<chat_id>/YYYY-MM-DD.md`（append）
- 長期偏好記憶（profile）寫入 `memory/chat_<chat_id>/profile.json`，並注入到 LM Studio prompt

## 需求
- Windows / macOS / Linux
- Python 3.10+
- LM Studio 已啟動 OpenAI Compatible Server

## 安裝
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 設定
1. 複製環境變數檔
```powershell
copy .env.example .env
```

2. 編輯 `.env`
- `TELEGRAM_BOT_TOKEN`：你的 Bot token（請勿提交到 git）
- `LMSTUDIO_BASE_URL`：通常是 `http://localhost:1234/v1`
- `LMSTUDIO_CHAT_MODEL`：`qwen/qwen2.5-coder-14b`（或你在 LM Studio 看到的 model id）
- `BRAVE_API_KEY`：你的 Brave API key
- `BRAVE_COUNTRY`：搜尋地區（預設 `TW`）
- `BRAVE_LANG`：搜尋語系（預設 `zh-hant`）
- `BRAVE_COUNT`：每次 web search 取回的結果數（預設 `10`）
- `FETCH_TOP_N`：從搜尋結果中最多抓取幾個網頁做全文擷取（預設 `10`）
- `FETCH_MAX_CHARS`：每個網頁最多擷取的純文字字數上限（預設 `8000`）
- `MEMORY_DIR`：記憶檔資料夾（預設 `memory`）
- `MEMORY_MODE`：`daily` / `per_chat_daily` / `per_chat`
- `MEMORY_DAYS`：跨天讀取天數（僅 `daily`、`per_chat_daily` 生效）
- `RECENT_TURNS`：讀取最後 N 則訊息作為上下文

### （選用）用 MCP 呼叫 Brave Search
此專案支援讓 bot 透過 MCP（stdio）啟動並呼叫 `@modelcontextprotocol/server-brave-search`。
如需啟用，請在 `.env` 設定：
- `MCP_BRAVE_ENABLED=1`
- `MCP_BRAVE_COMMAND=npx`
- `MCP_BRAVE_ARGS=-y @modelcontextprotocol/server-brave-search`

## 執行
```powershell
python main.py
```

## 記憶資料
- 會把對話以 append 方式寫入 markdown 檔（可由 `.env` 控制）
  - 每行格式：`- [HH:MM:SS] chat:<chat_id> (user|assistant) <content>`
- `MEMORY_MODE=per_chat_daily`：寫到 `memory/chat_<chat_id>/YYYY-MM-DD.md`（推薦，依 chat 分資料夾）
- `MEMORY_MODE=daily`：此專案同樣會寫到 `memory/chat_<chat_id>/YYYY-MM-DD.md`（避免不同 chat 混在同一檔）
- `MEMORY_MODE=per_chat`：寫到 `memory/chat_<chat_id>.md`
- bot 會讀取（可跨天）最後 N 則訊息作為上下文
- 另有長期偏好檔：`memory/chat_<chat_id>/profile.json`
  - 目前會記錄：語言偏好、預設天氣地區、是否偏好附來源連結
  - 這些偏好會在回覆前注入 system prompt，改善個人化體驗

## 指令
- `/memory`：查看目前聊天室的長期偏好記憶（profile）
- `/forget`：清除目前聊天室的長期偏好記憶（profile）

## 常見問題
### 1) 為什麼不需要 `LMSTUDIO_EMBED_MODEL`？
目前使用文字紀錄檔保存與讀取最近對話（非向量檢索），因此不需要 embeddings。

### 2) 群組可以用嗎？
可以。把 bot 拉進群組並給它讀取訊息權限即可。此程式目前會處理所有非指令文字訊息。

### 3) `/memory` 和 `/forget` 會不會刪掉聊天紀錄？
不會。這兩個指令只操作 `profile.json`（長期偏好），不會刪除 markdown 對話紀錄。

### 4) Brave 搜尋結果如何引用？
  若 bot 判斷需要搜尋，會把搜尋結果以 `[n]` 列表提供給模型，並要求回答時用 `[n]` 引用來源；預設不直接貼 URL（除非使用者明確要求連結）。

### 5) 什麼情況下會傾向使用 `web_search`？
此專案目前採「偏準確」策略：只要涉及時效性、需要驗證/來源、問題不夠明確或模型不夠有把握，就會傾向先搜尋再回答。
