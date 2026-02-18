from __future__ import annotations

import datetime as _dt
import json
import os
import re
from typing import Any


class MarkdownMemory:
    def __init__(
        self,
        *,
        memory_dir: str = "memory",
        mode: str = "daily",
        days: int = 1,
    ):
        self._dir = memory_dir
        self._mode = mode
        self._days = max(1, int(days))

    def _path_for(self, *, day: _dt.date, chat_id: int) -> str:
        d = day.isoformat()

        if self._mode == "daily":
            return os.path.join(self._dir, f"chat_{chat_id}", f"{d}.md")
        if self._mode == "per_chat_daily":
            return os.path.join(self._dir, f"chat_{chat_id}", f"{d}.md")
        if self._mode == "per_chat":
            return os.path.join(self._dir, f"chat_{chat_id}.md")

        # fallback
        return os.path.join(self._dir, f"{d}.md")

    def _paths_to_read(self, *, chat_id: int) -> list[str]:
        today = _dt.date.today()

        if self._mode == "per_chat":
            return [self._path_for(day=today, chat_id=chat_id)]

        out: list[str] = []
        for i in range(self._days):
            day = today - _dt.timedelta(days=i)
            out.append(self._path_for(day=day, chat_id=chat_id))
        out.reverse()
        return out

    def _profile_path(self, *, chat_id: int) -> str:
        return os.path.join(self._dir, f"chat_{chat_id}", "profile.json")

    async def add_turn(self, *, chat_id: int, role: str, content: str, ts: float) -> None:
        day = _dt.date.fromtimestamp(ts)
        path = self._path_for(day=day, chat_id=chat_id)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        t = _dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
        safe = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")

        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- [{t}] chat:{chat_id} ({role}) {safe}\n")

    async def recent_turns(self, *, chat_id: int, limit: int) -> list[dict[str, Any]]:
        pattern = re.compile(r"^- \[[0-9:]{8}\] chat:(-?\d+) \((user|assistant)\) (.*)$")
        turns: list[dict[str, Any]] = []

        for path in self._paths_to_read(chat_id=chat_id):
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    m = pattern.match(line.rstrip("\n"))
                    if not m:
                        continue
                    if int(m.group(1)) != int(chat_id):
                        continue
                    role = m.group(2)
                    content = m.group(3).replace("\\n", "\n")
                    turns.append({"role": role, "content": content})

        if limit <= 0:
            return []
        return turns[-limit:]

    async def get_profile(self, *, chat_id: int) -> dict[str, Any]:
        path = self._profile_path(chat_id=chat_id)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
        except Exception:
            return {}

    async def upsert_profile(self, *, chat_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        base = await self.get_profile(chat_id=chat_id)
        merged: dict[str, Any] = dict(base)

        for k, v in updates.items():
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            merged[k] = v

        path = self._profile_path(chat_id=chat_id)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

        return merged

    async def clear_profile(self, *, chat_id: int) -> bool:
        path = self._profile_path(chat_id=chat_id)
        if not os.path.exists(path):
            return False
        try:
            os.remove(path)
            return True
        except Exception:
            return False
