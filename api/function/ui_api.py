"""The operator console: `GET /ui` (a single static page) and `/ui/api/*`.

Every API route needs ADMIN_TOKEN when it is set, and every response masks
phone numbers. Settings overrides live in `Function.overrides` (memory only):
values are never logged and never written to KV.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Dict, List

from . import clinic, simulator, tools, webhook
from .config import OVERRIDABLE, lookup, mask_phone, mask_secret, normalize_phone, override_values
from .log import log
from .mcp import ToolError

STATIC = Path(__file__).resolve().parent / "static" / "console.html"
CSP = ("default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
CONSOLE_PHONE = "+15550199999"
AGENT_KEY = re.compile(r"^ag[0-9a-f]{8}$")


def _ms() -> int:
    return int(time.time() * 1000)


def _resp(status: int, body: Any, **kw: Any):
    from .func import Response

    return Response(status, body, **kw)


def _err(status: int, message: str):
    return _resp(status, {"error": message})


def _phone(value: Any) -> str:
    return normalize_phone(value) or CONSOLE_PHONE


async def handle(fn: Any, app: Any, req: Any, trace: Any):
    from .func import _token_ok

    p, m = req.path, req.method
    if p == "/favicon.ico":
        return _resp(204, None)
    if p == "/ui" and m == "GET":
        try:
            html = STATIC.read_bytes()
        except OSError:
            html = b"<!doctype html><title>Console</title><p>console.html is missing."
        return _resp(200, html, content_type="text/html; charset=utf-8",
                     headers={"content-security-policy": CSP, "x-frame-options": "DENY",
                              "x-content-type-options": "nosniff", "cache-control": "no-store"})
    if not p.startswith("/ui/api/"):
        return _err(404, "not found")
    if not _token_ok(app.settings.admin_token, req):
        return _err(401, "admin token required")
    sub = p[len("/ui/api/"):]
    try:
        body = req.json() if m == "POST" else {}
    except ValueError:
        return _err(400, "invalid JSON")
    if not isinstance(body, dict):
        return _err(400, "expected a JSON object")
    trace.set(console=sub.split("/")[0])
    try:
        return await _route(fn, app, trace, m, sub, body, req)
    except simulator.SimError as exc:
        return _err(exc.status, exc.message)
    except ToolError as exc:
        return _err(400, str(exc))


async def _route(fn: Any, app: Any, trace: Any, m: str, sub: str, body: Dict[str, Any], req: Any):
    parts = sub.split("/")
    head = parts[0]

    if sub == "overview" and m == "GET":
        return _resp(200, await _overview(fn, app, trace))

    if sub == "settings":
        if m == "POST":
            values = body.get("values")
            if not isinstance(values, dict):
                return _err(400, "expected {\"values\": {NAME: value|null}}")
            unknown = [k for k in values if k not in OVERRIDABLE]
            if unknown:
                return _err(400, f"not overridable: {', '.join(sorted(unknown))}")
            for name, value in values.items():
                if value is None or str(value).strip() == "":
                    fn.overrides.pop(name, None)
                else:
                    fn.overrides[name] = str(value).strip()
            app.reconfigure(fn.overrides)
            log("info", "console.settings_changed", names=sorted(values), trace_id=trace.trace_id)
        return _resp(200, _settings_view(fn, app))

    if head == "flags" and len(parts) == 2 and parts[1] in ("forge", "clinic"):
        store = app.flags if parts[1] == "forge" else app.clinic_flags
        if m == "POST":
            await store.update(body)
        flags, source = await store.get(trace)
        return _resp(200, {"flags": flags.to_dict(), "source": source})

    if head == "flow" and len(parts) == 2 and m == "GET":
        adef = await simulator.get_def(app, trace, parts[1])
        return _resp(200, {
            "id": adef.id, "name": adef.name, "flow": adef.flow, "node_tools": adef.node_tools,
            "defaults": adef.defaults, "instructions": adef.instructions,
            "tools": [{"name": t.name, "kind": t.kind, "description": t.description} for t in adef.tools.values()],
        })

    if head == "chat":
        engine = simulator.Engine(app, trace)
        if sub == "chat/start" and m == "POST":
            s = await engine.start(str(body.get("assistant") or "forge"), _phone(body.get("phone")))
            return _resp(200, await _chat_view(app, trace, s, engine.new_events))
        if sub == "chat/send" and m == "POST":
            s = await simulator.load(app, str(body.get("session_id") or ""))
            s = await engine.send(s, str(body.get("text") or ""))
            return _resp(200, await _chat_view(app, trace, s, engine.new_events))
        if len(parts) == 2 and m == "GET":
            s = await simulator.load(app, parts[1])
            return _resp(200, await _chat_view(app, trace, s, s["events"]))

    if head == "agents":
        if sub == "agents" and m == "GET":
            listed = await app.state.call("registry", "global", "list", 100, trace_id=trace.trace_id)
            return _resp(200, {"agents": [_mask_agent(a) for a in listed.get("agents") or []]})
        if len(parts) >= 2 and AGENT_KEY.match(parts[1]):
            key = parts[1]
            if len(parts) == 2 and m == "GET":
                return _resp(200, await _agent_detail(app, trace, key))
            if len(parts) == 3 and m == "POST" and parts[2] in ("test-call", "delete"):
                return await _agent_action(app, trace, key, parts[2], body)

    if sub == "forge/build" and m == "POST":
        return _resp(200, await _form_build(app, trace, body))

    if head == "owner" and len(parts) == 2 and m == "GET":
        phone = normalize_phone(parts[1])
        if not phone:
            return _err(400, "expected a phone number")
        from .func import mask_profile

        snap = await app.state.call("builder", phone, "snapshot", trace_id=trace.trace_id)
        return _resp(200, mask_profile(snap.get("profile") or {}))

    if head == "clinic":
        return await _clinic(app, trace, m, parts, body)

    return _err(404, "not found")


# ---------------------------------------------------------------------------
# overview + settings
# ---------------------------------------------------------------------------

def _settings_view(fn: Any, app: Any) -> Dict[str, Any]:
    values = override_values(app.settings, fn.overrides)
    rows = []
    for name, meta in OVERRIDABLE.items():
        _, source = lookup(name, fn.overrides)
        value = values.get(name, "")
        rows.append({
            "name": name, "label": meta["label"], "secret": meta["secret"], "source": source,
            "value": mask_secret(value) if meta["secret"] else value, "set": bool(value),
        })
    return {"settings": rows, "overrides": sorted(fn.overrides), "admin_token_set": bool(app.settings.admin_token)}


async def _overview(fn: Any, app: Any, trace: Any) -> Dict[str, Any]:
    forge_flags, forge_src = await app.flags.get(trace)
    clinic_flags, clinic_src = await app.clinic_flags.get(trace)
    base = app.settings.public_api_url
    return {
        "service": app.settings.service_name,
        "mode": app.mode,
        "overrides": sorted(fn.overrides),
        "flags": {"forge": forge_flags.to_dict(), "forge_source": forge_src,
                  "clinic": clinic_flags.to_dict(), "clinic_source": clinic_src},
        "endpoints": {
            "forge_variables": f"{base}/webhook/dynamic-variables",
            "forge_mcp": f"{base}/mcp",
            "check_draft": f"{base}/tools/check-draft",
            "build_agent": f"{base}/tools/build-agent",
            "clinic_variables": f"{base}/clinic/webhook/dynamic-variables",
            "clinic_mcp": f"{base}/clinic/mcp",
            "clinic_triage": f"{base}/clinic/tools/triage",
            "clinic_book": f"{base}/clinic/tools/book",
            "docs": f"{base}/docs",
            "health": f"{base}/ops/health",
        },
        "forge_tools": tools.FORGE.tool_names,
        "clinic_tools": clinic.SERVER.tool_names,
    }


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------

async def _chat_view(app: Any, trace: Any, s: Dict[str, Any], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    session = simulator.public(s)
    adef = await simulator.get_def(app, trace, s["assistant"])
    session["node_tools"] = [t.name for t in adef.tools_for(s["node"])] if s["node"] else []
    session["node_name"] = adef.node(s["node"]).get("name") if s["node"] else ""
    try:
        if s["assistant"] == "forge":
            got = await app.state.call("builder", s["phone"], "getDraft", trace_id=trace.trace_id)
            session["draft"] = (got.get("draft") or {}).get("fields") or {}
        elif s["assistant"] == "clinic":
            session["patient"] = await _patient_view(app, trace, s["phone"])
    except Exception as exc:
        log("warn", "console.chat_extras_failed", error=str(exc)[:120])
    return {"session": session, "events": _mask_events(events)}


def _mask_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    text = json.dumps(events, default=str)
    text = re.sub(r"\+\d{11,15}", lambda m: mask_phone(m.group(0)), text)
    return json.loads(text)


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

def _mask_agent(rec: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(rec)
    out["owner"] = mask_phone(out.get("owner"))
    return out


async def _agent_detail(app: Any, trace: Any, key: str) -> Dict[str, Any]:
    reg = (await app.state.call("registry", "global", "get", key, trace_id=trace.trace_id)).get("agent")
    if not reg:
        raise simulator.SimError(404, "unknown agent")
    owner = str(reg.get("owner") or "")
    detail: Dict[str, Any] = {"registry": _mask_agent(reg)}
    try:
        detail["archive"] = await app.storage.get_json(f"forge/builds/{key}/latest.json")
        detail["versions"] = [o["key"] for o in await app.storage.list(f"forge/builds/{key}/", 50)]
    except Exception as exc:
        detail["archive"], detail["versions"] = None, []
        detail["archive_error"] = str(exc)[:120]
    if owner:
        from .func import mask_profile

        snap = mask_profile((await app.state.call("builder", owner, "snapshot", trace_id=trace.trace_id)).get("profile") or {})
        detail["agent"] = next((a for a in snap.get("agents") or [] if a.get("key") == key), None)
        detail["messages"] = [msg for msg in snap.get("messages") or [] if msg.get("agentKey") == key]
        detail["jobs"] = [j for j in snap.get("jobs") or [] if j.get("agentKey") == key]
        detail["draft"] = snap.get("draft")
    return detail


async def _agent_action(app: Any, trace: Any, key: str, action: str, body: Dict[str, Any]):
    reg = (await app.state.call("registry", "global", "get", key, trace_id=trace.trace_id)).get("agent")
    if not reg or not reg.get("owner"):
        return _err(404, "unknown agent")
    owner = reg["owner"]
    now = _ms()
    if action == "test-call":
        flags, _ = await app.flags.get(trace)
        delay = min(max(int(body.get("delay_secs") or flags.test_call_delay_secs), 5), 86400)
        res = await app.state.call("builder", owner, "requestTestCall", key, delay * 1000,
                                   flags.max_test_calls_per_hour, now, trace.trace_id, trace_id=trace.trace_id)
        if not res.get("ok"):
            return _err(409, f"test call refused: {res.get('reason')}")
        app.local_alarm(owner, res.get("at"))
        return _resp(200, {"ok": True, "at": res.get("at"), "delay_secs": delay})
    res = await app.state.call("builder", owner, "deleteAgent", key, now, trace_id=trace.trace_id)
    if not res.get("ok"):
        return _err(409, f"delete refused: {res.get('reason')}")
    try:
        await app.state.call("registry", "global", "setStatus", key, "deleting", now, trace_id=trace.trace_id)
    except Exception as exc:
        log("warn", "registry.set_status_failed", agent_key=key, error=str(exc)[:120])
    app.local_alarm(owner, now)
    return _resp(200, {"ok": True})


async def _form_build(app: Any, trace: Any, body: Dict[str, Any]) -> Dict[str, Any]:
    """Build from a form: the same calls a phone call makes, step by step."""
    phone = _phone(body.get("phone"))
    call_id = "web_" + secrets.token_hex(8)
    fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
    steps: List[Dict[str, Any]] = []

    async def step(name: str, coro: Any) -> Any:
        started = time.monotonic()
        try:
            result = await coro
            steps.append({"step": name, "ok": True, "ms": round((time.monotonic() - started) * 1000, 1), "result": result})
            return result
        except ToolError as exc:
            steps.append({"step": name, "ok": False, "ms": round((time.monotonic() - started) * 1000, 1), "error": str(exc)})
            return None

    args = {"caller_phone": phone, "call_id": call_id}
    await step("variables", webhook.forge_variables(app, trace, simulator._init_body(phone, call_id)))
    if body.get("reset", True):
        await step("start_new_draft", tools.start_new_draft(app, trace, args))
    await step("save_draft", tools.save_draft(app, trace, {**args, "values": fields}))
    checked = await step("check_draft", webhook.check_draft(app, trace, args))
    built = None
    if checked and checked.get("draft_complete") == "true":
        built = await step("build_agent", webhook.build_agent(app, trace, args))
    return {"phone": mask_phone(phone), "steps": _mask_events(steps), "result": built or checked,
            "spans": list(trace.spans)}


# ---------------------------------------------------------------------------
# clinic
# ---------------------------------------------------------------------------

async def _bind_patient(app: Any, trace: Any, phone: str) -> str:
    call_id = "web_" + secrets.token_hex(8)
    await app.state.call("patient", phone, "startCall", phone, call_id, True,
                         clinic.slot_str(clinic.local_now(app)), _ms(), trace_id=trace.trace_id)
    return call_id


async def _patient_view(app: Any, trace: Any, phone: str) -> Dict[str, Any]:
    snap = (await app.state.call("patient", phone, "snapshot", trace_id=trace.trace_id)).get("patient") or {}
    snap = json.loads(json.dumps(snap, default=str))
    snap.pop("attested", None)
    snap["phone"] = mask_phone(snap.get("phone"))
    for appt in snap.get("appointments") or []:
        appt["patient"] = mask_phone(appt.get("patient"))
        if appt.get("slot"):
            appt["spoken"] = clinic.spoken_slot(appt["slot"], str(appt.get("specialist_id") or ""))
    return snap


async def _clinic(app: Any, trace: Any, m: str, parts: List[str], body: Dict[str, Any]):
    action = parts[1] if len(parts) > 1 else ""
    if not action and m == "GET":
        return _resp(200, await clinic.overview(app, trace, 7))
    if action == "patient" and len(parts) == 3 and m == "GET":
        phone = normalize_phone(parts[2])
        if not phone:
            return _err(400, "expected a phone number")
        return _resp(200, await _patient_view(app, trace, phone))
    if m != "POST":
        return _err(404, "not found")
    if action == "triage":
        result = await clinic.triage(app, trace, str(body.get("symptoms") or ""),
                                     clinic._coerce_age(body.get("age")), bool(body.get("for_child")))
        return _resp(200, result)
    if action == "availability":
        return _resp(200, await clinic.availability(
            app, trace, specialty=str(body.get("specialty") or ""), specialist=str(body.get("specialist") or ""),
            preferred_day=str(body.get("preferred_day") or ""), part_of_day=str(body.get("part_of_day") or ""),
            urgency=str(body.get("urgency") or "routine"), language=str(body.get("language") or ""),
            limit=int(body.get("limit") or 6)))
    if action == "book":
        phone = _phone(body.get("phone"))
        call_id = await _bind_patient(app, trace, phone)
        result = await clinic.book(app, trace, phone, str(body.get("slot_id") or ""), str(body.get("full_name") or ""),
                                   str(body.get("reason") or ""), str(body.get("urgency") or "routine"),
                                   source="console", call_id=call_id)
        return _resp(200, result)
    if action == "cancel":
        if body.get("specialist_id") and body.get("appointment_id"):
            res = await app.state.call("schedule", str(body["specialist_id"]), "cancel", str(body["appointment_id"]),
                                       None, _ms(), trace_id=trace.trace_id)
            booking = res.get("booking") or {}
            if res.get("ok") and booking.get("patient"):
                await app.state.call("patient", booking["patient"], "cancelAppointment", booking.get("id"), _ms(),
                                     trace_id=trace.trace_id)
                await app.state.call("desk", "global", "log",
                                     {"event": "cancelled", "appointment_id": booking.get("id"), "by": "console",
                                      "patient": mask_phone(booking["patient"])}, _ms(), trace_id=trace.trace_id)
            return _resp(200 if res.get("ok") else 404, {"booking_status": "cancelled" if res.get("ok") else res.get("reason")})
        phone = _phone(body.get("phone"))
        call_id = await _bind_patient(app, trace, phone)
        return _resp(200, await clinic.cancel(app, trace, phone, str(body.get("appointment_id") or ""),
                                              str(body.get("which") or ""), call_id))
    if action == "callback-done":
        res = await app.state.call("desk", "global", "resolveCallback", str(body.get("id") or ""), _ms(),
                                   trace_id=trace.trace_id)
        return _resp(200 if res.get("ok") else 404, {"ok": bool(res.get("ok"))})
    return _err(404, "not found")

