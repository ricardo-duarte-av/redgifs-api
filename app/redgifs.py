"""Thin async client for the (unofficial) api.redgifs.com backend used by the website."""

import asyncio
import base64
import json
import logging
import re
import time

import httpx

API_BASE = "https://api.redgifs.com"

log = logging.getLogger(__name__)

# Matches gif ids in watch/embed URLs (redgifs.com/watch/<id>, /ifr/<id>, /i/<id>)
# and in CDN URLs (media.redgifs.com/<CamelCaseId>[-mobile|-silent|...].mp4).
_URL_ID = re.compile(r"redgifs\.com/(?:watch|ifr|i)/([A-Za-z]+)")
_MEDIA_ID = re.compile(r"media\.redgifs\.com/([A-Za-z]+)")
_BARE_ID = re.compile(r"^[A-Za-z]+$")


def parse_gif_id(ref: str) -> str | None:
    """Accept a bare gif id or any redgifs watch/embed/media URL; return the lowercase id."""
    ref = ref.strip()
    if _BARE_ID.match(ref):
        return ref.lower()
    m = _URL_ID.search(ref) or _MEDIA_ID.search(ref)
    return m.group(1).lower() if m else None


class UpstreamError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


class RedgifsClient:
    """Holds an anonymous API token and refreshes it on expiry or 401.

    The token is bound to the client's IP and User-Agent, so the same
    User-Agent must be used for every request made with it.
    """

    def __init__(self, user_agent: str, timeout: float = 20.0):
        self.http = httpx.AsyncClient(
            base_url=API_BASE,
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _get_token(self, stale: str | None = None) -> str:
        async with self._lock:
            # Another request may already have refreshed the token we saw fail.
            needs_refresh = (
                self._token is None
                or time.time() > self._expires - 300
                or (stale is not None and self._token == stale)
            )
            if needs_refresh:
                r = await self.http.get("/v2/auth/temporary")
                r.raise_for_status()
                self._token = r.json()["token"]
                self._expires = _jwt_exp(self._token) or time.time() + 3600
                log.info("obtained new redgifs token, expires at %d", self._expires)
            return self._token

    async def get(self, path: str, params: dict | None = None) -> dict:
        token = await self._get_token()
        r = await self.http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            token = await self._get_token(stale=token)
            r = await self.http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
                message = err.get("message") or err.get("description") or r.text
                raise UpstreamError(r.status_code, err.get("code", "Error"), message)
            except (ValueError, KeyError, TypeError):
                raise UpstreamError(r.status_code, "UpstreamError", r.text[:500])
        return r.json()

    async def gif(self, gif_id: str) -> dict:
        return (await self.get(f"/v2/gifs/{gif_id}"))["gif"]

    async def user_feed(self, username: str, order: str, page: int, count: int) -> dict:
        return await self.get(
            f"/v2/users/{username}/search",
            {"order": order, "page": page, "count": count},
        )

    async def search(self, text: str | None, tags: str | None, order: str, page: int, count: int) -> dict:
        params: dict = {"order": order, "page": page, "count": count}
        if text:
            params["search_text"] = text
        if tags:
            params["tags"] = tags
        return await self.get("/v2/gifs/search", params)
