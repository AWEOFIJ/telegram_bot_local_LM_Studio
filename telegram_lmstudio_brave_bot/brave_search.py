from __future__ import annotations

from typing import Any

import httpx


class BraveSearchClient:
    def __init__(self, api_key: str, *, timeout_s: float = 30.0):
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def web_search(
        self,
        *,
        query: str,
        country: str = "TW",
        lang: str = "zh-hant",
        count: int = 5,
    ) -> list[dict[str, Any]]:
        r = await self._client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self._api_key,
            },
            params={
                "q": query,
                "country": country,
                "search_lang": lang,
                "count": str(count),
                "safesearch": "moderate",
                "text_decorations": "false",
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
        return results
