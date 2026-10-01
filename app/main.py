import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.background import BackgroundTask

from .redgifs import RedgifsClient, UpstreamError, parse_gif_id

USER_AGENT = os.getenv(
    "REDGIFS_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
)
# When false, /download never streams through this service (redirect only).
ALLOW_PROXY = os.getenv("ALLOW_PROXY", "true").lower() in ("1", "true", "yes")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

UserOrder = Literal["latest", "oldest", "trending", "top", "top7", "top28"]
SearchOrder = Literal["trending", "latest", "score", "top", "top7", "top28"]
Quality = Literal["hd", "sd", "silent"]

# Fallback order when the requested variant is missing for a gif.
QUALITY_FALLBACK = {"hd": ["hd", "sd"], "sd": ["sd", "hd"], "silent": ["silent", "hd", "sd"]}

_USERNAME = re.compile(r"^[\w.\-]+$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redgifs = RedgifsClient(USER_AGENT)
    app.state.media = httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True)
    yield
    await app.state.redgifs.aclose()
    await app.state.media.aclose()


app = FastAPI(
    title="redgifs-api",
    description="Small intermediary service exposing redgifs.com user feeds and gifs.",
    lifespan=lifespan,
)


@app.exception_handler(UpstreamError)
async def upstream_error_handler(request: Request, exc: UpstreamError):
    # Pass client errors (404 user not found, 400 bad order...) through; anything else is a bad gateway.
    status = exc.status if 400 <= exc.status < 500 else 502
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=status)


@app.exception_handler(httpx.HTTPError)
async def http_error_handler(request: Request, exc: httpx.HTTPError):
    return JSONResponse({"error": {"code": "UpstreamUnavailable", "message": str(exc)}}, status_code=502)


def trim_gif(g: dict) -> dict:
    return {
        "id": g["id"],
        "url": f"https://www.redgifs.com/watch/{g['id']}",
        "user": g.get("userName"),
        "created": g.get("createDate"),
        "description": g.get("description"),
        "duration": g.get("duration"),
        "width": g.get("width"),
        "height": g.get("height"),
        "has_audio": g.get("hasAudio"),
        "tags": g.get("tags") or [],
        "views": g.get("views"),
        "likes": g.get("likes"),
        "urls": {k: v for k, v in (g.get("urls") or {}).items() if k != "html"},
    }


def trim_user(u: dict) -> dict:
    return {
        "name": u.get("name") or u.get("username"),
        "url": f"https://www.redgifs.com/users/{u.get('name') or u.get('username')}",
        "description": u.get("description"),
        "followers": u.get("followers"),
        "gifs": u.get("gifs"),
        "created": u.get("creationtime"),
        "profile_image": u.get("profileImageUrl"),
        "verified": u.get("verified"),
    }


def page_response(data: dict, page: int, user: dict | None = None) -> dict:
    gifs = data.get("gifs") or []
    resp = {
        "page": data.get("page", page),
        "pages": data.get("pages"),
        "total": data.get("total"),
        "gifs": [trim_gif(g) for g in gifs],
    }
    if user is not None:
        resp = {"user": user, **resp}
    return resp


def require_gif_id(ref: str) -> str:
    gif_id = parse_gif_id(ref)
    if not gif_id:
        raise HTTPException(400, "Not a redgifs id or URL")
    return gif_id


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/users/{username}")
async def user_feed(
    request: Request,
    username: str,
    order: UserOrder = "latest",
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    """A user's profile and one page of their gifs."""
    if not _USERNAME.match(username):
        raise HTTPException(400, "Invalid username")
    data = await request.app.state.redgifs.user_feed(username, order, page, limit)
    users = data.get("users") or []
    user = next((u for u in users if (u.get("name") or "").lower() == username.lower()), None)
    return page_response(data, page, trim_user(user) if user else {"name": username})


@app.get("/search")
async def search(
    request: Request,
    q: str | None = None,
    tags: str | None = Query(None, description="Comma-separated tag names"),
    order: SearchOrder = "trending",
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    """Search gifs. Note: promoted slots mean fewer than `limit` results may be returned."""
    data = await request.app.state.redgifs.search(q, tags, order, page, limit)
    return page_response(data, page)


# Declared before /gif/{ref:path} so ".../download" isn't swallowed by the greedy path param.
@app.get("/gif/{ref:path}/download")
async def download(
    request: Request,
    ref: str,
    quality: Quality = "hd",
    proxy: bool = Query(False, description="Stream through this service instead of redirecting to the CDN"),
):
    """Download a gif's video. Redirects to the CDN by default; `proxy=true` streams it as an attachment."""
    gif_id = require_gif_id(ref)
    urls = (await request.app.state.redgifs.gif(gif_id)).get("urls") or {}
    chosen = next((q for q in QUALITY_FALLBACK[quality] if urls.get(q)), None)
    if not chosen:
        raise HTTPException(404, "No video available for this gif")
    media_url = urls[chosen]

    if not proxy:
        return RedirectResponse(media_url, status_code=302)
    if not ALLOW_PROXY:
        raise HTTPException(403, "Proxy downloads are disabled on this instance")

    fwd = {"Range": request.headers["range"]} if "range" in request.headers else {}
    media: httpx.AsyncClient = request.app.state.media
    upstream = await media.send(media.build_request("GET", media_url, headers=fwd), stream=True)
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(502, f"CDN returned {upstream.status_code}")

    filename = gif_id if chosen == "hd" else f"{gif_id}-{chosen}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}.mp4"'}
    for h in ("content-length", "content-range", "accept-ranges", "last-modified", "etag"):
        if h in upstream.headers:
            headers[h] = upstream.headers[h]
    return StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "video/mp4"),
        headers=headers,
        background=BackgroundTask(upstream.aclose),
    )


@app.get("/gif/{ref:path}")
async def gif(request: Request, ref: str):
    """Metadata and media URLs for one gif. `ref` may be an id or a redgifs URL."""
    return trim_gif(await request.app.state.redgifs.gif(require_gif_id(ref)))
