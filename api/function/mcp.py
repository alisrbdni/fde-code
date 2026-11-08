"""A generic, stateless MCP server (Streamable HTTP, JSON only, JSON-RPC 2.0).

The Forge server and the Clinic server are both instances of `Server`; the
ASGI router forwards POST /mcp (and the clinic's /clinic/mcp) into
`handle_http`, which never touches the request envelope.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .log import log

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

Handler = Callable[..., Awaitable[Dict[str, Any]]]


class ToolError(Exception):
    """A tool-level failure the assistant should speak to the caller."""


class Server:
    def __init__(
        self,
        name: str,
        instructions: str,
        tool_defs: List[Dict[str, Any]],
        handlers: Dict[str, Handler],
        crash_hint: str,
    ) -> None:
        self.name = name
        self.instructions = instructions
        self.tool_defs = tool_defs
        self.handlers = handlers
        self.crash_hint = crash_hint

    @property
    def tool_names(self) -> List[str]:
        return [t["name"] for t in self.tool_defs]


def _rpc_result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_result(data: Dict[str, Any], is_error: bool = False) -> Dict[str, Any]:
    text = json.dumps(data, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": data,
        "isError": bool(is_error),
    }


def _negotiate(protocol: Optional[str]) -> str:
    if protocol in PROTOCOL_VERSIONS:
        return protocol
    return PROTOCOL_VERSIONS[0]


async def _one(
    app: Any,
    server: Server,
    trace: Any,
    req: Dict[str, Any],
) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Process one JSON-RPC request. Returns (status, response-or-None)."""
    method = req.get("method")
    req_id = req.get("id")
    if not isinstance(method, str) or not method:
        return 200, _rpc_error(req_id, -32600, "Invalid Request")
    if method.startswith("notifications/"):
        return 202, None

    if method == "initialize":
        protocol = req.get("params", {}).get("protocolVersion") if isinstance(req.get("params"), dict) else None
        return 200, _rpc_result(
            req_id,
            {
                "protocolVersion": _negotiate(protocol),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": server.name, "version": "1.0.0"},
                "instructions": server.instructions,
            },
        )

    if method == "ping":
        return 200, _rpc_result(req_id, {})

    if method == "tools/list":
        return 200, _rpc_result(req_id, {"tools": server.tool_defs})

    if method == "tools/call":
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        name = params.get("name")
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name not in server.handlers:
            return 200, _rpc_result(req_id, _tool_result({"error": f"Unknown tool {name}"}, is_error=True))
        if isinstance(args.get("call_id"), str) and args.get("call_id"):
            try:
                trace.trace_id = args["call_id"]
            except Exception:
                pass
        try:
            data = await server.handlers[name](app, trace, args)
            return 200, _rpc_result(req_id, _tool_result(data))
        except ToolError as exc:
            return 200, _rpc_result(req_id, _tool_result({"error": str(exc)}, is_error=True))
        except Exception as exc:  # never leak internals to the voice assistant
            log("error", "mcp.tool_crashed", server=server.name, tool=str(name), error=str(exc)[:200])
            return 200, _rpc_result(req_id, _tool_result({"error": server.crash_hint}, is_error=True))

    return 200, _rpc_error(req_id, -32601, "Method not found")


async def handle_http(
    app: Any,
    raw: bytes,
    trace: Any,
    server: Server,
) -> Tuple[int, Optional[bytes], Dict[str, str]]:
    """Entry from the router: returns (status, body-bytes, headers)."""
    headers = {"content-type": "application/json"}
    try:
        body: Any = json.loads(raw.decode("utf-8", "replace") or "")
    except Exception:
        payload = _rpc_error(None, -32700, "Parse error")
        return 400, json.dumps(payload).encode("utf-8"), headers

    if isinstance(body, list):
        responses = []
        for req in body:
            if not isinstance(req, dict):
                responses.append(_rpc_error(None, -32600, "Invalid Request"))
                continue
            _, one = await _one(app, server, trace, req)
            if one is not None:
                responses.append(one)
        if not responses:
            return 202, None, headers
        return 200, json.dumps(responses).encode("utf-8"), headers

    if not isinstance(body, dict):
        payload = _rpc_error(None, -32600, "Invalid Request")
        return 400, json.dumps(payload).encode("utf-8"), headers

    status, response = await _one(app, server, trace, body)
    if response is None:
        # A single notification: 202, no body.
        return 202, None, {"content-length": "0", "content-type": "application/json"}
    return status, json.dumps(response).encode("utf-8"), headers
