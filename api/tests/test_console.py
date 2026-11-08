"""Console: the workflow simulator driven by a scripted model, the console
API, settings overrides, docs, and Cloud Storage archives (Phases 11-13)."""
from __future__ import annotations

import json
import secrets
from datetime import datetime

import pytest

from conftest import OTHER, OWNER, SUNRISE, Client, local_only, make_app
from function import builder, clinic
from function.config import load_settings
from function.func import App, Function
from function.log import Trace
from function.storage import EMPTY_SHA256, redact_payload, sign_v4

ADMIN = "admin-secret"
AUTH = {"authorization": f"Bearer {ADMIN}"}


def tc(name: str, **args):
    """An OpenAI-style tool call, as the scripted model returns it."""
    return {"id": "call_" + secrets.token_hex(3), "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def say(text: str):
    return {"content": text}


def calls(*items):
    return {"content": None, "tool_calls": list(items)}


@pytest.fixture
def capp() -> App:
    return make_app(ADMIN_TOKEN=ADMIN)


@pytest.fixture
def cc(capp) -> Client:
    return Client(Function(capp))


async def _start(cc: Client, assistant: str, phone: str = OWNER):
    resp = await cc.post("/ui/api/chat/start", {"assistant": assistant, "phone": phone}, headers=AUTH)
    assert resp.status == 200, resp
    return resp.json


async def _send(cc: Client, session_id: str, text: str, status: int = 200):
    resp = await cc.post("/ui/api/chat/send", {"session_id": session_id, "text": text}, headers=AUTH)
    assert resp.status == status, resp
    return resp.json


def _types(events, kind):
    return [e for e in events if e["type"] == kind]


# ---------------------------------------------------------------------------
# the simulator
# ---------------------------------------------------------------------------

async def test_forge_chat_builds_an_agent_end_to_end(cc: Client, capp):
    llm = capp.llm
    start = await _start(cc, "forge")
    (welcome,) = _types(start["events"], "speak")
    assert welcome["node"] == "welcome" and "deleted automatically after 24 hours" in welcome["text"]
    assert start["session"]["node"] == "router" and start["session"]["phone"] == "+15***2001"
    sid = start["session"]["id"]

    llm.route_choices = [1]  # router -> discover ("wants to build")
    llm.chat_script = [
        calls(tc("save_draft", values={"business_name": "Sunrise Dental", "template": "receptionist"})),
        say("Lovely. What should the agent be called?"),
    ]
    turn = await _send(cc, sid, "I want an agent for my dental office, Sunrise Dental")
    assert turn["session"]["node"] == "discover"
    (move,) = _types(turn["events"], "transition")
    assert (move["edge"], move["condition"]) == ("e_router_build", "llm")
    assert turn["session"]["draft"]["business_name"] == "Sunrise Dental"

    llm.route_choices = [2]  # discover -> design
    rest = {k: v for k, v in SUNRISE.items() if k not in ("business_name", "template")}
    llm.chat_script = [
        calls(tc("save_draft", values=rest), tc("check_draft")),
        say("Here's what I have: Sam for Sunrise Dental. Shall I build it?"),
    ]
    turn = await _send(cc, sid, "Call it Sam, friendly and warm, here are the facts")
    edges = [e["edge"] for e in _types(turn["events"], "transition")]
    assert edges == ["e_discover_design", "e_design_review"]  # the variable edge fired right after the tool
    tool = _types(turn["events"], "tool")[-1]
    assert tool["name"] == "check_draft" and tool["vars_set"] == {"draft_complete": "true"}
    assert turn["session"]["node"] == "review"

    llm.route_choices = [1]  # review -> build
    llm.chat_script = [calls(tc("build_agent")), say("Sam is live! Want a test call?")]
    turn = await _send(cc, sid, "Yes, build it")
    assert [e["edge"] for e in _types(turn["events"], "transition")] == ["e_review_build", "e_build_ready"]
    assert turn["session"]["node"] == "deliver"
    v = turn["session"]["vars"]
    assert v["build_status"] == "ready" and v["built_agent_name"] == "Sam - Sunrise Dental"

    agents = (await cc.get("/ui/api/agents", headers=AUTH)).json["agents"]
    (agent,) = agents
    assert agent["source"] == "console" and agent["owner"] == "+15***2001"
    detail = (await cc.get(f"/ui/api/agents/{agent['key']}", headers=AUTH)).json
    archive = detail["archive"]
    auth = next(t for t in archive["payload"]["tools"] if t["name"] == "take_message")["webhook"]["headers"]
    assert auth == {"Authorization": "Bearer <redacted>"}
    assert any(s["name"] == "telnyx.create_assistant" for s in archive["spans"])
    assert detail["versions"] == [f"forge/builds/{agent['key']}/latest.json", f"forge/builds/{agent['key']}/v1.json"]


async def test_tool_scoping_is_enforced_per_node(cc: Client, capp):
    sid = (await _start(cc, "forge"))["session"]["id"]
    capp.llm.chat_script = [calls(tc("save_draft", values={"business_name": "Sneaky"})), say("Okay.")]
    turn = await _send(cc, sid, "hello")  # stays in router (route choice 0): no tools allowed
    (tool,) = _types(turn["events"], "tool")
    assert tool["error"] == "The tool save_draft isn't available in this step."
    snap = (await cc.get(f"/ui/api/owner/{OWNER}", headers=AUTH)).json
    assert snap["draft"]["fields"] == {}


async def test_forge_hands_off_to_clara(cc: Client, capp):
    sid = (await _start(cc, "forge"))["session"]["id"]
    capp.llm.route_choices = [3]  # router -> clinic_handoff
    capp.llm.chat_script = [calls(tc("handoff"))]
    turn = await _send(cc, sid, "Actually I need a doctor's appointment for my rash")
    (handoff,) = _types(turn["events"], "handoff")
    assert (handoff["frm"], handoff["to"]) == ("forge", "clinic")
    speak = _types(turn["events"], "speak")[-1]
    assert speak["node"] == "welcome" and "Northside Family Clinic" in speak["text"]
    s = turn["session"]
    assert s["assistant"] == "clinic" and s["chain"] == ["forge", "clinic"] and s["node"] == "intent"
    assert "patient" in s


async def test_clinic_chat_emergency_goes_to_911_speak_node(cc: Client, capp):
    sid = (await _start(cc, "clinic", OTHER))["session"]["id"]
    capp.llm.route_choices = [1]  # intent -> symptoms
    capp.llm.chat_script = [calls(tc("triage_symptoms", symptoms="crushing chest pain and my left arm is numb"))]
    turn = await _send(cc, sid, "I have crushing chest pain")
    edges = [e["edge"] for e in _types(turn["events"], "transition")]
    assert edges == ["e_intent_book", "e_sym_emergency", "e_emergency_wrap"]
    emergency = next(e for e in _types(turn["events"], "speak") if e["node"] == "emergency")
    assert "dial 911" in emergency["text"]
    assert turn["session"]["vars"]["triage_urgency"] == "emergency"


async def test_clinic_chat_books(cc: Client, capp, monkeypatch):
    monkeypatch.setattr(clinic, "local_now", lambda app: datetime(2026, 11, 9, 8, 0))
    sid = (await _start(cc, "clinic", OTHER))["session"]["id"]
    capp.llm.route_choices = [1]
    capp.llm.chat_script = [
        calls(tc("triage_symptoms", symptoms="my knee hurts when I run")),
        calls(tc("check_availability", specialty="orthopedics")),
        say("The earliest is Monday at 9 AM. Does that work?"),
    ]
    turn = await _send(cc, sid, "My knee hurts")
    assert turn["session"]["node"] == "schedule"
    assert turn["session"]["vars"]["matched_specialty"] == "orthopedics"
    options = _types(turn["events"], "tool")[-1]["result"]["options"]
    capp.llm.chat_script = [calls(tc("book_appointment", slot_id=options[0]["slot_id"], full_name="Bo Lee",
                                      reason="knee pain"))]
    turn = await _send(cc, sid, "The first one. I'm Bo Lee")
    booked = next(e for e in _types(turn["events"], "speak") if e["node"] == "booked")
    assert booked["text"].startswith("You're booked with Dr. Tom Becker on Monday, November 9 at 9 AM.")
    assert turn["session"]["node"] == "wrap"
    assert turn["session"]["patient"]["appointments"][0]["status"] == "booked"


async def test_chat_with_generated_agent_takes_message(cc: Client, capp):
    call = await cc.ready_draft()
    key = (await cc.build(call))["agent_key"]
    start = await _start(cc, key, OTHER)
    greet = _types(start["events"], "speak")[0]
    assert greet["text"].startswith("Hi, thanks for calling Sunrise Dental. I'm Sam, an AI assistant.")
    assert start["session"]["vars"]["is_owner"] == "false"
    capp.llm.route_choices = [2]  # help -> message (transfer is option 1)
    capp.llm.chat_script = [calls(tc("take_message", caller_name="Ana", callback_number="5550102002",
                                     message="Please call me about a cleaning")), say("Saved!")]
    turn = await _send(cc, start["session"]["id"], "Can I leave a message?")
    assert turn["session"]["node"] == "message"
    tool = _types(turn["events"], "tool")[0]
    assert tool["error"] is None and tool["result"]["saved"] is True
    inbox = await cc.tool("list_messages", caller_phone=OWNER, call_id=call["_call_id"])
    assert inbox["messages"][0]["message"] == "Please call me about a cleaning"


async def test_ended_session_refuses_input(cc: Client, capp):
    sid = (await _start(cc, "forge"))["session"]["id"]
    capp.llm.route_choices = [4]  # router -> wrap
    capp.llm.chat_script = [calls(tc("hangup"))]
    turn = await _send(cc, sid, "Nothing, thanks, bye")
    assert turn["session"]["ended"] is True and _types(turn["events"], "end")
    await _send(cc, sid, "hello?", status=409)
    again = await cc.get(f"/ui/api/chat/{sid}", headers=AUTH)
    assert again.status == 200 and again.json["session"]["ended"] is True
    assert (await cc.get("/ui/api/chat/ffffffffffff", headers=AUTH)).status == 404


# ---------------------------------------------------------------------------
# console API and settings
# ---------------------------------------------------------------------------

async def test_console_requires_admin_token(cc: Client):
    assert (await cc.get("/ui/api/overview")).status == 401
    assert (await cc.get("/ui/api/overview", headers={"authorization": "Bearer wrong"})).status == 401
    ok = await cc.get("/ui/api/overview", headers=AUTH)
    assert ok.status == 200 and ok.json["mode"]["kv"] == "memory"
    page = await cc.get("/ui")
    assert page.status == 200 and "<title>Agent Forge Console</title>" in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert (await cc.get("/favicon.ico")).status == 204


async def test_settings_override_on_top_of_env(capsys):
    app = make_app(ADMIN_TOKEN=ADMIN, MCP_TOKEN="env-token-0001")
    client = Client(Function(app))
    rows = {r["name"]: r for r in (await client.get("/ui/api/settings", headers=AUTH)).json["settings"]}
    assert rows["MCP_TOKEN"]["source"] == "env" and rows["MCP_TOKEN"]["value"] == "******0001"
    assert rows["ASSISTANT_MODEL"]["source"] == "default"
    capsys.readouterr()
    changed = await client.post("/ui/api/settings", {"values": {"MCP_TOKEN": "override-token-9999"}}, headers=AUTH)
    rows = {r["name"]: r for r in changed.json["settings"]}
    assert rows["MCP_TOKEN"]["source"] == "override" and rows["MCP_TOKEN"]["value"] == "******9999"
    assert "override-token-9999" not in capsys.readouterr().out
    assert (await client.rpc("tools/list", token="env-token-0001")).status == 401
    assert (await client.rpc("tools/list", token="override-token-9999")).status == 200
    refused = await client.post("/ui/api/settings", {"values": {"ADMIN_TOKEN": "mine"}}, headers=AUTH)
    assert refused.status == 400
    cleared = await client.post("/ui/api/settings", {"values": {"MCP_TOKEN": None}}, headers=AUTH)
    assert {r["name"]: r for r in cleared.json["settings"]}["MCP_TOKEN"]["source"] == "env"
    assert not any(k.startswith("settings") for k in app.kv.store)


async def test_reconfigure_keeps_local_state():
    make_app()  # resets the environment
    app = App(load_settings())
    app.auto_alarms = False
    client = Client(Function(app))
    call = await client.start_call()
    await client.save(call, business_name="Sunrise Dental")
    state, kv = app.state, app.kv
    await client.post("/ui/api/settings", {"values": {"ASSISTANT_MODEL": "zai-org/GLM-5.3"}})
    assert app.settings.assistant_model == "zai-org/GLM-5.3"
    assert app.state is state and app.kv is kv
    again = await client.start_call()
    assert again["draft_business"] == "Sunrise Dental"


async def test_form_build_steps_and_flags(cc: Client):
    built = await cc.post("/ui/api/forge/build", {"phone": OWNER, "fields": SUNRISE}, headers=AUTH)
    assert [s["step"] for s in built.json["steps"]] == ["variables", "start_new_draft", "save_draft", "check_draft",
                                                        "build_agent"]
    assert built.json["result"]["build_status"] == "ready"
    assert OWNER not in built.text
    await cc.post("/ui/api/flags/forge", {"builds_enabled": False}, headers=AUTH)
    flags = (await cc.get("/ui/api/flags/forge", headers=AUTH)).json
    assert flags["flags"]["builds_enabled"] is False and flags["source"] in ("kv", "memo")
    paused = await cc.post("/ui/api/forge/build", {"phone": OTHER, "fields": SUNRISE}, headers=AUTH)
    assert paused.json["result"]["build_status"] == "paused"


async def test_flow_endpoint_and_docs(cc: Client):
    forge = (await cc.get("/ui/api/flow/forge", headers=AUTH)).json
    assert len(forge["flow"]["nodes"]) == 14 and forge["node_tools"]["build"] == ["build_agent"]
    assert {t["name"] for t in forge["tools"]} >= {"save_draft", "check_draft", "build_agent", "handoff"}
    clara = (await cc.get("/ui/api/flow/clinic", headers=AUTH)).json
    assert len(clara["flow"]["nodes"]) == 13 and clara["defaults"]["triage_urgency"] == "none"
    assert (await cc.get("/ui/api/flow/nope", headers=AUTH)).status == 404
    spec = (await cc.get("/openapi.json")).json
    assert spec["openapi"].startswith("3.1")
    assert {"/mcp", "/clinic/mcp", "/webhook/dynamic-variables", "/internal/jobs"} <= set(spec["paths"])
    assert "save_draft" in spec["paths"]["/mcp"]["post"]["description"]
    docs = await cc.get("/docs")
    assert docs.status == 200 and "text/html" in docs.headers["content-type"]


async def test_clinic_console_overview_and_booking(cc: Client, monkeypatch):
    monkeypatch.setattr(clinic, "local_now", lambda app: datetime(2026, 11, 9, 8, 0))
    av = (await cc.post("/ui/api/clinic/availability", {"specialty": "ent"}, headers=AUTH)).json
    booked = await cc.post("/ui/api/clinic/book", {"phone": OTHER, "slot_id": av["options"][0]["slot_id"],
                                                   "full_name": "Bo Lee"}, headers=AUTH)
    assert booked.json["booking_status"] == "confirmed"
    over = (await cc.get("/ui/api/clinic", headers=AUTH)).json
    novak = next(c for c in over["clinicians"] if c["id"] == "ent-novak")
    (b,) = novak["bookings"]
    assert b["patient"] == "+15***2002" and av["options"][0]["slot"] not in novak["free_slots"]
    cancelled = await cc.post("/ui/api/clinic/cancel", {"specialist_id": "ent-novak", "appointment_id": b["id"]},
                              headers=AUTH)
    assert cancelled.json["booking_status"] == "cancelled"
    patient = (await cc.get(f"/ui/api/clinic/patient/{OTHER}", headers=AUTH)).json
    assert patient["appointments"][0]["status"] == "cancelled" and "attested" not in patient


# ---------------------------------------------------------------------------
# Cloud Storage + registry
# ---------------------------------------------------------------------------

def test_sigv4_matches_aws_reference_vector():
    # AWS's documented example: GET /test.txt from examplebucket with a Range header.
    headers = sign_v4(
        "GET", "https://examplebucket.s3.amazonaws.com/test.txt",
        {"range": "bytes=0-9", "x-amz-content-sha256": EMPTY_SHA256, "x-amz-date": "20130524T000000Z"},
        EMPTY_SHA256, "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "us-east-1",
        "20130524T000000Z",
    )
    auth = headers["authorization"]
    assert "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request" in auth
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in auth
    assert auth.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_redact_payload():
    payload = {"tools": [
        {"name": "take_message", "type": "webhook", "webhook": {"headers": {"Authorization": "Bearer s3cret"}}},
        {"name": "x", "type": "webhook", "webhook": {"headers": [{"name": "authorization", "value": "Bearer s3cret"}]}},
        {"name": "hangup", "type": "hangup"},
    ]}
    out = redact_payload(payload)
    assert out["tools"][0]["webhook"]["headers"]["Authorization"] == "Bearer <redacted>"
    assert out["tools"][1]["webhook"]["headers"][0]["value"] == "Bearer <redacted>"
    assert payload["tools"][0]["webhook"]["headers"]["Authorization"] == "Bearer s3cret"  # input untouched
    assert "s3cret" not in json.dumps(out)


async def test_storage_outage_never_fails_a_build(cc: Client, capp, capsys):
    capp.storage.fail = True
    built = await cc.build(await cc.ready_draft())
    assert built["build_status"] == "ready"
    assert "record_build_failed" in capsys.readouterr().err
    health = (await cc.get("/ops/health")).json
    assert health["ok"] is True and health["checks"]["storage"]["ok"] is False


@local_only
async def test_registry_tracks_delete(cc: Client, capp):
    key = (await cc.build(await cc.ready_draft()))["agent_key"]
    resp = await cc.post(f"/ui/api/agents/{key}/delete", {}, headers=AUTH)
    assert resp.status == 200
    reg = (await cc.get(f"/ui/api/agents/{key}", headers=AUTH)).json["registry"]
    assert reg["status"] == "deleting"
    await builder.run_due_jobs(capp, Trace("t", "alarm"), OWNER, now=builder._ms() + 1000)
    detail = (await cc.get(f"/ui/api/agents/{key}", headers=AUTH)).json
    assert detail["registry"]["status"] == "deleted" and detail["agent"]["status"] == "deleted"
    assert capp.telnyx.assistants == {}
