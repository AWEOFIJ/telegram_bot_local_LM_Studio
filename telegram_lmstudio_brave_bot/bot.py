from __future__ import annotations

import asyncio
import ipaddress
import time
import logging
import os
import re
from html.parser import HTMLParser
from collections import defaultdict, deque
from urllib.parse import urlparse

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

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


_TW_LOCATIONS = {
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
}


def _is_tw_location(loc: str) -> bool:
    return _normalize_location(loc).strip() in {_normalize_location(x) for x in _TW_LOCATIONS}


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


def _is_followup_continue(user_text: str) -> bool:
    t = (user_text or "").strip().lower()
    if not t:
        return False
    exact = {
        "繼續",
        "继续",
        "更多",
        "再來",
        "再給",
        "再多",
        "再多列",
        "再多幾條",
        "再多幾則",
        "再多一點",
        "再多一些",
        "more",
        "continue",
    }
    if t in exact:
        return True
    # e.g. "再多列 5 條" / "再列3則" / "更多 10"
    if re.search(r"(再|再多|更多|繼續|继续).{0,6}(\d{1,2})", t):
        return True
    return False


def _extract_followup_count(user_text: str, default: int = 5, *, max_n: int = 10) -> int:
    t = (user_text or "")
    m = re.search(r"(\d{1,2})", t)
    if not m:
        return max(1, min(default, max_n))
    try:
        n = int(m.group(1))
    except Exception:
        n = default
    return max(1, min(n, max_n))


def _strip_leading_bot_mention(user_text: str, bot_username: str) -> tuple[str, bool]:
    t = (user_text or "").strip()
    u = (bot_username or "").strip().lstrip("@").lower()
    if not t or not u:
        return t, False

    # Accept: "@MyBot ..." or "@MyBot: ..." or "@MyBot，..."
    m = re.match(r"^@([A-Za-z0-9_]+)\b\s*([:：,，\-—]*)\s*(.*)$", t)
    if not m:
        return t, False
    mentioned = (m.group(1) or "").lower() == u
    if not mentioned:
        return t, False
    rest = (m.group(3) or "").strip()
    return rest, True


async def _summarize_conversation_for_profile(
    lm: LMStudioClient,
    *,
    model: str,
    existing_summary: str,
    turns: list[dict],
) -> str:
    # Keep it short, stable, and suitable for system prompt injection.
    content_lines: list[str] = []
    for m in turns:
        role = str(m.get("role") or "")
        txt = str(m.get("content") or "").strip()
        if not role or not txt:
            continue
        txt = re.sub(r"\s+", " ", txt)
        content_lines.append(f"{role}: {txt}")
    convo = "\n".join(content_lines)

    prompt = (
        "你要為 Telegram 對話生成『長期上下文摘要』，用於之後每次回答的 system prompt。\n"
        "要求：\n"
        "1) 使用繁體中文\n"
        "2) 只保留可穩定延續對話的資訊：使用者偏好、目前主題、已確認的背景/定義\n"
        "3) 不要包含 URL，不要列出逐條新聞，不要複製原文\n"
        "4) 最多 8 行，每行一句\n"
    )
    if existing_summary.strip():
        prompt += "\n已有摘要（可更新/融合，不要變長）：\n" + existing_summary.strip() + "\n"

    out = await lm.chat_completions(
        model=model,
        temperature=0.1,
        max_tokens=300,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": "以下是最近對話片段：\n" + convo},
        ],
    )
    return (out or "").strip()


def _infer_profile_updates(user_text: str) -> dict[str, object]:
    t = user_text.strip()
    tl = t.lower()
    updates: dict[str, object] = {}

    if re.search(r"(以後|之後|請|麻煩).*(繁體|繁中)", t):
        updates["preferred_language"] = "zh-Hant"
    elif re.search(r"(以後|之後|請|麻煩).*(簡體|简体|簡中|简中)", t):
        updates["preferred_language"] = "zh-Hans"
    elif re.search(r"(以後|之後|請|麻煩).*(英文|english)", tl):
        updates["preferred_language"] = "en"

    if re.search(r"(以後|之後|都).*(附|給).*(連結|链接|網址|网址|來源|来源|link|url)", tl):
        updates["prefer_links"] = True
    elif re.search(r"(以後|之後|都).*(不要|別|不必).*(連結|链接|網址|网址|來源|来源|link|url)", tl):
        updates["prefer_links"] = False

    return updates


def _profile_to_system_prompt(profile: dict[str, object]) -> str:
    if not profile:
        return ""

    lines: list[str] = []

    lang = str(profile.get("preferred_language") or "").strip()
    if lang == "zh-Hant":
        lines.append("User preference: MUST reply in Traditional Chinese unless user explicitly asks otherwise.")
    elif lang == "zh-Hans":
        lines.append("User preference: MUST reply in Simplified Chinese unless user explicitly asks otherwise.")
    elif lang == "en":
        lines.append("User preference: MUST reply in English unless user explicitly asks otherwise.")

    default_loc = str(profile.get("default_weather_location") or "").strip()
    if default_loc:
        lines.append(f"User default weather location: {default_loc}.")

    prefer_links = profile.get("prefer_links")
    if isinstance(prefer_links, bool):
        if prefer_links:
            lines.append("User preference: include source links when possible.")
        else:
            lines.append("User preference: avoid source links unless explicitly requested.")

    convo_summary = str(profile.get("conversation_summary") or "").strip()
    if convo_summary:
        lines.append("Conversation summary (for context, do not repeat verbatim unless asked):")
        lines.append(convo_summary)

    if not lines:
        return ""
    return "Long-term user preferences:\n- " + "\n- ".join(lines)


def _format_profile_text(profile: dict[str, object]) -> str:
    if not profile:
        return "目前沒有已儲存的長期記憶偏好。"

    lines: list[str] = []

    lang = str(profile.get("preferred_language") or "").strip()
    if lang == "zh-Hant":
        lines.append("- 語言偏好：繁體中文")
    elif lang == "zh-Hans":
        lines.append("- 語言偏好：簡體中文")
    elif lang == "en":
        lines.append("- 語言偏好：英文")

    default_loc = str(profile.get("default_weather_location") or "").strip()
    if default_loc:
        lines.append(f"- 預設天氣地區：{default_loc}")

    prefer_links = profile.get("prefer_links")
    if isinstance(prefer_links, bool):
        lines.append(f"- 連結偏好：{'會附上來源連結' if prefer_links else '不主動附上來源連結'}")

    if not lines:
        return "目前沒有已儲存的長期記憶偏好。"
    return "目前的長期記憶偏好：\n" + "\n".join(lines)


_SIMPLIFIED_ONLY_MARKERS = {
    "这",
    "个",
    "为",
    "对",
    "后",
    "们",
    "来",
    "时",
    "于",
    "国",
    "军",
    "关",
    "里",
    "实",
    "预",
    "报",
    "无",
    "并",
}


def _looks_simplified_chinese(text: str) -> bool:
    if not text:
        return False
    hit = sum(text.count(ch) for ch in _SIMPLIFIED_ONLY_MARKERS)
    return hit >= 2


def _is_recent_news_query(user_text: str) -> bool:
    t = user_text.lower()
    recent_markers = ["最近", "近日", "最新", "近幾日", "近几日", "這幾天", "这几天", "24小時", "24小时"]
    news_markers = [
        "新聞",
        "新闻",
        "news",
        "軍演",
        "军演",
        "海域",
        "時事",
        "时事",
        "快訊",
        "快讯",
        "財經",
        "财经",
        "金融",
        "股市",
        "指數",
        "指数",
        "道瓊",
        "道琼",
        "dow jones",
        "nasdaq",
        "s&p",
        "sp500",
    ]
    return any(k in t for k in recent_markers) and any(k in t for k in news_markers)


def _is_market_index_query(user_text: str) -> bool:
    t = user_text.lower()
    keywords = [
        "道瓊",
        "道琼",
        "dow jones",
        "djia",
        "納斯達克",
        "纳斯达克",
        "nasdaq",
        "s&p",
        "sp500",
        "指數",
        "指数",
        "股市",
        "美股",
    ]
    return any(k in t for k in keywords)


def _extract_years(text: str) -> list[int]:
    years = {int(y) for y in re.findall(r"(20\d{2})年?", text or "")}
    return sorted(years)


def _contains_stale_year_for_recent(text: str) -> bool:
    current_year = int(time.localtime().tm_year)
    return any(y <= (current_year - 1) for y in _extract_years(text))


def _has_source_links_block(text: str) -> bool:
    t = (text or "").lower()
    if any(k in t for k in ["來源連結", "来源链接", "來源鏈接", "source links", "references"]):
        return True
    return bool(re.search(r"\n\[[0-9]+\]\s+https?://", text or "", flags=re.IGNORECASE))


def _extract_citation_indices(text: str, max_index: int) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for m in re.finditer(r"\[(\d{1,3})\]", text or ""):
        i = int(m.group(1))
        if i < 1 or i > max_index:
            continue
        if i in seen:
            continue
        seen.add(i)
        out.append(i)
    return out


def _news_has_diverse_citations(text: str, max_index: int) -> bool:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith(("-", "•", "*"))]
    if len(lines) < 3:
        return True

    cited: set[int] = set()
    for ln in lines:
        for m in re.finditer(r"\[(\d{1,3})\]", ln):
            i = int(m.group(1))
            if 1 <= i <= max_index:
                cited.add(i)
    # 至少要有兩個不同來源，避免全部集中在 [1]
    return len(cited) >= 2


def _ensure_bullets(text: str) -> list[str]:
    raw_lines = [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]
    bullets = [ln.strip() for ln in raw_lines if ln.strip().startswith(("-", "•", "*"))]
    if bullets:
        return bullets
    return ["- " + ln.strip() for ln in raw_lines[:5] if ln.strip()]


async def _deterministic_news_fallback(
    lm: LMStudioClient,
    *,
    model: str,
    user_text: str,
    search_results: list[dict],
    fetched_pages: list[dict],
    source_date_hints: dict[int, str],
    max_items: int = 8,
) -> str:
    n = min(max_items, len(search_results) if search_results else 0)
    if n <= 0:
        return ""

    tasks: list[asyncio.Task[str]] = []
    for i in range(1, n + 1):
        sr = search_results[i - 1] if i - 1 < len(search_results) else {}
        fp = fetched_pages[i - 1] if i - 1 < len(fetched_pages) else {}
        url = str(sr.get("url") or "")
        title = str(sr.get("title") or fp.get("title") or "").strip()
        domain = _domain(url)
        content = str(fp.get("text") or "").strip()
        if not content:
            content = f"Title: {title}\n\nSnippet: {str(sr.get('description') or '').strip()}"

        tasks.append(
            asyncio.create_task(
                _summarize_source(
                    lm,
                    model=model,
                    user_text=user_text,
                    source_index=i,
                    title=title,
                    domain=domain,
                    content=content,
                )
            )
        )

    parts = await asyncio.gather(*tasks, return_exceptions=True)

    bullets: list[str] = []
    for i, part in enumerate(parts, start=1):
        if isinstance(part, Exception):
            continue
        lines = _ensure_bullets(str(part))
        if not lines:
            continue
        line = lines[0].strip()

        hint = source_date_hints.get(i) or "[未提供日期]"
        if hint != "[未提供日期]" and not re.search(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", line):
            if "未提供日期" in line[:40]:
                line = re.sub(r"\[未提供日期\]", hint, line, count=1)
            else:
                line = "- **" + hint + "** " + re.sub(r"^[-•*]\s*", "", line)

        if f"[{i}]" not in line:
            line = line.rstrip() + f" [{i}]"

        bullets.append(line)

    if not bullets:
        return ""
    header = "以下是今天的一些國際要聞摘要："
    return header + "\n\n" + "\n".join(bullets)


def _strip_source_links_block(text: str) -> str:
    lines = (text or "").splitlines()
    cut = len(lines)
    for i, ln in enumerate(lines):
        l = ln.strip().lower()
        if any(k in l for k in ["來源連結", "来源链接", "來源鏈接", "source links", "references"]):
            cut = i
            break
    return "\n".join(lines[:cut]).rstrip()


def _build_recent_news_fallback(*, user_text: str, search_results: list[dict[str, object]]) -> str:
    lines = [
        "我已查詢最新來源，但目前前幾個來源多為背景頁或歷史內容，無法可靠支持「最近幾天」的具體財經數字。",
        "若你要，我可以改成：",
        "1) 只整理『今天/過去 24 小時』可驗證的快訊",
        "2) 鎖定特定市場（美股/台股/中國股市/原油）再即時整理",
        "",
        "目前可用來源（先供你點閱）：",
    ]

    for i, item in enumerate(search_results[:5], start=1):
        title = str(item.get("title") or "").strip() or "(untitled)"
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        lines.append(f"[{i}] {title}")
        lines.append(url)

    return "\n".join(lines).strip()


def _news_output_has_date_prefix(text: str) -> bool:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith(("-", "•", "*"))]
    if not lines:
        return False

    date_re = re.compile(r"20\d{2}\s*[-/年]\s*\d{1,2}(?:\s*[-/月]\s*\d{1,2}\s*日?)?")
    for ln in lines:
        head = ln[:64]
        if date_re.search(head):
            continue
        if "未提供日期" in head:
            continue
        return False
    return True


def _normalize_date_parts(year: int, month: int, day: int) -> str | None:
    if year < 2000 or year > 2100:
        return None
    if month < 1 or month > 12:
        return None
    if day < 1 or day > 31:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _extract_date_candidates(text: str) -> list[str]:
    s = text or ""
    out: list[str] = []

    # 2026-02-18 / 2026/02/18 / 2026年2月18日
    for m in re.finditer(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*(?:[-/月]\s*(\d{1,2})\s*日?)?", s):
        y = int(m.group(1))
        mo = int(m.group(2))
        d = int(m.group(3) or 1)
        norm = _normalize_date_parts(y, mo, d)
        if norm:
            out.append(norm)

    # February 17, 2026
    month_map = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    for m in re.finditer(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s*(20\d{2})\b",
        s,
        flags=re.IGNORECASE,
    ):
        mo = month_map.get((m.group(1) or "").lower(), 0)
        d = int(m.group(2))
        y = int(m.group(3))
        norm = _normalize_date_parts(y, mo, d)
        if norm:
            out.append(norm)

    # de-dup keep order
    seen: set[str] = set()
    uniq: list[str] = []
    for d in out:
        if d in seen:
            continue
        seen.add(d)
        uniq.append(d)
    return uniq


def _build_source_date_hints(search_results: list[dict], fetched_pages: list[dict]) -> tuple[dict[int, str], set[str]]:
    hints: dict[int, str] = {}
    allowed_dates: set[str] = set()

    max_n = max(len(search_results), len(fetched_pages))
    for i in range(1, max_n + 1):
        sr = search_results[i - 1] if i - 1 < len(search_results) else {}
        fp = fetched_pages[i - 1] if i - 1 < len(fetched_pages) else {}

        text_parts = [
            str(sr.get("title") or ""),
            str(sr.get("description") or ""),
            str(fp.get("title") or ""),
            str(fp.get("text") or "")[:1500],
        ]
        dates: list[str] = []
        for part in text_parts:
            dates.extend(_extract_date_candidates(part))

        date_value = dates[0] if dates else "[未提供日期]"
        hints[i] = date_value
        if date_value != "[未提供日期]":
            allowed_dates.add(date_value)

    return hints, allowed_dates


def _extract_news_bullet_dates(text: str) -> list[str]:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith(("-", "•", "*"))]
    out: list[str] = []
    for ln in lines:
        head = ln[:80]
        if "未提供日期" in head:
            out.append("[未提供日期]")
            continue
        found = _extract_date_candidates(head)
        if found:
            out.append(found[0])
    return out


def _news_dates_grounded_in_sources(text: str, allowed_dates: set[str]) -> bool:
    bullet_dates = _extract_news_bullet_dates(text)
    if not bullet_dates:
        return False
    for d in bullet_dates:
        if d == "[未提供日期]":
            continue
        if d not in allowed_dates:
            return False
    return True


def _is_weather_refusal(text: str) -> bool:
    t = (text or "").lower()
    patterns = [
        "無法提供最新",
        "无法提供最新",
        "無法提供即時",
        "无法提供实时",
        "cannot provide",
        "can't provide",
        "unable to provide",
        "無法查詢",
        "无法查询",
    ]
    return any(p in t for p in patterns)


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
    # 1) Known Taiwan cities/counties first.
    for loc in _TW_LOCATIONS:
        if loc in user_text:
            return loc

    # 2) Generic Chinese location patterns for non-TW cities (e.g., 蘇州、上海).
    patterns = [
        r"的([\u4e00-\u9fff]{1,20}?)(?:天氣|天气|氣象|气象|降雨|溫度|温度)",
        r"(?:今天|今日|目前|現在|最新)?\s*([\u4e00-\u9fff]{2,20}?)(?:天氣|天气|氣象|气象|降雨機率|降雨|溫度|温度)",
    ]
    for p in patterns:
        m = re.search(p, user_text)
        if not m:
            continue
        cand = (m.group(1) or "").strip()
        cand = re.sub(r"^(我想問|請問|想問|幫我查|查一下|查詢)", "", cand).strip()
        cand = cand.replace("的", "").strip()
        if cand and cand not in {"今天", "今日", "目前", "現在", "最新", "天氣", "天气"}:
            return cand

    return ""


def _should_force_web_search(user_text: str) -> bool:
    t = user_text.lower()
    keywords = [
        "天氣",
        "天气",
        "新聞",
        "新闻",
        "news",
        "軍演",
        "军演",
        "海域",
        "時事",
        "时事",
        "最近",
        "近日",
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
                    "For each bullet, include the source publication date at the beginning when available (YYYY-MM-DD). "
                    "If no date is found in the source, begin with '[未提供日期]'. "
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
    news_max_items = int(getattr(settings, "news_max_items", 8) or 8)
    news_followup_default_count = int(getattr(settings, "news_followup_default_count", 5) or 5)

    memory = MarkdownMemory(
        memory_dir=settings.memory_dir,
        mode=settings.memory_mode,
        days=settings.memory_days,
    )
    recent: dict[int, deque[dict]] = defaultdict(lambda: deque(maxlen=settings.recent_turns * 2))
    last_web_context: dict[int, dict[str, object]] = {}

    async def on_memory_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat or not update.message:
            return
        chat_id = update.effective_chat.id
        profile = await memory.get_profile(chat_id=chat_id)
        text = _format_profile_text(profile)
        await update.message.reply_text(text, disable_web_page_preview=True)

    async def on_forget_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat or not update.message:
            return
        chat_id = update.effective_chat.id
        cleared = await memory.clear_profile(chat_id=chat_id)
        if cleared:
            text = "已清除這個聊天室的長期記憶偏好（profile）。"
        else:
            text = "目前沒有可清除的長期記憶偏好。"
        await update.message.reply_text(text, disable_web_page_preview=True)

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return

        chat_id = update.effective_chat.id
        user_text = update.message.text.strip()

        # Group mode: only respond when explicitly mentioned.
        chat_type = getattr(update.effective_chat, "type", "")
        if chat_type in {"group", "supergroup"}:
            bot_username = getattr(context.bot, "username", "") or ""
            cleaned, mentioned = _strip_leading_bot_mention(user_text, bot_username)
            if not mentioned:
                return
            user_text = cleaned
            if not user_text:
                return

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

        profile = await memory.get_profile(chat_id=chat_id)
        profile_updates = _infer_profile_updates(user_text)
        if _is_weather_question(user_text):
            detected_loc = _extract_tw_location(user_text)
            if detected_loc:
                profile_updates["default_weather_location"] = _normalize_location(detected_loc)
        if profile_updates:
            profile = await memory.upsert_profile(chat_id=chat_id, updates=profile_updates)
            if dbg.enabled:
                dbg.write_json(
                    request_id=request_id,
                    name="memory_profile_update",
                    data={"chat_id": chat_id, "updates": profile_updates, "profile": profile},
                )

        plan = await llm_plan_tools(lm, model=settings.lmstudio_chat_model, user_text=user_text)
        tool = (plan.get("tool") or "none").strip()
        query = (plan.get("query") or "").strip()

        # A) Follow-up continuation: reuse last web_search context for news.
        is_followup = _is_followup_continue(user_text)
        prev_ctx = last_web_context.get(chat_id) or {}
        if is_followup and prev_ctx.get("tool") == "web_search" and bool(prev_ctx.get("is_news")):
            tool = "web_search"
            query = ""

        followup_n = _extract_followup_count(
            user_text,
            default=news_followup_default_count,
            max_n=news_max_items,
        )

        force_web_search = _should_force_web_search(user_text)
        weather_override = False
        if _is_weather_question(user_text) or force_web_search:
            if _is_weather_question(user_text):
                weather_override = True
            tool = "web_search"

        if dbg.enabled:
            dbg.write_json(
                request_id=request_id,
                name="plan",
                data={"tool": tool, "query": query, "weather_override": weather_override},
            )

        search_results = []
        if tool == "web_search":
            if is_followup and prev_ctx.get("tool") == "web_search" and bool(prev_ctx.get("is_news")):
                search_results = list(prev_ctx.get("search_results") or [])
            else:
                is_weather = _is_weather_question(user_text)
                loc = _extract_tw_location(user_text) if is_weather else ""
                if is_weather and not loc:
                    loc = str(profile.get("default_weather_location") or "").strip()
                if is_weather and not loc:
                    assistant_text = "你想查哪個城市/地區的天氣？例如：台北 / 台中 / 高雄 / 蘇州 / 上海。"
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
                    if _is_tw_location(nloc):
                        query = f"{nloc} 今天 天氣預報 降雨機率 最高溫 最低溫 體感 風速 中央氣象署"
                    else:
                        query = f"{nloc} 今天 天氣預報 降雨機率 最高溫 最低溫 體感 風速"

                if not is_weather and not query and _is_market_index_query(user_text):
                    if any(k in user_text.lower() for k in ["道瓊", "道琼", "dow jones", "djia"]):
                        query = "Dow Jones Industrial Average latest close past 5 trading days"

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

            if dbg.enabled and is_followup and prev_ctx.get("tool") == "web_search" and bool(prev_ctx.get("is_news")):
                dbg.write_json(
                    request_id=request_id,
                    name="followup_reused_web_search",
                    data={"chat_id": chat_id, "reused_results": len(search_results)},
                )

        search_block = _format_search_results(search_results) if search_results else ""

        fetched_pages: list[dict] = []
        if tool == "web_search" and search_results:
            if is_followup and prev_ctx.get("tool") == "web_search" and bool(prev_ctx.get("is_news")):
                fetched_pages = list(prev_ctx.get("fetched_pages") or [])
            else:
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
        is_weather_q = _is_weather_question(user_text)

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
        is_recent_news_q = _is_recent_news_query(user_text)
        source_date_hints, allowed_news_dates = _build_source_date_hints(search_results, fetched_pages)

        # B) Conversation summarization to profile when context is long.
        try:
            maxlen = int(settings.recent_turns * 2)
        except Exception:
            maxlen = 12
        if len(recent[chat_id]) >= maxlen:
            # Summarize older part, keep last few turns verbatim.
            keep_last = 6
            turns_list = list(recent[chat_id])
            older = turns_list[:-keep_last]
            if older:
                existing_summary = str(profile.get("conversation_summary") or "")
                try:
                    summary = await _summarize_conversation_for_profile(
                        lm,
                        model=settings.lmstudio_chat_model,
                        existing_summary=existing_summary,
                        turns=older,
                    )
                except Exception:
                    summary = ""
                if summary.strip():
                    profile = await memory.upsert_profile(chat_id=chat_id, updates={"conversation_summary": summary.strip()})
                    if dbg.enabled:
                        dbg.write_json(
                            request_id=request_id,
                            name="conversation_summary_updated",
                            data={"chat_id": chat_id, "chars": len(summary.strip())},
                        )
                    recent[chat_id].clear()
                    recent[chat_id].extend(turns_list[-keep_last:])

        messages = [
            {
                "role": "system",
                "content": "You are a helpful Telegram chatbot.",
            },
        ]
        profile_prompt = _profile_to_system_prompt(profile)
        if profile_prompt:
            messages.append({"role": "system", "content": profile_prompt})
        lang = str(profile.get("preferred_language") or "").strip()
        if lang == "zh-Hant":
            messages.append(
                {
                    "role": "system",
                    "content": "Output language rule: reply in Traditional Chinese only unless user explicitly requests another language.",
                }
            )
        elif lang == "zh-Hans":
            messages.append(
                {
                    "role": "system",
                    "content": "Output language rule: reply in Simplified Chinese only unless user explicitly requests another language.",
                }
            )
        elif lang == "en":
            messages.append(
                {
                    "role": "system",
                    "content": "Output language rule: reply in English only unless user explicitly requests another language.",
                }
            )

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
                    if is_followup and prev_ctx.get("tool") == "web_search" and bool(prev_ctx.get("is_news")):
                        n = min(news_max_items, max(1, followup_n))
                    else:
                        n = min(news_max_items, len(search_results) if search_results else news_max_items)
                    date_hint_lines = [f"[{i}] {source_date_hints.get(i, '[未提供日期]')}" for i in range(1, n + 1)]
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "The user is asking for news. "
                                f"You MUST list at least {n} distinct news items if sources are available. "
                                "Return a bullet list. Each bullet must contain: publication date (YYYY-MM-DD, or [未提供日期] if not available), a short headline, a 1-2 sentence summary, and a citation like [n]. "
                                "The publication date must be placed at the beginning of each bullet. "
                                "Use only publication dates from the following source-date hints; do NOT invent dates:\n"
                                + "\n".join(date_hint_lines)
                                + "\n"
                                "Each bullet MUST cite a different source index when possible. "
                                "Do NOT write generic summaries. Do NOT merge multiple news into one bullet."
                            ),
                        }
                    )

                if is_weather_q:
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "This is a weather / real-time info question. You MUST use the provided web content to answer. "
                                "Do NOT say you cannot provide real-time info. "
                                "If the provided sources do not include specific numbers, clearly say 'sources do not contain the detailed forecast numbers' and ask the user for a more specific time/window (e.g., morning/afternoon) or district. "
                                "Never copy stale values from prior conversation. Ignore previous assistant claims if they conflict with current sources. "
                                "Do NOT output any explicit date (e.g., '截至2023年...') unless that exact date appears in the provided sources. "
                                "If a number (temperature/rain chance) is not present in current sources, say it is unavailable rather than guessing. "
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

        history_for_prompt = list(recent[chat_id])
        if tool == "web_search" and (is_weather_q or is_recent_news_q):
            # Avoid stale contamination from earlier assistant turns in recency-sensitive queries.
            history_for_prompt = [m for m in history_for_prompt if m.get("role") == "user"][-3:]

        messages.extend(history_for_prompt)

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

        if tool == "web_search" and is_weather_q and search_results and _is_weather_refusal(assistant_text):
            retry_messages = list(messages)
            retry_messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous answer is invalid because it refused real-time weather. "
                        "You must answer using current provided sources and citations [n]. "
                        "Do NOT refuse. Do NOT say you cannot provide real-time info. "
                        "If exact numbers are unavailable, explicitly state 'sources do not contain the detailed forecast numbers'. "
                        "Use Traditional Chinese with sections: 概況 / 溫度範圍 / 降雨機率 / 注意事項."
                    ),
                }
            )
            retry_messages.append(
                {
                    "role": "user",
                    "content": user_text,
                }
            )
            assistant_text = await lm.chat_completions(
                model=settings.lmstudio_chat_model,
                messages=retry_messages,
                temperature=0.1,
                max_tokens=450,
                request_id=request_id,
            )

        lang_pref = str(profile.get("preferred_language") or "").strip()
        if lang_pref == "zh-Hant" and _looks_simplified_chinese(assistant_text):
            rewrite_messages = [
                {
                    "role": "system",
                    "content": (
                        "Rewrite the text into Traditional Chinese (繁體中文) only. "
                        "Keep meaning, structure, citations like [n], and URLs unchanged. "
                        "Do not add or remove facts."
                    ),
                },
                {"role": "user", "content": assistant_text},
            ]
            rewritten = await lm.chat_completions(
                model=settings.lmstudio_chat_model,
                messages=rewrite_messages,
                temperature=0.0,
                max_tokens=1200,
                request_id=request_id,
            )
            if (rewritten or "").strip():
                assistant_text = rewritten.strip()
            if dbg.enabled:
                dbg.write_json(
                    request_id=request_id,
                    name="language_rewrite_applied",
                    data={"preferred_language": lang_pref},
                )

        if tool == "web_search" and is_recent_news_q and search_results and _contains_stale_year_for_recent(assistant_text):
            stale_years = _extract_years(assistant_text)
            retry_messages = list(messages)
            retry_messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous answer is invalid for a recent-news query because it used stale timeline years. "
                        "Re-answer using only very recent updates from provided sources. "
                        "If provided sources do not clearly support events in the last few days, explicitly say so and ask user whether to broaden the time range. "
                        "Do NOT fabricate dates. Keep citations [n]."
                    ),
                }
            )
            retry_messages.append({"role": "user", "content": user_text})
            assistant_text = await lm.chat_completions(
                model=settings.lmstudio_chat_model,
                messages=retry_messages,
                temperature=0.1,
                max_tokens=700,
                request_id=request_id,
            )
            if dbg.enabled:
                dbg.write_json(
                    request_id=request_id,
                    name="recent_news_stale_retry",
                    data={"stale_years": stale_years},
                )

        if tool == "web_search" and is_recent_news_q and search_results and _contains_stale_year_for_recent(assistant_text):
            assistant_text = _build_recent_news_fallback(user_text=user_text, search_results=search_results)
            if dbg.enabled:
                dbg.write_json(
                    request_id=request_id,
                    name="recent_news_fallback_used",
                    data={"reason": "stale_year_after_retry", "years": _extract_years(assistant_text)},
                )

        if tool == "web_search" and is_news and search_results and not _news_output_has_date_prefix(assistant_text):
            retry_messages = list(messages)
            retry_messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous news output is invalid because each bullet must start with publication date. "
                        "Rewrite as bullet list and put date at beginning of each bullet in YYYY-MM-DD. "
                        "If source date is unavailable, start the bullet with [未提供日期]. "
                        "Keep citations [n] and do not fabricate unsupported facts."
                    ),
                }
            )
            retry_messages.append({"role": "user", "content": user_text})
            assistant_text = await lm.chat_completions(
                model=settings.lmstudio_chat_model,
                messages=retry_messages,
                temperature=0.1,
                max_tokens=900,
                request_id=request_id,
            )
            if dbg.enabled:
                dbg.write_json(
                    request_id=request_id,
                    name="news_date_retry",
                    data={"enforced": True},
                )

        if tool == "web_search" and is_news and search_results and not _news_dates_grounded_in_sources(assistant_text, allowed_news_dates):
            retry_messages = list(messages)
            retry_messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous dates are invalid because they are not grounded in source-date hints. "
                        "Rewrite the news bullets and use only allowed dates from source-date hints or [未提供日期]. "
                        "Do NOT fabricate dates. Keep citations [n]."
                    ),
                }
            )
            retry_messages.append({"role": "user", "content": user_text})
            assistant_text = await lm.chat_completions(
                model=settings.lmstudio_chat_model,
                messages=retry_messages,
                temperature=0.1,
                max_tokens=900,
                request_id=request_id,
            )
            if dbg.enabled:
                dbg.write_json(
                    request_id=request_id,
                    name="news_date_grounding_retry",
                    data={"allowed_dates": sorted(allowed_news_dates)},
                )

        if tool == "web_search" and is_news and search_results and not _news_has_diverse_citations(assistant_text, len(search_results)):
            retry_messages = list(messages)
            retry_messages.append(
                {
                    "role": "system",
                    "content": (
                        "Your previous answer overused a single citation index. "
                        "Rewrite using multiple different citation indices [n] that match the corresponding sources. "
                        "If multiple sources are available, do not cite only [1]."
                    ),
                }
            )
            retry_messages.append({"role": "user", "content": user_text})
            assistant_text = await lm.chat_completions(
                model=settings.lmstudio_chat_model,
                messages=retry_messages,
                temperature=0.1,
                max_tokens=900,
                request_id=request_id,
            )
            if dbg.enabled:
                dbg.write_json(
                    request_id=request_id,
                    name="news_citation_diversity_retry",
                    data={"enforced": True},
                )

        if tool == "web_search" and is_news and search_results and not _news_has_diverse_citations(assistant_text, len(search_results)):
            fallback_text = await _deterministic_news_fallback(
                lm,
                model=settings.lmstudio_chat_model,
                user_text=user_text,
                search_results=search_results,
                fetched_pages=fetched_pages,
                source_date_hints=source_date_hints,
                max_items=min(news_max_items, len(search_results)),
            )
            if fallback_text.strip():
                assistant_text = fallback_text.strip()
            if dbg.enabled:
                dbg.write_json(
                    request_id=request_id,
                    name="news_deterministic_fallback_applied",
                    data={"applied": bool(fallback_text.strip())},
                )

        # Persist last web_search context for follow-up.
        if tool == "web_search":
            last_web_context[chat_id] = {
                "tool": "web_search",
                "is_news": bool(is_news),
                "is_weather": bool(is_weather_q),
                "query": query or user_text,
                "search_results": search_results,
                "fetched_pages": fetched_pages,
                "source_date_hints": source_date_hints,
                "ts": time.time(),
            }

        if lang_pref == "zh-Hant" and _looks_simplified_chinese(assistant_text):
            rewrite_messages = [
                {
                    "role": "system",
                    "content": (
                        "Rewrite the text into Traditional Chinese (繁體中文) only. "
                        "Keep meaning, structure, citations like [n], and URLs unchanged. "
                        "Do not add or remove facts."
                    ),
                },
                {"role": "user", "content": assistant_text},
            ]
            rewritten = await lm.chat_completions(
                model=settings.lmstudio_chat_model,
                messages=rewrite_messages,
                temperature=0.0,
                max_tokens=1200,
                request_id=request_id,
            )
            if (rewritten or "").strip():
                assistant_text = rewritten.strip()
            if dbg.enabled:
                dbg.write_json(
                    request_id=request_id,
                    name="language_rewrite_applied",
                    data={"preferred_language": lang_pref, "stage": "final"},
                )

        if tool == "web_search" and is_news and search_results:
            base_text = _strip_source_links_block(assistant_text)
            cited = _extract_citation_indices(base_text, len(search_results))
            if not cited:
                cited = [i for i in range(1, min(10, len(search_results)) + 1)]

            link_lines: list[str] = []
            for i in cited[:10]:
                url = (search_results[i - 1].get("url") or "").strip()
                if not url:
                    continue
                link_lines.append(f"[{i}] {url}")

            assistant_text = base_text
            if link_lines:
                assistant_text = assistant_text.rstrip() + "\n\n" + "來源連結：\n" + "\n".join(link_lines)

        if tool == "web_search" and _wants_links(user_text) and search_results and not is_news:
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
    app.add_handler(CommandHandler("memory", on_memory_command))
    app.add_handler(CommandHandler("forget", on_forget_command))
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
