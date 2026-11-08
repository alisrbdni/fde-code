#!/usr/bin/env python3
"""Validate the assistant workflow JSONs under api/function/assistants/.

Checks (per the build spec, Phase 8):
- duplicate node/edge ids; the start node exists
- every edge's source and target exist
- default edges only out of speak nodes; every speak node has exactly ONE
  default edge and no other edges
- variables used in expression comparisons, {{messages}} and {{instructions}}
  exist in the assistant's dynamic_variables (or are Telnyx system vars)
- every node is reachable from the start node
- node_tools.json references only real nodes and real tools

Usage: python3 scripts/validate_workflow.py [assistant_name ...]
With no arguments every assistant directory is validated.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
ASSISTANTS_DIR = ROOT / "api" / "function" / "assistants"

# Telnyx-provided variables (never in dynamic_variables).
SYSTEM_VARS = {
    "telnyx_current_time", "telnyx_conversation_channel", "telnyx_agent_target",
    "telnyx_end_user_target", "telnyx_shaken_stir_attestation", "call_control_id",
    "telnyx_conversation_duration_secs", "telnyx_current_date",
    "telnyx_current_weekday", "telnyx_last_tool_status_code",
}

VAR_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}")


def _var_names(text: str) -> List[str]:
    return VAR_RE.findall(text or "")


def validate(flow: Dict[str, Any], defaults: Dict[str, Any]) -> List[str]:
    """Return a list of problems ([] means the workflow is valid)."""
    errors: List[str] = []
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    ids = [n.get("id") for n in nodes]
    if len(ids) != len(set(ids)):
        errors.append("duplicate node ids")
    edge_ids = [e.get("id") for e in edges]
    if len(edge_ids) != len(set(edge_ids)):
        errors.append("duplicate edge ids")
    if not flow.get("start_node_id"):
        errors.append("missing start_node_id")
    by_id = {n.get("id"): n for n in nodes}

    for edge in edges:
        frm, to = edge.get("start_node_id"), (edge.get("target") or {}).get("node_id")
        if frm not in by_id:
            errors.append(f"{edge.get('id')}: unknown source {frm!r}")
        if to not in by_id:
            errors.append(f"{edge.get('id')}: unknown target {to!r}")

    # default edges only from speak nodes; speak nodes: exactly one default edge, no others
    defaults_from: Dict[str, int] = {}
    edges_from: Dict[str, List[Any]] = {}
    for edge in edges:
        frm = edge.get("start_node_id")
        edges_from.setdefault(frm, []).append(edge)
        if (edge.get("condition") or {}).get("type") == "default":
            defaults_from[frm] = defaults_from.get(frm, 0) + 1
    for node_id, count in defaults_from.items():
        if (by_id.get(node_id) or {}).get("type") != "speak":
            errors.append(f"default edge from non-speak node {node_id!r}")
    for node in nodes:
        if node.get("type") == "speak":
            outs = edges_from.get(node.get("id"), [])
            if len(outs) != 1 or (outs[0].get("condition") or {}).get("type") != "default":
                errors.append(f"speak node {node.get('id')!r} must have exactly one default edge")

    # expression variables
    for edge in edges:
        condition = edge.get("condition") or {}
        if condition.get("type") == "expression":
            name = ((condition.get("expression") or {}).get("left") or {}).get("name")
            if name and name not in defaults and name not in SYSTEM_VARS:
                errors.append(f"{edge.get('id')}: expression variable {name!r} has no default")

    # {{var}} usage in node text
    for node in nodes:
        for field in ("message", "instructions"):
            for name in _var_names(str(node.get(field) or "")):
                if name not in defaults and name not in SYSTEM_VARS:
                    errors.append(f"node {node.get('id')!r}: {{{{{name}}}}} has no default")

    # reachability from the start node
    start = flow.get("start_node_id")
    seen = {start}
    frontier = [start]
    while frontier:
        current = frontier.pop()
        for edge in edges_from.get(current, []):
            target = (edge.get("target") or {}).get("node_id")
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    for node_id in ids:
        if node_id not in seen:
            errors.append(f"node {node_id!r} is unreachable from the start node")
    return errors


def _mcp_tool_names(name: str) -> set:
    """Tool names the assistant's MCP server exposes (empty if not built yet)."""
    if str(ROOT / "api") not in sys.path:
        sys.path.insert(0, str(ROOT / "api"))
    try:
        if name == "forge":
            from function.tools import FORGE

            return {t["name"] for t in FORGE.tool_defs}
        if name == "clinic":
            from function.clinic import SERVER

            return {t["name"] for t in SERVER.tool_defs}
    except Exception:
        return set()
    return set()


def check(name: str, directory: Path) -> List[str]:
    """Validate one assistant directory (flow + node_tools against assistant.json)."""
    errors: List[str] = []
    try:
        assistant = json.loads((directory / "assistant.json").read_text())
    except OSError as exc:
        return [f"cannot read assistant.json: {exc}"]
    defaults = {str(k) for k in (assistant.get("dynamic_variables") or {})}
    flow = assistant.pop("conversation_flow", None)
    if isinstance(flow, str):  # "@conversation_flow.json" reference
        try:
            flow = json.loads((directory / "conversation_flow.json").read_text())
        except OSError as exc:
            return [f"cannot read conversation_flow.json: {exc}"]
    if not isinstance(flow, dict):
        return ["assistant.json has no conversation_flow"]
    errors.extend(validate(flow, defaults))

    node_ids = {n.get("id") for n in (flow.get("nodes") or [])}
    tool_names = {t.get("name") for t in (assistant.get("tools") or [])}
    tool_names |= _mcp_tool_names(name)
    node_tools_path = directory / "node_tools.json"
    if node_tools_path.exists():
        try:
            node_tools = json.loads(node_tools_path.read_text())
        except OSError as exc:
            return errors + [f"cannot read node_tools.json: {exc}"]
        for node_id, tools in node_tools.items():
            if node_id not in node_ids:
                errors.append(f"node_tools: unknown node {node_id!r}")
            for tool in tools or []:
                if tool not in tool_names:
                    errors.append(f"node_tools: unknown tool {tool!r} on node {node_id!r}")
    return errors


def summarise(flow: Dict[str, Any]) -> str:
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    speak = sum(1 for n in nodes if n.get("type") == "speak")
    comp = sum(1 for e in edges if (e.get("condition") or {}).get("type") == "expression")
    llm = sum(1 for e in edges if (e.get("condition") or {}).get("type") == "llm")
    return (
        f"{len(nodes)} nodes, {len(edges)} edges, {speak} speak nodes, "
        f"{comp} variable-comparison edges, {llm} LLM edges"
    )


def main(argv: List[str]) -> int:
    dirs = {
        p.name: p
        for p in sorted(ASSISTANTS_DIR.iterdir())
        if p.is_dir() and (p / "assistant.json").exists()
    }
    argv = [a for a in argv if not a.startswith("-")]
    chosen = argv or list(dirs)
    unknown = [a for a in chosen if a not in dirs]
    if unknown:
        print(f"unknown assistant(s): {', '.join(unknown)}; have: {', '.join(dirs)}")
        return 2
    failures = 0
    for name in chosen:
        errors = check(name, dirs[name])
        flow = json.loads((dirs[name] / "conversation_flow.json").read_text())
        if errors:
            failures += len(errors)
            print(f"{name}: {summarise(flow)}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"{name}: {summarise(flow)} -> OK")
    if failures:
        print(f"FAILED with {failures} problem(s)")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
