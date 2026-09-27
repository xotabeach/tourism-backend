"""The home-lab model through the tunnel (spec 16, section 1).

LM Studio on the owner's PC is reachable on the production server's
loopback; the editor's machine forwards it with
``ssh -N -L 11234:127.0.0.1:1234 crimeatrip-prod``. The API key sits in
``$EDITORIAL_WORK_DIR/.lmstudio_key`` (mode 600) and is never printed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from typing import Any

import httpx
import state

BASE_URL = os.environ.get("EDITORIAL_LLM_URL", "http://127.0.0.1:11234/v1")
MODEL = os.environ.get("EDITORIAL_LLM_MODEL", "gemma-4-26b-it")
_JSON = re.compile(r"\{.*\}", re.S)


def _mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class ModelUnavailable(RuntimeError):
    """The PC or the tunnel is down: stop the run, resume later."""


def _key() -> str:
    return (state.WORK_DIR / ".lmstudio_key").read_text().strip()


async def chat_json(
    client: httpx.AsyncClient,
    *,
    system: str,
    user: str,
    images: list[bytes] | None = None,
    max_tokens: int = 900,
) -> dict[str, Any]:
    """One request answering a JSON object; retried on transient failures."""
    content: Any = user
    if images:
        content = [{"type": "text", "text": user}] + [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{_mime(i)};base64," + base64.b64encode(i).decode()},
            }
            for i in images
        ]
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",
    }
    last: Exception | None = None
    for attempt in range(4):
        try:
            response = await client.post(
                f"{BASE_URL}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {_key()}"},
                timeout=300,
            )
            if response.status_code >= 500:
                raise httpx.HTTPStatusError("server", request=response.request, response=response)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"] or ""
            match = _JSON.search(text)
            if not match:
                raise ValueError(f"no JSON in answer: {text[:200]}")
            raw = re.sub(r",\s*([}\]])", r"\1", match.group(0))
            return json.loads(raw)
        except httpx.TransportError as exc:
            last = exc
            await asyncio.sleep(5 * (attempt + 1))
        except (httpx.HTTPStatusError, ValueError, json.JSONDecodeError) as exc:
            last = exc
            await asyncio.sleep(2)
    if isinstance(last, httpx.TransportError):
        raise ModelUnavailable(str(last))
    raise ValueError(str(last))
