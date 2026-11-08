"""Test harness: drive the ASGI app in-process, mirroring what Telnyx sends.

Set STATE_URL_TEST=http://localhost:8798 to run the same suite against the
real TypeScript actors (the contract test in `./run.sh test`); each test then
gets its own actor-name prefix so instances are fresh.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Any, Dict, Optional

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from function.config import load_settings  # noqa: E402
from function.func import App, Function  # noqa: E402
from function.kv import MemoryKV  # noqa: E402
from function.state import Isolated, LocalState, RemoteState  # noqa: E402
from function.storage import MemoryStorage  # noqa: E402
from function.telnyx_api import FakeTelnyx  # noqa: E402

OWNER = "+15550102001"
OTHER = "+15550102002"
PATIENT = "+15550103001"
SUNRISE: Dict[str, Any] = {
    "business_name": "Sunrise Dental",
    "agent_name": "Sam",
    "template": "receptionist",
    "tone": "friendly",
    "voice": "warm",
    "hours": "Monday to Friday, 8 AM to 5 PM",
    "facts": ["We are at 12 Oak Street", "New patient cleanings are 99 dollars"],
    "escalation_number": "+15550109999",
}

STATE_URL_TEST = os.environ.get("STATE_URL_TEST", "")
STATE_TOKEN_TEST = os.environ.get("STATE_TOKEN", "dev-token")
REMOTE = bool(STATE_URL_TEST)

CLEARED = (
    "TELNYX_API_KEY", "KV_NAMESPACE_ID", "STATE_URL", "STATE_TOKEN", "MCP_TOKEN", "ADMIN_TOKEN",
    "STORAGE_ENDPOINT", "STORAGE_ACCESS_KEY", "CLINIC_ASSISTANT_ID", "CLINIC_FRONT_DESK_NUMBER",
    "FORGE_DRY_RUN", "VERIFY_SIGNATURES", "CHAT_MODEL",
)

local_only = pytest.mark.skipif(REMOTE, reason="uses the emulator's alarm internals (local only)")


def make_app(**env: str) -> App:
    for name in CLEARED:
        os.environ.pop(name, None)
    os.environ.update({"TEXML_APP_ID": "texml-test", "FORGE_NUMBER": "+15550100000", "API_URL": "https://forge.test"})
    os.environ.update(env)
    state: Any = LocalState() if not REMOTE else Isolated(RemoteState(STATE_URL_TEST, STATE_TOKEN_TEST))
    app = App(load_settings(), kv=MemoryKV(), state=state, telnyx=FakeTelnyx(), storage=MemoryStorage())
    app.auto_alarms = False
    return app


class Resp:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body
        self.text = body.decode("utf-8", "replace")
        self.json: Any = None
        if "json" in headers.get("content-type", "") and body:
            self.json = json.loads(self.text)

    def __repr__(self) -> str:  # helpful assertion output
        return f"<Resp {self.status} {self.text[:300]}>"


class Client:
    """In-process ASGI client plus Telnyx-shaped helpers."""

    def __init__(self, fn: Function) -> None:
        self.fn = fn

    @property
    def app(self) -> App:
        return self.fn.get_app()

    async def request(
        self, method: str, path: str, body: Any = None, headers: Optional[Dict[str, str]] = None,
        raw: Optional[bytes] = None,
    ) -> Resp:
        path, _, query = path.partition("?")
        data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode("utf-8"))
        hdrs = {"content-type": "application/json", **(headers or {})}
        scope = {
            "type": "http", "method": method, "path": path, "query_string": query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in hdrs.items()],
        }
        sent = False

        async def receive() -> Dict[str, Any]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": data, "more_body": False}

        out: Dict[str, Any] = {"body": b""}

        async def send(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                out["status"] = message["status"]
                out["headers"] = {k.decode(): v.decode() for k, v in message["headers"]}
            else:
                out["body"] += message.get("body") or b""

        await self.fn.handle(scope, receive, send)
        return Resp(out["status"], out["headers"], out["body"])

    async def post(self, path: str, body: Any = None, **kw: Any) -> Resp:
        return await self.request("POST", path, body, **kw)

    async def get(self, path: str, **kw: Any) -> Resp:
        return await self.request("GET", path, **kw)

    # -- Telnyx-shaped helpers --------------------------------------------------

    @staticmethod
    def call_payload(phone: str, call_id: str, attested: bool = True, channel: str = "phone_call") -> Dict[str, Any]:
        return {
            "data": {
                "event_type": "assistant.initialization",
                "payload": {
                    "telnyx_end_user_target": phone,
                    "telnyx_agent_target": "+15550100000",
                    "call_control_id": call_id,
                    "telnyx_end_user_target_verified": attested,
                    "telnyx_conversation_channel": channel,
                },
            }
        }

    async def start_call(self, phone: str = OWNER, call_id: Optional[str] = None, attested: bool = True,
                         path: str = "/webhook/dynamic-variables") -> Dict[str, Any]:
        call_id = call_id or "v3:" + secrets.token_hex(6)
        resp = await self.post(path, self.call_payload(phone, call_id, attested))
        assert resp.status == 200, resp
        variables = resp.json["dynamic_variables"]
        variables["_call_id"] = call_id
        return variables

    async def rpc(self, method: str, params: Optional[Dict[str, Any]] = None, path: str = "/mcp",
                  token: str = "") -> Resp:
        headers = {"authorization": f"Bearer {token}"} if token else {}
        return await self.post(path, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                               headers=headers)

    async def tool(self, name: str, path: str = "/mcp", **args: Any) -> Dict[str, Any]:
        resp = await self.rpc("tools/call", {"name": name, "arguments": args}, path=path)
        assert resp.status == 200, resp
        result = resp.json["result"]
        data = dict(result["structuredContent"])
        data["_is_error"] = result["isError"]
        return data

    async def save(self, call: Dict[str, Any], phone: str = OWNER, **values: Any) -> Dict[str, Any]:
        return await self.tool("save_draft", caller_phone=phone, call_id=call["_call_id"], values=values)

    async def check(self, call: Dict[str, Any], phone: str = OWNER) -> Dict[str, Any]:
        resp = await self.post("/tools/check-draft", {"caller_phone": phone, "call_id": call["_call_id"]})
        assert resp.status == 200, resp
        return resp.json

    async def build(self, call: Dict[str, Any], phone: str = OWNER) -> Dict[str, Any]:
        resp = await self.post("/tools/build-agent", {"caller_phone": phone, "call_id": call["_call_id"]})
        assert resp.status == 200, resp
        return resp.json

    async def ready_draft(self, phone: str = OWNER, attested: bool = True, **overrides: Any) -> Dict[str, Any]:
        call = await self.start_call(phone, attested=attested)
        fields = {**SUNRISE, **overrides}
        saved = await self.save(call, phone, **fields)
        assert saved["draft_complete"], saved
        return call


@pytest.fixture
def app() -> App:
    return make_app()


@pytest.fixture
def client(app: App) -> Client:
    return Client(Function(app))
