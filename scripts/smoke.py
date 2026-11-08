#!/usr/bin/env python3
"""Walk a real call through a running forge-api and print PASS/FAIL per step.

    python3 scripts/smoke.py --base http://localhost:8080 [--mcp-token T] [--admin-token T]
                             [--phone +15550102001] [--test-call --wait 40]

Steps: Forge variables -> MCP initialize/list -> save_draft x3 -> check_draft ->
build_agent -> agent variables -> take_message with a bad token (401) ->
list agents -> [test call + wait + no pending job] -> clinic variables ->
triage (emergency, dermatology) -> availability -> book -> cancel -> health ->
metrics. The exit code is the number of failed steps.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

RESULTS: List[Tuple[str, bool, float, str]] = []


class Api:
    def __init__(self, base: str, mcp_token: str, admin_token: str) -> None:
        self.base = base.rstrip("/")
        self.mcp_token = mcp_token
        self.admin_token = admin_token

    def call(self, method: str, path: str, body: Any = None, token: str = "",
             headers: Optional[Dict[str, str]] = None) -> Tuple[int, Any, Dict[str, str]]:
        data = None if body is None else json.dumps(body).encode()
        hdrs = {"content-type": "application/json", "user-agent": "agent-forge-smoke/1.0", **(headers or {})}
        if token:
            hdrs["authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None), dict(resp.headers)
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw), dict(err.headers)
            except ValueError:
                return err.code, raw.decode("utf-8", "replace"), dict(err.headers)

    def rpc(self, path: str, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        status, body, _ = self.call("POST", path, {"jsonrpc": "2.0", "id": 1, "method": method,
                                                   "params": params or {}}, token=self.mcp_token)
        assert status == 200, f"HTTP {status}: {body}"
        assert "error" not in body, body
        return body["result"]

    def tool(self, path: str, name: str, **args: Any) -> Dict[str, Any]:
        result = self.rpc(path, "tools/call", {"name": name, "arguments": args})
        assert not result.get("isError"), result["structuredContent"]
        return result["structuredContent"]


def step(name: str, fn: Callable[[], str]) -> Any:
    started = time.monotonic()
    try:
        note = fn() or ""
        ok = True
    except Exception as exc:  # noqa: BLE001 - the smoke test reports every failure
        note, ok = f"{type(exc).__name__}: {str(exc)[:200]}", False
    ms = (time.monotonic() - started) * 1000
    RESULTS.append((name, ok, ms, note))
    print(f"{'PASS' if ok else 'FAIL'}  {ms:7.0f} ms  {name}{('  -- ' + note) if note else ''}", flush=True)
    return ok


def payload(phone: str, call_id: str) -> Dict[str, Any]:
    return {"data": {"event_type": "assistant.initialization", "payload": {
        "telnyx_end_user_target": phone, "call_control_id": call_id, "telnyx_end_user_target_verified": True,
        "telnyx_agent_target": "+15550100000"}}}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://localhost:8080")
    ap.add_argument("--mcp-token", default="")
    ap.add_argument("--admin-token", default="")
    ap.add_argument("--phone", default="+1555" + "".join(secrets.choice("0123456789") for _ in range(7)),
                    help="caller ID to use (default: a random 555 number, so reruns start clean)")
    ap.add_argument("--patient", default="")
    ap.add_argument("--test-call", action="store_true", help="schedule a real outbound test call")
    ap.add_argument("--wait", type=int, default=35, help="seconds to wait for the test call alarm")
    a = ap.parse_args()
    api = Api(a.base, a.mcp_token, a.admin_token)
    phone = a.phone
    patient = a.patient or "+1555" + "".join(secrets.choice("0123456789") for _ in range(7))
    call_id = "v3:smoke-" + secrets.token_hex(6)
    args = {"caller_phone": phone, "call_id": call_id}
    ctx: Dict[str, Any] = {}
    print(f"smoke test against {api.base} (caller {phone[:5]}***{phone[-4:]}, call {call_id})")

    def forge_vars() -> str:
        status, body, headers = api.call("POST", "/webhook/dynamic-variables", payload(phone, call_id))
        assert status == 200, status
        v = body["dynamic_variables"]
        assert v["caller_phone"] == phone and headers.get("x-trace-id") == call_id, v
        return f"degraded={v['degraded']} builds_enabled={v['builds_enabled']}"

    def mcp_list() -> str:
        init = api.rpc("/mcp", "initialize", {"protocolVersion": "2025-06-18"})
        tools = api.rpc("/mcp", "tools/list")["tools"]
        assert len(tools) == 9, len(tools)
        return f"{init['serverInfo']['name']} {init['protocolVersion']}, {len(tools)} tools"

    def save(values: Dict[str, Any]) -> Callable[[], str]:
        def run() -> str:
            res = api.tool("/mcp", "save_draft", **args, values=values)
            ctx["complete"] = res["draft_complete"]
            return f"complete={res['draft_complete']} missing={res['missing']}"
        return run

    def check() -> str:
        status, body, _ = api.call("POST", "/tools/check-draft", args, token=a.mcp_token)
        assert status == 200 and body["draft_complete"] == "true", body
        return body["message"]

    def build() -> str:
        status, body, headers = api.call("POST", "/tools/build-agent", args, token=a.mcp_token)
        assert status == 200 and body["build_status"] == "ready", body
        ctx["key"] = body["agent_key"]
        return f"{body['built_agent_name']} level={body['level']} {headers.get('server-timing', '')[:80]}"

    def agent_vars() -> str:
        status, body, _ = api.call("POST", f"/agents/{ctx['key']}/dynamic-variables", payload(phone, "v3:smoke-agent"))
        assert status == 200 and body["dynamic_variables"]["is_owner"] == "true", body
        return "is_owner=true"

    def bad_token() -> str:
        status, _, _ = api.call("POST", f"/agents/{ctx['key']}/tools/take-message",
                                {"message": "hi"}, token="definitely-wrong")
        assert status == 401, status
        return "401 as expected"

    def list_agents() -> str:
        res = api.tool("/mcp", "list_my_agents", **args)
        assert res["count"] >= 1, res
        return res["say"]

    def test_call() -> str:
        res = api.tool("/mcp", "call_me_with_agent", **args, delay_secs=5)
        return res["say"]

    def wait_job() -> str:
        deadline = time.time() + a.wait
        while time.time() < deadline:
            time.sleep(3)
            status, body, _ = api.call("GET", f"/ops/builder/{phone}", token=a.admin_token)
            assert status == 200, f"HTTP {status} (pass --admin-token)"
            pending = [j for j in body.get("jobs") or [] if j["type"] == "test_call"]
            if not pending:
                return "alarm ran: no pending test_call job"
        raise AssertionError("test_call job still pending")

    clinic_call = "v3:smoke-clinic-" + secrets.token_hex(4)
    cargs = {"caller_phone": patient, "call_id": clinic_call}

    def clinic_vars() -> str:
        status, body, _ = api.call("POST", "/clinic/webhook/dynamic-variables", payload(patient, clinic_call))
        assert status == 200, status
        v = body["dynamic_variables"]
        return f"front_desk_open={v['front_desk_open']} booking_enabled={v['booking_enabled']}"

    def triage_emergency() -> str:
        res = api.tool("/clinic/mcp", "triage_symptoms", **cargs, symptoms="crushing chest pain")
        assert res["triage_urgency"] == "emergency", res
        return res["say"]

    def triage_derm() -> str:
        res = api.tool("/clinic/mcp", "triage_symptoms", **cargs, symptoms="an itchy rash on my arm")
        assert res["matched_specialty"] == "dermatology", res
        return res["say"]

    def availability() -> str:
        res = api.tool("/clinic/mcp", "check_availability", specialty="dermatology")
        assert res["options"], res
        ctx["slot_id"] = res["options"][0]["slot_id"]
        return res["say"][:100]

    def book() -> str:
        res = api.tool("/clinic/mcp", "book_appointment", **cargs, slot_id=ctx["slot_id"], full_name="Smoke Test")
        assert res["booking_status"] == "confirmed", res
        ctx["appointment"] = res["appointment_id"]
        return res["booking_line"]

    def cancel() -> str:
        res = api.tool("/clinic/mcp", "cancel_appointment", **cargs, appointment_id=ctx["appointment"])
        assert res["booking_status"] == "cancelled", res
        return res["message"]

    def health() -> str:
        status, body, _ = api.call("GET", "/ops/health")
        assert status == 200 and body["ok"], body
        return ", ".join(f"{k}={'ok' if v['ok'] else 'FAIL'}" for k, v in body["checks"].items())

    def metrics() -> str:
        status, body, _ = api.call("GET", "/ops/metrics?window=5", token=a.admin_token)
        assert status == 200, f"HTTP {status} (pass --admin-token)"
        return f"requests={body['requests']} error_rate={body['error_rate']} p95={body['latency_p95_ms']}ms"

    step("forge variables webhook", forge_vars)
    step("forge MCP initialize + tools/list", mcp_list)
    step("save_draft: business", save({"business_name": "Smoke Test Bakery", "template": "restaurant"}))
    step("save_draft: agent", save({"agent_name": "Riley", "tone": "upbeat", "voice": "bright"}))
    step("save_draft: facts", save({"hours": "Daily 7 AM to 3 PM", "facts": ["Sourdough is baked every morning"]}))
    step("check_draft webhook", check)
    if step("build_agent webhook", build):
        step("generated agent variables (owner)", agent_vars)
        step("take_message rejects a bad token", bad_token)
        step("list_my_agents", list_agents)
        if a.test_call and step("call_me_with_agent", test_call):
            step("alarm places the call", wait_job)
    step("clinic variables webhook", clinic_vars)
    step("triage: emergency by rule", triage_emergency)
    step("triage: dermatology", triage_derm)
    if step("check_availability", availability) and step("book_appointment", book):
        step("cancel_appointment", cancel)
    step("deep health", health)
    step("metrics", metrics)

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} steps passed")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
