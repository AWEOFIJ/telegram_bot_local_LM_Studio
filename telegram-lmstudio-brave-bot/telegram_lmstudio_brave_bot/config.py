from __future__ import annotations

from pydantic import BaseModel


class Settings(BaseModel):
    telegram_bot_token: str
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_chat_model: str = "qwen/qwen2.5-coder-14b"
    brave_api_key: str
    brave_country: str = "TW"
    brave_lang: str = "zh-hant"
    memory_dir: str = "memory"
    memory_mode: str = "daily"
    memory_days: int = 1
    recent_turns: int = 6


def load_settings() -> Settings:
    import os

    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    brave_api_key = os.environ.get("BRAVE_API_KEY", "").strip()

    if not telegram_bot_token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
    if not brave_api_key:
        raise RuntimeError("Missing BRAVE_API_KEY")

    return Settings(
        telegram_bot_token=telegram_bot_token,
        lmstudio_base_url=os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").strip(),
        lmstudio_chat_model=os.environ.get("LMSTUDIO_CHAT_MODEL", "qwen/qwen2.5-coder-14b").strip(),
        brave_api_key=brave_api_key,
        brave_country=os.environ.get("BRAVE_COUNTRY", "TW").strip(),
        brave_lang=os.environ.get("BRAVE_LANG", "zh-hant").strip(),
        memory_dir=os.environ.get("MEMORY_DIR", "memory").strip(),
        memory_mode=os.environ.get("MEMORY_MODE", "daily").strip(),
        memory_days=int(os.environ.get("MEMORY_DAYS", "1")),
        recent_turns=int(os.environ.get("RECENT_TURNS", "6")),
    )
