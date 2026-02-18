from __future__ import annotations

from typing import Any

import httpx

from .debug_logger import DebugLogger
from .mcp_stdio_client import MCPServerConfig, MCPStdioClient


class BraveSearchClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_s: float = 30.0,
        debug_logger: DebugLogger | None = None,
        mcp_enabled: bool = False,
        mcp_command: str = "npx",
        mcp_args: list[str] | None = None,
    ):
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._debug = debug_logger

        self._mcp_enabled = mcp_enabled
        self._mcp: MCPStdioClient | None = None
        self._mcp_config = MCPServerConfig(
            command=mcp_command,
            args=mcp_args or ["-y", "@modelcontextprotocol/server-brave-search"],
            env={"BRAVE_API_KEY": api_key},
        )

    async def close(self) -> None:
        if self._mcp is not None:
            await self._mcp.close()
            self._mcp = None
        await self._client.aclose()

    async def _ensure_mcp(self) -> MCPStdioClient:
        if self._mcp is None:
            self._mcp = MCPStdioClient(self._mcp_config, timeout_s=30.0)
            await self._mcp.start()
        return self._mcp

    def _parse_mcp_web_results(self, body: Any, *, count: int) -> list[dict[str, Any]]:
        data = body if isinstance(body, dict) else {}
        results_raw = (data.get("web", {}) or {}).get("results") or []
        out: list[dict[str, Any]] = []
        for item in list(results_raw)[:count]:
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "title": item.get("title") or "",
                    "url": item.get("url") or "",
                    "description": item.get("description") or item.get("snippet") or "",
                }
            )
        return out

    async def web_search(
        self,
        *,
        query: str,
        country: str = "TW",
        lang: str = "zh-hant",
        count: int = 5,
        request_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if self._mcp_enabled:
            try:
                mcp = await self._ensure_mcp()
                args = {
                    "query": query,
                    "country": country,
                    "search_lang": lang,
                    "count": int(count),
                    "safesearch": "moderate",
                    "text_decorations": False,
                }

                if self._debug and self._debug.enabled and request_id:
                    self._debug.write_json(
                        request_id=request_id,
                        name="brave_mcp_request",
                        data={"tool": "brave_web_search", "arguments": args, "command": self._mcp_config.command, "args": self._mcp_config.args},
                    )

                res = await mcp.tools_call(name="brave_web_search", arguments=args)

                if self._debug and self._debug.enabled and request_id:
                    self._debug.write_json(
                        request_id=request_id,
                        name="brave_mcp_response",
                        data=res,
                    )

                content = res.get("content")
                body: Any = None
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict):
                        if "json" in first:
                            body = first.get("json")
                        elif first.get("type") == "text" and isinstance(first.get("text"), str):
                            try:
                                body = httpx.Response(200, text=first["text"]).json()
                            except Exception:
                                body = None

                results = self._parse_mcp_web_results(body, count=count)
                if self._debug and self._debug.enabled and request_id:
                    self._debug.write_json(
                        request_id=request_id,
                        name="brave_mcp_parsed_results",
                        data={"count": len(results), "results": results},
                    )
                return results
            except Exception:
                pass

        url = "https://api.search.brave.com/res/v1/web/search"
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        params = {
            "q": query,
            "country": country,
            "search_lang": lang,
            "count": str(count),
            "safesearch": "moderate",
            "text_decorations": "false",
        }

        if self._debug and self._debug.enabled and request_id:
            self._debug.write_json(
                request_id=request_id,
                name="brave_request",
                data={"url": url, "headers": headers, "params": params},
            )

        r = await self._client.get(
            url,
            headers=headers,
            params=params,
        )

        if self._debug and self._debug.enabled and request_id:
            try:
                body = r.json()
            except Exception:
                body = {"_non_json_text": r.text}
            self._debug.write_json(
                request_id=request_id,
                name="brave_response",
                data={
                    "status_code": r.status_code,
                    "headers": dict(r.headers),
                    "body": body,
                },
            )

        r.raise_for_status()
        data = r.json()

        results: list[dict[str, Any]] = []
        for item in (data.get("web", {}).get("results") or [])[:count]:
            results.append(
                {
                    "title": item.get("title") or "",
                    "url": item.get("url") or "",
                    "description": item.get("description") or "",
                }
            )

        if self._debug and self._debug.enabled and request_id:
            self._debug.write_json(
                request_id=request_id,
                name="brave_parsed_results",
                data={"count": len(results), "results": results},
            )
        return results
