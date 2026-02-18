from __future__ import annotations

import datetime as _dt
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
