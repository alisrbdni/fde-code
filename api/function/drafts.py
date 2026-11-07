"""Draft field handling: cleaning/validating patches, completeness assessment
and the one-paragraph spoken summary."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .catalog import ENUM_FIELDS, FIELDS, REQUIRED, TEMPLATES, VOICES, TONES
from .config import normalize_phone

_STRIP_RE = re.compile(r"[{}<>]")
_WS_RE = re.compile(r"\s+")


def _clean(text: str) -> str:
    """Strip braces/angle brackets (template injection) and collapse whitespace."""
    return _WS_RE.sub(" ", _STRIP_RE.sub("", str(text))).strip()


def build_patch(
    args: Dict[str, Any], current: Optional[Dict[str, Any]]
) -> Tuple[Dict[str, Any], List[str]]:
    """Turn loose tool args into a {set, append, clear} patch plus user notes.

    Values are cleaned and cut to their max length; enum values validated
    (lower-cased); the template pre-fills purpose/tasks; invalid values are
    dropped with a note explaining why.
    """
    patch: Dict[str, Any] = {"set": {}, "append": {}, "clear": []}
    notes: List[str] = []
    current = current or {}
    provided = args.get("values")
    if not isinstance(provided, dict):
        provided = {}

    for name, value in provided.items():
        if name not in FIELDS:
            notes.append(f"I can't save a field called {name}.")
            continue
        spec = FIELDS[name]
        if spec["list"]:
            values = value if isinstance(value, list) else [value]
            cleaned = []
            for v in values:
                if v is None:
                    continue
                text = _clean(v)
                if not text:
                    continue
                if len(text) > spec["max"]:
                    text = text[: spec["max"]].strip()
                    notes.append(f"I shortened {name.replace('_', ' ')} to {spec['max']} characters.")
                cleaned.append(text)
            if cleaned:
                patch["append"][name] = cleaned
            continue

        if isinstance(value, list):
            value = "; ".join(str(v) for v in value)
        if value is None or (isinstance(value, str) and not value.strip()):
            patch["clear"].append(name)
            continue
        text = _clean(value)
        if not text:
            patch["clear"].append(name)
            continue
        if len(text) > spec["max"]:
            text = text[: spec["max"]].strip()
            notes.append(f"I shortened {name.replace('_', ' ')} to {spec['max']} characters.")

        if name in ENUM_FIELDS:
            low = text.lower()
            if name == "tone" and low not in TONES:
                notes.append(f"I can't do a {text} tone. Pick one of: {', '.join(TONES)}.")
                continue
            if name == "voice" and low not in VOICES:
                notes.append(f"I don't have a {text} voice. Pick one of: {', '.join(VOICES)}.")
                continue
            if name == "template" and low not in TEMPLATES:
                notes.append(f"I don't have a {text} template. Pick one of: {', '.join(TEMPLATES)}.")
                continue
            if name == "language" and low not in ("en", "es"):
                notes.append("I can only speak English or Spanish right now.")
                continue
            if name == "template" and "template" not in current:
                tpl = TEMPLATES[low]
                patch["set"]["template"] = low
                if "purpose" not in current and "purpose" not in provided:
                    patch["set"]["purpose"] = tpl["purpose"]
                if "tasks" not in current and "tasks" not in provided:
                    patch["append"]["tasks"] = list(tpl["tasks"])
                continue
            patch["set"][name] = low
            continue

        if name == "escalation_number":
            phone = normalize_phone(text)
            if not phone:
                notes.append("Transfer numbers need to be a full phone number, like +1 555 010 9999.")
                continue
            patch["set"][name] = phone
            continue

        patch["set"][name] = text

    return patch, notes


def assess(fields: Dict[str, Any]) -> Dict[str, Any]:
    """Completeness + policy assessment of a draft's fields."""
    from .policy import check as policy_check

    missing: List[str] = []
    for name in REQUIRED:
        spec = FIELDS[name]
        value = fields.get(name)
        if spec["list"]:
            if not (isinstance(value, list) and value):
                missing.append(name)
        elif not (isinstance(value, str) and value.strip()):
            missing.append(name)

    has_tasks = bool(fields.get("tasks"))
    has_facts = bool(fields.get("facts"))
    if not (has_tasks or has_facts or fields.get("template")):
        missing.append("at least one task or fact")

    verdict = policy_check(fields)

    suggestions: List[str] = []
    template = fields.get("template")
    if template and template in TEMPLATES and len(fields.get("facts") or []) < 2:
        for item in TEMPLATES[template]["ask_for"][:3 - len(fields.get("facts") or [])]:
            suggestions.append(f"What are your {item}?")
    if not fields.get("voice"):
        suggestions.append("Which voice should it have? I have warm, bright, calm, professional and British.")
    if not fields.get("escalation_number"):
        suggestions.append("Should it transfer urgent calls to another number?")

    missing_left = len(missing)
    return {
        "ok": True,
        "complete": missing_left == 0 and verdict.allowed,
        "missing": missing,
        "policy_blocked": not verdict.allowed,
        "policy_reason": "; ".join(verdict.reasons) if verdict.reasons else "",
        "suggestions": suggestions,
        "summary": summarize(fields) if not missing_left or verdict.allowed else "",
    }


def summarize(fields: Dict[str, Any]) -> str:
    """One spoken paragraph: 'Sam, the AI assistant for Sunrise Dental …'."""
    agent = fields.get("agent_name") or "your assistant"
    business = fields.get("business_name") or "your business"
    purpose = fields.get("purpose") or "help callers"
    parts = [f"{agent}, the AI assistant for {business}, whose job is to {purpose}."]
    tone = fields.get("tone")
    if tone:
        parts.append(f"It should sound {tone}.")
    hours = fields.get("hours")
    if hours:
        parts.append(f"Hours are {hours}.")
    facts = fields.get("facts") or []
    if facts:
        parts.append("Facts: " + "; ".join(str(f) for f in facts[:3]) + ".")
    tasks = fields.get("tasks") or []
    if tasks:
        parts.append("It will: " + "; ".join(str(t) for t in tasks[:2]) + ".")
    if fields.get("escalation_number"):
        parts.append("Urgent calls can be transferred.")
    if fields.get("language") == "es":
        parts.append("It can speak Spanish.")
    return " ".join(parts)
