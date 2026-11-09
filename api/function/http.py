"""Async HTTP client around urllib (stdlib only) and tiny response/helpers."""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional, Union

Body = Union[bytes, str, dict, None]
TIMEOUT = 10.0


class HttpResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body

    @property
    def text(self) -> str:
        try:
            return self.body.decode("utf-8", "replace")
        except Exception:
            return ""

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except Exception:
            return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def _encode_body(body: Body, content_type: str) -> tuple[bytes, str]:
    if body is None:
        return b"", content_type
    if isinstance(body, (dict, list)):
        payload = json.dumps(body).encode("utf-8")
        if "json" not in content_type and "json" not in (content_type or ""):
            content_type = "application/json"
        return payload, content_type
    if isinstance(body, str):
        return body.encode("utf-8"), content_type
    return bytes(body), content_type


def _do_request(method: str, url: str, headers: Mapping[str, str], data: bytes) -> HttpResponse:
    req = urllib.request.Request(url, data=data if data else None, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return HttpResponse(resp.status, resp.read())
    except urllib.error.HTTPError as err:
        try:
            return HttpResponse(err.code, err.read())
        except Exception:
            return HttpResponse(err.code, b"")
    except urllib.error.URLError as err:
        return HttpResponse(0, str(err).encode("utf-8"))


async def request(
    method: str,
    url: str,
    headers: Optional[Mapping[str, str]] = None,
    body: Body = None,
    timeout: float = TIMEOUT,
) -> HttpResponse:
    """Fetch a URL. HTTP errors are returned as responses, never raised."""
    hdrs: Dict[str, str] = dict(headers or {})
    content_type = hdrs.pop("content_type", "application/json" if isinstance(body, (dict, list)) else "")
    if isinstance(body, (dict, list)):
        hdrs.setdefault("content-type", "application/json")
    data, _ = _encode_body(body, content_type)

    def run() -> HttpResponse:
        return _do_request(method.upper(), url, hdrs, data)

    future = asyncio.ensure_future(asyncio.to_thread(run)) if timeout is None else asyncio.ensure_future(
        asyncio.wait_for(asyncio.to_thread(run), timeout)
    )
    # asyncio.wait_for returns the result or raises TimeoutError; handle simply:
    try:
        return await future
    except asyncio.TimeoutError:
        return HttpResponse(0, b"client timeout")


async def get_json(url: str, headers: Optional[Mapping[str, str]] = None, timeout: float = TIMEOUT) -> Any:
    resp = await request("GET", url, headers=headers, timeout=timeout)
    return resp.json()


def join_url(base: str, *parts: str) -> str:
    """Join a base URL and path segments with single slashes: join_url("https://x/v2/", "/keys", "a%2Fb")."""
    url = base.rstrip("/")
    for part in parts:
        url += "/" + str(part).strip("/")
    return url


def quote_key(key: str) -> str:
    return urllib.parse.quote(key, safe="")
