"""Forge catalog: templates, voices, tones, draft fields (Appendix A.1–A.2)."""
from __future__ import annotations

from typing import Any, Dict, List

TEMPLATES: Dict[str, Dict[str, Any]] = {
    "receptionist": {
        "id": "receptionist",
        "label": "Front-desk receptionist",
        "purpose": "answer calls, share business information and take messages",
        "tasks": [
            "answer questions about hours, location and services",
            "take a message with name and callback number",
            "transfer urgent calls to the owner if a transfer number is set",
        ],
        "ask_for": [
            "opening hours",
            "address",
            "main services",
            "prices or price ranges",
        ],
    },
    "appointments": {
        "id": "appointments",
        "label": "Appointment request line",
        "purpose": "collect appointment requests and answer questions about visits",
        "tasks": [
            "collect the caller's name, callback number, preferred day and time and reason for visit",
            "explain what to bring and how to prepare",
            "answer questions about cancellations",
        ],
        "ask_for": [
            "opening hours",
            "cancellation policy",
            "what to bring",
        ],
    },
    "faq": {
        "id": "faq",
        "label": "Information hotline",
        "purpose": "answer frequently asked questions",
        "tasks": [
            "answer frequently asked questions",
            "take a message when the answer is unknown",
        ],
        "ask_for": [
            "the five questions callers ask most, with answers",
        ],
    },
    "restaurant": {
        "id": "restaurant",
        "label": "Restaurant line",
        "purpose": "answer menu and hours questions and take reservation requests",
        "tasks": [
            "answer questions about the menu, dietary options and hours",
            "take reservation requests with name, party size, date and time and callback number",
        ],
        "ask_for": [
            "opening hours",
            "menu highlights",
            "dietary options",
            "largest party size",
        ],
    },
    "events": {
        "id": "events",
        "label": "Event information line",
        "purpose": "tell callers about an event and take questions",
        "tasks": [
            "explain the date, time, venue and schedule",
            "answer questions about tickets and parking",
        ],
        "ask_for": [
            "date and time",
            "venue address",
            "ticket prices",
            "parking",
        ],
    },
    "lead_intake": {
        "id": "lead_intake",
        "label": "New customer intake",
        "purpose": "learn what a prospective customer needs and pass the details to the team",
        "tasks": [
            "ask what the caller needs, their timeline and budget range",
            "collect name and callback number",
            "promise a call back from the team",
        ],
        "ask_for": [
            "services offered",
            "service area",
            "typical response time",
        ],
    },
}

VOICES: Dict[str, Dict[str, str]] = {
    "warm": {
        "id": "warm",
        "voice": "Telnyx.KokoroTTS.af_heart",
        "description": "warm and friendly, female",
    },
    "bright": {
        "id": "bright",
        "voice": "Telnyx.KokoroTTS.af_bella",
        "description": "bright and upbeat, female",
    },
    "calm": {
        "id": "calm",
        "voice": "Telnyx.KokoroTTS.am_michael",
        "description": "calm and steady, male",
    },
    "professional": {
        "id": "professional",
        "voice": "Telnyx.KokoroTTS.am_adam",
        "description": "crisp and professional, male",
    },
    "british": {
        "id": "british",
        "voice": "Telnyx.KokoroTTS.bf_emma",
        "description": "British English, female",
    },
}

TONES: List[str] = ["friendly", "professional", "calm", "upbeat", "formal"]

# field -> (required, is_list, max_len)
FIELDS: Dict[str, Dict[str, Any]] = {
    "business_name": {"required": True, "list": False, "max": 80},
    "agent_name": {"required": True, "list": False, "max": 40},
    "purpose": {"required": True, "list": False, "max": 200},
    "template": {"required": False, "list": False, "max": 20},
    "tone": {"required": False, "list": False, "max": 20},
    "voice": {"required": False, "list": False, "max": 20},
    "greeting": {"required": False, "list": False, "max": 200},
    "audience": {"required": False, "list": False, "max": 120},
    "hours": {"required": False, "list": False, "max": 200},
    "tasks": {"required": False, "list": True, "max": 160},
    "facts": {"required": False, "list": True, "max": 240},
    "escalation_number": {"required": False, "list": False, "max": 20},
    "language": {"required": False, "list": False, "max": 10},
}

REQUIRED: List[str] = ["business_name", "agent_name", "purpose"]
ENUM_FIELDS = {
    "template": "template",
    "tone": "tone",
    "voice": "voice",
    "language": "language",
}
