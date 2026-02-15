from __future__ import annotations

import asyncio

from dotenv import load_dotenv

from telegram_lmstudio_brave_bot.bot import run_bot
from telegram_lmstudio_brave_bot.config import load_settings


def main() -> None:
    load_dotenv()
    settings = load_settings()
    asyncio.run(run_bot(settings))


if __name__ == "__main__":
    main()
