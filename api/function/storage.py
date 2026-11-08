"""Object storage for build archives and booking documents.

`S3Storage` talks to any S3-compatible endpoint (Telnyx Cloud Storage) with
path-style URLs and AWS Signature V4 implemented on the standard library.
`MemoryStorage` mirrors the interface for local mode and tests. Storage is
never a dependency: callers log and swallow its failures.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import hmac
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Mapping

from .http import request

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class StorageError(Exception):
    pass


# ---------------------------------------------------------------------------
# AWS Signature Version 4 (stdlib only)
# ---------------------------------------------------------------------------

def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(path: str) -> str:
    return urllib.parse.quote(path, safe="/-_.~")


def _canonical_query(query: str) -> str:
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    encoded = sorted(
        (urllib.parse.quote(k, safe="-_.~"), urllib.parse.quote(v, safe="-_.~")) for k, v in pairs
    )
    return "&".join(f"{k}={v}" for k, v in encoded)


def sign_v4(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    amz_date: str,
    service: str = "s3",
) -> Dict[str, str]:
    """Return the headers to send (input headers + host + Authorization).

    `headers` must already contain `x-amz-date` and `x-amz-content-sha256`;
    every header passed in is signed.
    """
    parsed = urllib.parse.urlsplit(url)
    all_headers = {k.lower(): str(v).strip() for k, v in headers.items()}
    all_headers["host"] = parsed.netloc
    signed = sorted(all_headers)
    canonical_headers = "".join(f"{name}:{re.sub(r'  +', ' ', all_headers[name])}\n" for name in signed)
    signed_headers = ";".join(signed)
    canonical_request = "\n".join(
        [
            method.upper(),
            _uri_encode(parsed.path or "/"),
            _canonical_query(parsed.query),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )
    day = amz_date[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), day)
    key = _hmac(key, region)
    key = _hmac(key, service)
    key = _hmac(key, "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = dict(all_headers)
    out["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return out


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------

class S3Storage:
    """Path-style S3 client: {endpoint}/{bucket}/{key}."""

    local = False

    def __init__(self, endpoint: str, bucket: str, region: str, access_key: str, secret_key: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key

    def _url(self, key: str = "", query: str = "") -> str:
        path = f"/{self.bucket}" + (f"/{key}" if key else "")
        url = self.endpoint + _uri_encode(path)
        return url + (f"?{query}" if query else "")

    async def _send(self, method: str, url: str, body: bytes = b"", content_type: str = "") -> Any:
        amz_date = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {"x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
        signed = sign_v4(method, url, headers, payload_hash, self.access_key, self.secret_key, self.region, amz_date)
        if content_type:
            signed["content-type"] = content_type
        signed.pop("host", None)  # urllib sets it
        return await request(method, url, headers=signed, body=body or None)

    async def put_json(self, key: str, value: Any) -> None:
        body = json.dumps(value, default=str).encode("utf-8")
        resp = await self._send("PUT", self._url(key), body, "application/json")
        if resp.status == 404 and "NoSuchBucket" in resp.text:
            created = await self._send("PUT", self._url())
            if not created.ok and created.status != 409:
                raise StorageError(f"create bucket -> {created.status} {created.text[:120]}")
            resp = await self._send("PUT", self._url(key), body, "application/json")
        if not resp.ok:
            raise StorageError(f"put {key} -> {resp.status} {resp.text[:120]}")

    async def get_json(self, key: str) -> Any:
        resp = await self._send("GET", self._url(key))
        if resp.status == 404:
            return None
        if not resp.ok:
            raise StorageError(f"get {key} -> {resp.status} {resp.text[:120]}")
        return resp.json()

    async def list(self, prefix: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        query = urllib.parse.urlencode({"list-type": "2", "prefix": prefix, "max-keys": str(limit)})
        resp = await self._send("GET", self._url(query=query))
        if resp.status == 404:
            return []
        if not resp.ok:
            raise StorageError(f"list {prefix} -> {resp.status} {resp.text[:120]}")
        return parse_list_xml(resp.text)

    async def probe(self) -> bool:
        await self.list("", 1)
        return True


def parse_list_xml(text: str) -> List[Dict[str, Any]]:
    """Parse a ListObjectsV2 response into [{key, size, modified}]."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    items = []
    for node in root.iter():
        if node.tag.split("}")[-1] != "Contents":
            continue
        entry: Dict[str, Any] = {}
        for child in node:
            tag = child.tag.split("}")[-1]
            if tag == "Key":
                entry["key"] = child.text or ""
            elif tag == "Size":
                entry["size"] = int(child.text or 0)
            elif tag == "LastModified":
                entry["modified"] = child.text or ""
        if entry.get("key"):
            items.append(entry)
    return items


class MemoryStorage:
    """In-memory object store with an outage switch (`fail`)."""

    local = True

    def __init__(self) -> None:
        self.objects: Dict[str, Any] = {}
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise StorageError("storage outage")

    async def put_json(self, key: str, value: Any) -> None:
        self._check()
        self.objects[key] = json.loads(json.dumps(value, default=str))

    async def get_json(self, key: str) -> Any:
        self._check()
        value = self.objects.get(key)
        return copy.deepcopy(value) if value is not None else None

    async def list(self, prefix: str = "", limit: int = 100) -> List[Dict[str, Any]]:
        self._check()
        keys = sorted(k for k in self.objects if k.startswith(prefix))[:limit]
        return [{"key": k, "size": len(json.dumps(self.objects[k]))} for k in keys]

    async def probe(self) -> bool:
        self._check()
        return True


def make_storage(settings: Any) -> Any:
    if settings.local_storage:
        return MemoryStorage()
    return S3Storage(
        settings.storage_endpoint,
        settings.storage_bucket,
        settings.storage_region,
        settings.storage_access_key,
        settings.storage_secret_key,
    )


def redact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of an assistant payload with every webhook Authorization header
    value replaced by `Bearer <redacted>`. Never mutates the input."""
    out = copy.deepcopy(payload)
    for tool in out.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        hook = tool.get("webhook")
        if not isinstance(hook, dict):
            continue
        headers = hook.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() == "authorization":
                    headers[name] = "Bearer <redacted>"
        elif isinstance(headers, list):
            for header in headers:
                if isinstance(header, dict) and str(header.get("name", "")).lower() == "authorization":
                    header["value"] = "Bearer <redacted>"
    return out

