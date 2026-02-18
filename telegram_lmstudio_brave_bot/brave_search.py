from __future__ import annotations

import json
import traceback
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

    def _extract_mcp_body(self, res: dict[str, Any]) -> Any:
        # Common MCP tool shape: result.structuredContent carries machine-readable data.
        structured = res.get("structuredContent")
        if isinstance(structured, dict):
            return structured

        # Some servers return direct web/search body at top-level result.
        if isinstance(res.get("web"), dict):
            return res

        content = res.get("content")
        if isinstance(content, list) and content:
            # Prefer JSON payload blocks.
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("json"), dict):
                    return item.get("json")

            # Fallback: attempt to parse text blocks as JSON.
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                    text = item["text"].strip()
                    if not text:
                        continue
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            return parsed
                    except Exception:
                        continue

        return None

    def _parse_mcp_text_results(self, res: dict[str, Any], *, count: int) -> list[dict[str, Any]]:
        content = res.get("content")
        if not isinstance(content, list):
            return []

        out: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text" or not isinstance(item.get("text"), str):
                continue

            text = item.get("text", "")
            current: dict[str, str] = {"title": "", "url": "", "description": ""}

            def _flush_current() -> None:
                if (current.get("title") or "").strip() and (current.get("url") or "").strip():
                    out.append(
                        {
                            "title": current.get("title", "").strip(),
                            "url": current.get("url", "").strip(),
                            "description": current.get("description", "").strip(),
                        }
                    )

            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue

                if line.startswith("Title:"):
                    _flush_current()
                    current = {"title": line[len("Title:") :].strip(), "url": "", "description": ""}
                    continue

                if line.startswith("Description:"):
                    current["description"] = line[len("Description:") :].strip()
                    continue

                if line.startswith("URL:"):
                    current["url"] = line[len("URL:") :].strip()
                    continue

                # Continuation lines are appended to description.
                if current.get("description"):
                    current["description"] = (current.get("description", "") + " " + line).strip()

            _flush_current()

            if len(out) >= count:
                break

        return out[:count]

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

                body: Any = self._extract_mcp_body(res)

                results = self._parse_mcp_web_results(body, count=count)
                if not results:
                    results = self._parse_mcp_text_results(res, count=count)
                if not results:
                    raise RuntimeError("MCP returned no parseable web results")
                if self._debug and self._debug.enabled and request_id:
                    self._debug.write_json(
                        request_id=request_id,
                        name="brave_mcp_parsed_results",
                        data={"count": len(results), "results": results},
                    )
                return results
            except Exception as e:
                if self._debug and self._debug.enabled and request_id:
                    self._debug.write_json(
                        request_id=request_id,
                        name="brave_mcp_error",
                        data={
                            "error": repr(e),
                            "traceback": traceback.format_exc(),
                            "command": self._mcp_config.command,
                            "args": self._mcp_config.args,
                        },
                    )

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
