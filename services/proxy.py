"""Async HTTP proxy service — forwards requests to the OpenAI-compatible backend."""

import json
import logging
from typing import Any, AsyncIterator

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# "" — built via chr() to survive markdown rendering
_DATA_PREFIX = chr(100) + chr(97) + chr(116) + chr(97) + chr(58)  # d-a-t-a-:


async def forward_request(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Send a non-streaming POST request and return the parsed JSON response."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            logger.error("Request to upstream failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"Upstream request failed: {exc}")

        if response.status_code != 200:
            logger.error(
                "Upstream returned %s: %s",
                response.status_code,
                response.text[:500],
            )
            raise HTTPException(
                status_code=response.status_code,
                detail=f"Upstream error: {response.text[:500]}",
            )

        try:
            return response.json()
        except Exception as exc:
            logger.error("Failed to parse upstream JSON: %s", exc)
            raise HTTPException(status_code=502, detail="Upstream returned invalid JSON")


async def stream_request(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """
    Send a streaming POST request and yield parsed OpenAI SSE chunks as dicts.
    Skips [DONE] and malformed lines.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    logger.error(
                        "Upstream stream returned %s: %s",
                        response.status_code,
                        body[:500],
                    )
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=f"Upstream error: {body.decode(errors='replace')[:500]}",
                    )

                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    if not line.startswith(_DATA_PREFIX):
                        continue
                    # Strip "" prefix; .strip() handles optional trailing space
                    # IBM ICA sends "{...}" (no space); standard SSE sends " {...}"
                    data = line[len(_DATA_PREFIX):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        logger.warning("Skipping unparseable SSE line: %s", data[:200])
                        continue
        except httpx.RequestError as exc:
            logger.error("Streaming request to upstream failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"Upstream stream failed: {exc}")