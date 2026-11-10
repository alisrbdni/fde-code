"""Forge end to end: variables webhook, MCP tools, build pipeline, alarm jobs,
generated agents and observability (spec Phases 5-9)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

from conftest import OTHER, OWNER, SUNRISE, Client, local_only, make_app
from function import builder, generator, policy
from function.func import Function
from function.log import Trace
from function.telnyx_api import FakeTelnyx, TelnyxError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import validate_workflow  # noqa: E402

HOUR_MS = 3_600_000


def _ms() -> int:
    return int(time.time() * 1000)


def _trace() -> Trace:
    return Trace("test-trace", "test")


async def _profile(app, owner=OWNER):
    return (await app.state.call("builder", owner, "snapshot"))["profile"]


async def _archive(app, key):
    return await app.storage.get_json(f"forge/builds/{key}/latest.json")


# ---------------------------------------------------------------------------
# variables webhook
# ---------------------------------------------------------------------------

async def test_new_builder_gets_defaults_and_wrapper(client: Client):
    resp = await client.post("/webhook/dynamic-variables", client.call_payload(OWNER, "v3:new"))
    assert resp.status == 200
    assert set(resp.json) == {"dynamic_variables"}
    v = resp.json["dynamic_variables"]
    assert v["caller_phone"] == OWNER
    assert v["returning_builder"] == "false"
    assert v["has_draft"] == "false"
    assert v["caller_id_verified"] == "true"
    assert v["welcome_line"].startswith("Tell me about the business")
    assert v["trace_id"] == "v3:new"
    assert resp.headers["x-trace-id"] == "v3:new"
    assert all(isinstance(x, str) for x in v.values())


async def test_returning_builder_hears_saved_draft(client: Client):
    call = await client.start_call()
    await client.save(call, business_name="Sunrise Dental")
    again = await client.start_call()
    assert again["returning_builder"] == "true"
    assert again["has_draft"] == "true"
    assert again["draft_business"] == "Sunrise Dental"
    assert "Welcome back." in again["welcome_line"]
    assert "Sunrise Dental" in again["welcome_line"]


async def test_garbage_body_still_returns_defaults(client: Client):
    resp = await client.post("/webhook/dynamic-variables", raw=b"{not json")
    assert resp.status == 200
    assert resp.json["dynamic_variables"]["build_status"] == "none"


async def test_actor_outage_degrades_not_fails(client: Client, app):
    async def down(*a, **kw):
        raise RuntimeError("actor host unreachable")

    app.state.call = down
    resp = await client.post("/webhook/dynamic-variables", client.call_payload(OWNER, "v3:outage"))
    assert resp.status == 200
    v = resp.json["dynamic_variables"]
    assert v["degraded"] == "true"
    assert v["welcome_line"]


# ---------------------------------------------------------------------------
# drafts and policy
# ---------------------------------------------------------------------------

async def test_draft_accumulates_and_completes(client: Client):
    call = await client.start_call()
    first = await client.save(call, business_name="Sunrise Dental")
    assert not first["draft_complete"]
    assert "agent_name" in first["missing"]
    await client.save(call, agent_name="Sam", template="receptionist")
    await client.save(call, facts=["We are at 12 Oak Street"])
    done = await client.save(call, facts=["New patient cleanings are 99 dollars"])
    assert done["draft_complete"], done
    assert len(done["draft"]["facts"]) == 2
    assert done["draft"]["purpose"]  # pre-filled by the template
    checked = await client.check(call)
    assert checked["draft_complete"] == "true"


async def test_invalid_values_are_explained(client: Client):
    call = await client.start_call()
    res = await client.save(call, business_name="Sunrise Dental", tone="sarcastic", voice="robot",
                            escalation_number="call my cell")
    notes = " ".join(res["notes"])
    assert "tone" in notes and "voice" in notes.lower()
    assert "tone" not in res["draft"] and "voice" not in res["draft"]
    assert "escalation_number" not in res["draft"]


async def test_template_injection_is_stripped(client: Client):
    call = await client.start_call()
    res = await client.save(call, business_name="{{system}} <b>Sunrise</b> Dental")
    name = res["draft"]["business_name"]
    assert "{" not in name and "<" not in name
    assert "Sunrise" in name


async def test_llm_cannot_act_for_another_number(client: Client):
    call = await client.start_call(OWNER)
    other = await client.tool("save_draft", caller_phone=OTHER, call_id=call["_call_id"],
                              values={"business_name": "Hijack"})
    assert other["_is_error"]
    templated = await client.tool("save_draft", caller_phone=OWNER, call_id="{{call_control_id}}",
                                  values={"business_name": "X"})
    assert templated["_is_error"]
    snap = await _profile(client.app, OTHER)
    assert snap["draft"]["fields"] == {}


@pytest.mark.parametrize("fields", [
    {"business_name": "IRS Refund Department"},
    {"business_name": "Amazon Support"},
    {"business_name": "Quick Help", "purpose": "ask callers for their credit card number"},
    {"business_name": "Quick Help", "tasks": ["pretend to be a human"]},
    {"business_name": "Quick Help", "purpose": "threaten to arrest people who owe money"},
])
def test_policy_blocks(fields):
    assert not policy.check(fields).allowed


@pytest.mark.parametrize("fields", [
    {"business_name": "Ups and Downs Yoga"},
    {"business_name": "Corner Cafe", "facts": ["we accept credit cards"]},
    {"business_name": "Sunrise Dental"},
])
def test_policy_allows_normal_businesses(fields):
    assert policy.check(fields).allowed


async def test_blocked_draft_never_builds(client: Client, app):
    call = await client.start_call()
    res = await client.save(call, **{**SUNRISE, "business_name": "Amazon Support"})
    assert res["policy_blocked"] and res["say"]
    assert (await client.check(call))["policy_blocked"] is True
    built = await client.build(call)
    assert built["build_status"] == "blocked"
    assert app.telnyx.assistants == {}


# ---------------------------------------------------------------------------
# the build pipeline
# ---------------------------------------------------------------------------

async def test_build_creates_real_assistant_with_workflow(client: Client, app):
    call = await client.ready_draft()
    built = await client.build(call)
    assert built["build_status"] == "ready", built
    assert built["built_agent_name"] == "Sam - Sunrise Dental"
    assert built["level"] == "full"
    (payload,) = app.telnyx.assistants.values()
    assert payload["conversation_flow"]["start_node_id"] == "greet"
    names = {t["name"] for t in payload["tools"]}
    assert {"take_message", "hangup", "transfer"} <= names
    key = built["agent_key"]
    assert payload["dynamic_variables_webhook_url"] == f"https://forge.test/agents/{key}/dynamic-variables"
    assert "never claim to be a person" in payload["instructions"]  # SAFETY appended by code
    record = json.loads(await app.kv.get(f"agents/{key}"))
    assert record["owner"] == OWNER and record["assistant_id"] == payload["id"]
    reg = await app.state.call("registry", "global", "get", key)
    assert reg["agent"]["status"] == "active"


async def test_double_build_creates_one_assistant(client: Client, app):
    call = await client.ready_draft()
    results = await asyncio.gather(*[client.build(call) for _ in range(4)])
    statuses = [r["build_status"] for r in results]
    assert len(app.telnyx.assistants) == 1, statuses
    fresh = [r for r in results if r["build_status"] == "ready" and not r.get("duplicate")]
    assert len(fresh) == 1
    assert set(statuses) <= {"ready", "building"}


async def test_rebuild_updates_same_assistant(client: Client, app):
    call = await client.ready_draft()
    first = await client.build(call)
    await client.save(call, facts=["Parking is free behind the building"])
    second = await client.build(call)
    assert second["build_status"] == "ready" and not second.get("duplicate")
    assert second["agent_key"] == first["agent_key"]
    assert len(app.telnyx.assistants) == 1
    (payload,) = app.telnyx.assistants.values()
    assert "Parking is free" in payload["instructions"]
    again = await client.build(call)
    assert again.get("duplicate") is True


async def test_quota_enforced(client: Client, app):
    await app.flags.update({"max_agents_per_caller": 1})
    call = await client.ready_draft()
    assert (await client.build(call))["build_status"] == "ready"
    await client.tool("start_new_draft", caller_phone=OWNER, call_id=call["_call_id"])
    await client.save(call, **{**SUNRISE, "business_name": "Moonlight Bakery"})
    blocked = await client.build(call)
    assert blocked["build_status"] == "quota"
    v = await client.start_call()
    assert v["quota_remaining"] == "0"


async def test_ladder_degrades_when_api_rejects_workflow():
    app = make_app()
    app.telnyx.reject = {"conversation_flow"}
    client = Client(Function(app))
    built = await client.build(await client.ready_draft())
    assert built["build_status"] == "ready" and built["level"] == "no_workflow"
    (payload,) = app.telnyx.assistants.values()
    assert "conversation_flow" not in payload
    assert payload["greeting"].startswith("Hi, thanks for calling Sunrise Dental")
    assert "{{" not in payload["greeting"]


async def test_ladder_all_the_way_down():
    app = make_app()
    app.telnyx.reject = {"voice_settings"}
    client = Client(Function(app))
    built = await client.build(await client.ready_draft())
    assert built["level"] == "minimal"
    archive = await _archive(app, built["agent_key"])
    assert len(archive["ladder_warnings"]) == 3


async def test_transient_api_error_is_retried(client: Client, app):
    app.telnyx.next_fail(TelnyxError(503, "busy", "POST /ai/assistants"))
    built = await client.build(await client.ready_draft())
    assert built["build_status"] == "ready" and built["level"] == "full"
    archive = await _archive(app, built["agent_key"])
    assert any("retried" in w for w in archive["ladder_warnings"])


async def test_failed_build_releases_lock(client: Client, app):
    call = await client.ready_draft()
    app.telnyx.next_fail(TelnyxError(500, "down", "POST /ai/assistants"))
    app.telnyx.next_fail(TelnyxError(500, "down", "POST /ai/assistants"))
    failed = await client.build(call)
    assert failed["build_status"] == "failed"
    draft = (await _profile(app))["draft"]
    assert draft["status"] == "editing" and draft["lastError"]
    assert (await client.build(call))["build_status"] == "ready"


async def test_inference_failure_falls_back_to_template(client: Client, app):
    app.llm.fail_chat = True
    built = await client.build(await client.ready_draft())
    assert built["build_status"] == "ready"
    archive = await _archive(app, built["agent_key"])
    assert archive["persona_source"] == "template"
    assert archive["persona"].startswith("You are Sam, the friendly AI phone assistant for Sunrise Dental")


async def test_polish_is_cached_by_spec(client: Client, app):
    call = await client.ready_draft()
    first = await client.build(call)
    assert (await _archive(app, first["agent_key"]))["persona_source"] == "llm"
    await client.save(call, facts=["Open late on Thursdays"])  # not a persona field
    second = await client.build(call)
    assert (await _archive(app, second["agent_key"]))["persona_source"] == "llm_cache"


async def test_builds_paused_by_flag(client: Client, app):
    call = await client.ready_draft()
    await app.flags.update({"builds_enabled": False})
    v = await client.start_call()
    assert v["builds_enabled"] == "false"
    assert v["maintenance_message"] and v["welcome_line"] == v["maintenance_message"]
    assert (await client.build(call))["build_status"] == "paused"
    assert app.telnyx.assistants == {}


# ---------------------------------------------------------------------------
# alarm jobs: test calls, expiry, delete
# ---------------------------------------------------------------------------

@local_only
async def test_call_me_schedules_outbound_call_to_owner_only(client: Client, app):
    call = await client.ready_draft()
    built = await client.build(call)
    res = await client.tool("call_me_with_agent", caller_phone=OWNER, call_id=call["_call_id"])
    assert not res["_is_error"] and "Hang up" in res["say"].replace("hang up", "Hang up")
    assert app.telnyx.calls == []  # not before the delay
    await builder.run_due_jobs(app, _trace(), OWNER, now=_ms() + 60_000)
    (placed,) = app.telnyx.calls
    assert placed["to"] == OWNER
    assert placed["from"] == "+15550100000"
    assert placed["connection_id"] == "texml-test"
    agent = next(a for a in (await _profile(app))["agents"] if a["key"] == built["agent_key"])
    assert placed["assistant_id"] == agent["assistantId"]
    assert not [j for j in (await _profile(app))["jobs"] if j["type"] == "test_call"]


async def test_test_calls_rate_limited(client: Client, app):
    await app.flags.update({"max_test_calls_per_hour": 2})
    call = await client.ready_draft()
    await client.build(call)
    args = dict(caller_phone=OWNER, call_id=call["_call_id"])
    assert not (await client.tool("call_me_with_agent", **args))["_is_error"]
    assert not (await client.tool("call_me_with_agent", **args))["_is_error"]
    third = await client.tool("call_me_with_agent", **args)
    assert third["_is_error"] and "2 test calls per hour" in third["error"]


async def test_private_tools_need_verified_caller_id(client: Client):
    call = await client.ready_draft(attested=False)
    await client.build(call)
    for name in ("call_me_with_agent", "list_messages", "delete_agent"):
        res = await client.tool(name, caller_phone=OWNER, call_id=call["_call_id"])
        assert res["_is_error"] and "verified" in res["error"], name
    public = await client.tool("list_my_agents", caller_phone=OWNER, call_id=call["_call_id"])
    assert not public["_is_error"] and public["count"] == 1


@local_only
async def test_agent_auto_expires(client: Client, app):
    call = await client.ready_draft()
    built = await client.build(call)
    key = built["agent_key"]
    await builder.run_due_jobs(app, _trace(), OWNER, now=_ms() + 2 * HOUR_MS)
    assert len(app.telnyx.assistants) == 1  # not yet
    await builder.run_due_jobs(app, _trace(), OWNER, now=_ms() + 25 * HOUR_MS)
    assert app.telnyx.assistants == {}
    assert await app.kv.get(f"agents/{key}") is None
    agents = await client.tool("list_my_agents", caller_phone=OWNER, call_id=call["_call_id"])
    assert agents["count"] == 0
    reg = await app.state.call("registry", "global", "get", key)
    assert reg["agent"]["status"] == "deleted"


@local_only
async def test_delete_agent(client: Client, app):
    call = await client.ready_draft()
    built = await client.build(call)
    res = await client.tool("delete_agent", caller_phone=OWNER, call_id=call["_call_id"], agent_name="Sam")
    assert not res["_is_error"], res
    reg = await app.state.call("registry", "global", "get", built["agent_key"])
    assert reg["agent"]["status"] == "deleting"
    await builder.run_due_jobs(app, _trace(), OWNER, now=_ms() + 1000)
    assert app.telnyx.assistants == {}
    agent = next(a for a in (await _profile(app))["agents"] if a["key"] == built["agent_key"])
    assert agent["status"] == "deleted"
    assert (await _profile(app))["jobs"] == []


async def test_due_jobs_run_in_time_order(app):
    order = []

    class Recording(FakeTelnyx):
        async def ai_call(self, *a, **kw):
            order.append("ai_call")
            return await super().ai_call(*a, **kw)

        async def delete_assistant(self, assistant_id):
            order.append("delete")
            return await super().delete_assistant(assistant_id)

    app.telnyx = Recording()
    jobs = [
        {"id": "j2", "type": "expire", "agentKey": "ag00000002", "assistantId": "A2", "owner": OWNER, "at": 2000},
        {"id": "j1", "type": "test_call", "agentKey": "ag00000001", "assistantId": "A1", "owner": OWNER, "at": 1000},
    ]
    results = await builder.run_jobs(app, _trace(), jobs)
    assert order == ["ai_call", "delete"]
    assert [r["id"] for r in results] == ["j1", "j2"] and all(r["ok"] for r in results)


@local_only
async def test_retryable_job_failure_is_requeued(client: Client, app):
    call = await client.ready_draft()
    await client.build(call)
    await client.tool("call_me_with_agent", caller_phone=OWNER, call_id=call["_call_id"])
    app.telnyx.next_fail(TelnyxError(503, "busy", "POST /texml/ai_calls"))
    now = _ms() + 60_000
    out = await builder.run_due_jobs(app, _trace(), OWNER, now=now)
    assert out["results"][0]["ok"] is False and not out["results"][0].get("permanent")
    retry = next(j for j in (await _profile(app))["jobs"] if j["type"] == "test_call")
    assert retry["attempts"] == 1 and retry["at"] == now + 60_000
    await builder.run_due_jobs(app, _trace(), OWNER, now=now + 61_000)
    assert len(app.telnyx.calls) == 1


async def test_missing_texml_config_is_permanent(app):
    app.settings = type(app.settings)(**{**app.settings.__dict__, "texml_app_id": ""})
    job = {"id": "j", "type": "test_call", "agentKey": "ag1", "assistantId": "A", "owner": OWNER, "at": 1}
    (res,) = await builder.run_jobs(app, _trace(), [job])
    assert res["ok"] is False and res["permanent"] is True


# ---------------------------------------------------------------------------
# generated agents at runtime
# ---------------------------------------------------------------------------

async def test_generated_agent_knows_its_owner_and_takes_messages(client: Client, app):
    call = await client.ready_draft()
    key = (await client.build(call))["agent_key"]
    owner_vars = await client.post(f"/agents/{key}/dynamic-variables", client.call_payload(OWNER, "v3:t1"))
    assert owner_vars.json["dynamic_variables"]["is_owner"] == "true"
    assert "test call" in owner_vars.json["dynamic_variables"]["owner_line"]
    stranger = await client.post(f"/agents/{key}/dynamic-variables", client.call_payload(OTHER, "v3:t2"))
    assert stranger.json["dynamic_variables"] == {"is_owner": "false", "owner_line": ""}

    token = generator.agent_token(app.settings.mcp_token, key)
    msg = {"caller_name": "Ana", "callback_number": "555 010 2002", "message": "Please call me back", "call_id": "v3:t2"}
    bad = await client.post(f"/agents/{key}/tools/take-message", msg, headers={"authorization": "Bearer nope"})
    assert bad.status == 401
    ok = await client.post(f"/agents/{key}/tools/take-message", msg, headers={"authorization": f"Bearer {token}"})
    assert ok.status == 200 and ok.json["saved"] is True
    inbox = await client.tool("list_messages", caller_phone=OWNER, call_id=call["_call_id"])
    (first,) = inbox["messages"]
    assert first["from"] == "Ana" and first["callback_number"] == OTHER
    assert (await client.start_call())["unread_messages"] == "0"  # list_messages marked them read


async def test_concurrent_messages_all_land(client: Client, app):
    call = await client.ready_draft()
    key = (await client.build(call))["agent_key"]
    headers = {"authorization": f"Bearer {generator.agent_token(app.settings.mcp_token, key)}"}
    await asyncio.gather(*[
        client.post(f"/agents/{key}/tools/take-message", {"caller_name": f"C{i}", "message": f"msg {i}"}, headers=headers)
        for i in range(8)
    ])
    assert (await client.start_call())["unread_messages"] == "8"


async def test_unknown_agent_route_is_404(client: Client):
    resp = await client.post("/agents/not-a-key/dynamic-variables", {})
    assert resp.status == 404


# ---------------------------------------------------------------------------
# MCP protocol
# ---------------------------------------------------------------------------

async def test_mcp_initialize_and_list(client: Client):
    init = await client.rpc("initialize", {"protocolVersion": "2025-03-26"})
    result = init.json["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "agent-forge-mcp"
    assert "tools" in result["capabilities"]
    newest = await client.rpc("initialize", {"protocolVersion": "1999-01-01"})
    assert newest.json["result"]["protocolVersion"] == "2025-06-18"
    listed = await client.rpc("tools/list")
    tools = listed.json["result"]["tools"]
    assert len(tools) == 9
    assert all("inputSchema" in t and t["inputSchema"]["type"] == "object" for t in tools)
    assert (await client.rpc("ping")).json["result"] == {}


async def test_mcp_errors(client: Client):
    bad = await client.post("/mcp", raw=b"{oops")
    assert bad.status == 400 and bad.json["error"]["code"] == -32700
    invalid = await client.post("/mcp", {"jsonrpc": "2.0", "id": 1})
    assert invalid.json["error"]["code"] == -32600
    unknown = await client.rpc("resources/list")
    assert unknown.json["error"]["code"] == -32601 and "result" not in unknown.json
    note = await client.post("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note.status == 202 and note.body == b""
    assert (await client.get("/mcp")).status == 405
    tool = await client.tool("no_such_tool")
    assert tool["_is_error"]
    batch = await client.post("/mcp", [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    assert [r["id"] for r in batch.json] == [1, 2]


async def test_mcp_token_enforced_when_set():
    app = make_app(MCP_TOKEN="mcp-secret")
    client = Client(Function(app))
    assert (await client.rpc("tools/list")).status == 401
    assert (await client.rpc("tools/list", token="mcp-secret")).status == 200
    assert (await client.post("/tools/check-draft?token=mcp-secret", {})).status == 200
    assert (await client.post("/tools/check-draft", {})).status == 401


# ---------------------------------------------------------------------------
# internal jobs + observability
# ---------------------------------------------------------------------------

async def test_internal_jobs_requires_token():
    app = make_app(STATE_TOKEN="state-secret")
    client = Client(Function(app))
    assert (await client.post("/internal/jobs", {"jobs": []})).status == 401
    ok = await client.post("/internal/jobs", {"jobs": []}, headers={"authorization": "Bearer state-secret"})
    assert ok.status == 200 and ok.json == {"results": []}
    bad = await client.post("/internal/jobs", {"nope": 1}, headers={"authorization": "Bearer state-secret"})
    assert bad.status == 400


async def test_metrics_health_and_log_line(capsys):
    app = make_app(ADMIN_TOKEN="admin-secret")
    client = Client(Function(app))
    capsys.readouterr()
    await client.start_call(OWNER, call_id="v3:logline")
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    (req,) = [line for line in lines if line["event"] == "request"]
    assert req["route"] == "forge_variables" and req["trace_id"] == "v3:logline"
    assert req["caller"] == "+15***2001" and OWNER not in json.dumps(req)
    assert req["level"] == "info" and req["outcome"] == "ok" and "latency_ms" in req

    await client.post("/tools/build-agent", raw=b"{bad")  # a client error, counted
    assert (await client.get("/ops/metrics")).status == 401
    admin = {"authorization": "Bearer admin-secret"}
    metrics = (await client.get("/ops/metrics?window=5", headers=admin)).json
    assert metrics["by_route"]["forge_variables"]["count"] == 1
    assert metrics["by_route"]["build_agent"]["count"] == 1
    assert metrics["errors"] == 0
    health = await client.get("/ops/health")
    assert health.status == 200 and health.json["ok"] is True
    assert health.json["checks"]["kv"]["ok"] and health.json["checks"]["actors"]["ok"]
    timing = health.headers["server-timing"]
    assert "kv.probe;dur=" in timing and "total;dur=" in timing
    flags = await client.post("/ops/flags", {"clinic": {"bookings_enabled": False}}, headers=admin)
    assert flags.json["clinic"]["bookings_enabled"] is False
    snap = await client.get(f"/ops/builder/{OWNER}", headers=admin)
    assert snap.json["owner"] == "+15***2001" and "attested" not in snap.json


async def test_health_reports_unhealthy_error_rate(client: Client, app):
    for _ in range(6):
        await app.state.call("ops", "global", "record", "build_agent", "error", 10, _ms(), "t", "boom")
    resp = await client.get("/ops/health")
    assert resp.status == 503 and resp.json["ok"] is False


async def test_crash_in_a_webhook_falls_back(client: Client, app, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr("function.webhook.build_agent", boom)
    resp = await client.post("/tools/build-agent", {"caller_phone": OWNER, "call_id": "x"})
    assert resp.status == 200 and resp.json["build_status"] == "failed"
    assert "The draft is saved." in resp.json["message"]


async def test_body_too_large_is_413(client: Client):
    resp = await client.post("/tools/check-draft", raw=b"x" * (257 * 1024))
    assert resp.status == 413


@pytest.mark.parametrize("escalation", [True, False], ids=["escalation_on", "escalation_off"])
def test_generated_workflow_passes_validator(escalation):
    fields = dict(SUNRISE)
    if not escalation:
        fields.pop("escalation_number")
    flow = generator.build_flow(fields, "How can I help you today?")
    errors = validate_workflow.validate(flow, {"is_owner": "false", "owner_line": ""})
    assert errors == []
    ids = {n["id"] for n in flow["nodes"]}
    assert ("transfer" in ids) is escalation


async def test_rest_kv_wire_format(monkeypatch):
    """RestKV must match the live Telnyx KV REST API: raw value body (text/plain), ?ttl_secs=N,
    GET returns the raw value, 404 = missing. (Found against the real API: a JSON envelope was
    stored verbatim, and join_url crashed on three segments.)"""
    from function import kv as kvmod
    from function.http import HttpResponse

    sent = []
    store = {}

    async def fake_request(method, url, headers=None, body=None, timeout=10):
        sent.append((method, url, dict(headers or {}), body))
        key = url.split("/keys/")[1].split("?")[0]
        if method == "PUT":
            store[key] = body
            return HttpResponse(200, b"{}")
        if method == "GET":
            return HttpResponse(200, store[key]) if key in store else HttpResponse(404, b"{}")
        store.pop(key, None)
        return HttpResponse(200, b"{}")

    monkeypatch.setattr(kvmod, "request", fake_request)
    kv = kvmod.RestKV("https://api.telnyx.com/v2/storage/kvs/", "ns-1", "KEY")
    await kv.put("flags/global", '{"a": 1}', ttl_secs=60)
    method, url, headers, body = sent[-1]
    assert url == "https://api.telnyx.com/v2/storage/kvs/ns-1/keys/flags%2Fglobal?ttl_secs=60"
    assert body == b'{"a": 1}' and headers["content-type"].startswith("text/plain")
    assert headers["authorization"] == "Bearer KEY"
    assert await kv.get("flags/global") == '{"a": 1}'
    await kv.delete("flags/global")
    assert await kv.get("flags/global") is None


async def test_tokens_required_on_public_deployment():
    """REQUIRE_TOKENS (auto-on for https API_URL): a missing secret refuses, never opens the route."""
    app = make_app(REQUIRE_TOKENS="true")
    client = Client(Function(app))
    call = client.call_payload(OWNER, "v3:closed")
    assert (await client.post("/webhook/dynamic-variables", call)).status == 401
    assert (await client.post("/clinic/webhook/dynamic-variables", call)).status == 401
    assert (await client.rpc("tools/list")).status == 401
    assert (await client.get("/ui/api/overview")).status == 401
    assert (await client.post("/internal/jobs", {"jobs": []})).status in (401, 500)
    resp = await client.post("/agents/ag12345678/tools/take-message", {"message": "hi"},
                             headers={"authorization": "Bearer " + generator.agent_token("", "ag12345678")})
    assert resp.status == 500  # no dev-secret fallback in production
    assert (await client.get("/health")).status == 200


async def test_variables_webhook_needs_token_when_set():
    app = make_app(MCP_TOKEN="mcp-secret", REQUIRE_TOKENS="true")
    client = Client(Function(app))
    call = client.call_payload(OWNER, "v3:tok")
    assert (await client.post("/webhook/dynamic-variables", call)).status == 401
    ok = await client.post("/webhook/dynamic-variables?token=mcp-secret", call)
    assert ok.status == 200 and ok.json["dynamic_variables"]["caller_phone"] == OWNER
    assert (await client.post("/clinic/webhook/dynamic-variables?token=mcp-secret", call)).status == 200


def test_require_tokens_auto_from_api_url(monkeypatch):
    from function.config import load_settings
    monkeypatch.delenv("REQUIRE_TOKENS", raising=False)
    monkeypatch.setenv("API_URL", "https://forge-api-x.telnyxcompute.com")
    assert load_settings().require_tokens is True
    monkeypatch.setenv("API_URL", "http://localhost:8080")
    assert load_settings().require_tokens is False
