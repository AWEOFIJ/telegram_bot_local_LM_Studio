from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_REDACT_KEYS = {
    "authorization",
    "x-subscription-token",
    "api_key",
    "brave_api_key",
    "token",
    "telegram_bot_token",
}


def _utc_datestr(ts: float | None = None) -> str:
    t = time.gmtime(ts if ts is not None else time.time())
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _chat_id_from_request_id(request_id: str) -> str:
    rid = (request_id or "").strip()
    if not rid:
        return "unknown"

    # Common request_id formats in this repo:
    # - chat<chat_id>_<ts>
    # - spec<chat_id>_<ts>
    # - tech<chat_id>_<ts>
    # - gen<chat_id>_<ts>
    m = re.match(r"^(?:chat|spec|tech|gen)[_-]?(-?\d+)(?:_|-)", rid)
    if m:
        return m.group(1)

    # Allow embedded 'chat<id>_' anywhere (defensive).
    m2 = re.search(r"chat[_-]?(-?\d+)(?:_|-)", rid)
    if m2:
        return m2.group(1)

    # Last resort: extract a plausible chat id anywhere in the string.
    # Telegram chat ids are typically 6+ digits (users) or negative for groups.
    m3 = re.search(r"(-?\d{6,})", rid)
    if m3:
        return m3.group(1)

    return "unknown"


def _kind_from_request_id(request_id: str) -> str:
    rid = (request_id or "").strip().lower()
    if rid.startswith("spec"):
        return "spec"
    if rid.startswith("tech"):
        return "tech"
    if rid.startswith("gen"):
        return "gen"
    if rid.startswith("chat"):
        return "chat"
    return "misc"


def _bucket_for_event_name(name: str) -> str:
    n = (name or "").strip().lower()
    if n in {"telegram_in", "plan", "final_messages"}:
        return "request"
    if n.endswith("_request"):
        return "request"
    return "final"


def _safe_filename(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s).strip("_")
    return s[:180] if len(s) > 180 else s


def _truncate_text(s: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n...[truncated {len(s) - limit} chars]"


def _sanitize(obj: Any, *, max_str: int, max_list: int, _depth: int = 0) -> Any:
    if _depth > 12:
        return "[max_depth]"

    if obj is None or isinstance(obj, (bool, int, float)):
        return obj

    if isinstance(obj, str):
        return _truncate_text(obj, max_str)

    if isinstance(obj, bytes):
        return f"[bytes:{len(obj)}]"

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if ks.strip().lower() in _REDACT_KEYS:
                out[ks] = "[redacted]"
            else:
                out[ks] = _sanitize(v, max_str=max_str, max_list=max_list, _depth=_depth + 1)
        return out

    if isinstance(obj, (list, tuple)):
        items = list(obj)
        trimmed = items[:max_list]
        out = [_sanitize(x, max_str=max_str, max_list=max_list, _depth=_depth + 1) for x in trimmed]
        if len(items) > max_list:
            out.append(f"...[truncated {len(items) - max_list} items]")
        return out

    return _truncate_text(repr(obj), max_str)


@dataclass
class DebugLogger:
    base_dir: str = "debug"
    enabled: bool = False
    max_str: int = 8000
    max_list: int = 50

    def _ensure_dir(self, *, request_id: str) -> Path:
        chat_id = _chat_id_from_request_id(request_id)
        kind = _kind_from_request_id(request_id)
        d = Path(self.base_dir) / f"{_utc_datestr()}_chat" / str(chat_id) / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_json(self, *, request_id: str, name: str, data: Any) -> None:
        if not self.enabled:
            return
        out_dir = self._ensure_dir(request_id=request_id)
        bucket = _bucket_for_event_name(name)
        fp = out_dir / f"{bucket}.json"
        payload = {
            "ts": time.time(),
            "request_id": request_id,
            "name": name,
            "data": _sanitize(data, max_str=self.max_str, max_list=self.max_list),
        }

        items: list[dict[str, Any]] = []
        if fp.exists():
            try:
                existing = json.loads(fp.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    items = existing
                elif isinstance(existing, dict):
                    items = [existing]
            except Exception:
                items = []

        items.append(payload)
        fp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def debug_logger_from_settings(settings: Any) -> DebugLogger:
    enabled = bool(getattr(settings, "debug", False))
    base_dir = str(getattr(settings, "debug_dir", "debug") or "debug")
    max_str = int(getattr(settings, "debug_max_str", 8000) or 8000)
    max_list = int(getattr(settings, "debug_max_list", 50) or 50)
    return DebugLogger(base_dir=base_dir, enabled=enabled, max_str=max_str, max_list=max_list)


def debug_logger_from_env() -> DebugLogger:
    debug = os.environ.get("DEBUG", "").strip() in {"1", "true", "True", "yes", "YES"}
    base_dir = os.environ.get("DEBUG_DIR", "debug").strip() or "debug"

    max_str_s = os.environ.get("DEBUG_MAX_STR", "8000").strip()
    max_list_s = os.environ.get("DEBUG_MAX_LIST", "50").strip()
    try:
        max_str = int(max_str_s)
    except Exception:
        max_str = 8000
    try:
        max_list = int(max_list_s)
    except Exception:
        max_list = 50

    return DebugLogger(base_dir=base_dir, enabled=debug, max_str=max_str, max_list=max_list)
