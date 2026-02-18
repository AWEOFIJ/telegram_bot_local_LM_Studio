from __future__ import annotations

import asyncio
import ipaddress
import time
import logging
import os
from html.parser import HTMLParser
from collections import defaultdict, deque
from urllib.parse import urlparse

import httpx
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from .brave_search import BraveSearchClient
from .config import Settings
from .lmstudio import LMStudioClient, llm_plan_tools
from .memory import MarkdownMemory
from .debug_logger import debug_logger_from_env


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            return
        t = " ".join(data.split())
        if t:
            self._out.append(t)

    def text(self) -> str:
        return "\n".join(self._out)


def _is_public_http_url(url: str) -> bool:
    try:
        u = urlparse(url)
        if u.scheme not in {"http", "https"}:
            return False
        host = (u.hostname or "").strip().lower()
        if not host:
            return False
        if host in {"localhost"}:
            return False

        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            return True

        return True
    except Exception:
        return False


async def _fetch_page_text(
    client: httpx.AsyncClient,
    *,
    url: str,
    max_chars: int,
) -> str:
    if not _is_public_http_url(url):
        return ""

    r = await client.get(
        url,
        follow_redirects=True,
        headers={"User-Agent": "telegram-bot/1.0"},
    )
    r.raise_for_status()

    html = r.text
    parser = _TextExtractor()
    parser.feed(html)
    text = parser.text()
    if len(text) > max_chars:
        return text[:max_chars]
    return text


def _normalize_location(loc: str) -> str:
    t = loc.strip()
    for suffix in ("市", "縣"):
        if t.endswith(suffix):
            return t[: -len(suffix)]
    return t


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").strip().lower()
    except Exception:
        return ""


def _wants_links(user_text: str) -> bool:
    t = user_text.lower()
    keywords = [
        "連結",
        "链接",
        "網址",
        "网址",
        "url",
        "link",
        "來源",
        "来源",
        "source",
    ]
    return any(k in t for k in keywords)


def _is_weather_question(user_text: str) -> bool:
    t = user_text.lower()
    keywords = [
        "天氣",
        "天气",
        "氣象",
        "氣温",
        "气温",
        "溫度",
        "温度",
        "降雨",
        "下雨",
        "雷雨",
        "颱風",
        "台风",
        "降雨機率",
        "降雨概率",
    ]
    return any(k in t for k in keywords)


def _extract_tw_location(user_text: str) -> str:
    locations = [
        "台北",
        "臺北",
        "新北",
        "基隆",
        "桃園",
        "新竹",
        "苗栗",
        "台中",
        "臺中",
        "彰化",
        "南投",
        "雲林",
        "嘉義",
        "台南",
        "臺南",
        "高雄",
        "屏東",
        "宜蘭",
        "花蓮",
        "台東",
        "臺東",
        "澎湖",
        "金門",
        "馬祖",
    ]
    for loc in locations:
        if loc in user_text:
            return loc
    return ""


def _should_force_web_search(user_text: str) -> bool:
    t = user_text.lower()
    keywords = [
        "天氣",
        "天气",
        "氣象",
        "温度",
        "溫度",
        "下雨",
        "降雨",
        "雷雨",
        "颱風",
        "台风",
        "即時",
        "实时",
        "今天",
        "現在",
        "目前",
        "最新",
    ]
    return any(k in t for k in keywords)


async def _summarize_source(
    lm: LMStudioClient,
    *,
    model: str,
    user_text: str,
    source_index: int,
    title: str,
    domain: str,
    content: str,
) -> str:
    return await lm.chat_completions(
        model=model,
        temperature=0.2,
        max_tokens=450,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are summarizing a single web source for a Telegram bot. "
                    "Return concise Traditional Chinese bullet points that are directly relevant to the user's question. "
                    "Do NOT include URLs. Do NOT mention you cannot browse. "
                    "If the source does not contain relevant information, say so briefly. "
                    "End each bullet with the citation marker like [n] where n is the source index."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"User question: {user_text}\n\n"
                    f"Source [{source_index}]\n"
                    f"Title: {title}\n"
                    f"Domain: {domain}\n"
                    f"Content:\n{content}"
                ),
            },
        ],
    )


def _format_search_results(results: list[dict]) -> str:
    lines: list[str] = []
    for i, r in enumerate(results, start=1):
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        desc = (r.get("description") or "").strip()
        lines.append(f"[{i}] {title}\nDomain: {_domain(url)}\nSnippet: {desc}")
    return "\n\n".join(lines)


def _format_fetched_pages(pages: list[dict]) -> str:
    lines: list[str] = []
    for i, p in enumerate(pages, start=1):
        title = (p.get("title") or "").strip()
        url = (p.get("url") or "").strip()
        text = (p.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{i}] {title}\nDomain: {_domain(url)}\nContent:\n{text}")
    return "\n\n".join(lines)


async def run_bot(settings: Settings) -> None:
    dbg = debug_logger_from_env()

    lm = LMStudioClient(settings.lmstudio_base_url, debug_logger=dbg)
    brave = BraveSearchClient(
        settings.brave_api_key,
        debug_logger=dbg,
        mcp_enabled=getattr(settings, "mcp_brave_enabled", False),
        mcp_command=getattr(settings, "mcp_brave_command", "npx"),
        mcp_args=getattr(settings, "mcp_brave_args", None),
    )
    fetch_client = httpx.AsyncClient(timeout=12.0)

    import os

    debug = os.environ.get("DEBUG", "").strip() in {"1", "true", "True", "yes", "YES"}

    fetch_top_n = int(getattr(settings, "fetch_top_n", 10) or 10)
    fetch_max_chars = int(getattr(settings, "fetch_max_chars", 8000) or 8000)

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

        request_id = f"chat{chat_id}_{int(time.time()*1000)}"
        if dbg.enabled:
            dbg.write_json(
                request_id=request_id,
                name="telegram_in",
                data={
                    "chat_id": chat_id,
                    "message_id": update.message.message_id,
                    "user_text": user_text,
                },
            )

        recent[chat_id].append({"role": "user", "content": user_text})
        await memory.add_turn(chat_id=chat_id, role="user", content=user_text, ts=time.time())

        persisted = await memory.recent_turns(chat_id=chat_id, limit=settings.recent_turns * 2)
        recent[chat_id].clear()
        recent[chat_id].extend(persisted)

        plan = await llm_plan_tools(lm, model=settings.lmstudio_chat_model, user_text=user_text)
        tool = (plan.get("tool") or "none").strip()
        query = (plan.get("query") or "").strip()

        if dbg.enabled:
            dbg.write_json(
                request_id=request_id,
                name="plan",
                data={"tool": tool, "query": query},
            )

        search_results = []
        if tool == "web_search":
            is_weather = _is_weather_question(user_text)
            loc = _extract_tw_location(user_text) if is_weather else ""
            if is_weather and not loc:
                assistant_text = "你想查哪個城市/地區的天氣？例如：台北 / 新北 / 台中 / 高雄。"
                if dbg.enabled:
                    dbg.write_json(
                        request_id=request_id,
                        name="telegram_out",
                        data={"chat_id": chat_id, "assistant_text": assistant_text},
                    )
                recent[chat_id].append({"role": "assistant", "content": assistant_text})
                await memory.add_turn(chat_id=chat_id, role="assistant", content=assistant_text, ts=time.time())
                await update.message.reply_text(assistant_text, disable_web_page_preview=True)
                return

            if is_weather and loc and not query:
                nloc = _normalize_location(loc)
                query = f"{nloc} 今天 天氣預報 降雨機率 最高溫 最低溫 體感 風速 中央氣象署"

            q = query or user_text
            if debug:
                print(f"[debug] web_search query={q!r}")
            try:
                search_results = await brave.web_search(
                    query=q,
                    country=settings.brave_country,
                    lang=settings.brave_lang,
                    count=int(getattr(settings, "brave_count", 10) or 10),
                    request_id=request_id,
                )
            except Exception:
                search_results = []

        search_block = _format_search_results(search_results) if search_results else ""

        fetched_pages: list[dict] = []
        if tool == "web_search" and search_results:
            for item in search_results[:fetch_top_n]:
                url = (item.get("url") or "").strip()
                if not url:
                    continue
                try:
                    text = await _fetch_page_text(fetch_client, url=url, max_chars=fetch_max_chars)
                except Exception:
                    text = ""
                fetched_pages.append({"title": item.get("title") or "", "url": url, "text": text})

        if debug and tool == "web_search":
            domains = [_domain((p.get("url") or "").strip()) for p in fetched_pages]
            sizes = [len((p.get("text") or "").strip()) for p in fetched_pages]
            print(f"[debug] results={len(search_results)} fetched={len(fetched_pages)} domains={domains} sizes={sizes}")

        if tool == "web_search" and _is_weather_question(user_text):
            if not any((p.get("text") or "").strip() for p in fetched_pages):
                await update.message.reply_text(
                    "我目前抓不到可用的即時天氣內容（可能被網站阻擋或來源不穩）。請再提供城市/地區，或改問：『台北今天降雨機率』。",
                    disable_web_page_preview=True,
                )
                return

        fetched_block = _format_fetched_pages(fetched_pages)

        source_summaries: list[str] = []
        if tool == "web_search" and fetched_pages:
            for i, p in enumerate(fetched_pages, start=1):
                text = (p.get("text") or "").strip()
                if not text:
                    continue
                title = (p.get("title") or "").strip()
                url = (p.get("url") or "").strip()
                domain = _domain(url)
                try:
                    s = await _summarize_source(
                        lm,
                        model=settings.lmstudio_chat_model,
                        user_text=user_text,
                        source_index=i,
                        title=title,
                        domain=domain,
                        content=text,
                    )
                except Exception:
                    s = ""
                if s.strip():
                    source_summaries.append(f"[{i}] {title} ({domain})\n{s.strip()}")

        summaries_block = "\n\n".join(source_summaries)

        is_news = any(
            k in user_text.lower()
            for k in [
                "新聞",
                "新闻",
                "news",
                "headline",
                "頭條",
                "头条",
                "兩岸",
                "两岸",
                "國際",
                "国际",
                "財經",
                "财经",
                "金融",
                "finance",
            ]
        )

        messages = [
            {
                "role": "system",
                "content": "You are a helpful Telegram chatbot.",
            },
        ]

        if tool == "web_search":
            if search_block:
                messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Web search results are provided. Use them first.\n"
                            "Do NOT paste URLs in the final answer. Use [n] citations only.\n"
                            "Only include URLs if the user explicitly asks for links/sources.\n"
                            f"Web search results:\n{search_block}"
                        ),
                    }
                )

                if is_news:
                    n = min(10, len(search_results) if search_results else 10)
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "The user is asking for news. "
                                f"You MUST list at least {n} distinct news items if sources are available. "
                                "Return a bullet list. Each bullet must contain: a short headline, a 1-2 sentence summary, and a citation like [n]. "
                                "Each bullet MUST cite a different source index when possible. "
                                "Do NOT write generic summaries. Do NOT merge multiple news into one bullet."
                            ),
                        }
                    )

                if _is_weather_question(user_text):
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "This is a weather / real-time info question. You MUST use the provided web content to answer. "
                                "Do NOT say you cannot provide real-time info. "
                                "If the provided sources do not include specific numbers, clearly say 'sources do not contain the detailed forecast numbers' and ask the user for a more specific time/window (e.g., morning/afternoon) or district. "
                                "Answer in Traditional Chinese with a compact structure: 概況 / 溫度範圍 / 降雨機率 / 注意事項. "
                                "Cite sources with [n]. Do NOT include URLs."
                            ),
                        }
                    )

                if summaries_block:
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Per-source summaries are provided. Prefer using these summaries to answer. "
                                "Cite sources with [n]. Do NOT include URLs.\n"
                                f"Source summaries:\n{summaries_block}"
                            ),
                        }
                    )
                elif fetched_block:
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Fetched page contents are provided. Prefer using these contents to answer. "
                                "Cite sources with [n]. Do NOT include URLs.\n"
                                f"Fetched contents:\n{fetched_block}"
                            ),
                        }
                    )
            else:
                messages.append(
                    {
                        "role": "system",
                        "content": "Web search was requested, but results are empty. Say you couldn't find sources, then answer from general knowledge.",
                    }
                )

        messages.extend(list(recent[chat_id]))

        if dbg.enabled:
            dbg.write_json(
                request_id=request_id,
                name="final_messages",
                data={"messages": messages},
            )

        assistant_text = await lm.chat_completions(
            model=settings.lmstudio_chat_model,
            messages=messages,
            temperature=0.3,
            max_tokens=900 if (tool == "web_search" and is_news) else None,
            request_id=request_id,
        )

        if tool == "web_search" and is_news and search_results:
            urls = [(item.get("url") or "").strip() for item in search_results]
            urls = [u for u in urls if u]
            if urls:
                n = min(10, len(urls))
                link_lines = [f"[{i}] {urls[i - 1]}" for i in range(1, n + 1)]
                assistant_text = assistant_text.rstrip() + "\n\n" + "來源連結：\n" + "\n".join(link_lines)

        if tool == "web_search" and _wants_links(user_text) and search_results:
            urls = [
                (item.get("url") or "").strip()
                for item in search_results
                if (item.get("url") or "").strip()
            ]
            if urls:
                assistant_text = assistant_text.rstrip() + "\n\n" + "\n".join(urls[:5])

        recent[chat_id].append({"role": "assistant", "content": assistant_text})
        await memory.add_turn(chat_id=chat_id, role="assistant", content=assistant_text, ts=time.time())

        if dbg.enabled:
            dbg.write_json(
                request_id=request_id,
                name="telegram_out",
                data={"chat_id": chat_id, "assistant_text": assistant_text},
            )

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
        await fetch_client.aclose()
        await lm.close()
        await app.stop()
        await app.shutdown()
