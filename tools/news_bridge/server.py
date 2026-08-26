"""Local news bridge for the A-share AI Trader research loop.

Serves plain JSON in the shape expected by llm_advisor.NewsFeedClient
(a_share_ai_trader/llm_advisor.py `_parse_json`):

    GET /news?query=600690 海尔智家
    -> {"data": [{"title": ..., "content": ..., "url": ..., "date": ...}, ...]}

Upstream: Eastmoney search API (search-api-web.eastmoney.com, JSONP).
The public Bing/Google/Baidu RSS endpoints are unusable from CN networks
(HTML walls / 503), so this bridge converts the working Eastmoney source
into the feed contract the advisor already understands. No auth needed.

Stdlib only: python tools/news_bridge/server.py [--port 8082]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EASTMONEY_URL = "https://search-api-web.eastmoney.com/search/jsonp"

CACHE_TTL_SECONDS = 60.0
UPSTREAM_TIMEOUT_SECONDS = 8.0
USER_AGENT = "a-share-news-bridge/1.0"
MAX_ITEMS = 12

_JSONP_WRAPPER = re.compile(r"^[^(]*\((.*)\)\s*;?\s*$", re.DOTALL)


class NewsError(Exception):
    pass


def _http_get(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Referer": "https://www.eastmoney.com/"},
    )
    with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
        return response.read()


def _strip_jsonp(body: bytes) -> dict:
    text = body.decode("utf-8", errors="replace").strip()
    match = _JSONP_WRAPPER.match(text)
    if match:
        text = match.group(1)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise NewsError("eastmoney: payload is not an object")
    return payload


def _fetch_eastmoney(query: str) -> list[dict]:
    param = {
        "uid": "",
        "keyword": query,
        "type": ["cmsArticleWebOld"],
        "client": "web",
        "clientType": "web",
        "clientVersion": "curr",
        "param": {
            "cmsArticleWebOld": {
                "searchScope": "default",
                "sort": "time",
                "pageIndex": 1,
                "pageSize": MAX_ITEMS,
                "preTag": "",
                "postTag": "",
            }
        },
    }
    url = (
        f"{EASTMONEY_URL}?cb=cb&param="
        + urllib.parse.quote(json.dumps(param, ensure_ascii=False))
    )
    payload = _strip_jsonp(_http_get(url))
    result = payload.get("result") or {}
    items = result.get("cmsArticleWebOld") or []
    articles = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = re.sub(r"<[^>]+>", "", str(item.get("title") or "")).strip()
        if not title:
            continue
        content = re.sub(r"<[^>]+>", "", str(item.get("content") or "")).strip()
        url = str(item.get("url") or "").strip()
        date = str(item.get("date") or "").strip()
        if url:
            articles.append(
                {
                    "title": title[:300],
                    "content": content[:1000],
                    "url": url[:1000],
                    "date": date,
                }
            )
    return articles


class NewsCache:
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, list[dict] | Exception]] = {}

    def get_or_fetch(self, query: str) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            cached = self._items.get(query)
            if cached and now - cached[0] < self._ttl:
                value = cached[1]
                if isinstance(value, Exception):
                    raise value
                return value
        try:
            articles = _fetch_eastmoney(query)
        except Exception as exc:
            with self._lock:
                self._items[query] = (time.monotonic(), exc)
            raise
        with self._lock:
            self._items[query] = (time.monotonic(), articles)
        return articles


CACHE = NewsCache(CACHE_TTL_SECONDS)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "A-Share-News-Bridge/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {"status": "ok", "time": time.time()})
            return
        if parsed.path == "/":
            self._send_json(
                200,
                {"service": "a-share-news-bridge", "usage": "/news?query=600690 海尔智家"},
            )
            return
        if parsed.path != "/news":
            self._send_json(404, {"error": "not found"})
            return
        query = urllib.parse.parse_qs(parsed.query).get("query", [""])[0].strip()
        if not query:
            self._send_json(400, {"error": "missing query"})
            return
        try:
            articles = CACHE.get_or_fetch(query)
        except Exception as exc:
            self._send_json(502, {"error": str(exc)})
            return
        self._send_json(200, {"data": articles})


def main() -> int:
    parser = argparse.ArgumentParser(description="A-share news bridge (JSON -> advisor contract)")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    print(f"news bridge listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
