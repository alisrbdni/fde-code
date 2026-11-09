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


DEFAULTS = {"ASSISTANT_MODEL": "moonshotai/Kimi-K3", "API_URL": "http://localhost:8080"}


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
        if tool.get("type") == "transfer" and not (tool.get("transfer") or {}).get("destination"):
            dropped.append(f"{tool_name} (CLINIC_FRONT_DESK_NUMBER is empty)")
            continue
        kept_tools.append(tool)
    assistant["tools"] = kept_tools
    for line in dropped:
        print(f"warning: dropping tool {line}", file=sys.stderr)
    return assistant


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assistant", choices=["forge", "clinic"])
    parser.add_argument("--dry-run", action="store_true", help="print the payload, do not POST")
    parser.add_argument("--update", metavar="ID", help="update the assistant with this id")
    parser.add_argument("--base", default=os.environ.get("TELNYX_API_BASE", "https://api.telnyx.com/v2"))
    args = parser.parse_args()

    payload = load_payload(args.assistant)
    body = json.dumps(payload).encode("utf-8")
    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    api_key = os.environ.get("TELNYX_API_KEY", "")
    base = args.base.rstrip("/")
    if not base.endswith("/v2"):
        base += "/v2"
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
