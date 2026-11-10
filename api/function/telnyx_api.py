"""Telnyx v2 API client plus the scripted fake used by local mode and tests.

Every call raises TelnyxError on a non-2xx; HTTP errors never escape as
exceptions elsewhere. `is_validation` drives the ladder (a rejected workflow
is not retryable — build less), `is_retryable` drives the build retry (429/5xx
once).
"""
from __future__ import annotations

import secrets
import threading
from typing import Any, Dict, List, Optional, Sequence

from .http import HttpResponse, join_url, request


class TelnyxError(Exception):
    """A failed Telnyx API call."""

    def __init__(self, status: int, body: str, op: str) -> None:
        self.status = status
        self.body = body
        self.op = op
        super().__init__(f"{op} -> {status} {str(body)[:120]}")

    @property
    def is_validation(self) -> bool:
        return self.status in (400, 422)

    @property
    def is_retryable(self) -> bool:
        return self.status == 0 or self.status == 429 or self.status >= 500


def _txt(resp: HttpResponse) -> str:
    return resp.text


class TelnyxClient:
    """Thin wrapper over the Telnyx v2 REST API."""

    def __init__(self, api_key: str, base_url: str = "https://api.telnyx.com/v2") -> None:
        self.api_key = api_key
        self.base = base_url.rstrip("/")

    @property
    def _headers(self) -> Dict[str, str]:
        return {"authorization": f"Bearer {self.api_key}", "content-type": "application/json"}

    def _url(self, path: str) -> str:
        return join_url(self.base, path)

    def _parse_json(self, resp: HttpResponse, resp_key: str = "data") -> Any:
        if not resp.ok:
            raise TelnyxError(resp.status, _txt(resp), self._op)
        data = resp.json()
        if isinstance(data, dict) and resp_key and resp_key in data:
            return data[resp_key]
        return data

    _op: str = "telnyx"

    INFERENCE_TIMEOUT = 60.0  # chat completions routinely take 10-30 s; the default 10 s cut them off

    async def _req(self, method: str, path: str, body: Any = None, resp_key: str = "data", timeout: float = 15.0) -> Any:
        self._op = f"{method.upper()} {path}"
        resp = await request(method, self._url(path), headers=self._headers, body=body, timeout=timeout)
        return self._parse_json(resp, resp_key)

    async def create_assistant(self, payload: Dict[str, Any]) -> str:
        data = await self._req("POST", "/ai/assistants", payload)
        assert isinstance(data, dict)
        return str(data.get("id", ""))

    async def update_assistant(self, assistant_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = await self._req("POST", f"/ai/assistants/{assistant_id}", payload)
        assert isinstance(data, dict)
        return data

    async def delete_assistant(self, assistant_id: str) -> Dict[str, Any]:
        self._op = f"DELETE /ai/assistants/{assistant_id}"
        resp = await request("DELETE", self._url(f"/ai/assistants/{assistant_id}"), headers=self._headers)
        if resp.status == 404:
            return {"deleted": True, "was_missing": True}
        if not resp.ok:
            raise TelnyxError(resp.status, _txt(resp), self._op)
        return {"deleted": True}

    async def get_assistant(self, assistant_id: str) -> Dict[str, Any]:
        data = await self._req("GET", f"/ai/assistants/{assistant_id}")
        assert isinstance(data, dict)
        return data

    async def ai_call(
        self, connection_id: str, from_number: str, to_number: str, assistant_id: str
    ) -> Dict[str, Any]:
        payload = {"From": from_number, "To": to_number, "AIAssistantId": assistant_id}
        data = await self._req("POST", f"/texml/ai_calls/{connection_id}", payload)
        assert isinstance(data, dict)
        return data

    async def chat_json(
        self, model: str, messages: List[Dict[str, Any]], schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Structured output: response_format json_schema, strict."""
        payload = {
            "model": model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": True},
            },
        }
        data = await self._req("POST", "/ai/openai/chat/completions", payload, timeout=self.INFERENCE_TIMEOUT)
        assert isinstance(data, dict)
        content = ""
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except Exception:
            content = ""
        import json as _json

        return _json.loads(content)

    async def chat(
        self, model: str, messages: List[Dict[str, Any]], tools: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Tool-calling chat; returns the assistant's message object."""
        payload: Dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = list(tools)
        data = await self._req("POST", "/ai/openai/chat/completions", payload, timeout=self.INFERENCE_TIMEOUT)
        assert isinstance(data, dict)
        try:
            message = data["choices"][0]["message"]
            if isinstance(message, dict):
                return message
        except Exception:
            pass
        return {"role": "assistant", "content": ""}

    async def list_models(self) -> List[Dict[str, Any]]:
        data = await self._req("GET", "/ai/models")
        return data if isinstance(data, list) else []


class FakeTelnyx:
    """In-memory stand-in: records what was built/called, runs the scripted
    LLM, and can be told to reject specific payload keys (ladder tests) or
    fail (retries and fallbacks)."""

    fake = True

    def __init__(self, reject: Optional[set] = None, fail_chat: bool = False) -> None:
        self.reject = set(reject or ())
        self.fail_chat = fail_chat
        self.assistants: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Dict[str, Any]] = []
        self.fail_next: List[TelnyxError] = []
        self.route_choices: List[int] = []
        self.chat_script: List[Any] = []
        self.triage_specialty = "family_medicine"
        self._counter = 0
        self._lock = threading.Lock()

    def next_fail(self, error: TelnyxError) -> None:
        self.fail_next.append(error)

    def _id(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{prefix}-{self._counter:04d}"

    def _check_reject(self, payload: Dict[str, Any]) -> None:
        if self.fail_next:
            raise self.fail_next.pop(0)
        hit = [key for key in self.reject if key in payload]
        if hit:
            raise TelnyxError(422, f'{{"errors": [{{"detail": "rejected key {hit[0]}"}}]}}', f"reject {hit[0]}")

    # -- assistants --------------------------------------------------------

    async def create_assistant(self, payload: Dict[str, Any]) -> str:
        self._check_reject(payload)
        assistant_id = self._id("ASST")
        stored = {"id": assistant_id, **payload}
        self.assistants[assistant_id] = stored
        return assistant_id

    async def update_assistant(self, assistant_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._check_reject(payload)
        stored = self.assistants.get(assistant_id)
        if not stored:
            raise TelnyxError(404, "not found", f"POST /ai/assistants/{assistant_id}")
        stored.update(payload)
        return stored

    async def delete_assistant(self, assistant_id: str) -> Dict[str, Any]:
        self._check_reject({})
        removed = self.assistants.pop(assistant_id, None)
        return {"deleted": True, "was_missing": removed is None}

    async def get_assistant(self, assistant_id: str) -> Dict[str, Any]:
        stored = self.assistants.get(assistant_id)
        if not stored:
            raise TelnyxError(404, "not found", f"GET /ai/assistants/{assistant_id}")
        return stored

    # -- calls -------------------------------------------------------------

    async def ai_call(
        self, connection_id: str, from_number: str, to_number: str, assistant_id: str
    ) -> Dict[str, Any]:
        self._check_reject({})
        call_id = self._id("CAL")
        record = {
            "call_id": call_id,
            "connection_id": connection_id,
            "from": from_number,
            "to": to_number,
            "assistant_id": assistant_id,
        }
        self.calls.append(record)
        return {"call_id": call_id}

    # -- scripted LLM ------------------------------------------------------

    async def chat_json(
        self, model: str, messages: List[Dict[str, Any]], schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        if self.fail_chat:
            raise TelnyxError(500, "chat down", "POST /ai/openai/chat/completions")
        props = (schema or {}).get("properties", {})
        if "choice" in props:
            return {"choice": self.route_choices.pop(0) if self.route_choices else 0}
        if "specialty" in props:
            return {"specialty": self.triage_specialty}
        if "persona" in props:
            return {
                "persona": "You are the friendly AI phone assistant for this business.",
                "greeting": "How can I help you today?",
            }
        return {"ok": True}

    async def chat(
        self, model: str, messages: List[Dict[str, Any]], tools: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        if self.fail_chat:
            raise TelnyxError(500, "chat down", "POST /ai/openai/chat/completions")
        if self.chat_script:
            item = self.chat_script.pop(0)
            if isinstance(item, dict):
                message = {"role": "assistant", "content": None, **item}
                return message
            return {"role": "assistant", "content": str(item)}
        return {"role": "assistant", "content": "(offline mode) Sure, I can help with that."}

    async def list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": "moonshotai/Kimi-K3", "status": "available"},
            {"id": "zai-org/GLM-5.3-Flash", "status": "available"},
        ]
