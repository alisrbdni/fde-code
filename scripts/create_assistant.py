#!/usr/bin/env python3
"""Create (or update) the Forge / clinic assistant on the Telnyx API.

Usage: python3 scripts/create_assistant.py forge|clinic [--dry-run] [--update ID]

- substitutes every ${VAR} in the assistant JSON from the environment
- inlines "@conversation_flow.json"
- drops the `handoff` tool when CLINIC_ASSISTANT_ID is empty and the `transfer`
  tool when CLINIC_FRONT_DESK_NUMBER is empty (with a warning)
- POSTs to TELNYX_API_BASE (default https://api.telnyx.com/v2) /ai/assistants
  with Authorization: Bearer TELNYX_API_KEY; --update ID POSTs to /ai/assistants/ID
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
ASSISTANTS_DIR = ROOT / "api" / "function" / "assistants"
VAR_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


DEFAULTS = {"ASSISTANT_MODEL": "moonshotai/Kimi-K2.6", "API_URL": "http://localhost:8080"}


def substitute(value: Any) -> Any:
    if isinstance(value, str):
        return VAR_RE.sub(lambda m: os.environ.get(m.group(1)) or DEFAULTS.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: substitute(v) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v) for v in value]
    return value


def load_payload(name: str) -> Dict[str, Any]:
    directory = ASSISTANTS_DIR / name
    assistant = json.loads((directory / "assistant.json").read_text())
    flow = assistant.get("conversation_flow")
    if isinstance(flow, str):  # "@conversation_flow.json"
        flow = json.loads((directory / "conversation_flow.json").read_text())
        assistant["conversation_flow"] = substitute(flow)
    assistant = substitute(assistant)

    kept_tools, dropped = [], []
    for tool in assistant.get("tools") or []:
        tool_name = tool.get("name")
        if tool.get("type") == "handoff" and not any(
            (a or {}).get("id") for a in (tool.get("handoff") or {}).get("ai_assistants") or []
        ):
            dropped.append(f"{tool_name} (CLINIC_ASSISTANT_ID is empty)")
            continue
        if tool.get("type") == "transfer" and not any(
            (t or {}).get("to") for t in (tool.get("transfer") or {}).get("targets") or []
        ):
            dropped.append(f"{tool_name} (CLINIC_FRONT_DESK_NUMBER is empty)")
            continue
        kept_tools.append(tool)
    # The API refuses a handoff tool on an assistant that has a conversation_flow: hand-offs must be edges whose
    # target is another assistant. Keep the tool in the JSON (the console simulator uses it) and convert here.
    flow = assistant.get("conversation_flow")
    if isinstance(flow, dict):
        for tool in [t for t in kept_tools if t.get("type") == "handoff"]:
            kept_tools.remove(tool)
            for target in (tool.get("handoff") or {}).get("ai_assistants") or []:
                node = next((n for n in flow["nodes"] if "handoff" in json.dumps(n.get("instructions", ""))), None)
                if node and target.get("id"):
                    flow["edges"].append({
                        "id": f"e_{node['id']}_to_{target.get('name', 'assistant').lower()}",
                        "start_node_id": node["id"],
                        "target": {"type": "assistant", "assistant_id": target["id"],
                                   "voice_mode": (tool.get("handoff") or {}).get("voice_mode", "unified")},
                        "condition": {"type": "llm", "prompt": f"The caller agrees to be connected to {target.get('name', 'the other assistant')}."},
                    })
                    print(f"note: handoff tool -> assistant edge {node['id']} -> {target['id']}", file=sys.stderr)
    assistant["tools"] = kept_tools
    for line in dropped:
        print(f"warning: dropping tool {line}", file=sys.stderr)
    tel = assistant.get("telephony_settings") or {}
    if not tel.get("default_texml_app_id"):
        tel.pop("default_texml_app_id", None)  # an empty id is rejected; set it once TEXML_APP_ID exists
    return assistant


MCP_PATHS = {"forge": ("agent-forge-mcp", "/mcp"), "clinic": ("northside-clinic-mcp", "/clinic/mcp")}


def api_request(base: str, method: str, path: str, body: Any = None) -> Any:
    req = urllib.request.Request(
        f"{base}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {os.environ.get('TELNYX_API_KEY', '')}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw) if raw else {}


def ensure_mcp_server(base: str, name: str) -> str:
    """Register our MCP endpoint with Telnyx (once) and return its id. The token rides in the URL as
    ?token=, which our server accepts, so no separate secret reference is needed."""
    label, path = MCP_PATHS[name]
    api_url = os.environ.get("API_URL", "").rstrip("/")
    token = os.environ.get("MCP_TOKEN", "")
    url = f"{api_url}{path}" + (f"?token={token}" if token else "")
    listed = api_request(base, "GET", "/ai/mcp_servers")
    for server in (listed.get("data", listed) if isinstance(listed, dict) else listed) or []:
        if isinstance(server, dict) and server.get("name") == label:
            if server.get("url") != url:
                api_request(base, "PUT", f"/ai/mcp_servers/{server['id']}", {"name": label, "type": "http", "url": url})
            return str(server["id"])
    created = api_request(base, "POST", "/ai/mcp_servers", {"name": label, "type": "http", "url": url})
    created = created.get("data", created)
    print(f"mcp_server_id={created['id']}  ({label} -> {api_url}{path})")
    return str(created["id"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assistant", choices=["forge", "clinic"])
    parser.add_argument("--dry-run", action="store_true", help="print the payload, do not POST")
    parser.add_argument("--update", metavar="ID", help="update the assistant with this id")
    parser.add_argument("--base", default=os.environ.get("TELNYX_API_BASE", "https://api.telnyx.com/v2"))
    args = parser.parse_args()

    payload = load_payload(args.assistant)
    base = args.base.rstrip("/")
    if not base.endswith("/v2"):
        base += "/v2"
    if not args.dry_run and os.environ.get("API_URL", "").startswith("https://"):
        try:
            payload["mcp_servers"] = [{"id": ensure_mcp_server(base, args.assistant)}]
        except urllib.error.HTTPError as exc:
            print(f"warning: could not register the MCP server ({exc.code}): "
                  f"{exc.read().decode('utf-8', 'replace')[:300]}\n  add it in the Portal (AI Assistants -> Tools -> MCP Servers)",
                  file=sys.stderr)
    body = json.dumps(payload).encode("utf-8")
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    api_key = os.environ.get("TELNYX_API_KEY", "")
    url = f"{base}/ai/assistants"
    if args.update:
        url = f"{url}/{args.update}"
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
            print(f"{response.status} {raw[:400]}")
            try:
                data = json.loads(raw)
                data = data.get("data", data) if isinstance(data, dict) else {}
                if data.get("id"):
                    print(f"assistant_id={data['id']}")  # machine-readable line for deployment.sh
            except ValueError:
                pass
            return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI reports any failure
        print(f"request failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
