from __future__ import annotations

from pydantic import BaseModel


class Settings(BaseModel):
    telegram_bot_token: str
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_chat_model: str = "qwen/qwen2.5-coder-14b"
    lmstudio_planner_model: str = "qwen/qwen2.5-coder-14b"
    brave_api_key: str
    brave_country: str = "TW"
    brave_lang: str = "zh-hant"
    brave_count: int = 10
    debug: bool = False
    debug_dir: str = "debug"
    debug_max_str: int = 8000
    debug_max_list: int = 50
    mcp_brave_enabled: bool = False
    mcp_brave_command: str = "npx"
    mcp_brave_args: list[str] = ["-y", "@modelcontextprotocol/server-brave-search"]
    fetch_top_n: int = 10
    fetch_max_chars: int = 8000
    memory_dir: str = "memory"
    memory_mode: str = "per_chat_daily"
    memory_days: int = 1
    recent_turns: int = 6
    news_followup_default_count: int = 5
    news_max_items: int = 8


def load_settings() -> Settings:
    import os

    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    brave_api_key = os.environ.get("BRAVE_API_KEY", "").strip()

    mcp_brave_enabled = os.environ.get("MCP_BRAVE_ENABLED", "").strip() in {"1", "true", "True", "yes", "YES"}
    mcp_brave_command = os.environ.get("MCP_BRAVE_COMMAND", "npx").strip() or "npx"
    mcp_brave_args_raw = os.environ.get("MCP_BRAVE_ARGS", "-y @modelcontextprotocol/server-brave-search").strip()
    mcp_brave_args = [a for a in mcp_brave_args_raw.split() if a.strip()]

    if not telegram_bot_token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not brave_api_key:
        raise RuntimeError("Missing BRAVE_API_KEY")
 
    return Settings(
        telegram_bot_token=telegram_bot_token,
        lmstudio_base_url=os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").strip(),
        lmstudio_chat_model=os.environ.get("LMSTUDIO_CHAT_MODEL", "qwen/qwen2.5-coder-14b").strip(),
        lmstudio_planner_model=(
            os.environ.get("LMSTUDIO_PLANNER_MODEL", "").strip()
            or os.environ.get("LMSTUDIO_CHAT_MODEL", "qwen/qwen2.5-coder-14b").strip()
        ),
        brave_api_key=brave_api_key,
        brave_country=os.environ.get("BRAVE_COUNTRY", "TW").strip(),
        brave_lang=os.environ.get("BRAVE_LANG", "zh-hant").strip(),
        brave_count=int(os.environ.get("BRAVE_COUNT", "10")),
        debug=os.environ.get("DEBUG", "").strip() in {"1", "true", "True", "yes", "YES"},
        debug_dir=os.environ.get("DEBUG_DIR", "debug").strip() or "debug",
        debug_max_str=int(os.environ.get("DEBUG_MAX_STR", "8000")),
        debug_max_list=int(os.environ.get("DEBUG_MAX_LIST", "50")),
        mcp_brave_enabled=mcp_brave_enabled,
        mcp_brave_command=mcp_brave_command,
        mcp_brave_args=mcp_brave_args,
        fetch_top_n=int(os.environ.get("FETCH_TOP_N", "10")),
        fetch_max_chars=int(os.environ.get("FETCH_MAX_CHARS", "8000")),
        memory_dir=os.environ.get("MEMORY_DIR", "memory").strip(),
        memory_mode=os.environ.get("MEMORY_MODE", "per_chat_daily").strip(),
        memory_days=int(os.environ.get("MEMORY_DAYS", "1")),
        recent_turns=int(os.environ.get("RECENT_TURNS", "6")),
        news_followup_default_count=int(os.environ.get("NEWS_FOLLOWUP_DEFAULT_COUNT", "5")),
        news_max_items=int(os.environ.get("NEWS_MAX_ITEMS", "8")),
    )
