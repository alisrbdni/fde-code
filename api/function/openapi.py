"""OpenAPI 3.1 description of every forge-api route, plus a self-contained
docs page (`/docs`) that renders `/openapi.json` grouped by tag."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import clinic, tools, webhook

BEARER = [{"bearer": []}]


def _op(tag: str, summary: str, description: str = "", security: Optional[List[Dict[str, Any]]] = None,
        body: Optional[Dict[str, Any]] = None, response: Optional[Dict[str, Any]] = None,
        params: Optional[List[Dict[str, Any]]] = None, codes: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    op: Dict[str, Any] = {"tags": [tag], "summary": summary}
    if description:
        op["description"] = description
    if security is not None:
        op["security"] = security
    if params:
        op["parameters"] = params
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"example": body}}}
    responses: Dict[str, Any] = {"200": {"description": "OK"}}
    if response is not None:
        responses["200"]["content"] = {"application/json": {"example": response}}
    for code, text in (codes or {}).items():
        responses[code] = {"description": text}
    op["responses"] = responses
    return op


def _path_param(name: str, description: str) -> Dict[str, Any]:
    return {"name": name, "in": "path", "required": True, "description": description, "schema": {"type": "string"}}


def _tool_list(server: Any) -> str:
    return "\n".join(f"- `{t['name']}`: {t['description']}" for t in server.tool_defs)


CALL = {"data": {"event_type": "assistant.initialization", "payload": {
    "telnyx_end_user_target": "+15550102001", "telnyx_agent_target": "+15550100000",
    "call_control_id": "v3:abc123", "telnyx_end_user_target_verified": True}}}
TOOL_ARGS = {"caller_phone": "+15550102001", "call_id": "v3:abc123"}
MCP_CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "save_draft", "arguments": {**TOOL_ARGS, "values": {"business_name": "Sunrise Dental"}}}}


def spec(base_url: str) -> Dict[str, Any]:
    paths: Dict[str, Any] = {
        "/health": {"get": _op("Ops", "Liveness", response={"ok": True, "service": "forge-api", "dry_run": True})},
        "/webhook/dynamic-variables": {"post": _op(
            "Forge", "Forge dynamic variables webhook",
            "Called by the Forge assistant at call start. Always 200 with `{\"dynamic_variables\": {...}}`; "
            "backend failures degrade to defaults (`degraded=true`).",
            body=CALL, response={"dynamic_variables": webhook.forge_defaults("v3:abc123")},
            codes={"401": "Bad signature (when VERIFY_SIGNATURES=true)"})},
        "/tools/check-draft": {"post": _op(
            "Forge", "Webhook tool: check_draft", "Response maps `draft_complete` into a variable.", BEARER,
            body=TOOL_ARGS, response={"draft_complete": "true", "policy_blocked": False, "missing_fields": [],
                                      "message": "That's everything I need. Would you like me to build it?"},
            codes={"401": "MCP_TOKEN missing or wrong (header or ?token=)"})},
        "/tools/build-agent": {"post": _op(
            "Forge", "Webhook tool: build_agent",
            "Builds a real Telnyx AI Assistant from the draft. `build_status` is one of ready, blocked, quota, "
            "incomplete, paused, building, failed.", BEARER, body=TOOL_ARGS,
            response={"build_status": "ready", "built_agent_name": "Sam - Sunrise Dental", "agent_key": "ag1a2b3c4d",
                      "level": "full", "expires_in_hours": 24})},
        "/mcp": {"post": _op(
            "Forge", "Forge MCP server (Streamable HTTP, JSON)",
            "JSON-RPC 2.0: initialize, ping, tools/list, tools/call, notifications, batches.\n\nTools:\n"
            + _tool_list(tools.FORGE), BEARER, body=MCP_CALL,
            response={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{...}"}],
                                                            "structuredContent": {"saved": True}, "isError": False}},
            codes={"202": "Notification accepted", "400": "Parse error (-32700)"})},
        "/agents/{key}/dynamic-variables": {"post": _op(
            "Generated agents", "A generated agent's variables webhook",
            "Returns `is_owner` and `owner_line` (the owner's test call).", body=CALL,
            response={"dynamic_variables": {"is_owner": "true", "owner_line": "This is your test call, so try anything you like."}},
            params=[_path_param("key", "Agent key, `ag` + 8 hex")])},
        "/agents/{key}/tools/take-message": {"post": _op(
            "Generated agents", "A generated agent's take_message tool",
            "Authorization is a per-agent token (HMAC of the agent key). Saves into the owner's inbox.", BEARER,
            body={"caller_name": "Ana", "callback_number": "+15550102002", "message": "Please call me back",
                  "call_id": "v3:def456"},
            response={"saved": True, "say": "I've saved your message."},
            params=[_path_param("key", "Agent key")], codes={"401": "Wrong agent token", "404": "Unknown agent"})},
        "/clinic/webhook/dynamic-variables": {"post": _op(
            "Clinic", "Clara dynamic variables webhook", "Always 200; degrades to defaults.", body=CALL,
            response={"dynamic_variables": clinic.clinic_defaults("v3:abc123")})},
        "/clinic/tools/triage": {"post": _op(
            "Clinic", "Webhook tool: triage_symptoms",
            "Rules decide urgency (emergency, crisis, urgent, routine); the LLM may only pick a specialty from the "
            "closed enum. Maps `triage_urgency`, `matched_specialty`, `specialty_label`.", BEARER,
            body={**TOOL_ARGS, "symptoms": "my knee hurts", "age": 40},
            response={"triage_urgency": "routine", "matched_specialty": "orthopedics", "specialty_label": "orthopedics",
                      "say": "Based on what you told me, the right person to see is a bone and joint specialist.",
                      "disclaimer": "This is not a diagnosis, just who to see and how soon."})},
        "/clinic/tools/book": {"post": _op(
            "Clinic", "Webhook tool: book_appointment", "Maps `booking_status` and `booking_line`.", BEARER,
            body={**TOOL_ARGS, "slot_id": "ortho-becker@2026-11-10T09:00", "full_name": "Ana Diaz"},
            response={"booking_status": "confirmed", "appointment_id": "ap1a2b3c4d",
                      "booking_line": "You're booked with Dr. Tom Becker on Tuesday, November 10 at 9 AM."})},
        "/clinic/mcp": {"post": _op(
            "Clinic", "Clinic MCP server", "Tools:\n" + _tool_list(clinic.SERVER), BEARER,
            body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})},
        "/internal/jobs": {"post": _op(
            "Internal", "Run due actor-alarm jobs",
            "Called by the BuilderProfile actor alarm with `Bearer STATE_TOKEN`. 4xx results are permanent; others "
            "are requeued with backoff.", BEARER,
            body={"jobs": [{"id": "jb1", "type": "test_call", "agentKey": "ag1a2b3c4d", "assistantId": "...",
                            "owner": "+15550102001", "at": 0}]},
            response={"results": [{"id": "jb1", "ok": True}]})},
        "/ops/health": {"get": _op(
            "Ops", "Deep health", "KV probe, actor snapshot, Telnyx mode, storage probe (reported only). 503 when a "
            "check fails or the 5-minute error rate is above 20% with at least 5 requests.",
            codes={"503": "Unhealthy"})},
        "/ops/metrics": {"get": _op(
            "Ops", "Request metrics from the OpsMetrics actor", security=BEARER,
            params=[{"name": "window", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 120}}],
            response={"window_minutes": 5, "requests": 42, "errors": 0, "error_rate": 0, "latency_p50_ms": 12,
                      "latency_p95_ms": 180, "by_route": {}, "recent_errors": []})},
        "/ops/flags": {
            "get": _op("Ops", "Feature flags (KV flags/global, flags/clinic)", security=BEARER),
            "post": _op("Ops", "Patch feature flags", security=BEARER,
                        body={"forge": {"builds_enabled": False}, "clinic": {"bookings_enabled": True}}),
        },
        "/ops/builder/{phone}": {"get": _op(
            "Ops", "A builder's actor state (masked)", security=BEARER, params=[_path_param("phone", "E.164")])},
        "/ui": {"get": _op("Console", "Operator console (HTML)")},
        "/ui/api/overview": {"get": _op("Console", "Mode, flags, endpoints", security=BEARER)},
        "/ui/api/settings": {
            "get": _op("Console", "Overridable settings (masked) with their source", security=BEARER),
            "post": _op("Console", "Set in-memory overrides", "Values are never logged or stored. ADMIN_TOKEN can't "
                        "be overridden.", BEARER, body={"values": {"MCP_TOKEN": "new-token", "STATE_URL": None}}),
        },
        "/ui/api/flow/{assistant}": {"get": _op("Console", "Workflow, node tools, defaults", security=BEARER,
                                                params=[_path_param("assistant", "forge, clinic or an agent key")])},
        "/ui/api/chat/start": {"post": _op("Console", "Start a simulated call over text", security=BEARER,
                                           body={"assistant": "forge", "phone": "+15550102001"})},
        "/ui/api/chat/send": {"post": _op("Console", "Send a caller turn", security=BEARER,
                                          body={"session_id": "0123456789ab", "text": "I run a dental office"},
                                          codes={"409": "The conversation has ended"})},
        "/ui/api/chat/{id}": {"get": _op("Console", "A chat session and all its events", security=BEARER,
                                         params=[_path_param("id", "Session id")])},
        "/ui/api/agents": {"get": _op("Console", "Registry of generated agents", security=BEARER)},
        "/ui/api/agents/{key}": {"get": _op("Console", "Agent detail: registry, archive, versions, messages, jobs",
                                            security=BEARER, params=[_path_param("key", "Agent key")])},
        "/ui/api/agents/{key}/test-call": {"post": _op("Console", "Schedule a test call", security=BEARER,
                                                       body={"delay_secs": 20}, params=[_path_param("key", "Agent key")])},
        "/ui/api/agents/{key}/delete": {"post": _op("Console", "Delete an agent now", security=BEARER, body={},
                                                    params=[_path_param("key", "Agent key")])},
        "/ui/api/forge/build": {"post": _op("Console", "Build from a form (same calls as a phone call)",
                                            security=BEARER, body={"phone": "+15550102001", "fields": {
                                                "business_name": "Sunrise Dental", "agent_name": "Sam",
                                                "template": "receptionist"}})},
        "/ui/api/owner/{phone}": {"get": _op("Console", "BuilderProfile snapshot (masked)", security=BEARER,
                                             params=[_path_param("phone", "E.164")])},
        "/ui/api/clinic": {"get": _op("Console", "Clinic overview: free slots, bookings, desk", security=BEARER)},
        "/ui/api/clinic/triage": {"post": _op("Console", "Triage text", security=BEARER, body={"symptoms": "itchy rash"})},
        "/ui/api/clinic/availability": {"post": _op("Console", "Availability", security=BEARER,
                                                    body={"specialty": "dermatology"})},
        "/ui/api/clinic/book": {"post": _op("Console", "Book for a phone number", security=BEARER,
                                            body={"phone": "+15550103001", "slot_id": "derm-raman@2026-11-10T10:00",
                                                  "full_name": "Ana Diaz"})},
        "/ui/api/clinic/cancel": {"post": _op("Console", "Cancel by phone, or by specialist_id + appointment_id",
                                              security=BEARER, body={"specialist_id": "derm-raman", "appointment_id": "ap1a2b3c4d"})},
        "/ui/api/clinic/callback-done": {"post": _op("Console", "Resolve a desk callback", security=BEARER,
                                                     body={"id": "cb1a2b3c4d"})},
        "/ui/api/clinic/patient/{phone}": {"get": _op("Console", "Patient actor (masked)", security=BEARER,
                                                      params=[_path_param("phone", "E.164")])},
        "/openapi.json": {"get": _op("Ops", "This document")},
        "/docs": {"get": _op("Ops", "Human-readable API docs")},
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Agent Forge API (forge-api)",
            "version": "1.0.0",
            "description": "Telnyx Edge Function behind Forge (builds voice agents) and Clara (clinic assistant). "
                           "All call-scoped routes use the call id as `x-trace-id`; every response carries "
                           "`server-timing`.",
        },
        "servers": [{"url": base_url}],
        "tags": [{"name": t} for t in ("Forge", "Clinic", "Generated agents", "Console", "Ops", "Internal")],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "paths": paths,
    }


DOCS_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent Forge API</title>
<style>
:root{--bg:#fff;--fg:#1d2330;--muted:#5d6678;--line:#e3e6ec;--card:#f7f8fa;--accent:#0b6bcb;--get:#1a7f37;--post:#9a3b00}
@media (prefers-color-scheme:dark){:root{--bg:#12151b;--fg:#e6e9ef;--muted:#9aa3b2;--line:#2a2f3a;--card:#1a1e26;--accent:#58a6ff;--get:#3fb950;--post:#f0883e}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px}
h1{margin:0 0 4px;font-size:24px}h2{margin:32px 0 8px;font-size:18px;border-bottom:1px solid var(--line);padding-bottom:6px}
p.lead{color:var(--muted);margin:0 0 16px}
details{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0;padding:8px 12px}
summary{cursor:pointer;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.m{font:600 12px ui-monospace,monospace;text-transform:uppercase;min-width:42px}.m.get{color:var(--get)}.m.post{color:var(--post)}
code,pre{font:13px ui-monospace,SFMono-Regular,Menlo,monospace}pre{overflow:auto;background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:8px}
.lock{color:var(--muted);font-size:12px}.desc{white-space:pre-wrap;color:var(--muted)}
a{color:var(--accent)}
</style></head><body><main>
<h1>Agent Forge API</h1><p class="lead" id="lead">Loading <a href="/openapi.json">/openapi.json</a>...</p><div id="out"></div>
<script>
fetch('/openapi.json').then(r=>r.json()).then(spec=>{
  document.getElementById('lead').textContent=spec.info.description;
  const groups={};
  for(const [path,ops] of Object.entries(spec.paths)) for(const [method,op] of Object.entries(ops)){
    (groups[op.tags[0]]=groups[op.tags[0]]||[]).push([method,path,op]);}
  const out=document.getElementById('out');
  const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  for(const tag of spec.tags.map(t=>t.name)){
    if(!groups[tag]) continue;
    const h=document.createElement('h2');h.textContent=tag;out.appendChild(h);
    for(const [method,path,op] of groups[tag]){
      const d=document.createElement('details');
      let html='<summary><span class="m '+method+'">'+method+'</span><code>'+esc(path)+'</code><span>'+esc(op.summary)+'</span>'+(op.security?'<span class="lock">bearer</span>':'')+'</summary>';
      if(op.description) html+='<p class="desc">'+esc(op.description)+'</p>';
      const req=op.requestBody&&op.requestBody.content['application/json'].example;
      if(req!==undefined) html+='<div>Request</div><pre>'+esc(JSON.stringify(req,null,2))+'</pre>';
      for(const [code,r] of Object.entries(op.responses)){
        const ex=r.content&&r.content['application/json'].example;
        html+='<div>'+code+' '+esc(r.description)+'</div>'+(ex!==undefined?'<pre>'+esc(JSON.stringify(ex,null,2))+'</pre>':'');
      }
      d.innerHTML=html;out.appendChild(d);
    }
  }
}).catch(e=>{document.getElementById('lead').textContent='Could not load /openapi.json: '+e});
</script></main></body></html>
"""
