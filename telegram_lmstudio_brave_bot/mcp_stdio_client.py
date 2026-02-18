from __future__ import annotations

import asyncio
import json
import os
import shutil
from dataclasses import dataclass
from typing import Any


@dataclass
class MCPServerConfig:
    command: str
    args: list[str]
    env: dict[str, str] | None = None


class MCPStdioClient:
    def __init__(self, config: MCPServerConfig, *, timeout_s: float = 30.0):
        self._config = config
        self._timeout_s = timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._proc is not None:
            return

        child_env = dict(os.environ)
        if self._config.env:
            child_env.update(self._config.env)

        command = self._config.command
        resolved = shutil.which(command)
        if resolved:
            command = resolved
        elif os.name == "nt":
            if not command.lower().endswith((".exe", ".cmd", ".bat")):
                cmd2 = command + ".cmd"
                resolved2 = shutil.which(cmd2)
                if resolved2:
                    command = resolved2
                else:
                    command = cmd2

        try:
            self._proc = await asyncio.create_subprocess_exec(
                command,
                *self._config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
        except FileNotFoundError:
            if os.name != "nt":
                raise
            joined = " ".join([self._config.command, *self._config.args])
            self._proc = await asyncio.create_subprocess_exec(
                "cmd.exe",
                "/c",
                joined,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=child_env,
            )
        assert self._proc.stdout is not None
        self._reader_task = asyncio.create_task(self._reader_loop(self._proc.stdout))

        if self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc.stderr))

        await self._initialize()

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return

        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except Exception:
            pass

        try:
            proc.terminate()
        except Exception:
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        if self._reader_task is not None:
            self._reader_task.cancel()

        if self._stderr_task is not None:
            self._stderr_task.cancel()

        self._proc = None

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await self.start()
        async with self._lock:
            req_id = self._next_id
            self._next_id += 1

        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut

        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})

        try:
            return await asyncio.wait_for(fut, timeout=self._timeout_s)
        finally:
            self._pending.pop(req_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.start()
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def tools_list(self) -> list[dict[str, Any]]:
        resp = await self.request("tools/list")
        return list(resp.get("result", {}).get("tools") or [])

    async def tools_call(self, *, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        resp = await self.request("tools/call", {"name": name, "arguments": arguments})
        return dict(resp.get("result") or {})

    async def _initialize(self) -> None:
        resp = await self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "telegram-lmstudio-brave-bot", "version": "0.1"},
            },
        )
        if "error" in resp:
            raise RuntimeError(f"MCP initialize failed: {resp['error']}")
        await self.notify("notifications/initialized")

    async def _send(self, msg: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("MCP process not started")

        data = json.dumps(msg, ensure_ascii=False)
        if "\n" in data:
            raise RuntimeError("MCP message contains newline; stdio transport requires newline-delimited JSON")

        proc.stdin.write((data + "\n").encode("utf-8"))
        await proc.stdin.drain()

    async def _reader_loop(self, stdout: asyncio.StreamReader) -> None:
        while True:
            line_b = await stdout.readline()
            if not line_b:
                return
            line = line_b.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue

            msg_id = msg.get("id")
            if isinstance(msg_id, int) and msg_id in self._pending:
                fut = self._pending.get(msg_id)
                if fut is not None and not fut.done():
                    fut.set_result(msg)

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        while True:
            line_b = await stderr.readline()
            if not line_b:
                return
