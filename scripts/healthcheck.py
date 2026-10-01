#!/usr/bin/env python3
"""End-to-end checks against a running redgifs-api instance.

Usage: BASE_URL=https://... python3 scripts/healthcheck.py [report.md]

Prints a Markdown report and exits non-zero if any check fails. The base URL is
never printed: it is scrubbed from all output so the report can be posted publicly.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
TEST_USER = os.environ.get("TEST_USER", "susanna")
TIMEOUT = 30


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


opener = urllib.request.build_opener(NoRedirect)


def scrub(text: str) -> str:
    # Also scrub the bare hostname: TLS and DNS errors mention it without the scheme.
    for secret in (BASE_URL, urllib.parse.urlsplit(BASE_URL).hostname):
        if secret:
            text = text.replace(secret, "<instance>")
    return text


def request(path: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "redgifs-api-healthcheck", **(headers or {})})
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_json(path: str, expect: int = 200) -> dict:
    status, _, body = request(path)
    if status != expect:
        raise AssertionError(f"expected HTTP {expect}, got {status}: {body[:200]!r}")
    return json.loads(body)


class Checks:
    def __init__(self):
        self.results: list[tuple[str, bool, str]] = []
        self.gif_id: str | None = None

    def run(self, name: str, fn) -> None:
        start = time.monotonic()
        try:
            detail = fn() or ""
            ok = True
        except Exception as e:
            detail, ok = f"{type(e).__name__}: {e}", False
        elapsed = time.monotonic() - start
        self.results.append((name, ok, scrub(f"{detail} ({elapsed:.2f}s)".strip())))

    def health(self):
        d = get_json("/health")
        assert d.get("status") == "ok", f"unexpected body {d}"

    def user_feed(self):
        d = get_json(f"/users/{TEST_USER}?limit=5")
        assert d["user"]["name"].lower() == TEST_USER.lower(), f"wrong user {d['user']}"
        assert d["gifs"], "feed returned no gifs"
        self.gif_id = d["gifs"][0]["id"]
        return f"{len(d['gifs'])} gifs, total {d['total']}"

    def user_feed_paging(self):
        d = get_json(f"/users/{TEST_USER}?order=top&page=2&limit=5")
        assert d["page"] == 2 and d["gifs"], f"page {d.get('page')}, {len(d.get('gifs', []))} gifs"

    def unknown_user(self):
        d = get_json("/users/zzzz-no-such-user-healthcheck", expect=404)
        assert d["error"]["code"] == "UserNotFound", f"unexpected error {d}"

    def _need_gif(self) -> str:
        if not self.gif_id:
            raise AssertionError("skipped: no gif id from user feed")
        return self.gif_id

    def gif_by_id(self):
        d = get_json(f"/gif/{self._need_gif()}")
        assert d["urls"].get("hd") or d["urls"].get("sd"), f"no video urls: {d['urls']}"

    def gif_by_url(self):
        gif_id = self._need_gif()
        d = get_json(f"/gif/https://www.redgifs.com/watch/{gif_id}")
        assert d["id"] == gif_id, f"resolved to {d['id']}"

    def download_redirect(self):
        status, headers, _ = request(f"/gif/{self._need_gif()}/download?quality=sd")
        location = headers.get("location") or headers.get("Location") or ""
        assert status == 302, f"expected HTTP 302, got {status}"
        assert location.startswith("https://media.redgifs.com/"), f"unexpected location {location}"

    def download_proxy(self):
        status, headers, body = request(f"/gif/{self._need_gif()}/download?proxy=true", {"Range": "bytes=0-1023"})
        if status == 403:
            return "proxy disabled on this instance, skipped"
        assert status == 206, f"expected HTTP 206, got {status}"
        assert len(body) == 1024, f"got {len(body)} bytes"
        assert body[4:8] == b"ftyp", "response is not an mp4"

    def search(self):
        d = get_json("/search?tags=Amateur&order=latest&limit=10")
        assert d["gifs"], "search returned no gifs"
        return f"{len(d['gifs'])} gifs"


def main() -> int:
    if not BASE_URL:
        print("BASE_URL is not set", file=sys.stderr)
        return 2

    c = Checks()
    for name, fn in [
        ("health", c.health),
        ("user feed", c.user_feed),
        ("user feed paging", c.user_feed_paging),
        ("unknown user 404", c.unknown_user),
        ("gif by id", c.gif_by_id),
        ("gif by URL", c.gif_by_url),
        ("download redirect", c.download_redirect),
        ("download proxy (range)", c.download_proxy),
        ("search", c.search),
    ]:
        c.run(name, fn)

    failed = [r for r in c.results if not r[1]]
    lines = [
        f"**{len(c.results) - len(failed)}/{len(c.results)} checks passed**",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for name, ok, detail in c.results:
        lines.append(f"| {name} | {'✅' if ok else '❌'} | {detail.replace('|', '\\|')} |")
    report = "\n".join(lines) + "\n"

    print(report)
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w") as f:
            f.write(report)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
