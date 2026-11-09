"""Telnyx KV REST client, in-memory KV, feature flags and a cache-aside helper.

KV keys use `/` and never `:`. The variables webhooks and console depend on the
KV REST API from Python; the in-memory implementation mirrors it for local mode
and tests (including TTL enforcement).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, fields as dc_fields
from typing import Any, Callable, Dict, List, Optional, Tuple

from .log import log
from .http import HttpResponse, join_url, quote_key, request


class KVError(Exception):
    pass


class KVBackend:
    async def get(self, key: str) -> Optional[str]:
        raise NotImplementedError

    async def put(self, key: str, value: str, ttl_secs: Optional[int] = None) -> None:
        raise NotImplementedError

    async def delete(self, key: str) -> None:
        raise NotImplementedError


class RestKV(KVBackend):
    """Telnyx KV storage over the v2 REST API."""

    def __init__(self, base_url: str, namespace_id: str, api_key: str) -> None:
        self.base = join_url(base_url, namespace_id)
        self.api_key = api_key

    @property
    def _headers(self) -> Dict[str, str]:
        return {"authorization": f"Bearer {self.api_key}", "content-type": "application/json"}

    async def get(self, key: str) -> Optional[str]:
        url = join_url(self.base, "keys", quote_key(key))
        resp: HttpResponse = await request("GET", url, headers=self._headers)
        if resp.status == 404:
            return None
        if not resp.ok:
            raise KVError(f"kv get {key} -> {resp.status} {resp.text[:120]}")
        # GET returns the stored bytes as-is (what put() sent).
        return resp.text

    async def put(self, key: str, value: str, ttl_secs: Optional[int] = None) -> None:
        url = join_url(self.base, "keys", quote_key(key))
        if ttl_secs:
            url += f"?ttl_secs={int(ttl_secs)}"
        # The KV REST API stores the request body verbatim (no JSON envelope), like the CLI does.
        headers = {**self._headers, "content-type": "text/plain; charset=utf-8"}
        resp = await request("PUT", url, headers=headers, body=value.encode("utf-8"))
        if not resp.ok:
            raise KVError(f"kv put {key} -> {resp.status} {resp.text[:120]}")

    async def delete(self, key: str) -> None:
        url = join_url(self.base, "keys", quote_key(key))
        resp = await request("DELETE", url, headers=self._headers)
        if resp.status == 404 or resp.ok:
            return
        raise KVError(f"kv delete {key} -> {resp.status} {resp.text[:120]}")


class MemoryKV(KVBackend):
    """Local emulator of the KV API, with TTL and outage simulation."""

    def __init__(self) -> None:
        self.store: Dict[str, Tuple[str, Optional[float]]] = {}
        self.fail = False

    async def get(self, key: str) -> Optional[str]:
        if self.fail:
            raise KVError("kv outage")
        self._check_key(key)
        entry = self.store.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires is not None and expires <= time.monotonic():
            del self.store[key]
            return None
        return value

    async def put(self, key: str, value: str, ttl_secs: Optional[int] = None) -> None:
        if self.fail:
            raise KVError("kv outage")
        self._check_key(key)
        expires = time.monotonic() + ttl_secs if ttl_secs else None
        self.store[key] = (value, expires)

    async def delete(self, key: str) -> None:
        if self.fail:
            raise KVError("kv outage")
        self._check_key(key)
        self.store.pop(key, None)

    def _check_key(self, key: str) -> None:
        if ":" in key:
            raise KVError(f"kv key must not contain ':' {key!r}")


@dataclass
class Flags:
    """`flags/global` – forge feature flags (Appendix A.5)."""

    builds_enabled: bool = True
    max_agents_per_caller: int = 3
    agent_ttl_hours: int = 24
    max_test_calls_per_hour: int = 3
    test_call_delay_secs: int = 20
    llm_polish: bool = True
    require_attestation_for_private: bool = True
    maintenance_message: str = (
        "Agent building is paused for maintenance right now. Your draft is saved, "
        "so please call back in a little while."
    )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Flags":
        parsed: Dict[str, Any] = {}
        for f in dc_fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if isinstance(value, bool):
                parsed[f.name] = value
            elif f.name == "maintenance_message":
                parsed[f.name] = str(value)
            else:
                try:
                    parsed[f.name] = int(value)
                except (TypeError, ValueError):
                    continue
        result = cls(**parsed)
        result.clamp()
        return result

    def clamp(self) -> None:
        self.max_agents_per_caller = min(max(self.max_agents_per_caller, 1), 20)
        self.agent_ttl_hours = min(max(self.agent_ttl_hours, 1), 720)
        self.max_test_calls_per_hour = min(max(self.max_test_calls_per_hour, 0), 20)
        self.test_call_delay_secs = min(max(self.test_call_delay_secs, 5), 3600)
        if not isinstance(self.builds_enabled, bool):
            self.builds_enabled = True
        if not isinstance(self.llm_polish, bool):
            self.llm_polish = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "builds_enabled": self.builds_enabled,
            "max_agents_per_caller": self.max_agents_per_caller,
            "agent_ttl_hours": self.agent_ttl_hours,
            "max_test_calls_per_hour": self.max_test_calls_per_hour,
            "test_call_delay_secs": self.test_call_delay_secs,
            "llm_polish": self.llm_polish,
            "require_attestation_for_private": self.require_attestation_for_private,
            "maintenance_message": self.maintenance_message,
        }


class FlagStore:
    """Read-through flag store with a 5 s memo; fails open to defaults."""

    def __init__(self, kv: KVBackend, key: str = "flags/global", cls: type = Flags) -> None:
        self.kv = kv
        self.key = key
        self.cls = cls
        self._memo: Optional[Flags] = None
        self._memo_at = 0.0

    async def get(self, trace=None) -> Tuple[Any, str]:
        now = time.monotonic()
        if self._memo is not None and now - self._memo_at < 5.0:
            return self._memo, "memo"
        raw = None
        try:
            if trace is not None:
                async with trace.span("kv.get flags"):
                    raw = await self.kv.get(self.key)
            else:
                raw = await self.kv.get(self.key)
        except Exception as exc:
            log("warn", "flags.read_failed", key=self.key, error=str(exc))
            return self.cls(), "default_on_error"
        if raw is None:
            return self.cls(), "default"
        try:
            import json

            data = json.loads(raw)
        except Exception:
            data = {}
        flags = self.cls.from_dict(data if isinstance(data, dict) else {})
        self._memo, self._memo_at = flags, now
        return flags, "kv"

    async def update(self, patch: Dict[str, Any]) -> Any:
        import json

        current, _ = await self.get()
        data = current.to_dict()
        for name, value in patch.items():
            if name in data:
                data[name] = value
        flags = self.cls.from_dict(data)
        flags.clamp()
        payload = json.dumps(flags.to_dict())
        try:
            await self.kv.put(self.key, payload)
        finally:
            self._memo, self._memo_at = flags, time.monotonic()
        return flags


async def cached(kv: KVBackend, trace, key: str, ttl: int, load: Callable[[], Any]) -> Tuple[Any, bool]:
    """Cache-aside: never a dependency — read/write errors are logged and swallowed."""
    import json

    async def read():
        return await kv.get(key)

    async def write(value: Any):
        if value is None:
            await kv.put(key, json.dumps({"none": True}), ttl_secs=ttl)
        else:
            await kv.put(key, json.dumps({"v": value}), ttl_secs=ttl)

    if trace is not None:
        async with trace.span("kv.get cache"):
            raw = await _safe(read)
    else:
        raw = await _safe(read)
    if raw is not None:
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("none"):
                return None, True
            return data.get("v") if isinstance(data, dict) else None, True
        except Exception:
            pass
    if trace is not None:
        async with trace.span("backend cache"):
            value = await load()
    else:
        value = await load()
    await _safe(lambda: write(value))
    return value, False


async def _safe(op):
    try:
        return await op()
    except Exception as exc:
        log("warn", "cache.kv_error", error=str(exc))
        return None
