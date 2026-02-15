from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from .brave_search import BraveSearchClient
from .config import Settings
from .lmstudio import LMStudioClient, llm_build_search_query
from .memory import MarkdownMemory


def _format_search_results(results: list[dict]) -> str:
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        desc = (r.get("description") or "").strip()
        lines.append(f"[{i}] {title}\n{url}\n{desc}")
    return "\n\n".join(lines)


async def run_bot(settings: Settings) -> None:
    lm = LMStudioClient(settings.lmstudio_base_url)
    brave = BraveSearchClient(settings.brave_api_key)

    memory = MarkdownMemory(
        memory_dir=settings.memory_dir,
        mode=settings.memory_mode,
        days=settings.memory_days,
    )
    recent: dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=settings.recent_turns * 2))

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return

        chat_id = update.effective_chat.id
        user_text = update.message.text.strip()

        recent[chat_id].append({"role": "user", "content": user_text})
        await memory.add_turn(chat_id=chat_id, role="user", content=user_text, ts=time.time())

        persisted = await memory.recent_turns(chat_id=chat_id, limit=settings.recent_turns * 2)
        recent[chat_id].clear()
        recent[chat_id].extend(persisted)

        qobj = await llm_build_search_query(lm, model=settings.lmstudio_chat_model, user_text=user_text)
        query = (qobj.get("query") or "").strip() or user_text

        try:
            search_results = await brave.web_search(
                query=query,
                country=settings.brave_country,
                lang=settings.brave_lang,
                count=5,
            )
        except Exception:
            search_results = []

        search_block = _format_search_results(search_results) if search_results else ""

        messages = [
            {
                "role": "system",
                "content": "You are a helpful Telegram chatbot. Use the provided web search results first and cite sources with [n]. If results are empty, say you couldn't find sources and then answer from general knowledge.",
            },
        ]

        if search_block:
            messages.append({"role": "system", "content": f"Web search results:\n{search_block}"})

        messages.extend(list(recent[chat_id]))

        assistant_text = await lm.chat_completions(
            model=settings.lmstudio_chat_model,
            messages=messages,
            temperature=0.3,
        )

        recent[chat_id].append({"role": "assistant", "content": assistant_text})
        await memory.add_turn(chat_id=chat_id, role="assistant", content=assistant_text, ts=time.time())

        await update.message.reply_text(assistant_text, disable_web_page_preview=True)

    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await asyncio.Future()
    finally:
        try:
            await app.updater.stop()
        except Exception:
            pass
        await brave.close()
        await lm.close()
        await app.stop()
        await app.shutdown()
