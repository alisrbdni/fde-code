"""Generated-agent assembly: persona polish, instructions, workflow, tools and
the feature ladder.

The SAFETY block and OWNER_RULE are appended by this code after any LLM
polish, so LLM output can never remove them; if the LLM's persona is empty or
fails the policy re-check we fall back to the deterministic template persona.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional, Tuple

from .catalog import TEMPLATES, VOICES
from .drafts import _clean
from .kv import cached
from .log import log
from .policy import check as policy_check

SAFETY = (
    "Rules you must always follow. You are an AI assistant: if anyone asks, say so, and never claim "
    "to be a person. Never ask for or accept payment card numbers, bank details, passwords, PINs, "
    "one-time codes or social security numbers; if a caller starts to share one, stop them politely. "
    "Do not give medical, legal or financial advice. If someone describes an emergency, tell them to "
    "hang up and dial 911. Only share the facts listed above; if you don't know something, say so and "
    "offer to take a message. Never promise prices, bookings or availability that aren't in the facts. "
    "Keep every reply to one or two short sentences, ask one question at a time, and never read out "
    "lists, symbols or web links."
)

OWNER_RULE = (
    "If {{is_owner}} is true, the caller is the person who built you and is testing you: help them "
    "try anything, and before saying goodbye ask what they would change, then save their answer with "
    "take_message as feedback."
)

POLISH_PROMPT = (
    "You write the persona paragraph for a phone AI assistant. Return JSON with 'persona' (2-3 "
    "sentences, second person 'You are ...', describing role, tone and audience only; no facts, "
    "prices, promises or rules) and 'greeting' (one short spoken sentence that asks how to help; do "
    "not greet with the business name, it is said just before)."
)

POLISH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "persona": {"type": "string"},
        "greeting": {"type": "string"},
    },
    "required": ["persona", "greeting"],
    "additionalProperties": False,
}

POLISH_TTL = 7 * 24 * 3600  # spec: cached for 7 days

# The feature ladder: each level keeps less of the payload. `create_with_ladder`
# (builder.py) walks down it when the Assistants API rejects the more complex
# shape, so a bad day degrades features instead of failing the build.
LADDER: List[str] = ["full", "no_workflow", "no_tools", "minimal"]


def agent_token(secret: str, key: str) -> str:
    """Stable per-agent token for webhook auth: HMAC-SHA256(secret, key)[:32]."""
    digest = hmac.new((secret or "dev-secret").encode("utf-8"), key.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:32]


def template_persona(fields: Dict[str, Any]) -> str:
    agent = fields.get("agent_name") or "the assistant"
    tone = fields.get("tone") or "friendly"
    business = fields.get("business_name") or "this business"
    purpose = fields.get("purpose") or "help callers"
    audience = fields.get("audience") or "callers"
    return (
        f"You are {agent}, the {tone} AI phone assistant for {business}. "
        f"Your job is to {purpose}. You are speaking with {audience}."
    )


def _persona_spec(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Only persona-relevant fields go into the polish prompt and cache key."""
    return {
        "agent_name": fields.get("agent_name") or "",
        "business_name": fields.get("business_name") or "",
        "purpose": fields.get("purpose") or "",
        "tone": fields.get("tone") or "",
        "audience": fields.get("audience") or "",
        "voice": fields.get("voice") or "",
    }


async def polish(app: Any, trace: Any, fields: Dict[str, Any], flags: Any) -> Tuple[str, str, str]:
    """Return (persona, greeting, source in {llm, llm_cache, template}).

    LLM polish is optional and never trusted: any failure, empty output or a
    policy-blocked persona falls back to the deterministic template persona.
    """
    spec = _persona_spec(fields)
    spec_hash = hashlib.sha256(json.dumps(spec, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    fallback = (template_persona(fields), "How can I help you today?", "template")
    if not getattr(flags, "llm_polish", False):
        return fallback

    async def load() -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": POLISH_PROMPT},
            {"role": "user", "content": json.dumps(spec)},
        ]
        return await app.llm.chat_json(app.settings.polish_model, messages, POLISH_SCHEMA)

    try:
        data, hit = await cached(app.kv, trace, f"cache/polish/{spec_hash}", POLISH_TTL, load)
    except Exception as exc:
        log("warn", "polish.failed", reason="inference_error", error=str(exc)[:160], trace_id=trace.trace_id)
        return fallback
    if not isinstance(data, dict):
        return fallback
    persona = _clean(str(data.get("persona") or ""))[:600]
    greeting = _clean(str(data.get("greeting") or ""))[:200]
    if not persona or not greeting:
        return fallback
    # Re-check the polished output with the same rules that gate the draft.
    verdict = policy_check(
        {
            "business_name": fields.get("business_name") or "",
            "purpose": persona,
            "facts": [greeting],
        }
    )
    if not verdict.allowed:
        log("warn", "polish.failed", reason="policy_blocked", trace_id=trace.trace_id)
        return fallback
    return persona, greeting, ("llm_cache" if hit else "llm")


def build_instructions(fields: Dict[str, Any], persona: str) -> str:
    sections: List[str] = [persona]
    tasks: List[Any] = list(fields.get("tasks") or [])
    template = fields.get("template")
    if not tasks and template in TEMPLATES:
        tasks = list(TEMPLATES[template]["tasks"])
    if tasks:
        sections.append("What you do:\n- " + "\n- ".join(str(t) for t in tasks[:10]))
    facts: List[str] = []
    if fields.get("hours"):
        facts.append(f"Hours: {fields['hours']}")
    facts.extend(str(f) for f in (fields.get("facts") or [])[:10])
    if facts:
        sections.append("Facts you may share:\n- " + "\n- ".join(facts))
    if fields.get("escalation_number"):
        sections.append(
            "If a caller asks for a person or says it is urgent, use the transfer tool to pass the call on."
        )
    sections.append(
        "If a caller needs to reach the owner, offer to take a message with their name, "
        "a callback number and what they want to pass on."
    )
    if fields.get("language") == "es":
        sections.append("You can reply in Spanish; if the caller speaks Spanish, answer in Spanish.")
    sections.append(SAFETY)
    sections.append(OWNER_RULE)
    return "\n\n".join(sections)


def _speak_greeting(fields: Dict[str, Any], greeting: str, owner_line: bool = True) -> str:
    agent = fields.get("agent_name") or "the assistant"
    business = fields.get("business_name") or "this business"
    line = " {{owner_line}}" if owner_line else ""
    return (
        f"Hi, thanks for calling {business}. I'm {agent}, an AI assistant.{line} {greeting}"
    ).strip()


def _node(node_id: str, name: str, kind: str, text: str) -> Dict[str, Any]:
    """A speak node says `message` verbatim; a prompt node augments the model
    with `instructions` (append mode keeps the assistant's own persona)."""
    node: Dict[str, Any] = {"id": node_id, "name": name, "type": kind}
    if kind == "speak":
        node["message"] = text
    else:
        node["instructions"] = text
        node["instructions_mode"] = "append"
    return node


def _edge(
    edge_id: str, frm: str, to: str, condition: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    if condition is None:
        condition = {"type": "default"}
    return {
        "id": edge_id,
        "start_node_id": frm,
        "target": {"type": "node", "node_id": to},
        "condition": condition,
    }


def _llm(prompt: str) -> Dict[str, Any]:
    return {"type": "llm", "prompt": prompt}


def _compare(name: str, op: str, value: Any, kind: str = "string_literal") -> Dict[str, Any]:
    return {
        "type": "expression",
        "expression": {
            "type": "comparison",
            "op": op,
            "left": {"type": "variable", "name": name},
            "right": {"type": kind, "value": value},
        },
    }


def build_flow(fields: Dict[str, Any], greeting: str) -> Dict[str, Any]:
    """The generated agent's conversation flow (Appendix B.3).

    Without an escalation number the transfer node and its two edges are
    omitted; everything else is identical.
    """
    has_transfer = bool(fields.get("escalation_number"))
    nodes: List[Dict[str, Any]] = [
        _node("greet", "Greet", "speak", _speak_greeting(fields, greeting)),
        _node(
            "help",
            "Help",
            "prompt",
            "Answer using only the facts in your instructions. Ask one question at a time.",
        ),
        _node(
            "message",
            "Take a message",
            "prompt",
            "Collect the caller's name, a callback number and their message, one at a time. Read "
            "the number back digit by digit. Then call take_message with "
            "call_id={{call_control_id}} and confirm it was saved.",
        ),
        _node(
            "goodbye",
            "Wrap up",
            "prompt",
            "Ask if there is anything else. If not, thank them, say goodbye and use the hangup "
            "tool.",
        ),
    ]
    if has_transfer:
        nodes.append(
            _node(
                "transfer",
                "Transfer",
                "prompt",
                "Say one short sentence that you are connecting them, then use the transfer tool.",
            )
        )
    edges: List[Dict[str, Any]] = [
        _edge("e_greet", "greet", "help"),
        _edge("e_timeout", "help", "goodbye", _compare("telnyx_conversation_duration_secs", ">=", 540, "number_literal")),
    ]
    if has_transfer:
        edges.append(
            _edge("e_help_xfer", "help", "transfer", _llm("The caller asks for a person or says it is urgent."))
        )
    edges.extend(
        [
            _edge(
                "e_help_msg",
                "help",
                "message",
                _llm(
                    "The caller wants to leave a message, asks something the facts do not cover, "
                    "or wants to reach the owner."
                ),
            ),
            _edge("e_help_bye", "help", "goodbye", _llm("The caller's questions are answered and they need nothing else.")),
            _edge("e_msg_bye", "message", "goodbye", _llm("The message was saved with take_message.")),
            _edge("e_msg_help", "message", "help", _llm("The caller has another question instead of leaving a message.")),
            _edge("e_bye_help", "goodbye", "help", _llm("The caller has another question.")),
        ]
    )
    if has_transfer:
        edges.append(
            _edge("e_xfer_msg", "transfer", "message", _llm("The transfer failed, or the caller would rather leave a message."))
        )
    return {"start_node_id": "greet", "nodes": nodes, "edges": edges}


def build_tools(fields: Dict[str, Any], key: str, settings: Any) -> List[Dict[str, Any]]:
    """take_message (authenticated webhook) and hangup; transfer only when the
    draft has an escalation number AND the platform has a number to call from."""
    tools: List[Dict[str, Any]] = [
        {
            "name": "take_message",
            "type": "webhook",
            "webhook": {
                "url": f"{settings.public_api_url}/agents/{key}/tools/take-message",
                "method": "POST",
                "description": "Save the caller's message for the owner, with their name and callback number.",
                "headers": {"Authorization": f"Bearer {agent_token(settings.mcp_token, key)}"},
                "body": {"call_id": "{{call_control_id}}"},
                "body_parameters": {
                    "type": "object",
                    "properties": {
                        "caller_name": {"type": "string", "description": "The caller's name."},
                        "callback_number": {"type": "string", "description": "The number to call them back on."},
                        "message": {"type": "string", "description": "The message, in the caller's words."},
                    },
                    "required": ["message"],
                },
            },
        },
        {"name": "hangup", "type": "hangup"},
    ]
    if fields.get("escalation_number") and settings.forge_number:
        tools.append(
            {
                "name": "transfer",
                "type": "transfer",
                "transfer": {"destination": fields["escalation_number"]},
            }
        )
    return tools


def full_payload(
    key: str,
    fields: Dict[str, Any],
    settings: Any,
    persona: str,
    greeting: str,
) -> Dict[str, Any]:
    """The richest shape: workflow, tools, variables webhook, telephony."""
    flow = build_flow(fields, greeting)
    payload: Dict[str, Any] = {
        "name": f"{fields.get('agent_name', '')} - {fields.get('business_name', '')} [forge {key}]",
        "instructions": build_instructions(fields, persona),
        "model": settings.assistant_model,
        "greeting": "",
        "voice_settings": {"voice": VOICES.get(fields.get("voice") or "warm", VOICES["warm"])["voice"]},
        "telephony_settings": {
            "time_limit_secs": 600,
            "user_idle_timeout_secs": 60,
            "default_texml_app_id": settings.texml_app_id,
        },
        "dynamic_variables_webhook_url": f"{settings.public_api_url}/agents/{key}/dynamic-variables",
        "dynamic_variables_webhook_timeout_ms": 2000,
        "dynamic_variables": {"is_owner": "false", "owner_line": ""},
        "tools": build_tools(fields, key, settings),
        "conversation_flow": flow,
    }
    return payload


def at_level(
    payload: Dict[str, Any], fields: Dict[str, Any], greeting: str, level: str
) -> Dict[str, Any]:
    """Trim a full payload down to a ladder level (always a fresh copy).

    `greeting` is the polished greeting text; below the full level the greet
    node no longer exists, so the payload's own `greeting` field speaks it
    instead (as static text, without the {{owner_line}} variable).
    """
    import copy as _copy

    trimmed = _copy.deepcopy(payload)
    if level == "full":
        return trimmed
    spoken = _speak_greeting(fields, greeting, owner_line=False)
    if level == "no_workflow":
        trimmed.pop("conversation_flow", None)
        trimmed["greeting"] = spoken
        return trimmed
    if level == "no_tools":
        trimmed.pop("conversation_flow", None)
        trimmed.pop("tools", None)
        trimmed["greeting"] = spoken
        return trimmed
    # minimal: the barest thing Telnyx will still accept.
    return {
        "name": trimmed["name"],
        "instructions": trimmed["instructions"],
        "greeting": spoken,
        "telephony_settings": trimmed.get("telephony_settings"),
    }
