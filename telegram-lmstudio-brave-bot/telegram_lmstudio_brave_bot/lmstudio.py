from __future__ import annotations

import json
from typing import Any

import httpx


class LMStudioClient:
    def __init__(self, base_url: str, timeout_s: float = 60.0):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def chat_completions(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format

        resp = await self._client.post(
            f"{self._base_url}/chat/completions",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def embeddings(self, *, model: str, input_texts: list[str]) -> list[list[float]]:
        resp = await self._client.post(
            f"{self._base_url}/embeddings",
            json={"model": model, "input": input_texts},
        )
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]


async def llm_need_search(
    client: LMStudioClient,
    *,
    model: str,
    user_text: str,
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "need_search": {"type": "boolean"},
            "query": {"type": "string"},
        },
        "required": ["need_search", "query"],
        "additionalProperties": False,
    }

    content = await client.chat_completions(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Decide if up-to-date web search is needed. Reply with JSON only.",
            },
            {"role": "user", "content": user_text},
        ],
        temperature=0.0,
        response_format={"type": "json_schema", "json_schema": {"name": "need_search", "schema": schema}},
    )

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"need_search": False, "query": ""}


async def llm_build_search_query(
    client: LMStudioClient,
    *,
    model: str,
    user_text: str,
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    content = await client.chat_completions(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "Rewrite the user message into a concise web search query. Reply with JSON only.",
            },
            {"role": "user", "content": user_text},
        ],
        temperature=0.0,
        response_format={"type": "json_schema", "json_schema": {"name": "search_query", "schema": schema}},
    )

    try:
        data = json.loads(content)
        q = (data.get("query") or "").strip()
        return {"query": q}
    except json.JSONDecodeError:
        return {"query": ""}
