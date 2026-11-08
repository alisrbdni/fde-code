"""forge-api: the ASGI entry point, request router and app wiring.

`new()` returns a `Function` whose `handle(scope, receive, send)` is an ASGI 3
app. Every request gets one `Trace` (id from `x-trace-id`, or the call id for
call-scoped routes), one JSON log line, `x-trace-id` + `server-timing`
response headers, and a sample in the shared OpsMetrics actor.

Webhooks the live assistants depend on never answer with an error: on any
exception they fall back to safe defaults (see `_FALLBACKS`).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import re
import time
import urllib.parse
from typing import Any, Callable, Dict, List, Optional

from . import builder, clinic, log as logmod, mcp, signature, tools, webhook
from .clinic import ClinicFlags
from .config import Settings, load_settings, mask_phone, normalize_phone
from .kv import FlagStore, MemoryKV, RestKV
from .log import Trace, log, new_trace_id
from .state import LocalState, RemoteState
from .storage import MemoryStorage, make_storage
from .telnyx_api import FakeTelnyx, TelnyxClient

MAX_BODY = 256 * 1024
UNMETERED_ROUTES = {"health", "ops", "ops_health", "console", "docs"}
AGENT_ROUTE = re.compile(r"^/agents/(ag[0-9a-f]{8})/(dynamic-variables|tools/take-message)$")


def _ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# App: settings + clients
# ---------------------------------------------------------------------------

class App:
    """Settings and the clients built from them. Pinned clients (tests) win."""

    def __init__(
        self,
        settings: Settings,
        kv: Any = None,
        state: Any = None,
        telnyx: Any = None,
        storage: Any = None,
        llm: Any = None,
    ) -> None:
        self._pinned = {"kv": kv, "state": state, "telnyx": telnyx, "storage": storage, "llm": llm}
        self.auto_alarms = True  # local alarm emulation (tests turn it off)
        self._alarm_tasks: set = set()
        self._wire(settings, None)

    def _wire(self, settings: Settings, previous: Optional["App"]) -> None:
        pinned = self._pinned
        prev = previous.__dict__ if previous is not None else {}
        self.settings = settings
        logmod.set_level(settings.log_level)

        if pinned["kv"] is not None:
            self.kv = pinned["kv"]
        elif settings.local_kv:
            self.kv = prev["kv"] if isinstance(prev.get("kv"), MemoryKV) else MemoryKV()
        else:
            self.kv = RestKV(settings.kv_base_url, settings.kv_namespace_id, settings.telnyx_api_key)

        if pinned["state"] is not None:
            self.state = pinned["state"]
        elif settings.local_state:
            self.state = prev["state"] if isinstance(prev.get("state"), LocalState) else LocalState()
        else:
            self.state = RemoteState(settings.state_url, settings.state_token)

        if pinned["storage"] is not None:
            self.storage = pinned["storage"]
        elif settings.local_storage:
            self.storage = prev["storage"] if isinstance(prev.get("storage"), MemoryStorage) else MemoryStorage()
        else:
            self.storage = make_storage(settings)

        if pinned["telnyx"] is not None:
            self.telnyx = pinned["telnyx"]
        elif settings.dry_run:
            self.telnyx = prev["telnyx"] if isinstance(prev.get("telnyx"), FakeTelnyx) else FakeTelnyx()
        else:
            self.telnyx = TelnyxClient(settings.telnyx_api_key, settings.telnyx_api_base)

        # Inference: real whenever a key exists (independent of FORGE_DRY_RUN).
        if pinned["llm"] is not None:
            self.llm = pinned["llm"]
        elif settings.telnyx_api_key:
            self.llm = TelnyxClient(settings.telnyx_api_key, settings.telnyx_api_base)
        else:
            self.llm = self.telnyx if isinstance(self.telnyx, FakeTelnyx) else FakeTelnyx()

        if prev.get("kv") is self.kv and prev.get("flags") is not None:
            self.flags = prev["flags"]
            self.clinic_flags = prev["clinic_flags"]
        else:
            self.flags = FlagStore(self.kv, "flags/global")
            self.clinic_flags = FlagStore(self.kv, "flags/clinic", ClinicFlags)

    def reconfigure(self, overrides: Optional[Dict[str, str]]) -> None:
        """Rebuild clients from settings with console overrides on top of env."""
        snapshot = App.__new__(App)
        snapshot.__dict__.update(self.__dict__)
        self._wire(load_settings(overrides), snapshot)

    @property
    def mode(self) -> Dict[str, Any]:
        s = self.settings
        return {
            "telnyx_api": "dry_run" if isinstance(self.telnyx, FakeTelnyx) else "live",
            "llm": "stub" if isinstance(self.llm, FakeTelnyx) else "telnyx_inference",
            "kv": "memory" if isinstance(self.kv, MemoryKV) else "telnyx_kv",
            "actors": "emulated" if getattr(self.state, "local", False) else "remote",
            "storage": "memory" if getattr(self.storage, "local", False) else "cloud_storage",
            "texml": bool(s.texml_app_id and s.forge_number),
            "clinic_handoff": bool(s.clinic_assistant_id),
        }

    # -- local alarm emulation ----------------------------------------------

    def local_alarm(self, owner: str, at_ms: Optional[int]) -> None:
        """With emulated actors, do what the TS actor alarm does: wake at `at`,
        run the owner's due jobs. A no-op against the real actor host."""
        if not getattr(self.state, "local", False) or not self.auto_alarms or at_ms is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def fire() -> None:
            await asyncio.sleep(max(0.0, (int(at_ms) - _ms()) / 1000.0))
            trace = Trace(new_trace_id(), "alarm")
            try:
                result = await builder.run_due_jobs(self, trace, owner)
                trace.end("ok", ran=result.get("ran"))
            except Exception as exc:  # the alarm never crashes the server
                trace.end("error", "error", error=str(exc)[:160])

        task = loop.create_task(fire())
        self._alarm_tasks.add(task)
        task.add_done_callback(self._alarm_tasks.discard)


# ---------------------------------------------------------------------------
# request / response
# ---------------------------------------------------------------------------

class Request:
    def __init__(self, scope: Dict[str, Any], body: bytes) -> None:
        self.method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "/")
        self.path = path.rstrip("/") or "/"
        raw_qs = scope.get("query_string") or b""
        qs = raw_qs.decode("latin-1") if isinstance(raw_qs, bytes) else str(raw_qs)
        self.query: Dict[str, str] = {k: v for k, v in urllib.parse.parse_qsl(qs, keep_blank_values=True)}
        self.headers: Dict[str, str] = {}
        for name, value in scope.get("headers") or []:
            key = name.decode("latin-1").lower() if isinstance(name, bytes) else str(name).lower()
            val = value.decode("latin-1") if isinstance(value, bytes) else str(value)
            self.headers[key] = val
        self.body = body

    def bearer(self) -> str:
        auth = self.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def json(self) -> Any:
        """Parsed JSON body ({} when empty). Raises ValueError on bad JSON."""
        if not self.body.strip():
            return {}
        return json.loads(self.body.decode("utf-8"))

    def json_lenient(self) -> Any:
        try:
            return self.json()
        except Exception:
            return {}


class Response:
    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        headers: Optional[Dict[str, str]] = None,
        content_type: str = "application/json",
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        if body is None:
            self.raw = b""
        elif isinstance(body, bytes):
            self.raw = body
        elif isinstance(body, str):
            self.raw = body.encode("utf-8")
        else:
            self.raw = json.dumps(body, default=str).encode("utf-8")
        if self.raw or status != 202:
            self.headers.setdefault("content-type", content_type)


def json_error(status: int, message: str, **extra: Any) -> Response:
    return Response(status, {"error": message, **extra})


def _token_ok(expected: str, req: Request, allow_query: bool = False) -> bool:
    """No expected token = open (local dev). Constant-time compare."""
    if not expected:
        return True
    given = req.bearer()
    if not given and allow_query:
        given = req.query.get("token", "")
    return bool(given) and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------

def route_name(req: Request) -> str:
    p = req.path
    if p in ("/", "/health"):
        return "health"
    if p == "/webhook/dynamic-variables":
        return "forge_variables"
    if p == "/tools/check-draft":
        return "check_draft"
    if p == "/tools/build-agent":
        return "build_agent"
    if p == "/mcp":
        return "mcp"
    if p == "/clinic/mcp":
        return "clinic_mcp"
    if p == "/clinic/webhook/dynamic-variables":
        return "clinic_variables"
    if p == "/clinic/tools/triage":
        return "clinic_triage"
    if p == "/clinic/tools/book":
        return "clinic_book"
    match = AGENT_ROUTE.match(p)
    if match:
        return "agent_variables" if match.group(2) == "dynamic-variables" else "take_message"
    if p == "/internal/jobs":
        return "internal_jobs"
    if p == "/ops/health":
        return "ops_health"
    if p.startswith("/ops/"):
        return "ops"
    if p in ("/ui", "/favicon.ico") or p.startswith("/ui/"):
        return "console"
    if p in ("/docs", "/openapi.json"):
        return "docs"
    return "not_found"


def _call_trace_id(body: Any) -> str:
    """Call-scoped routes use the call id as trace id (correlates one call)."""
    payload = webhook._payload(body)
    for key in ("call_control_id", "call_id"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, str) and value and "{{" not in value:
            return value
    return ""


# Fallbacks for the webhooks the assistants depend on mid-call.
_FALLBACKS: Dict[str, Callable[[Trace], Dict[str, Any]]] = {
    "forge_variables": lambda t: {"dynamic_variables": {**webhook.forge_defaults(t.trace_id), "degraded": "true"}},
    "agent_variables": lambda t: {"dynamic_variables": {"is_owner": "false", "owner_line": ""}},
    "check_draft": lambda t: {"draft_complete": "false", "build_status": "failed", "message": "The draft is saved."},
    "build_agent": lambda t: {"build_status": "failed", "built_agent_name": "", "message": "The draft is saved."},
    "clinic_variables": lambda t: {"dynamic_variables": {**clinic.clinic_defaults(t.trace_id), "degraded": "true"}},
    "clinic_triage": lambda t: clinic._triage_fallback(),
    "clinic_book": lambda t: clinic._book_fallback(),
}


class Function:
    """The ASGI app. `overrides` hold console settings (memory only)."""

    def __init__(self, app: Optional[App] = None) -> None:
        self.app = app
        self.overrides: Dict[str, str] = {}

    def get_app(self) -> App:
        if self.app is None:
            self.app = App(load_settings(self.overrides))
        return self.app

    async def handle(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope.get("type") == "lifespan":
            while True:
                message = await receive()
                if message.get("type") == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message.get("type") == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope.get("type") != "http":
            return

        chunks: List[bytes] = []
        size = 0
        too_big = False
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                break
            chunk = message.get("body") or b""
            size += len(chunk)
            if size > MAX_BODY:
                too_big = True
            elif chunk:
                chunks.append(chunk)
            if not message.get("more_body"):
                break
        req = Request(scope, b"".join(chunks))
        name = route_name(req)
        trace = Trace(req.headers.get("x-trace-id") or new_trace_id(), name)
        trace.set(method=req.method)

        if too_big:
            resp = json_error(413, "request body too large")
        else:
            resp = await self._dispatch(req, trace)

        if resp.status >= 500:
            outcome, level = "error", "error"
        elif resp.status >= 400:
            outcome, level = "client_error", "info"
        elif trace.outcome == "degraded":
            outcome, level = "degraded", "warn"
        else:
            outcome, level = trace.outcome or "ok", "info"
        if outcome == "error" and trace.level != "error":
            level = "error"

        headers = dict(resp.headers)
        headers["x-trace-id"] = trace.trace_id
        headers["server-timing"] = trace.server_timing()
        await send(
            {
                "type": "http.response.start",
                "status": resp.status,
                "headers": [(k.encode("latin-1"), str(v).encode("latin-1")) for k, v in headers.items()],
            }
        )
        await send({"type": "http.response.body", "body": resp.raw})

        latency = trace.latency_ms()
        trace.end(outcome, level, status=resp.status)
        if name not in UNMETERED_ROUTES:
            try:
                await self.get_app().state.call(
                    "ops", "global", "record", name, outcome, round(latency), _ms(), trace.trace_id,
                    trace.fields.get("error"), trace_id=trace.trace_id,
                )
            except Exception as exc:
                log("warn", "ops.record_failed", route=name, error=str(exc)[:160])

    async def _dispatch(self, req: Request, trace: Trace) -> Response:
        name = trace.route
        try:
            app = self.get_app()
            return await self.route(app, req, trace)
        except Exception as exc:
            trace.set(error=f"{type(exc).__name__}: {str(exc)[:160]}")
            if name in _FALLBACKS:
                trace.outcome = "degraded"
                log("error", "route.fallback", route=name, trace_id=trace.trace_id, error=str(exc)[:200])
                return Response(200, _FALLBACKS[name](trace))
            log("error", "route.crashed", route=name, trace_id=trace.trace_id, error=str(exc)[:200])
            return json_error(500, "internal error", trace_id=trace.trace_id)

    # -- the route table ------------------------------------------------------

    async def route(self, app: App, req: Request, trace: Trace) -> Response:
        p, m, s = req.path, req.method, app.settings
        name = trace.route

        if name == "health" and m == "GET":
            return Response(200, {"ok": True, "service": s.service_name, "dry_run": s.dry_run})

        # Forge ---------------------------------------------------------------
        if name in ("forge_variables", "clinic_variables"):
            if m != "POST":
                return json_error(405, "method not allowed")
            ok, why = signature.check_webhook_signature(req.headers, req.body, s.telnyx_public_key, s.verify_signatures)
            if not ok:
                trace.set(signature=why)
                return json_error(401, "invalid signature")
            body = req.json_lenient()
            trace.trace_id = _call_trace_id(body) or trace.trace_id
            if name == "forge_variables":
                return Response(200, await webhook.forge_variables(app, trace, body))
            return Response(200, await clinic.variables(app, trace, body))

        if name in ("check_draft", "build_agent", "clinic_triage", "clinic_book"):
            if m != "POST":
                return json_error(405, "method not allowed")
            if not _token_ok(s.mcp_token, req, allow_query=True):
                return json_error(401, "unauthorized")
            try:
                body = req.json()
            except ValueError:
                return json_error(400, "invalid JSON")
            if not isinstance(body, dict):
                return json_error(400, "expected a JSON object")
            trace.trace_id = _call_trace_id(body) or trace.trace_id
            handler = {
                "check_draft": webhook.check_draft,
                "build_agent": webhook.build_agent,
                "clinic_triage": clinic.triage_webhook,
                "clinic_book": clinic.book_webhook,
            }[name]
            result = await handler(app, trace, body)
            trace.set(**{k: result.get(k) for k in ("build_status", "draft_complete", "triage_urgency", "booking_status") if result.get(k) is not None})
            return Response(200, result)

        if name in ("mcp", "clinic_mcp"):
            if m != "POST":
                return Response(405, {"error": "use POST (stateless Streamable HTTP, JSON responses)"}, {"allow": "POST"})
            if not _token_ok(s.mcp_token, req, allow_query=True):
                return json_error(401, "unauthorized")
            server = tools.FORGE if name == "mcp" else clinic.SERVER
            status, raw, headers = await mcp.handle_http(app, req.body, trace, server)
            return Response(status, raw, headers)

        if name in ("agent_variables", "take_message"):
            if m != "POST":
                return json_error(405, "method not allowed")
            key = AGENT_ROUTE.match(p).group(1)  # type: ignore[union-attr]
            body = req.json_lenient() if name == "agent_variables" else None
            if name == "agent_variables":
                trace.trace_id = _call_trace_id(body) or trace.trace_id
                return Response(200, await webhook.agent_variables(app, trace, key, body))
            try:
                body = req.json()
            except ValueError:
                return json_error(400, "invalid JSON")
            trace.trace_id = _call_trace_id(body) or trace.trace_id
            try:
                return Response(200, await webhook.take_message(app, trace, key, body, req.headers.get("authorization")))
            except webhook.WebhookError as exc:
                return json_error(exc.status, exc.message)

        # Internal (actor alarms) ---------------------------------------------
        if name == "internal_jobs":
            if m != "POST":
                return json_error(405, "method not allowed")
            if not s.state_token and not s.local_state:
                return json_error(500, "STATE_TOKEN is not configured")
            if not _token_ok(s.state_token, req):
                return json_error(401, "unauthorized")
            try:
                body = req.json()
            except ValueError:
                return json_error(400, "invalid JSON")
            jobs = body.get("jobs") if isinstance(body, dict) else None
            if not isinstance(jobs, list):
                return json_error(400, "expected {\"jobs\": [...]}")
            results = await builder.run_jobs(app, trace, jobs)
            trace.set(jobs=len(jobs), failed=sum(1 for r in results if not r.get("ok")))
            return Response(200, {"results": results})

        # Ops -------------------------------------------------------------------
        if name == "ops_health" and m == "GET":
            return await self._ops_health(app, trace)
        if name == "ops":
            if not _token_ok(s.admin_token, req):
                return json_error(401, "unauthorized")
            return await self._ops(app, req, trace)

        # Console + docs ----------------------------------------------------------
        if name == "console":
            from . import ui_api

            return await ui_api.handle(self, app, req, trace)
        if name == "docs" and m == "GET":
            from . import openapi

            if p == "/openapi.json":
                return Response(200, openapi.spec(s.public_api_url))
            return Response(200, openapi.DOCS_HTML, content_type="text/html; charset=utf-8")

        return json_error(404, "not found")

    # -- ops ---------------------------------------------------------------------

    async def _ops_health(self, app: App, trace: Trace) -> Response:
        checks: Dict[str, Any] = {}
        healthy = True
        try:
            async with trace.span("kv.probe"):
                stamp = str(_ms())
                await app.kv.put("health/probe", stamp, ttl_secs=300)
                got = await app.kv.get("health/probe")
            checks["kv"] = {"ok": got == stamp, "mode": app.mode["kv"]}
            healthy &= got == stamp
        except Exception as exc:
            checks["kv"] = {"ok": False, "error": str(exc)[:160]}
            healthy = False
        metrics: Dict[str, Any] = {}
        try:
            async with trace.span("actor ops.snapshot"):
                metrics = await app.state.call("ops", "global", "snapshot", 5, _ms(), trace_id=trace.trace_id)
            checks["actors"] = {"ok": True, "mode": app.mode["actors"]}
        except Exception as exc:
            checks["actors"] = {"ok": False, "error": str(exc)[:160]}
            healthy = False
        checks["telnyx"] = {"ok": True, "mode": app.mode["telnyx_api"], "llm": app.mode["llm"]}
        try:
            async with trace.span("storage.probe"):
                await app.storage.probe()
            checks["storage"] = {"ok": True, "mode": app.mode["storage"]}
        except Exception as exc:  # reported, never fails health
            checks["storage"] = {"ok": False, "error": str(exc)[:160], "affects_health": False}
        requests = int(metrics.get("requests") or 0)
        error_rate = float(metrics.get("error_rate") or 0)
        if requests >= 5 and error_rate > 0.2:
            healthy = False
        body = {
            "ok": healthy,
            "service": app.settings.service_name,
            "checks": checks,
            "last_5_min": {"requests": requests, "error_rate": error_rate,
                           "latency_p95_ms": metrics.get("latency_p95_ms", 0)},
        }
        if not healthy:
            trace.outcome = "degraded"
        return Response(200 if healthy else 503, body)

    async def _ops(self, app: App, req: Request, trace: Trace) -> Response:
        p, m = req.path, req.method
        if p == "/ops/metrics" and m == "GET":
            try:
                window = int(req.query.get("window") or 5)
            except ValueError:
                window = 5
            window = min(max(window, 1), 120)
            snap = await app.state.call("ops", "global", "snapshot", window, _ms(), trace_id=trace.trace_id)
            return Response(200, snap)
        if p == "/ops/flags":
            if m == "POST":
                try:
                    body = req.json()
                except ValueError:
                    return json_error(400, "invalid JSON")
                if not isinstance(body, dict):
                    return json_error(400, "expected a JSON object")
                forge_patch = body.get("forge") if isinstance(body.get("forge"), dict) else {
                    k: v for k, v in body.items() if k not in ("forge", "clinic")
                }
                if forge_patch:
                    await app.flags.update(forge_patch)
                if isinstance(body.get("clinic"), dict):
                    await app.clinic_flags.update(body["clinic"])
                trace.set(flags_changed=sorted(list(forge_patch) + list(body.get("clinic") or {})))
            forge_flags, forge_src = await app.flags.get(trace)
            clinic_flags, clinic_src = await app.clinic_flags.get(trace)
            return Response(200, {
                "forge": forge_flags.to_dict(), "forge_source": forge_src,
                "clinic": clinic_flags.to_dict(), "clinic_source": clinic_src,
            })
        if p.startswith("/ops/builder/") and m == "GET":
            phone = normalize_phone(urllib.parse.unquote(p[len("/ops/builder/"):]))
            if not phone:
                return json_error(400, "expected a phone number")
            snap = await app.state.call("builder", phone, "snapshot", trace_id=trace.trace_id)
            return Response(200, mask_profile(snap.get("profile") or {}))
        return json_error(404, "not found")


def mask_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    """A BuilderProfile snapshot safe to show: phones masked, no call binding."""
    out = json.loads(json.dumps(profile, default=str))
    out.pop("attested", None)
    out["owner"] = mask_phone(out.get("owner"))
    for job in out.get("jobs") or []:
        job["owner"] = mask_phone(job.get("owner"))
    for msg in out.get("messages") or []:
        msg["from"] = mask_phone(msg.get("from"))
        msg["callback_number"] = mask_phone(msg.get("callback_number"))
    return out


def new() -> Function:
    return Function()
