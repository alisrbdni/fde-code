"""Structured JSON logging and a lightweight per-request trace.

One JSON line per request on stdout (warn/error on stderr). Never log full
phone numbers, draft contents, messages or tokens; never name a field `level`
(reserved for the line's own level).
"""
from __future__ import annotations

import json
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_min_level_name = "info"


def set_level(name: str) -> None:
    global _min_level_name
    _min_level_name = name if name in _LEVELS else "info"


def log(level: str, event: str, **fields: Any) -> None:
    if _LEVELS.get(level, 20) < _LEVELS.get(_min_level_name, 20):
        return
    record: Dict[str, Any] = {"ts": round(time.time() * 1000), "level": level, "event": event}
    for key, value in fields.items():
        if value is None:
            continue
        record[key] = value
    line = json.dumps(record, separators=(",", ":"), default=str)
    if level in ("warn", "error"):
        print(line, file=sys.stderr, flush=True)
    else:
        print(line, flush=True)


def new_trace_id() -> str:
    return uuid.uuid4().hex


class Span:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ms: Optional[float] = None
        self.ok: bool = True
        self.note: Optional[str] = None
        self._started = time.monotonic()

    def finish(self, ok: bool = True, note: Optional[str] = None) -> None:
        self.ms = round((time.monotonic() - self._started) * 1000, 1)
        self.ok = ok
        self.note = note


class Trace:
    """Collects fields, spans and the outcome for one request."""

    def __init__(self, trace_id: str, route: str, level: str = "info") -> None:
        self.trace_id = trace_id
        self.route = route
        self.outcome: str = "ok"
        self.level: str = level
        self.fields: Dict[str, Any] = {}
        self.spans: List[Dict[str, Any]] = []
        self._started = time.monotonic()

    def set(self, **fields: Any) -> "Trace":
        self.fields.update({k: v for k, v in fields.items() if v is not None})
        return self

    def span(self, name: str) -> "_SpanCtx":
        return _SpanCtx(self, name)

    def _add_span(self, span: Span) -> None:
        self.spans.append(
            {"name": span.name, "ms": span.ms, "ok": span.ok} | ({"note": span.note} if span.note else {})
        )

    def server_timing(self) -> str:
        parts = []
        for span in self.spans:
            parts.append(f"{span['name']};dur={span['ms']}")
        parts.append(f"total;dur={round((time.monotonic() - self._started) * 1000, 1)}")
        return ", ".join(parts)

    def latency_ms(self) -> float:
        return round((time.monotonic() - self._started) * 1000, 1)

    def end(self, outcome: Optional[str] = None, level: Optional[str] = None, **extra: Any) -> None:
        if outcome:
            self.outcome = outcome
        if level:
            self.level = level
        fields: Dict[str, Any] = {
            "trace_id": self.trace_id,
            "route": self.route,
            "outcome": self.outcome,
            "latency_ms": self.latency_ms(),
        }
        fields.update(self.fields)
        fields.update({k: v for k, v in extra.items() if v is not None})
        if self.spans:
            fields["spans"] = self.spans
        log(self.level, "request", **fields)


class _SpanCtx:
    def __init__(self, trace: Trace, name: str) -> None:
        self.trace = trace
        self.span = Span(name)

    async def __aenter__(self) -> Span:
        return self.span

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None:
            self.span.finish(ok=False, note=str(exc)[:200])
        else:
            self.span.finish()
        self.trace._add_span(self.span)
        return False
