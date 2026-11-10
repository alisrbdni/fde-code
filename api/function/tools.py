"""The nine Forge MCP tools. Common args: caller_phone (always
`{{telnyx_end_user_target}}`) and call_id (always `{{call_control_id}}`).

Tools never trust `caller_phone` alone: every tool verifies the call through
the builder actor's `callInfo(call_id)`, so one caller can't act for another
number and a fabricated `caller_phone` gets nothing.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from . import catalog
from .catalog import TEMPLATES, TONES, VOICES
from .config import normalize_phone
from .drafts import assess, build_patch
from .mcp import Server, ToolError

LEVEL_FEATURES = {
    "full": "full conversation flow, tools and message taking",
    "no_workflow": "tools and message taking (no conversation flow)",
    "no_tools": "message taking only",
    "minimal": "message taking only",
}

COMMON_ARGS = {
    "caller_phone": {
        "type": "string",
        "description": "Always {{telnyx_end_user_target}}. The caller's own phone number.",
    },
    "call_id": {
        "type": "string",
        "description": "Always {{call_control_id}}. Identifies this call.",
    },
}


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _owner(app, trace, args: Dict[str, Any], private: bool = False) -> str:
    """Verify the call binding and return the owner's normalised phone."""
    owner = normalize_phone(args.get("caller_phone"))
    if not owner:
        raise ToolError("I couldn't read your phone number, so I can't save anything for you.")
    call_id = args.get("call_id")
    if not isinstance(call_id, str) or not call_id or "{{" in str(call_id):
        raise ToolError("I can't verify this call yet, so I can't do that.")
    info = await app.state.call("builder", owner, "callInfo", call_id, trace_id=trace.trace_id)
    if not info.get("known"):
        raise ToolError("I can't verify this call yet, so I can't do that.")
    if private:
        flags, _ = await app.flags.get(trace)
        if flags.require_attestation_for_private and not info.get("attested"):
            raise ToolError(
                "I can only do that when your caller ID is verified. "
                "Please enable caller ID verification on your line and call back."
            )
    return owner


def _tool(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
    }


async def list_templates(app, trace, args) -> Dict[str, Any]:
    await _owner(app, trace, args)
    templates = [
        {"id": t["id"], "label": t["label"], "purpose": t["purpose"], "ask_for": t["ask_for"]}
        for t in TEMPLATES.values()
    ]
    return {
        "templates": templates,
        "say": "I have templates for a front desk, appointments, an FAQ line, a restaurant, events and new customer intake.",
    }


async def list_voices(app, trace, args) -> Dict[str, Any]:
    await _owner(app, trace, args)
    voices = [
        {"id": v["id"], "voice": v["voice"], "description": v["description"]} for v in VOICES.values()
    ]
    return {
        "voices": voices,
        "tones": list(TONES),
        "say": f"I can sound warm, bright, calm, professional or British, and I can be {', '.join(TONES)}.",
    }


async def save_draft(app, trace, args) -> Dict[str, Any]:
    owner = await _owner(app, trace, args)
    current = await app.state.call("builder", owner, "getDraft", trace_id=trace.trace_id)
    fields = (current.get("draft") or {}).get("fields") or {}
    patch, notes = build_patch(args, fields)
    set_map: Dict[str, Any] = dict(patch.get("set") or {})
    for name in patch.get("clear") or []:
        set_map[name] = None
    append_map = patch.get("append") or {}
    if not set_map and not append_map and not (patch.get("clear") or []):
        raise ToolError(
            "I didn't catch anything to save. Tell me the business name, what the agent should do, or a fact."
        )
    result = await app.state.call(
        "builder", owner, "saveDraft", {"set": set_map, "append": append_map}, _now_ms(), trace_id=trace.trace_id
    )
    if not result.get("ok"):
        raise ToolError("I'm already building your agent from this draft. Give me a few seconds, then try again.")
    draft = result.get("draft") or {}
    assessed = assess(draft.get("fields") or {})
    say = None
    if assessed["policy_blocked"]:
        say = "I can't build that, because " + assessed["policy_reason"] + ". I'm happy to build something else."
    return {
        "saved": True,
        "draft": draft.get("fields") or {},
        "draft_status": draft.get("status"),
        "draft_complete": assessed["complete"],
        "missing": assessed["missing"],
        "suggestions": assessed["suggestions"],
        "notes": notes,
        "policy_blocked": assessed["policy_blocked"],
        "policy_reason": assessed["policy_reason"],
        "summary": assessed["summary"],
        "say": say,
    }


async def get_draft(app, trace, args) -> Dict[str, Any]:
    owner = await _owner(app, trace, args)
    current = await app.state.call("builder", owner, "getDraft", trace_id=trace.trace_id)
    draft = current.get("draft") or {}
    assessed = assess(draft.get("fields") or {})
    return {
        "draft": draft.get("fields") or {},
        "draft_status": draft.get("status"),
        "draft_complete": assessed["complete"],
        "missing": assessed["missing"],
        "suggestions": assessed["suggestions"],
        "policy_blocked": assessed["policy_blocked"],
        "policy_reason": assessed["policy_reason"],
        "summary": assessed["summary"],
    }


async def start_new_draft(app, trace, args) -> Dict[str, Any]:
    owner = await _owner(app, trace, args)
    await app.state.call("builder", owner, "resetDraft", _now_ms(), trace_id=trace.trace_id)
    return {
        "ok": True,
        "say": "OK, I've cleared your draft. Tell me about the business you want an agent for.",
    }


async def list_my_agents(app, trace, args) -> Dict[str, Any]:
    owner = await _owner(app, trace, args)
    now = _now_ms()
    result = await app.state.call("builder", owner, "listAgents", now, trace_id=trace.trace_id)
    agents = []
    for agent in result.get("agents") or []:
        hours = max(0, round((agent.get("expiresAt", now) - now) / 3600000))
        agents.append(
            {
                "key": agent.get("key"),
                "name": agent.get("name"),
                "business": agent.get("business"),
                "expires_in_hours": hours,
                "expires_in": f"expires in about {hours} hours",
                "level": agent.get("level"),
                "features": LEVEL_FEATURES.get(agent.get("level", "full"), LEVEL_FEATURES["full"]),
            }
        )
    return {
        "agents": agents,
        "count": len(agents),
        "say": (
            f"You have {len(agents)} live agent{'s' if len(agents) != 1 else ''}."
            if agents
            else "You don't have any live agents right now."
        ),
    }


async def call_me_with_agent(app, trace, args) -> Dict[str, Any]:
    owner = await _owner(app, trace, args, private=True)
    from .config import is_demo_number

    if is_demo_number(owner):
        raise ToolError(
            "This is a browser demo chat, so there's no real phone number to call. You can talk to the agent right "
            "here instead: open My agents in the console, or press Call it in the browser."
        )
    flags, _ = await app.flags.get(trace)
    now = _now_ms()
    result = await app.state.call("builder", owner, "listAgents", now, trace_id=trace.trace_id)
    agents = result.get("agents") or []
    wanted = str(args.get("agent_name") or "").strip().lower()
    agent = None
    if wanted:
        for candidate in agents:
            if wanted in str(candidate.get("name", "")).lower() or wanted == candidate.get("key", ""):
                agent = candidate
                break
    else:
        agent = agents[-1] if agents else None
    if not agent:
        raise ToolError(
            "I couldn't find a live agent with that name. You can list your agents or build a new one."
        )
    try:
        delay_secs = int(args.get("delay_secs") or flags.test_call_delay_secs)
    except (TypeError, ValueError):
        delay_secs = flags.test_call_delay_secs
    delay_secs = min(max(delay_secs, 5), 86400)
    scheduled = await app.state.call(
        "builder",
        owner,
        "requestTestCall",
        agent.get("key"),
        delay_secs * 1000,
        flags.max_test_calls_per_hour,
        now,
        trace.trace_id,
        trace_id=trace.trace_id,
    )
    if scheduled.get("reason") == "rate_limited":
        raise ToolError("I can only schedule " + str(flags.max_test_calls_per_hour) + " test calls per hour per agent.")
    if not scheduled.get("ok"):
        raise ToolError("I couldn't schedule a test call right now. Your agent is still live; call back in a minute.")
    if hasattr(app, "local_alarm"):
        app.local_alarm(owner, scheduled.get("at"))
    return {
        "ok": True,
        "agent_key": scheduled.get("agentKey"),
        "agent_name": scheduled.get("name"),
        "delay_seconds": delay_secs,
        "say": (
            f"I'll call you with {scheduled.get('name')} in about {delay_secs} seconds. "
            "Please hang up now so the line is free."
        ),
    }


async def list_messages(app, trace, args) -> Dict[str, Any]:
    owner = await _owner(app, trace, args, private=True)
    result = await app.state.call("builder", owner, "listMessages", True, trace_id=trace.trace_id)
    messages = (result.get("messages") or [])[:10]
    if not messages:
        return {"messages": [], "count": 0, "say": "You have no messages right now."}
    lines = []
    for m in messages:
        lines.append(
            {
                "agent": m.get("agentName"),
                "from": m.get("caller_name"),
                "callback_number": m.get("callback_number"),
                "message": m.get("message"),
                "at": m.get("at"),
            }
        )
    return {"messages": lines, "count": result.get("unread", len(lines)), "say": f"You have {len(lines)} messages."}


async def delete_agent(app, trace, args) -> Dict[str, Any]:
    owner = await _owner(app, trace, args, private=True)
    now = _now_ms()
    result = await app.state.call("builder", owner, "listAgents", now, trace_id=trace.trace_id)
    agents = result.get("agents") or []
    key = str(args.get("agent_key") or "")
    wanted = str(args.get("agent_name") or "").strip().lower()
    agent = None
    for candidate in agents:
        if (key and candidate.get("key") == key) or (wanted and wanted in str(candidate.get("name", "")).lower()):
            agent = candidate
            break
    if not agent:
        agent = agents[-1] if (not wanted and not key and len(agents) == 1) else None
    if not agent:
        raise ToolError("Tell me which agent to delete. You can say the name, or list your agents first.")
    deleted = await app.state.call(
        "builder", owner, "deleteAgent", agent.get("key"), now, trace_id=trace.trace_id
    )
    if not deleted.get("ok"):
        raise ToolError("I couldn't find that agent. It may have already expired.")
    try:
        await app.state.call("registry", "global", "setStatus", agent.get("key"), "deleting", now)
    except Exception:
        pass
    if hasattr(app, "local_alarm"):
        app.local_alarm(owner, now)
    name = agent.get("name") or deleted.get("agent", {}).get("name") or "the agent"
    return {"ok": True, "agent_key": agent.get("key"), "say": f"I've deleted {name}. Anything else?"}


def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(COMMON_ARGS)
    merged.update(properties)
    return merged


TOOL_DEFS: List[Dict[str, Any]] = [
    _tool(
        "list_templates",
        "List the ready-made agent templates (label, purpose, and the facts they ask for).",
        dict(COMMON_ARGS),
        ["caller_phone", "call_id"],
    ),
    _tool(
        "list_voices",
        "List the available voices and tones the agent can use.",
        dict(COMMON_ARGS),
        ["caller_phone", "call_id"],
    ),
    _tool(
        "save_draft",
        "Save every detail you learn about the caller's business or project. Set a field to an empty string to clear it.",
        {
            **COMMON_ARGS,
            "values": {
                "type": "object",
                "description": "Draft fields to save: business_name, agent_name, purpose, template, tone, voice, greeting, audience, hours, tasks, facts, escalation_number, language.",
                "additionalProperties": True,
            },
        },
        ["caller_phone", "call_id", "values"],
    ),
    _tool(
        "get_draft",
        "Read the caller's saved draft: fields, completeness, missing pieces and a spoken summary.",
        dict(COMMON_ARGS),
        ["caller_phone", "call_id"],
    ),
    _tool(
        "start_new_draft",
        "Clear the current draft and start a fresh one.",
        dict(COMMON_ARGS),
        ["caller_phone", "call_id"],
    ),
    _tool(
        "list_my_agents",
        "List the caller's live agents with name, business and hours until expiry.",
        dict(COMMON_ARGS),
        ["caller_phone", "call_id"],
    ),
    _tool(
        "call_me_with_agent",
        "Schedule a test call: the agent calls the owner back. Private (needs a verified caller ID).",
        {
            **COMMON_ARGS,
            "agent_name": {"type": "string", "description": "Which agent to test. Defaults to the newest."},
            "delay_secs": {"type": "integer", "description": "Seconds from now to call back (5 to 86400)."},
        },
        ["caller_phone", "call_id"],
    ),
    _tool(
        "list_messages",
        "Read up to 10 of the newest messages your agents took. Marks them read. Private (needs a verified caller ID).",
        dict(COMMON_ARGS),
        ["caller_phone", "call_id"],
    ),
    _tool(
        "delete_agent",
        "Delete one of the caller's live agents immediately (after they confirm). Private (needs a verified caller ID).",
        {
            **COMMON_ARGS,
            "agent_name": {"type": "string", "description": "Name of the agent to delete."},
            "agent_key": {"type": "string", "description": "Key of the agent to delete, if you listed them."},
        },
        ["caller_phone", "call_id"],
    ),
]

HANDLERS = {
    "list_templates": list_templates,
    "list_voices": list_voices,
    "save_draft": save_draft,
    "get_draft": get_draft,
    "start_new_draft": start_new_draft,
    "list_my_agents": list_my_agents,
    "call_me_with_agent": call_me_with_agent,
    "list_messages": list_messages,
    "delete_agent": delete_agent,
}

FORGE = Server(
    name="agent-forge-mcp",
    instructions=(
        "Tools for Forge, the agent builder. Tools verify each call through the caller's phone number "
        "and call id, and private tools additionally need a verified caller ID. Save details as soon "
        "as you hear them; never invent business facts."
    ),
    tool_defs=TOOL_DEFS,
    handlers=HANDLERS,
    crash_hint="Something went wrong on my side. The draft is saved. Please try again in a minute.",
)
