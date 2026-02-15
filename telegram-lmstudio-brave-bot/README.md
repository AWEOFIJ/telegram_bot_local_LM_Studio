# Telegram + LM Studio + Brave 搜尋對話機器人（Python）

## 功能
- Telegram 私聊/群組文字對話
- 透過 LM Studio（OpenAI-compatible）呼叫本機模型
- 兩段式聯網：先由模型產生搜尋 query，再用 Brave Search API 搜尋，最後由模型彙整答案
- 對話記憶寫入 `memory/YYYY-MM-DD.md`（每日一個檔案）

## 需求
- Windows / macOS / Linux
- Python 3.10+
- LM Studio 已啟動 OpenAI Compatible Server
- Brave Search API Key

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
- `MEMORY_DIR`：記憶檔資料夾（預設 `memory`）
- `MEMORY_MODE`：`daily` / `per_chat_daily` / `per_chat`
- `MEMORY_DAYS`：跨天讀取天數（僅 `daily`、`per_chat_daily` 生效）
- `RECENT_TURNS`：讀取最後 N 則訊息作為上下文

## 執行
```powershell
python main.py
```

## 記憶資料
- 會把對話以 append 方式寫入 markdown 檔（可由 `.env` 控制）
  - 每行格式：`- [HH:MM:SS] chat:<chat_id> (user|assistant) <content>`
 - `MEMORY_MODE=daily`：寫到 `memory/YYYY-MM-DD.md`
 - `MEMORY_MODE=per_chat_daily`：寫到 `memory/chat_<chat_id>/YYYY-MM-DD.md`
 - `MEMORY_MODE=per_chat`：寫到 `memory/chat_<chat_id>.md`
 - bot 會讀取（可跨天）最後 N 則訊息作為上下文

## 常見問題
### 1) 為什麼不需要 `LMSTUDIO_EMBED_MODEL`？
目前使用文字紀錄檔保存與讀取最近對話（非向量檢索），因此不需要 embeddings。

### 2) 群組可以用嗎？
可以。把 bot 拉進群組並給它讀取訊息權限即可。此程式目前會處理所有非指令文字訊息。

### 3) Brave 搜尋結果如何引用？
程式會把搜尋結果以 `[n]` 列表提供給模型，並要求回答時用 `[n]` 引用來源。回答流程為：
 1) LM Studio 先把使用者問題改寫成搜尋 query
 2) Brave 搜尋
 3) LM Studio 根據搜尋結果整理輸出（若無結果會先說明找不到來源）
