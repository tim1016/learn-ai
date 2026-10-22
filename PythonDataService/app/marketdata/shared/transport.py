"""Authenticated bounded reads; transport failure never falls back to another source."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from pydantic import ValidationError

from app.marketdata.shared.protocol import FeedUnavailable, Frame, Subscription

MAX_FRAME_BYTES = 262_144


class FeedTransport:
    def __init__(self, url: str, token: str) -> None:
        if not token:
            raise ValueError("Shared feed token is required")
        self._url, self._token = url.rstrip("/"), token

    async def stream(self, subscription: Subscription) -> AsyncIterator[Frame]:
        timeout = httpx.Timeout(5, read=5)
        try:
            async with (
                httpx.AsyncClient(timeout=timeout, trust_env=False) as client,
                client.stream(
                    "POST",
                    self._url + "/stream",
                    json=subscription.model_dump(),
                    headers={"Authorization": f"Bearer {self._token}"},
                ) as response,
            ):
                response.raise_for_status()
                pending = b""
                async for chunk in response.aiter_bytes():
                    if len(chunk) > MAX_FRAME_BYTES:
                        raise FeedUnavailable("Shared feed chunk exceeds its bound")
                    pending += chunk
                    while b"\n" in pending:
                        raw, pending = pending.split(b"\n", 1)
                        if len(raw) > MAX_FRAME_BYTES:
                            raise FeedUnavailable("Shared feed frame exceeds its bound")
                        frame = Frame.model_validate_json(raw)
                        yield frame
                        if frame.kind == "error":
                            return
                    if len(pending) > MAX_FRAME_BYTES:
                        raise FeedUnavailable("Shared feed frame exceeds its bound")
                raise FeedUnavailable("Shared feed connection ended")
        except (httpx.HTTPError, ValidationError) as exc:
            # Do not include response/request objects: they carry auth headers.
            raise FeedUnavailable(f"Shared feed unavailable ({type(exc).__name__})") from exc
