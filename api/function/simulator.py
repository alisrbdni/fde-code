"""Workflow simulator: runs an assistant's real conversation_flow over text.

The console chat uses it to drive Forge, Clara or any generated agent with the
same JSON the Telnyx runtime gets: speak nodes are said verbatim, expression
edges are evaluated against the dynamic variables, LLM edges are decided by
one routing call, tools are scoped per node and their results are mapped into
variables (as the Portal's webhook response mapping does). Tool handlers run
in-process, so a console chat exercises the same actors, KV and Telnyx client
as a phone call. Sessions live in KV `session/chat/{id}` for two hours.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from . import clinic, generator, tools, webhook
from .config import mask_phone
from .log import log
from .mcp import ToolError

ASSISTANTS_DIR = Path(__file__).resolve().parent / "assistants"
SESSION_TTL = 7200
MAX_HISTORY = 40
MAX_EVENTS = 300
MAX_TOOL_ROUNDS = 6
HIDDEN_ARGS = ("caller_phone", "call_id")
CONSOLE_NOTE = (
    "Console note: this is a text chat in the operator console that simulates a phone call. "
    "Reply exactly as you would speak on the phone."
)

Runner = Callable[[Any, Any, Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]


class SimError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class ToolSpec:
    name: str
    kind: str  # mcp | webhook | hangup | transfer | handoff
    description: str
    parameters: Dict[str, Any]
    run: Optional[Runner] = None
    var_map: Dict[str, str] = field(default_factory=dict)

    def openai(self) -> Dict[str, Any]:
        params = json.loads(json.dumps(self.parameters or {"type": "object", "properties": {}}))
        props = params.get("properties") or {}
        for hidden in HIDDEN_ARGS:
            props.pop(hidden, None)
        params["properties"] = props
        params["required"] = [r for r in params.get("required") or [] if r not in HIDDEN_ARGS]
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": params}}


@dataclass
class AssistantDef:
    id: str
    name: str
    instructions: str
    defaults: Dict[str, str]
    flow: Dict[str, Any]
    node_tools: Optional[Dict[str, List[str]]]
    tools: Dict[str, ToolSpec]
    init: Callable[[Any, Any, str, str], Awaitable[Dict[str, str]]]
    meta: Dict[str, Any] = field(default_factory=dict)

    def node(self, node_id: str) -> Dict[str, Any]:
        for node in self.flow.get("nodes") or []:
            if node.get("id") == node_id:
                return node
        raise SimError(500, f"unknown node {node_id}")

    def edges_from(self, node_id: str) -> List[Dict[str, Any]]:
        return [e for e in self.flow.get("edges") or [] if e.get("start_node_id") == node_id]

    def tools_for(self, node_id: str) -> List[ToolSpec]:
        if self.node_tools is None:
            return list(self.tools.values())
        return [self.tools[n] for n in self.node_tools.get(node_id, []) if n in self.tools]


# ---------------------------------------------------------------------------
# assistant definitions
# ---------------------------------------------------------------------------

def _load(name: str) -> Dict[str, Any]:
    directory = ASSISTANTS_DIR / name
    assistant = json.loads((directory / "assistant.json").read_text())
    flow = assistant.get("conversation_flow")
    if isinstance(flow, str):
        assistant["conversation_flow"] = json.loads((directory / "conversation_flow.json").read_text())
    node_tools_path = directory / "node_tools.json"
    assistant["_node_tools"] = json.loads(node_tools_path.read_text()) if node_tools_path.exists() else None
    return assistant


def _init_body(phone: str, call_id: str) -> Dict[str, Any]:
    """A Telnyx-style variables payload. The console requires the admin token,
    so the caller ID counts as verified."""
    return {
        "data": {
            "event_type": "assistant.initialization",
            "payload": {
                "telnyx_end_user_target": phone,
                "telnyx_agent_target": "console",
                "call_control_id": call_id,
                "telnyx_end_user_target_verified": True,
                "telnyx_conversation_channel": "web_chat",
            },
        }
    }


def _mcp_runner(handler: Callable) -> Runner:
    async def run(app: Any, trace: Any, session: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
        return await handler(app, trace, {**args, "caller_phone": session["phone"], "call_id": session["call_id"]})

    return run


def _schema_of(tool_def: Dict[str, Any]) -> Dict[str, Any]:
    return tool_def.get("inputSchema") or tool_def.get("parameters") or {"type": "object", "properties": {}}


def _builtin(name: str, kind: str, description: str) -> ToolSpec:
    return ToolSpec(name, kind, description, {"type": "object", "properties": {}})


def forge_def() -> AssistantDef:
    spec = _load("forge")
    specs: Dict[str, ToolSpec] = {}
    for tdef in tools.TOOL_DEFS:
        specs[tdef["name"]] = ToolSpec(tdef["name"], "mcp", tdef["description"], _schema_of(tdef),
                                       _mcp_runner(tools.HANDLERS[tdef["name"]]))

    async def check_draft(app, trace, session, args):
        return await webhook.check_draft(app, trace, {"caller_phone": session["phone"], "call_id": session["call_id"]})

    async def build_agent(app, trace, session, args):
        return await webhook.build_agent(app, trace, {"caller_phone": session["phone"], "call_id": session["call_id"]})

    specs["check_draft"] = ToolSpec("check_draft", "webhook", "Check whether the saved draft is complete and allowed.",
                                    {"type": "object", "properties": {}}, check_draft,
                                    {"draft_complete": "draft_complete"})
    specs["build_agent"] = ToolSpec("build_agent", "webhook", "Build the agent from the saved draft. Call it once.",
                                    {"type": "object", "properties": {}}, build_agent,
                                    {"build_status": "build_status", "built_agent_name": "built_agent_name"})
    specs["hangup"] = _builtin("hangup", "hangup", "End the call.")
    specs["handoff"] = _builtin("handoff", "handoff", "Hand the caller off to Clara at Northside Family Clinic.")

    async def init(app, trace, phone, call_id):
        return (await webhook.forge_variables(app, trace, _init_body(phone, call_id)))["dynamic_variables"]

    return AssistantDef("forge", "Forge", spec["instructions"], dict(spec["dynamic_variables"]),
                        spec["conversation_flow"], spec["_node_tools"], specs, init)


def clinic_def() -> AssistantDef:
    spec = _load("clinic")
    specs: Dict[str, ToolSpec] = {}
    webhook_tools = {"triage_symptoms", "book_appointment"}
    for tdef in clinic.TOOL_DEFS:
        if tdef["name"] in webhook_tools:
            continue
        specs[tdef["name"]] = ToolSpec(tdef["name"], "mcp", tdef["description"], _schema_of(tdef),
                                       _mcp_runner(clinic.HANDLERS[tdef["name"]]))
    by_name = {t["name"]: t for t in clinic.TOOL_DEFS}

    async def triage(app, trace, session, args):
        return await clinic.triage_webhook(app, trace, {**args, "caller_phone": session["phone"], "call_id": session["call_id"]})

    async def book(app, trace, session, args):
        return await clinic.book_webhook(app, trace, {**args, "caller_phone": session["phone"], "call_id": session["call_id"]})

    async def transfer(app, trace, session, args):
        number = app.settings.clinic_front_desk_number
        return {"status": "simulated", "destination": mask_phone(number) if number else "",
                "say": "In the console the transfer is simulated; on a phone call this connects the front desk."}

    specs["triage_symptoms"] = ToolSpec("triage_symptoms", "webhook", by_name["triage_symptoms"]["description"],
                                        _schema_of(by_name["triage_symptoms"]), triage,
                                        {"triage_urgency": "triage_urgency", "matched_specialty": "matched_specialty",
                                         "specialty_label": "specialty_label"})
    specs["book_appointment"] = ToolSpec("book_appointment", "webhook", by_name["book_appointment"]["description"],
                                         _schema_of(by_name["book_appointment"]), book,
                                         {"booking_status": "booking_status", "booking_line": "booking_line"})
    specs["transfer"] = ToolSpec("transfer", "transfer", "Transfer the call to the clinic front desk.",
                                 {"type": "object", "properties": {}}, transfer)
    specs["hangup"] = _builtin("hangup", "hangup", "End the call.")

    async def init(app, trace, phone, call_id):
        return (await clinic.variables(app, trace, _init_body(phone, call_id)))["dynamic_variables"]

    return AssistantDef("clinic", "Clara", spec["instructions"], dict(spec["dynamic_variables"]),
                        spec["conversation_flow"], spec["_node_tools"], specs, init)


async def agent_def(app: Any, trace: Any, key: str) -> AssistantDef:
    """A generated agent, from its build archive (else the registry + API)."""
    payload: Optional[Dict[str, Any]] = None
    try:
        archive = await app.storage.get_json(f"forge/builds/{key}/latest.json")
        payload = (archive or {}).get("payload")
    except Exception as exc:
        log("warn", "sim.archive_read_failed", agent_key=key, error=str(exc)[:120])
    if not payload:
        reg = await app.state.call("registry", "global", "get", key, trace_id=trace.trace_id)
        agent = reg.get("agent") or {}
        if not agent.get("assistantId"):
            raise SimError(404, "unknown agent")
        payload = await app.telnyx.get_assistant(agent["assistantId"])
    flow = payload.get("conversation_flow")
    if not isinstance(flow, dict):
        flow = {
            "start_node_id": "greet",
            "nodes": [
                {"id": "greet", "name": "Greeting", "type": "speak", "message": payload.get("greeting") or "Hello."},
                {"id": "main", "name": "Conversation", "type": "prompt", "instructions": "", "instructions_mode": "append"},
            ],
            "edges": [{"id": "e_greet", "start_node_id": "greet", "target": {"type": "node", "node_id": "main"},
                       "condition": {"type": "default"}}],
        }
    specs: Dict[str, ToolSpec] = {}
    for tool in payload.get("tools") or []:
        name, kind = tool.get("name"), tool.get("type")
        if kind == "webhook" and name == "take_message":
            hook = tool.get("webhook") or {}

            async def take(app, trace, session, args, _key=key):
                auth = f"Bearer {generator.agent_token(app.settings.mcp_token, _key)}"
                body = {**args, "call_id": session["call_id"]}
                try:
                    return await webhook.take_message(app, trace, _key, body, auth)
                except webhook.WebhookError as exc:
                    raise ToolError(exc.message) from exc

            specs[name] = ToolSpec(name, "webhook", hook.get("description") or "Save a message for the owner.",
                                   hook.get("body_parameters") or {"type": "object", "properties": {
                                       "caller_name": {"type": "string"}, "callback_number": {"type": "string"},
                                       "message": {"type": "string"}}}, take)
        elif kind == "hangup":
            specs[name] = _builtin(name, "hangup", "End the call.")
        elif kind == "transfer":
            async def xfer(app, trace, session, args):
                return {"status": "simulated", "say": "In the console the transfer is simulated."}

            specs[name] = ToolSpec(name, "transfer", "Transfer the call to the owner.",
                                   {"type": "object", "properties": {}}, xfer)

    async def init(app, trace, phone, call_id):
        return (await webhook.agent_variables(app, trace, key, _init_body(phone, call_id)))["dynamic_variables"]

    defaults = {str(k): str(v) for k, v in (payload.get("dynamic_variables") or {}).items()}
    return AssistantDef(key, str(payload.get("name") or key), str(payload.get("instructions") or ""), defaults,
                        flow, None, specs, init, {"level": "full" if isinstance(payload.get("conversation_flow"), dict) else "no_workflow"})


async def get_def(app: Any, trace: Any, assistant_id: str) -> AssistantDef:
    if assistant_id == "forge":
        return forge_def()
    if assistant_id == "clinic":
        return clinic_def()
    if assistant_id.startswith("ag"):
        return await agent_def(app, trace, assistant_id)
    raise SimError(404, f"unknown assistant {assistant_id!r}")


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------

def _ms() -> int:
    return int(time.time() * 1000)


def _system_vars(s: Dict[str, Any]) -> Dict[str, str]:
    now = datetime.now(timezone.utc)
    return {
        "telnyx_current_time": now.strftime("%Y-%m-%d %H:%M UTC"),
        "telnyx_current_date": now.strftime("%Y-%m-%d"),
        "telnyx_current_weekday": now.strftime("%A"),
        "telnyx_conversation_channel": "web_chat",
        "telnyx_agent_target": "console",
        "telnyx_end_user_target": s["phone"],
        "telnyx_shaken_stir_attestation": "A",
        "call_control_id": s["call_id"],
        "telnyx_conversation_duration_secs": str(max(0, (_ms() - s["started_at"]) // 1000)),
        "telnyx_last_tool_status_code": str(s.get("last_tool_status", "")),
    }


def substitute(text: str, s: Dict[str, Any]) -> str:
    values = {**_system_vars(s), **s["vars"]}
    import re

    return re.sub(r"\{\{\s*([a-z0-9_]+)\s*\}\}", lambda m: str(values.get(m.group(1), "")), text or "").strip()


def _compare(op: str, left: Any, right: Any) -> bool:
    try:
        lnum, rnum = float(left), float(right)
        left, right = lnum, rnum
    except (TypeError, ValueError):
        left, right = str(left), str(right)
    try:
        return {"==": left == right, "!=": left != right, ">": left > right, ">=": left >= right,
                "<": left < right, "<=": left <= right}.get(op, False)
    except TypeError:
        return False


def _edge_label(edge: Dict[str, Any]) -> str:
    cond = edge.get("condition") or {}
    if cond.get("type") == "expression":
        exp = cond.get("expression") or {}
        return f"{(exp.get('left') or {}).get('name')} {exp.get('op')} {(exp.get('right') or {}).get('value')}"
    if cond.get("type") == "llm":
        return str(cond.get("prompt") or "")
    return "default"


class Engine:
    def __init__(self, app: Any, trace: Any) -> None:
        self.app = app
        self.trace = trace
        self.new_events: List[Dict[str, Any]] = []

    def _event(self, s: Dict[str, Any], etype: str, **data: Any) -> None:
        event = {"seq": s["seq"], "at": _ms(), "type": etype, **data}
        s["seq"] += 1
        s["events"].append(event)
        del s["events"][: max(0, len(s["events"]) - MAX_EVENTS)]
        self.new_events.append(event)

    # -- lifecycle -------------------------------------------------------------

    async def start(self, assistant_id: str, phone: str) -> Dict[str, Any]:
        adef = await get_def(self.app, self.trace, assistant_id)
        s: Dict[str, Any] = {
            "id": secrets.token_hex(6),
            "assistant": adef.id,
            "assistant_name": adef.name,
            "chain": [adef.id],
            "phone": phone,
            "call_id": "web_" + secrets.token_hex(8),
            "started_at": _ms(),
            "vars": {},
            "node": "",
            "visited": [],
            "edges_taken": [],
            "history": [],
            "events": [],
            "seq": 0,
            "ended": False,
        }
        self.trace.set(session=s["id"], assistant=adef.id)
        await self._init_vars(s, adef)
        await self._enter(s, adef, adef.flow["start_node_id"])
        await save(self.app, s)
        return s

    async def _init_vars(self, s: Dict[str, Any], adef: AssistantDef) -> None:
        values = dict(adef.defaults)
        try:
            values.update({k: str(v) for k, v in (await adef.init(self.app, self.trace, s["phone"], s["call_id"])).items()})
        except Exception as exc:
            log("warn", "sim.init_failed", assistant=adef.id, error=str(exc)[:160])
        s["vars"] = values
        shown = {k: (mask_phone(v) if k == "caller_phone" else v) for k, v in values.items()}
        self._event(s, "variables", assistant=adef.id, values=shown)

    async def send(self, s: Dict[str, Any], text: str) -> Dict[str, Any]:
        if s.get("ended"):
            raise SimError(409, "this conversation has ended")
        text = str(text or "").strip()[:2000]
        if not text:
            raise SimError(400, "empty message")
        adef = await get_def(self.app, self.trace, s["assistant"])
        s["history"].append({"role": "user", "content": text})
        self._event(s, "user", text=text)
        await self._advance(s, adef, use_llm=True)
        if not s["ended"]:
            adef = await get_def(self.app, self.trace, s["assistant"])  # a handoff may have switched it
            await self._generate(s, adef)
        _trim(s)
        await save(self.app, s)
        return s

    # -- nodes and edges -----------------------------------------------------------

    async def _enter(self, s: Dict[str, Any], adef: AssistantDef, node_id: str, depth: int = 0) -> None:
        node = adef.node(node_id)
        s["node"] = node_id
        if node_id not in s["visited"]:
            s["visited"].append(node_id)
        if node.get("type") != "speak":
            return
        text = substitute(str(node.get("message") or ""), s)
        if text:
            s["history"].append({"role": "assistant", "content": text})
        self._event(s, "speak", node=node_id, node_name=node.get("name"), text=text)
        if depth > 10:
            return
        for edge in adef.edges_from(node_id):
            if (edge.get("condition") or {}).get("type") == "default":
                self._transition(s, edge)
                await self._enter(s, adef, edge["target"]["node_id"], depth + 1)
                return

    def _transition(self, s: Dict[str, Any], edge: Dict[str, Any]) -> None:
        cond = (edge.get("condition") or {}).get("type") or "default"
        s["edges_taken"].append(edge["id"])
        del s["edges_taken"][:-50]
        self._event(s, "transition", edge=edge["id"], frm=edge["start_node_id"], to=edge["target"]["node_id"],
                    condition=cond, label=_edge_label(edge))

    async def _advance(self, s: Dict[str, Any], adef: AssistantDef, use_llm: bool) -> bool:
        """Follow edges out of the current prompt node: expression edges first,
        then (at most once per turn) one LLM routing decision."""
        moved_any = False
        llm_used = not use_llm
        for _ in range(8):
            node = adef.node(s["node"])
            if node.get("type") == "speak" or s["ended"]:
                break
            edges = [e for e in adef.edges_from(s["node"]) if (e.get("condition") or {}).get("type") != "default"]
            target = self._expression_edge(s, edges)
            if target is None and not llm_used:
                llm_used = True
                target = await self._llm_edge(s, edges)
            if target is None:
                break
            self._transition(s, target)
            await self._enter(s, adef, target["target"]["node_id"])
            moved_any = True
        return moved_any

    def _expression_edge(self, s: Dict[str, Any], edges: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        values = {**_system_vars(s), **s["vars"]}
        for edge in edges:
            cond = edge.get("condition") or {}
            if cond.get("type") != "expression":
                continue
            exp = cond.get("expression") or {}
            name = (exp.get("left") or {}).get("name")
            if name in values and _compare(str(exp.get("op")), values[name], (exp.get("right") or {}).get("value")):
                return edge
        return None

    async def _llm_edge(self, s: Dict[str, Any], edges: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        llm_edges = [e for e in edges if (e.get("condition") or {}).get("type") == "llm"]
        if not llm_edges:
            return None
        turns = [m for m in s["history"] if m.get("role") in ("user", "assistant") and m.get("content")][-8:]
        transcript = "\n".join(f"{m['role']}: {m['content']}" for m in turns)
        options = "\n".join(f"{i}. {e['condition'].get('prompt')}" for i, e in enumerate(llm_edges, start=1))
        messages = [
            {"role": "system", "content": (
                "You route a phone conversation. Read the conversation and the numbered conditions. "
                "Pick the ONE condition that is clearly true right now, or 0 if none is clearly true. "
                "Return JSON {\"choice\": n}.")},
            {"role": "user", "content": f"Conversation:\n{transcript}\n\nConditions:\n{options}"},
        ]
        schema = {"type": "object", "properties": {"choice": {"type": "integer", "minimum": 0,
                                                             "maximum": len(llm_edges)}},
                  "required": ["choice"], "additionalProperties": False}
        try:
            async with self.trace.span("llm.route"):
                picked = await self.app.llm.chat_json(self.app.settings.chat_model, messages, schema)
            choice = int((picked or {}).get("choice") or 0)
        except Exception as exc:
            log("warn", "sim.route_failed", error=str(exc)[:160])
            self._event(s, "system", text="Routing model unavailable; staying in this step.")
            return None
        if 1 <= choice <= len(llm_edges):
            return llm_edges[choice - 1]
        return None

    # -- generation and tools --------------------------------------------------------

    def _system_prompt(self, s: Dict[str, Any], adef: AssistantDef, node: Dict[str, Any]) -> str:
        node_text = str(node.get("instructions") or "")
        if node.get("instructions_mode") == "replace":
            parts = [node_text]
        else:
            parts = [adef.instructions, node_text]
        parts.append(CONSOLE_NOTE)
        return substitute("\n\n".join(p for p in parts if p), s)

    async def _generate(self, s: Dict[str, Any], adef: AssistantDef) -> None:
        for _ in range(MAX_TOOL_ROUNDS):
            if s["ended"]:
                return
            node = adef.node(s["node"])
            if node.get("type") == "speak":
                return
            scoped = adef.tools_for(s["node"])
            allowed = {t.name: t for t in scoped}
            messages = [{"role": "system", "content": self._system_prompt(s, adef, node)}, *s["history"]]
            try:
                async with self.trace.span("llm.chat"):
                    reply = await self.app.llm.chat(self.app.settings.chat_model, messages, [t.openai() for t in scoped])
            except Exception as exc:
                log("warn", "sim.chat_failed", error=str(exc)[:160])
                self._event(s, "system", text=f"The chat model is unavailable ({str(exc)[:80]}).")
                return
            content = str(reply.get("content") or "").strip()
            calls = reply.get("tool_calls") or []
            message: Dict[str, Any] = {"role": "assistant", "content": content or None}
            if calls:
                message["tool_calls"] = calls
            s["history"].append(message)
            if content:
                self._event(s, "assistant", node=s["node"], text=content)
            if not calls:
                return
            for call in calls:
                done = await self._run_tool(s, adef, allowed, call)
                if done == "ended":
                    return
                if done == "handoff":
                    return
            await self._advance(s, adef, use_llm=False)

    async def _run_tool(self, s: Dict[str, Any], adef: AssistantDef, allowed: Dict[str, ToolSpec],
                        call: Dict[str, Any]) -> str:
        fn = call.get("function") or {}
        name = str(fn.get("name") or "")
        call_id = str(call.get("id") or f"call_{secrets.token_hex(4)}")
        try:
            args = json.loads(fn.get("arguments") or "{}") if isinstance(fn.get("arguments"), str) else dict(fn.get("arguments") or {})
        except ValueError:
            args = {}
        for hidden in HIDDEN_ARGS:
            args.pop(hidden, None)
        started = time.monotonic()
        result: Dict[str, Any]
        error: Optional[str] = None
        vars_set: Dict[str, str] = {}
        outcome = ""
        spec = allowed.get(name)
        if spec is None:
            error = f"The tool {name} isn't available in this step."
            result = {"error": error}
        elif spec.kind == "hangup":
            result = {"ok": True}
            outcome = "ended"
        elif spec.kind == "handoff":
            result = {"ok": True, "to": "clinic"}
            outcome = "handoff"
        else:
            try:
                async with self.trace.span(f"tool {name}"):
                    result = await spec.run(self.app, self.trace, s, args)  # type: ignore[misc]
                for key, var in spec.var_map.items():
                    if key in result and result[key] is not None:
                        value = result[key]
                        vars_set[var] = value if isinstance(value, str) else json.dumps(value)
                s["vars"].update(vars_set)
                s["last_tool_status"] = 200
            except ToolError as exc:
                error = str(exc)
                result = {"error": error}
                s["last_tool_status"] = 200
            except Exception as exc:
                log("error", "sim.tool_crashed", tool=name, error=str(exc)[:200])
                error = "The tool failed."
                result = {"error": error}
                s["last_tool_status"] = 500
        ms = round((time.monotonic() - started) * 1000, 1)
        s["history"].append({"role": "tool", "tool_call_id": call_id, "content": json.dumps(result, default=str)[:4000]})
        shown_args = {k: v for k, v in args.items()}
        self._event(s, "tool", name=name, kind=spec.kind if spec else "unknown", args=shown_args,
                    result=None if error else result, error=error, vars_set=vars_set, ms=ms)
        if outcome == "ended":
            s["ended"] = True
            self._event(s, "end", reason="hangup")
        elif outcome == "handoff":
            clara = clinic_def()
            s["assistant"] = clara.id
            s["assistant_name"] = clara.name
            s["chain"].append(clara.id)
            self._event(s, "handoff", frm=adef.id, to=clara.id, name=clara.name)
            s["history"] = [m for m in s["history"] if m.get("role") in ("user", "assistant") and m.get("content")
                            and not m.get("tool_calls")][-6:]
            s["visited"] = []
            await self._init_vars(s, clara)
            await self._enter(s, clara, clara.flow["start_node_id"])
        return outcome


def _trim(s: Dict[str, Any]) -> None:
    history = s["history"]
    if len(history) <= MAX_HISTORY:
        return
    start = len(history) - MAX_HISTORY
    while start < len(history) and history[start].get("role") != "user":
        start += 1
    s["history"] = history[start:]


# ---------------------------------------------------------------------------
# persistence and views
# ---------------------------------------------------------------------------

async def save(app: Any, s: Dict[str, Any]) -> None:
    await app.kv.put(f"session/chat/{s['id']}", json.dumps(s, default=str), ttl_secs=SESSION_TTL)


async def load(app: Any, session_id: str) -> Dict[str, Any]:
    if not session_id or not all(c in "0123456789abcdef" for c in session_id) or len(session_id) != 12:
        raise SimError(400, "bad session id")
    raw = await app.kv.get(f"session/chat/{session_id}")
    if raw is None:
        raise SimError(404, "session not found or expired")
    return json.loads(raw)


def public(s: Dict[str, Any]) -> Dict[str, Any]:
    """What the console sees: phone masked, no raw history."""
    return {
        "id": s["id"],
        "assistant": s["assistant"],
        "assistant_name": s["assistant_name"],
        "chain": s["chain"],
        "phone": mask_phone(s["phone"]),
        "call_id": s["call_id"],
        "node": s["node"],
        "visited": s["visited"],
        "edges_taken": s["edges_taken"][-3:],
        "vars": {k: (mask_phone(v) if k == "caller_phone" else v) for k, v in s["vars"].items()},
        "ended": s["ended"],
        "started_at": s["started_at"],
    }
