"""Variable webhooks and webhook tools (flat JSON, never an MCP envelope).

The variables webhooks always answer 200 with `{"dynamic_variables": ...}`:
the assistants depend on them mid-call, so every backend failure degrades to
defaults instead of erroring. Webhook tools (check_draft, build_agent,
take_message) return flat dicts the workflow LLM reads; per Appendix D their
`draft_complete` / `build_status` / `built_agent_name` keys are mapped into
dynamic variables on the real platform.

No log() calls here: one JSON line per request comes from the router; these
handlers only set trace fields and outcomes.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from . import builder, generator
from .config import mask_phone, normalize_phone
from .policy import check as policy_check


def _now_ms() -> int:
    return int(time.time() * 1000)


class WebhookError(Exception):
    """A non-200 webhook-tool answer (the router turns it into JSON+status)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def forge_defaults(trace_id: str = "") -> Dict[str, str]:
    """Every Forge workflow variable, with its spoken-safe default (A.8)."""
    return {
        "welcome_line": (
            "Tell me about the business or project you want an agent for, "
            "and I'll build it while we talk."
        ),
        "caller_phone": "",
        "caller_id_verified": "false",
        "returning_builder": "false",
        "call_count": "1",
        "has_draft": "false",
        "draft_business": "",
        "active_agents": "0",
        "last_agent_name": "",
        "unread_messages": "0",
        "max_agents": "3",
        "quota_remaining": "3",
        "builds_enabled": "true",
        "maintenance_message": "",
        "agent_ttl_hours": "24",
        "draft_complete": "false",
        "build_status": "none",
        "built_agent_name": "",
        "degraded": "false",
        "trace_id": trace_id,
    }


def _payload(body: Any) -> Dict[str, Any]:
    """Telnyx posts `{"data": {"payload": {...}}}`; tolerate flat bodies too."""
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    if isinstance(data, dict):
        payload = data.get("payload")
        if isinstance(payload, dict):
            return payload
    return body


def _welcome_line(flags: Any, info: Optional[Dict[str, Any]]) -> str:
    """Compose the tail of the welcome speak-node text (spec Phase 6)."""
    if not getattr(flags, "builds_enabled", True):
        return flags.maintenance_message
    parts: List[str] = []
    if info:
        if info.get("returning"):
            parts.append("Welcome back.")
        if info.get("hasDraft"):
            parts.append(
                f"Your draft for {info.get('draftBusiness') or 'your business'} is saved, "
                "so we can pick up where you left off."
            )
        if info.get("activeAgents") and info.get("lastAgentName"):
            parts.append(f"{info['lastAgentName']} is still live.")
        unread = int(info.get("unreadMessages") or 0)
        if unread:
            parts.append(f"Your agents took {unread} new message{'' if unread == 1 else 's'}.")
    if not parts:
        return ""  # keep the A.8 default invitation
    parts.append("What would you like to do?")
    return " ".join(parts)


async def forge_variables(app: Any, trace: Any, body: Any) -> Dict[str, Any]:
    """The Forge dynamic-variables webhook. ALWAYS 200; failures degrade."""
    values = forge_defaults(trace.trace_id)
    payload = _payload(body)
    owner = normalize_phone(payload.get("telnyx_end_user_target")) or ""
    call_id = str(payload.get("call_control_id") or "")
    attested = bool(payload.get("telnyx_end_user_target_verified"))
    trace.set(caller=mask_phone(owner))
    if owner:
        now = _now_ms()
        flags_task = app.flags.get(trace)
        start_task = app.state.call(
            "builder", owner, "startCall", owner, call_id, attested, now, trace_id=trace.trace_id
        )
        flags_res, start_res = await asyncio.gather(flags_task, start_task, return_exceptions=True)
        flags = None if isinstance(flags_res, Exception) else flags_res[0]
        info = None if isinstance(start_res, Exception) else start_res
        if info is None:
            values["degraded"] = "true"
            trace.set(degraded=True, call_id=call_id)
            trace.outcome = "degraded"
        # caller identity comes from the payload, not the actor
        values["caller_phone"] = owner
        values["caller_id_verified"] = "true" if attested else "false"
        if flags is not None:
            values["builds_enabled"] = "true" if flags.builds_enabled else "false"
            if not flags.builds_enabled:
                values["maintenance_message"] = flags.maintenance_message
            values["agent_ttl_hours"] = str(flags.agent_ttl_hours)
            values["max_agents"] = str(flags.max_agents_per_caller)
            quota = max(0, flags.max_agents_per_caller - int(info.get("activeAgents") or 0)) if info else flags.max_agents_per_caller
            values["quota_remaining"] = str(quota)
        if info is not None:
            values["call_count"] = str(info.get("callCount") or 1)
            values["returning_builder"] = "true" if info.get("returning") else "false"
            values["has_draft"] = "true" if info.get("hasDraft") else "false"
            values["draft_business"] = str(info.get("draftBusiness") or "")
            values["active_agents"] = str(info.get("activeAgents") or 0)
            values["last_agent_name"] = str(info.get("lastAgentName") or "")
            values["unread_messages"] = str(info.get("unreadMessages") or 0)
        line = _welcome_line(flags, info)
        if line:
            values["welcome_line"] = line
    return {"dynamic_variables": values}


async def agent_variables(app: Any, trace: Any, key: str, body: Any) -> Dict[str, Any]:
    """A generated agent's dynamic-variables webhook: is_owner + owner_line.
    ALWAYS 200; on any failure `is_owner` is "false" (the safe default)."""
    values = {"is_owner": "false", "owner_line": ""}
    payload = _payload(body)
    caller = normalize_phone(payload.get("telnyx_end_user_target")) or ""
    trace.set(caller=mask_phone(caller), agent_key=key)
    try:
        import json

        record = await agent_record(app, key)
        if record and caller and record.get("owner") == caller:
            values["is_owner"] = "true"
            values["owner_line"] = "This is your test call, so try anything you like."
    except Exception:
        trace.set(degraded=True)
        trace.outcome = "degraded"
    return {"dynamic_variables": values}


AGENT_MEMO_SECS = 60.0


async def agent_record(app: Any, key: str) -> Optional[Dict[str, Any]]:
    """`agents/{key}` is written once per build and read on every call to that agent, and a KV REST read
    costs 1-3 s, so it is memoised per instance. A deleted agent may still look live here for up to a minute,
    which is harmless: it only affects the owner greeting and where a message lands."""
    import json

    memo = getattr(app, "_agent_memo", None)
    if memo is None:
        memo = app._agent_memo = {}
    hit = memo.get(key)
    if hit and time.monotonic() - hit[0] < AGENT_MEMO_SECS:
        return hit[1]
    raw = await app.kv.get(f"agents/{key}")
    record = json.loads(raw) if isinstance(raw, str) else (raw or None)
    if isinstance(record, dict):
        memo[key] = (time.monotonic(), record)
        if len(memo) > 500:
            memo.pop(next(iter(memo)))
    return record if isinstance(record, dict) else None


def _draft_fallback(message: str) -> Dict[str, Any]:
    """What check_draft/build_agent answer when the backend is unavailable."""
    return {
        "draft_complete": "false",
        "build_status": "failed",
        "missing_fields": [],
        "suggestions": [],
        "summary": "",
        "message": message,
    }


async def check_draft(app: Any, trace: Any, body: Any) -> Dict[str, Any]:
    """`POST /tools/check-draft`: a completeness snapshot of the caller's draft."""
    from .drafts import assess
    from .mcp import ToolError
    from .tools import _owner

    try:
        owner = await _owner(
            app, trace,
            {"caller_phone": (body or {}).get("caller_phone"), "call_id": (body or {}).get("call_id")},
        )
        current = await app.state.call("builder", owner, "getDraft", trace_id=trace.trace_id)
        draft = current.get("draft") or {}
        assessed = assess(draft.get("fields") or {})
        if assessed["policy_blocked"]:
            message = (
                "I can't build that, because " + assessed["policy_reason"]
                + ". I'm happy to build something else."
            )
        elif assessed["missing"]:
            message = "The draft isn't complete yet. I still need " + ", ".join(assessed["missing"]) + "."
        else:
            message = "That's everything I need. Would you like me to build it?"
        return {
            "draft_complete": "true" if assessed["complete"] else "false",
            "policy_blocked": assessed["policy_blocked"],
            "missing_fields": assessed["missing"],
            "suggestions": assessed["suggestions"],
            "summary": assessed["summary"],
            "message": message,
        }
    except ToolError as exc:
        return _draft_fallback(str(exc))
    except Exception as exc:
        trace.outcome = "degraded"
        trace.set(degraded=True, error=str(exc)[:120])
        return _draft_fallback("I couldn't check your draft just now, but your answers are saved.")


async def build_agent(app: Any, trace: Any, body: Any) -> Dict[str, Any]:
    """`POST /tools/build-agent`: run the build pipeline and report it."""
    from .mcp import ToolError
    from .tools import _owner

    body = body or {}
    try:
        owner = await _owner(
            app, trace,
            {"caller_phone": body.get("caller_phone"), "call_id": body.get("call_id")},
        )
        result = await builder.build(app, trace, owner, str(body.get("call_id") or ""))
        result = dict(result)
        result.setdefault("build_status", "failed")
        result["built_agent_name"] = str(result.get("agent_name") or "")
        return result
    except ToolError as exc:
        return {"build_status": "failed", "message": str(exc)}
    except Exception as exc:
        trace.outcome = "degraded"
        trace.set(degraded=True, error=str(exc)[:120])
        return {"build_status": "failed", "message": "The draft is saved."}


def _bearer_ok(token: str, auth: Any) -> bool:
    """Constant-time Bearer comparison for the per-agent webhook token."""
    import hmac

    if not isinstance(auth, str):
        return False
    auth = auth.strip()
    if not auth.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(auth[7:].strip(), token)


async def take_message(
    app: Any, trace: Any, key: str, body: Any, auth: Optional[str] = None
) -> Dict[str, Any]:
    """`POST /agents/{key}/tools/take-message`: the generated agent's inbox.

    The Authorization header must carry the per-agent token (401 otherwise);
    the message lands in the OWNER's inbox with the callback number."""
    import json

    from .drafts import _clean

    body = body or {}
    if not app.settings.mcp_token and app.settings.require_tokens:
        raise WebhookError(500, "MCP_TOKEN is not configured")  # never fall back to the dev secret in production
    if not _bearer_ok(generator.agent_token(app.settings.mcp_token, key), auth):
        raise WebhookError(401, "unauthorized")
    trace.set(agent_key=key)
    record = await agent_record(app, key)
    if not isinstance(record, dict) or not record.get("owner"):
        raise WebhookError(404, "unknown agent")
    caller_name = _clean(str(body.get("caller_name") or ""))[:80]
    callback = normalize_phone(body.get("callback_number")) or ""
    message = _clean(str(body.get("message") or ""))[:1000]
    if not message:
        return {"saved": False, "message": "The message was empty, so I didn't save anything."}
    saved = await app.state.call(
        "builder", record["owner"], "addMessage", key,
        {
            "from": callback,
            "caller_name": caller_name,
            "callback_number": callback,
            "message": message,
            "agent_name": record.get("name") or "",
        },
        _now_ms(), trace_id=trace.trace_id,
    )
    return {
        "saved": True,
        "id": saved.get("id"),
        "unread": saved.get("unread"),
        "say": "I've saved your message.",
    }
