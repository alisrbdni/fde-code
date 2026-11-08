"""Northside Family Clinic: Clara's triage, scheduling, booking and tools.

The triage ladder (crisis > emergency > urgent > routine) is decided by rules
and phrase lists only — never by an LLM. The LLM may only pick a specialty
from the closed enum in `SPECIALTIES` when the rules found none.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .clinic_catalog import (
    CLINIC_ADDRESS,
    CLINIC_HOURS_TEXT,
    CLINIC_NAME,
    CLINICIANS,
    CRISIS_PHRASES,
    DESK_HOURS,
    EMERGENCY_PHRASES,
    FACTS,
    LUNCH_HOUR,
    SPECIALTIES,
    URGENT_PHRASES,
    WEEKDAYS,
    clinician,
    specialty_who,
)
from .config import mask_phone, normalize_phone
from .kv import FlagStore
from .log import log
from .mcp import Server, ToolError

TRIAGE_CACHE_TTL = 30 * 24 * 3600  # 30 days
URGENT_WINDOW_DAYS = 3


@dataclass
class ClinicFlags:
    """`flags/clinic` (Appendix C.5)."""

    bookings_enabled: bool = True
    booking_window_days: int = 14
    min_notice_minutes: int = 60
    max_upcoming_per_patient: int = 3
    llm_triage: bool = True
    require_attestation_for_private: bool = False
    paused_message: str = (
        "Online booking is paused right now. I can take your details and "
        "the front desk will call you back."
    )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClinicFlags":
        parsed: Dict[str, Any] = {}
        for name in (
            "bookings_enabled", "booking_window_days", "min_notice_minutes",
            "max_upcoming_per_patient", "llm_triage",
            "require_attestation_for_private", "paused_message",
        ):
            if name not in data:
                continue
            value = data[name]
            if name == "paused_message":
                parsed[name] = str(value)
            elif isinstance(value, bool) or name in ("bookings_enabled", "llm_triage", "require_attestation_for_private"):
                if isinstance(value, bool):
                    parsed[name] = value
                elif str(value).lower() in ("true", "false"):
                    parsed[name] = str(value).lower() == "true"
                else:
                    continue
            else:
                try:
                    parsed[name] = int(value)
                except (TypeError, ValueError):
                    continue
        result = cls(**parsed)
        result.clamp()
        return result

    def clamp(self) -> None:
        if not isinstance(self.bookings_enabled, bool):
            self.bookings_enabled = True
        if not isinstance(self.llm_triage, bool):
            self.llm_triage = True
        if not isinstance(self.require_attestation_for_private, bool):
            self.require_attestation_for_private = False
        self.booking_window_days = min(max(self.booking_window_days, 1), 60)
        self.min_notice_minutes = min(max(self.min_notice_minutes, 0), 10080)
        self.max_upcoming_per_patient = min(max(self.max_upcoming_per_patient, 1), 10)
        if not self.paused_message:
            self.paused_message = ClinicFlags.paused_message

    def to_dict(self) -> Dict[str, Any]:
        return {
            "bookings_enabled": self.bookings_enabled,
            "booking_window_days": self.booking_window_days,
            "min_notice_minutes": self.min_notice_minutes,
            "max_upcoming_per_patient": self.max_upcoming_per_patient,
            "llm_triage": self.llm_triage,
            "require_attestation_for_private": self.require_attestation_for_private,
            "paused_message": self.paused_message,
        }


async def get_flags(app: Any, trace: Any = None) -> ClinicFlags:
    """Read `flags/clinic` (cached through FlagStore; fails open to defaults)."""
    store = getattr(app, "clinic_flags", None)
    if store is None:
        store = FlagStore(app.kv, "flags/clinic", ClinicFlags)
        app.clinic_flags = store  # functools-style memo on the app singleton
    flags, _source = await store.get(trace)
    return flags


def _now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# time helpers (all clinic-local; slots are `YYYY-MM-DDTHH:MM` strings)
# ---------------------------------------------------------------------------

_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def tz(app: Any) -> timezone:
    raw = str(getattr(app.settings, "clinic_utc_offset", "") or "").strip()
    match = _OFFSET_RE.match(raw) or _OFFSET_RE.match("-05:00")
    sign = 1 if match.group(1) == "+" else -1
    delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
    return timezone(sign * delta)


def local_now(app: Any) -> datetime:
    """Naive clinic-local 'now', truncated to the minute."""
    return datetime.now(tz(app)).replace(tzinfo=None, second=0, microsecond=0)


def slot_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


def parse_slot(slot: str) -> datetime:
    return datetime.strptime(slot, "%Y-%m-%dT%H:%M")


def spoken_slot(slot: str, specialist_id: str = "") -> str:
    """'Tuesday, November 10 at 9:30 AM', optionally 'with Dr. Maya Okafor'."""
    dt = parse_slot(slot)
    hour = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    clock = f"{hour} {ampm}" if dt.minute == 0 else f"{hour}:{dt.minute:02d} {ampm}"
    text = f"{dt.strftime('%A, %B')} {dt.day} at {clock}"
    if specialist_id and clinician(specialist_id):
        text += f" with {clinician(specialist_id)['name']}"
    return text


def desk_open(t: datetime) -> bool:
    """Is the front desk open at clinic-local time `t`?"""
    hours = DESK_HOURS.get(t.weekday())
    if not hours:
        return False
    minutes = t.hour * 60 + t.minute
    return _mins(hours[0]) <= minutes < _mins(hours[1])


def _mins(hhmm: str) -> int:
    hours_, minutes_ = hhmm.split(":")
    return int(hours_) * 60 + int(minutes_)


# ---------------------------------------------------------------------------
# day filter: text -> inclusive (from, to) clinic-local dates, or None
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day_range(app: Any, preferred_day: str) -> Optional[Tuple[datetime, datetime]]:
    text = re.sub(r"[^a-z0-9-]", "", str(preferred_day or "").lower())
    if not text or text in ("any", "earliest", "asap", "firstavailable", "none"):
        return None
    today = local_now(app).replace(hour=0, minute=0)
    if text == "today":
        return today, today
    if text == "tomorrow":
        day = today + timedelta(days=1)
        return day, day
    if text == "nextweek":
        monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        return monday, monday + timedelta(days=6)
    if text == "thisweek":
        sunday = today + timedelta(days=(6 - today.weekday()) % 7)
        return today, sunday
    if _ISO_DATE_RE.match(text):
        try:
            day = datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            return None
        return day, day
    # weekday names or 3-letter prefixes, optionally "next <weekday>"
    body = text
    wants_next = False
    if body.startswith("next"):
        wants_next = True
        body = body[4:]
    for index, names in enumerate(WEEKDAYS):
        if any(body == name or (len(body) >= 3 and name.startswith(body)) for name in names):
            delta = (index - today.weekday()) % 7
            if wants_next and delta == 0:
                delta = 7
            if body == today.strftime("%A").lower() and not wants_next:
                delta = 0
            day = today + timedelta(days=delta)
            return day, day
    return None


def _part_ok(part_of_day: str, dt: datetime) -> bool:
    part = str(part_of_day or "").lower().strip()
    if not part or part == "any":
        return True
    if part == "morning":
        return dt.hour < 12
    if part == "afternoon":
        return 12 <= dt.hour < 17
    if part == "evening":
        return dt.hour >= 16
    return True


# ---------------------------------------------------------------------------
# triage (rules first; the LLM may only pick from the closed enum)
# ---------------------------------------------------------------------------

def _normalise_text(text: str) -> str:
    text = str(text or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", text).strip().lower()


def _matched_phrases(text: str, phrases: Tuple[str, ...]) -> List[str]:
    hits: List[str] = []
    for phrase in phrases:
        if re.search(rf"(?<![a-z]){re.escape(phrase)}(?![a-z])", text):
            hits.append(phrase)
    return hits


def triage_rules(symptoms: str, age: Optional[int] = None, for_child: bool = False) -> Dict[str, Any]:
    """Phrase-list triage: urgency crisis > emergency > urgent > routine.

    The specialty is the one with the most keyword hits, excluding pediatrics;
    pediatrics overrides when the caller describes a child or the age is < 18.
    Never an LLM.
    """
    text = _normalise_text(symptoms)

    crisis = _matched_phrases(text, CRISIS_PHRASES)
    emergency = _matched_phrases(text, EMERGENCY_PHRASES)
    urgent = _matched_phrases(text, URGENT_PHRASES)
    red_flags = crisis + emergency + urgent

    if crisis:
        urgency = "crisis"
    elif emergency:
        urgency = "emergency"
    elif urgent:
        urgency = "urgent"
    else:
        urgency = "routine"

    hits: Dict[str, int] = {}
    matched_terms: List[str] = []
    for specialty_id, spec in SPECIALTIES.items():
        for keyword in spec["keywords"]:
            if re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", text):
                if keyword not in matched_terms:
                    matched_terms.append(keyword)
                if specialty_id != "pediatrics":
                    hits[specialty_id] = hits.get(specialty_id, 0) + 1

    child_hit = bool(_matched_phrases(text, SPECIALTIES["pediatrics"]["keywords"]))
    known_child = bool(for_child) or child_hit
    if isinstance(age, (int, float)) and age < 18:
        known_child = True

    specialty: Optional[str] = None
    best = sorted(hits.items(), key=lambda kv: (-kv[1], kv[0]))
    if best:
        specialty = best[0][0]
    if known_child:
        specialty = "pediatrics"  # pediatrics overrides everything

    return {
        "urgency": urgency,
        "specialty": specialty,
        "matched_terms": sorted(matched_terms),
        "red_flags": red_flags,
        "source": "rules",
    }


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "specialty": {"type": "string", "enum": list(SPECIALTIES)},
    },
    "required": ["specialty"],
    "additionalProperties": False,
}

TRIAGE_DISCLAIMER = "This is not a diagnosis, just who to see and how soon."


def _triage_say(urgency: str, who: str) -> str:
    if urgency == "emergency":
        return "What you describe could be an emergency."
    if urgency == "crisis":
        return "Thank you for telling me. You deserve support right now."
    if urgency == "urgent":
        return (
            f"Based on what you told me, it's best to be seen within a day or two, "
            f"by {who}. I'll look for the earliest time."
        )
    return f"Based on what you told me, the right person to see is {who}."


async def triage(
    app: Any, trace: Any, symptoms: str, age: Optional[int] = None, for_child: bool = False
) -> Dict[str, Any]:
    """Rules triage, with the closed-enum LLM specialty fallback."""
    rule = triage_rules(symptoms, age, for_child)
    specialty: Optional[str] = rule["specialty"]
    source = "rules" if specialty else "default"

    if specialty is None and rule["urgency"] in ("routine", "urgent"):
        flags = await get_flags(app, trace)
        if flags.llm_triage:
            key = f"cache/triage/{hashlib.sha256(_normalise_text(symptoms).encode()).hexdigest()[:24]}"
            try:
                cached = await app.kv.get(key)
            except Exception as exc:
                log("warn", "clinic.triage_cache_read_failed", error=str(exc)[:120])
                cached = None
            if cached is not None:
                try:
                    value = json.loads(cached) if isinstance(cached, str) else cached
                    if isinstance(value, str) and value in SPECIALTIES:
                        specialty = value
                        source = "inference_cache"
                except Exception:
                    specialty = None
            if specialty is None:
                try:
                    async with trace.span("triage inference"):
                        picked = await app.llm.chat_json(
                            app.settings.assistant_model,
                            [
                                {
                                    "role": "system",
                                    "content": (
                                        "You map a caller's description to exactly one clinic "
                                        f"specialty from this list: {', '.join(SPECIALTIES)}. "
                                        "Answer with the single best specialty."
                                    ),
                                },
                                {"role": "user", "content": str(symptoms or "")[:500]},
                            ],
                            TRIAGE_SCHEMA,
                        )
                    value = picked.get("specialty")
                    if isinstance(value, str) and value in SPECIALTIES:
                        specialty = value
                        source = "inference"
                        try:
                            await app.kv.put(key, json.dumps(value), ttl_secs=TRIAGE_CACHE_TTL)
                        except Exception as exc:
                            log("warn", "clinic.triage_cache_write_failed", error=str(exc)[:120])
                except Exception as exc:
                    log("warn", "clinic.triage_inference_failed", error=str(exc)[:120])
                    source = "default"

    if specialty is None:
        specialty = "family_medicine"  # the safe default per spec
        source = "default"

    who = specialty_who(specialty)
    result: Dict[str, Any] = {
        "urgency": rule["urgency"],
        "specialty": specialty,
        "specialty_label": SPECIALTIES[specialty]["label"],
        "matched_terms": rule["matched_terms"],
        "red_flags": rule["red_flags"],
        "source": source,
        "say": _triage_say(rule["urgency"], who),
        "disclaimer": TRIAGE_DISCLAIMER,
    }
    return result


# ---------------------------------------------------------------------------
# slots and availability
# ---------------------------------------------------------------------------

def _parse_clock(hhmm: str) -> dtime:
    return dtime.fromisoformat(hhmm)


def generate_slots(app: Any, specialist: Dict[str, Any], flags: ClinicFlags) -> List[str]:
    """Bookable slot strings: today … +window days, on the clinician's days,
    inside their hours, outside lunch, and at least min_notice minutes out."""
    now = local_now(app)
    today = now.replace(hour=0, minute=0)
    earliest = now + timedelta(minutes=flags.min_notice_minutes)
    slots: List[str] = []
    for offset in range(flags.booking_window_days):
        day = today + timedelta(days=offset)
        if day.weekday() not in specialist["days"]:
            continue
        start = datetime.combine(day.date(), _parse_clock(specialist["start"]))
        end = datetime.combine(day.date(), _parse_clock(specialist["end"]))
        step = timedelta(minutes=specialist["slot_min"])
        t = start
        while t + step <= end:
            if t.hour != LUNCH_HOUR and t >= earliest:
                slots.append(slot_str(t))
            t += step
    return slots


def _option(specialist: Dict[str, Any], slot: str) -> Dict[str, Any]:
    return {
        "slot_id": f"{specialist['id']}@{slot}",
        "specialist_id": specialist["id"],
        "specialist": specialist["name"],
        "slot": slot,
        "spoken": spoken_slot(slot, specialist["id"]),
    }


def _spread(options: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """First one per day (so the caller hears choices across days), then fill."""
    best: List[Dict[str, Any]] = []
    used_days: set = set()
    for option in options:  # already sorted by slot
        day = option["slot"][:10]
        if day not in used_days and len(best) < limit:
            used_days.add(day)
            best.append(option)
    for option in options:
        if len(best) >= limit:
            break
        if option not in best:
            best.append(option)
    return sorted(best, key=lambda o: o["slot"])


async def _resolve_clinicians(specialty: str = "", specialist: str = "", language: str = "") -> List[Dict[str, Any]]:
    """By id or name substring, else by specialty; language speakers first."""
    pool: List[Dict[str, Any]] = []
    text = str(specialist or "").strip()
    if text:
        for spec in CLINICIANS:
            if spec["id"] == text or text.lower() in spec["name"].lower():
                pool.append(spec)
    if not pool:
        if specialty and specialty in SPECIALTIES:
            pool = [spec for spec in CLINICIANS if spec["specialty"] == specialty]
        else:
            pool = list(CLINICIANS)
    want = str(language or "").strip().lower()
    if want:
        pool.sort(key=lambda spec: 0 if want in [l.lower() for l in spec["languages"]] else 1)
    return pool


async def availability(
    app: Any,
    trace: Any,
    specialty: str = "",
    specialist: str = "",
    preferred_day: str = "",
    part_of_day: str = "",
    urgency: str = "routine",
    language: str = "",
    limit: int = 3,
) -> Dict[str, Any]:
    """Earliest openings, one per day first.

    Urgent requests only look at the next three days. Each option carries a
    `slot_id` the workflow passes straight back to book_appointment.
    """
    flags = await get_flags(app, trace)
    pool = await _resolve_clinicians(specialty, specialist, language)
    day_range = _day_range(app, preferred_day)
    now = local_now(app)
    from_slot = slot_str(day_range[0]) if day_range else ""
    to_slot = slot_str(day_range[1].replace(hour=23, minute=59)) if day_range else ""

    options: List[Dict[str, Any]] = []
    for spec in pool:
        slots = [s for s in generate_slots(app, spec, flags) if (not from_slot or s >= from_slot) and (not to_slot or s <= to_slot)]
        if not slots:
            continue
        async with trace.span(f"actor schedule.taken {spec['id']}"):
            taken = await app.state.call(
                "schedule", spec["id"], "taken", from_slot if day_range else slots[0], to_slot if day_range else slots[-1],
                trace_id=trace.trace_id,
            )
        taken_set = set(taken.get("slots") or [])
        for slot in slots:
            dt = parse_slot(slot)
            if slot in taken_set:
                continue
            if not _part_ok(part_of_day, dt):
                continue
            options.append(_option(spec, slot))

    if urgency == "urgent":
        horizon = slot_str(now.replace(hour=0, minute=0) + timedelta(days=URGENT_WINDOW_DAYS - 1, hours=23, minutes=59))
        options = [o for o in options if o["slot"] <= horizon]

    options.sort(key=lambda o: o["slot"])
    picked = _spread(options, limit)
    if not picked:
        return {
            "options": [],
            "say": (
                "I couldn't find an open time that fits in the next two weeks. "
                "Would you like to hear different days, or shall I take a message "
                "for the front desk?"
            ),
        }
    if len(picked) == 1:
        say = f"The earliest time is {picked[0]['spoken']}. Does that work for you?"
    else:
        joined = "; or ".join(option["spoken"] for option in picked)
        say = f"The earliest times are: {joined}. Which works best?"
    return {"options": picked, "say": say}


# ---------------------------------------------------------------------------
# booking and cancelling
# ---------------------------------------------------------------------------

async def _verify_binding(app: Any, trace: Any, phone: str, call_id: str) -> bool:
    if not call_id or "{{" in str(call_id):
        return False
    info = await app.state.call("patient", phone, "callInfo", str(call_id), trace_id=trace.trace_id)
    return bool(info.get("known"))


_SLOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


async def book(
    app: Any, trace: Any, phone: str, slot_id: str, name: str,
    reason: str = "", urgency: str = "routine", source: str = "call", call_id: str = "",
) -> Dict[str, Any]:
    """Book a slot. `booking_status` is the contract (paused|invalid|need_name|
    failed|slot_taken|duplicate|limit|confirmed)."""
    flags = await get_flags(app, trace)
    if not flags.bookings_enabled:
        return {"booking_status": "paused", "message": flags.paused_message}

    patient = normalize_phone(phone) or str(phone or "").strip()
    if not patient:
        return {"booking_status": "failed", "message": "I can't verify this call yet, so I can't book for you."}
    if not await _verify_binding(app, trace, patient, str(call_id or "")):
        return {"booking_status": "failed", "message": "I can't verify this call yet, so I can't book for you."}

    specialist_id, _, slot = str(slot_id or "").partition("@")
    spec = clinician(specialist_id)
    if spec is None or not _SLOT_RE.match(slot) or slot not in generate_slots(app, spec, flags):
        return {
            "booking_status": "invalid",
            "message": "That time isn't one I can book. Let's pick another one together.",
        }

    full_name = re.sub(r"\s+", " ", str(name or "")).strip()[:80]
    if not full_name:
        return {"booking_status": "need_name", "message": "Could you give me your full name for the booking?"}

    now_slot = slot_str(local_now(app))
    now = _now_ms()
    booking = {
        "id": "ap" + secrets.token_hex(4),
        "patient": patient,
        "patient_name": full_name,
        "reason": re.sub(r"\s+", " ", str(reason or "")).strip()[:240],
        "urgency": str(urgency or "routine"),
        "source": str(source or "call"),
    }

    res = await app.state.call("schedule", specialist_id, "book", slot, booking, now_slot, now, trace_id=trace.trace_id)
    if not res.get("ok"):
        why = res.get("reason")
        if why == "taken":
            mitigate = await _alternatives(app, trace, spec, flags, avoid=slot)
            if mitigate:
                joined = "; or ".join(option["spoken"] for option in mitigate[:2])
                message = f"That time was just taken. The closest other times with {spec['name']} are: {joined}."
            else:
                message = (
                    f"That time was just taken, and I don't see another opening with "
                    f"{spec['name']} in the next two weeks. Would you like a different day?"
                )
            return {"booking_status": "slot_taken", "message": message, "alternatives": mitigate}
        if why == "duplicate":
            existing = res.get("existing") or {}
            when = spoken_slot(str(existing.get("slot") or slot), specialist_id) if existing.get("slot") else "soon"
            return {
                "booking_status": "duplicate",
                "message": (
                    f"You already have an appointment with {spec['name']} on {when}, "
                    "so I didn't book another one."
                ),
                "appointment_id": existing.get("id"),
            }
        if why == "past":
            return {"booking_status": "invalid", "message": "That time has already passed. Let's pick another one."}
        return {"booking_status": "failed", "message": "I couldn't save that just now. Please try again in a minute."}

    appt = {
        **booking,
        "slot": slot,
        "slot_id": f"{specialist_id}@{slot}",
        "specialist_id": specialist_id,
        "specialist": spec["name"],
        "specialty": spec["specialty"],
    }
    added = await app.state.call(
        "patient", patient, "addAppointment", appt, flags.max_upcoming_per_patient, now_slot, trace_id=trace.trace_id
    )
    if not added.get("ok") and added.get("reason") == "limit":
        # compensate: give back the schedule slot we just took
        try:
            await app.state.call("schedule", specialist_id, "cancel", booking["id"], patient, now, trace_id=trace.trace_id)
        except Exception as exc:
            log("warn", "clinic.compensate_failed", appointment_id=booking["id"], error=str(exc)[:120])
        return {
            "booking_status": "limit",
            "message": (
                "You already have the maximum number of upcoming appointments, "
                "so I couldn't add another. Would you like to cancel one first, "
                "or change an existing one?"
            ),
        }
    if not added.get("ok"):
        try:
            await app.state.call("schedule", specialist_id, "cancel", booking["id"], patient, now, trace_id=trace.trace_id)
        except Exception:
            pass
        return {"booking_status": "failed", "message": "I couldn't save that just now. Please try again in a minute."}

    booking_line = f"You're booked with {spec['name']} on {spoken_slot(slot)}."
    try:
        await app.state.call(
            "desk", "global", "log",
            {"event": "booked", "appointment_id": booking["id"], "specialist": spec["name"],
             "slot": slot, "patient": mask_phone(patient)},
            now, trace_id=trace.trace_id,
        )
    except Exception as exc:
        log("warn", "clinic.desk_log_failed", error=str(exc)[:120])
    try:
        await app.storage.put_json(
            f"clinic/appointments/{booking['id']}.json",
            {**appt, "patient": mask_phone(patient), "createdAt": now_slot},
        )
    except Exception as exc:
        log("warn", "clinic.archive_failed", appointment_id=booking["id"], error=str(exc)[:120])
    log(
        "info", "clinic.booked", patient=mask_phone(patient), appointment_id=booking["id"],
        specialist=specialist_id, slot=slot, urgency=booking["urgency"], trace_id=trace.trace_id,
    )
    return {
        "booking_status": "confirmed",
        "appointment_id": booking["id"],
        "booking_line": booking_line,
        "message": booking_line,
        "slot": slot,
        "specialist_id": specialist_id,
        "specialist": spec["name"],
    }


async def _alternatives(
    app: Any, trace: Any, spec: Dict[str, Any], flags: ClinicFlags, avoid: str = "", limit: int = 2
) -> List[Dict[str, Any]]:
    try:
        async with trace.span(f"actor schedule.taken {spec['id']}"):
            taken = await app.state.call("schedule", spec["id"], "taken", "", "", trace_id=trace.trace_id)
    except Exception:
        taken = {"slots": []}
    taken_set = set(taken.get("slots") or [])
    options = [
        _option(spec, s) for s in generate_slots(app, spec, flags)
        if s != avoid and s not in taken_set
    ]
    return options[:limit]


async def cancel(
    app: Any, trace: Any, phone: str, appointment_id: str = "", which: str = "", call_id: str = ""
) -> Dict[str, Any]:
    """Cancel an upcoming appointment by id, by description, or the only one."""
    patient = normalize_phone(phone) or str(phone or "").strip()
    if not patient:
        return {"booking_status": "failed", "message": "I can't verify this call yet, so I can't do that."}
    if not await _verify_binding(app, trace, patient, str(call_id or "")):
        return {"booking_status": "failed", "message": "I can't verify this call yet, so I can't do that."}
    now_slot = slot_str(local_now(app))
    listing = await app.state.call("patient", patient, "list", now_slot, trace_id=trace.trace_id)
    upcoming = listing.get("upcoming") or []

    chosen: Optional[Dict[str, Any]] = None
    if appointment_id:
        chosen = next((a for a in upcoming if str(a.get("id")) == str(appointment_id)), None)
        if chosen is None:
            return {
                "booking_status": "not_found",
                "message": "I couldn't find that appointment number among your upcoming visits.",
            }
    else:
        question = str(which or "").strip().lower()
        if len(upcoming) == 1 and not question:
            chosen = upcoming[0]
        elif question:
            best_score = 0
            best: List[Dict[str, Any]] = []
            for appt in upcoming:
                hay = " ".join((
                    str(appt.get("specialist") or ""), str(SPECIALTIES.get(appt.get("specialty"), {}).get("label", "")),
                    str(appt.get("slot") or ""),
                )).lower()
                score = sum(1 for token in re.split(r"\s+", question) if token and token in hay)
                if score > best_score:
                    best_score, best = score, [appt]
                elif score == best_score and score:
                    best.append(appt)
            if len(best) == 1:
                chosen = best[0]
            elif len(best) > 1:
                return {
                    "booking_status": "ambiguous",
                    "message": "I found more than one appointment that matches. Which one should I cancel?",
                }
        if chosen is None:
            if not upcoming:
                return {"booking_status": "none", "message": "I couldn't find an upcoming appointment for you."}
            return {
                "booking_status": "ambiguous",
                "message": "Which appointment should I cancel: the one with "
                + ", or the one with ".join(
                    f"{a.get('specialist') or 'your clinician'} on {spoken_slot(str(a.get('slot')), str(a.get('specialist_id')))}"
                    for a in upcoming[:3]
                ) + "?",
            }

    schedule_slot = str(chosen.get("slot") or "")
    specialist_id = str(chosen.get("specialist_id") or "")
    try:
        res = await app.state.call(
            "schedule", specialist_id, "cancel", str(chosen.get("id")), patient, _now_ms(), trace_id=trace.trace_id
        )
    except Exception as exc:
        raise ToolError(f"I couldn't reach the schedule just now: {str(exc)[:120]}") from exc
    if res.get("ok") is False and res.get("reason") == "forbidden":
        raise ToolError("I can't cancel that appointment from here. Please call the front desk.")
    if res.get("ok") is False and res.get("reason") == "not_found":
        # the clinic already cancelled it on their side; still tidy the patient list
        log("warn", "clinic.cancel_missing_in_schedule", appointment_id=str(chosen.get("id")))
    else:
        try:
            await app.state.call(
                "desk", "global", "log",
                {"event": "cancelled", "appointment_id": chosen.get("id"), "specialist": chosen.get("specialist"),
                 "slot": schedule_slot, "patient": mask_phone(patient)},
                _now_ms(), trace_id=trace.trace_id,
            )
        except Exception as exc:
            log("warn", "clinic.desk_log_failed", error=str(exc)[:120])

    await app.state.call("patient", patient, "cancelAppointment", str(chosen.get("id")), _now_ms(), trace_id=trace.trace_id)
    log("info", "clinic.cancelled", patient=mask_phone(patient), appointment_id=str(chosen.get("id")), trace_id=trace.trace_id)
    message = f"Okay, I cancelled your appointment with {chosen.get('specialist') or 'your clinician'} on {spoken_slot(schedule_slot)}."
    return {"booking_status": "cancelled", "appointment_id": chosen.get("id"), "message": message}


# ---------------------------------------------------------------------------
# MCP tools (8) — same binding discipline as Forge's tools
# ---------------------------------------------------------------------------

COMMON_ARGS = {
    "caller_phone": {
        "type": "string",
        "description": "Always {{telnyx_end_user_target}}. The caller's own phone number.",
    },
    "call_id": {
        "type": "string",
        "description": "Always {{call_control_id}}. Identifies this call.",
    },
}


async def _patient(app: Any, trace: Any, args: Dict[str, Any], private: bool = False) -> str:
    """The clinic's _owner(): verify the call binding through the patient actor."""
    phone = normalize_phone(args.get("caller_phone"))
    if not phone:
        raise ToolError("I couldn't read your phone number, so I can't do that for you.")
    call_id = args.get("call_id")
    if not isinstance(call_id, str) or not call_id or "{{" in str(call_id):
        raise ToolError("I can't verify this call yet, so I can't do that.")
    info = await app.state.call("patient", phone, "callInfo", call_id, trace_id=trace.trace_id)
    if not info.get("known"):
        raise ToolError("I can't verify this call yet, so I can't do that.")
    if private:
        flags = await get_flags(app, trace)
        if flags.require_attestation_for_private and not info.get("attested"):
            raise ToolError(
                "For that I need to check it's really you. Please call back from "
                "your own phone and speak to me; then I can help."
            )
    return phone


def _coerce_age(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _tool_get_clinic_info(app, trace, args: Dict[str, Any]) -> Dict[str, Any]:
    open_now = desk_open(local_now(app))
    say = (
        f"We're at {CLINIC_ADDRESS}. Our hours are {CLINIC_HOURS_TEXT}. "
        + ("The front desk is open right now." if open_now else "The front desk is closed right now.")
    )
    return {
        "name": CLINIC_NAME,
        "address": CLINIC_ADDRESS,
        "hours": CLINIC_HOURS_TEXT,
        "closed_now": not open_now,
        "facts": list(FACTS),
        "say": say,
    }


async def _tool_list_specialists(app, trace, args: Dict[str, Any]) -> Dict[str, Any]:
    specialty = str(args.get("specialty") or "")
    pool = await _resolve_clinicians(specialty=specialty)
    specialists = [
        {
            "id": spec["id"],
            "name": spec["name"],
            "specialty": SPECIALTIES[spec["specialty"]]["label"],
            "who": specialty_who(spec["specialty"]),
            "languages": list(spec["languages"]),
        }
        for spec in pool
    ]
    return {"specialists": specialists, "count": len(specialists)}


async def _tool_triage_symptoms(app, trace, args: Dict[str, Any]) -> Dict[str, Any]:
    phone = await _patient(app, trace, args)
    result = await triage(
        app, trace, str(args.get("symptoms") or ""), _coerce_age(args.get("age")),
        bool(args.get("for_child")),
    )
    try:
        await app.state.call(
            "patient", phone, "recordTriage",
            {"urgency": result["urgency"], "specialty": result["specialty"], "source": result["source"],
             "at": _now_ms()},
            trace_id=trace.trace_id,
        )
    except Exception as exc:
        log("warn", "clinic.triage_record_failed", error=str(exc)[:120])
    if result["urgency"] in ("emergency", "crisis"):
        try:
            await app.state.call(
                "desk", "global", "log",
                {"event": f"triage_{result['urgency']}", "patient": mask_phone(phone),
                 "urgency": result["urgency"], "specialty": result["specialty"]},
                _now_ms(), trace_id=trace.trace_id,
            )
        except Exception as exc:
            log("warn", "clinic.desk_log_failed", error=str(exc)[:120])
    return {
        **result,
        "triage_urgency": result["urgency"],
        "matched_specialty": result["specialty"],
        "message": result["say"],
    }


async def _tool_check_availability(app, trace, args: Dict[str, Any]) -> Dict[str, Any]:
    return await availability(
        app, trace,
        specialty=str(args.get("specialty") or ""),
        specialist=str(args.get("specialist") or ""),
        preferred_day=str(args.get("preferred_day") or ""),
        part_of_day=str(args.get("part_of_day") or ""),
        urgency=str(args.get("urgency") or "routine"),
        language=str(args.get("language") or ""),
        limit=3,
    )


async def _tool_book_appointment(app, trace, args: Dict[str, Any]) -> Dict[str, Any]:
    phone = await _patient(app, trace, args)
    return await book(
        app, trace, phone,
        str(args.get("slot_id") or ""),
        str(args.get("full_name") or args.get("name") or ""),
        str(args.get("reason") or ""),
        urgency=str(args.get("urgency") or "routine"),
        source="call",
        call_id=str(args.get("call_id") or ""),
    )


async def _tool_list_my_appointments(app, trace, args: Dict[str, Any]) -> Dict[str, Any]:
    phone = await _patient(app, trace, args, private=True)
    now_slot = slot_str(local_now(app))
    listing = await app.state.call("patient", phone, "list", now_slot, trace_id=trace.trace_id)
    upcoming = listing.get("upcoming") or []
    items = [
        {
            "appointment_id": a.get("id"),
            "specialist": a.get("specialist"),
            "specialty": a.get("specialty"),
            "slot": a.get("slot"),
            "spoken": spoken_slot(str(a.get("slot") or ""), str(a.get("specialist_id") or "")),
        }
        for a in upcoming
    ]
    if not items:
        say = "I don't see any upcoming appointments for you."
    elif len(items) == 1:
        say = f"I see one upcoming appointment: {items[0]['spoken']}."
    else:
        say = f"I see {len(items)} upcoming appointments. The next one is {items[0]['spoken']}."
    return {"upcoming": items, "count": len(items), "say": say}


async def _tool_cancel_appointment(app, trace, args: Dict[str, Any]) -> Dict[str, Any]:
    phone = await _patient(app, trace, args, private=True)
    return await cancel(
        app, trace, phone,
        appointment_id=str(args.get("appointment_id") or ""),
        which=str(args.get("which") or ""),
        call_id=str(args.get("call_id") or ""),
    )


async def _tool_request_callback(app, trace, args: Dict[str, Any]) -> Dict[str, Any]:
    phone = await _patient(app, trace, args)
    now = local_now(app)
    when = "today" if desk_open(now) else "on the next business day"
    name = re.sub(r"\s+", " ", str(args.get("full_name") or args.get("name") or "")).strip()[:80]
    reason = re.sub(r"\s+", " ", str(args.get("reason") or "")).strip()[:240]
    res = await app.state.call(
        "desk", "global", "addCallback",
        {"phone": phone, "name": name, "reason": reason, "when": when},
        _now_ms(), trace_id=trace.trace_id,
    )
    callback = res.get("callback") or {}
    log("info", "clinic.callback_requested", patient=mask_phone(phone), callback_id=callback.get("id"), trace_id=trace.trace_id)
    return {
        "callback_id": callback.get("id"),
        "requested": True,
        "when": when,
        "say": f"Someone from the front desk will call you back {when}.",
        "message": f"Someone from the front desk will call you back {when}.",
    }


def _tool(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
    }


TOOL_DEFS: List[Dict[str, Any]] = [
    _tool(
        "get_clinic_info",
        "Clinic facts: address, opening hours, insurance, parking and policies. Never medical advice.",
        {"question": {"type": "string", "description": "What the caller wants to know about the clinic."}},
        [],
    ),
    _tool(
        "list_specialists",
        "The clinicians at the clinic, optionally filtered by specialty.",
        {"specialty": {"type": "string", "enum": list(SPECIALTIES), "description": "Optional specialty filter."}},
        [],
    ),
    _tool(
        "triage_symptoms",
        "Decide who the caller should see and how soon, from their own words. Urgency comes from rules, never guesses.",
        {
            **COMMON_ARGS,
            "symptoms": {"type": "string", "description": "What the caller described, in their words."},
            "age": {"type": "integer", "description": "The patient's age in years, if mentioned."},
            "for_child": {"type": "boolean", "description": "True when the visit is for a child."},
        },
        ["caller_phone", "call_id", "symptoms"],
    ),
    _tool(
        "check_availability",
        "Find the earliest openings with the right clinician. Offer at most two spoken options.",
        {
            "specialty": {"type": "string", "enum": list(SPECIALTIES)},
            "specialist": {"type": "string", "description": "Clinician id or name, if the caller named one."},
            "preferred_day": {"type": "string", "description": "today, tomorrow, a weekday, an ISO date, or this/next week."},
            "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening"]},
            "urgency": {"type": "string", "enum": ["routine", "urgent"]},
            "language": {"type": "string", "description": "Preferred language, e.g. Spanish."},
        },
        [],
    ),
    _tool(
        "book_appointment",
        "Book one of the options from check_availability with the caller's slot_id and full name.",
        {
            **COMMON_ARGS,
            "slot_id": {"type": "string", "description": "The option's slot_id from check_availability."},
            "full_name": {"type": "string", "description": "The patient's full name, confirmed back to them."},
            "reason": {"type": "string", "description": "Why they're visiting, briefly."},
            "urgency": {"type": "string", "enum": ["routine", "urgent"]},
        },
        ["caller_phone", "call_id", "slot_id", "full_name"],
    ),
    _tool(
        "list_my_appointments",
        "The caller's upcoming appointments. Private (needs a verified caller ID).",
        dict(COMMON_ARGS),
        ["caller_phone", "call_id"],
    ),
    _tool(
        "cancel_appointment",
        "Cancel an upcoming appointment after the caller confirms which one. Private (needs a verified caller ID).",
        {
            **COMMON_ARGS,
            "appointment_id": {"type": "string", "description": "The appointment id, if you know it."},
            "which": {"type": "string", "description": "Which appointment, in the caller's words (clinician, day, specialty)."},
        },
        ["caller_phone", "call_id"],
    ),
    _tool(
        "request_callback",
        "Ask the front desk to call the caller back today, or on the next business day.",
        {
            **COMMON_ARGS,
            "full_name": {"type": "string", "description": "The caller's name, if given."},
            "reason": {"type": "string", "description": "What the call back is about, briefly."},
        },
        ["caller_phone", "call_id"],
    ),
]

HANDLERS = {
    "get_clinic_info": _tool_get_clinic_info,
    "list_specialists": _tool_list_specialists,
    "triage_symptoms": _tool_triage_symptoms,
    "check_availability": _tool_check_availability,
    "book_appointment": _tool_book_appointment,
    "list_my_appointments": _tool_list_my_appointments,
    "cancel_appointment": _tool_cancel_appointment,
    "request_callback": _tool_request_callback,
}

SERVER = Server(
    name="northside-clinic-mcp",
    instructions=(
        "Tools for Clara, the Northside Family Clinic assistant. Triage tools decide urgency "
        "through rules; follow their result. Never diagnose, never suggest treatments. "
        "Private tools need a verified caller ID."
    ),
    tool_defs=TOOL_DEFS,
    handlers=HANDLERS,
    crash_hint="Something went wrong on my side. I can take a message and the front desk will call you back.",
)


# ---------------------------------------------------------------------------
# webhooks: variables + flat webhook tools (never an MCP envelope)
# ---------------------------------------------------------------------------

def clinic_defaults(trace_id: str = "") -> Dict[str, str]:
    """Every Clara workflow variable with its spoken-safe default (C.6)."""
    return {
        "clinic_welcome_line": (
            "How can I help you today? I can book a visit, change an "
            "appointment, or answer questions about the clinic."
        ),
        "caller_phone": "",
        "patient_known": "false",
        "patient_name": "",
        "visit_count": "1",
        "upcoming_count": "0",
        "next_appointment": "",
        "booking_enabled": "true",
        "paused_message": "",
        "front_desk_open": "true",
        "triage_urgency": "none",
        "matched_specialty": "",
        "specialty_label": "",
        "booking_status": "none",
        "booking_line": "",
        "degraded": "false",
        "trace_id": trace_id,
    }


def _payload(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    if isinstance(data, dict):
        payload = data.get("payload")
        if isinstance(payload, dict):
            return payload
    return body


async def variables(app: Any, trace: Any, body: Any) -> Dict[str, Any]:
    """The clinic dynamic-variables webhook. ALWAYS 200; failures degrade."""
    values = clinic_defaults(trace.trace_id)
    payload = _payload(body)
    phone = normalize_phone(payload.get("telnyx_end_user_target")) or ""
    call_id = str(payload.get("call_control_id") or "")
    attested = bool(payload.get("telnyx_end_user_target_verified"))
    trace.set(caller=mask_phone(phone))
    if phone:
        try:
            flags = await get_flags(app, trace)
            async with trace.span("actor patient.startCall"):
                info = await app.state.call(
                    "patient", phone, "startCall", phone, call_id, attested,
                    slot_str(local_now(app)), _now_ms(), trace_id=trace.trace_id,
                )
        except Exception as exc:
            values["degraded"] = "true"
            trace.set(degraded=True, error=str(exc)[:120])
            trace.outcome = "degraded"
            log("warn", "clinic.variables_degraded", error=str(exc)[:120], trace_id=trace.trace_id)
            return {"dynamic_variables": values}
        values["caller_phone"] = phone
        values["clinic_welcome_line"] = _welcome_line(info)
        values["patient_known"] = "true" if info.get("name") else "false"
        values["patient_name"] = str(info.get("name") or "")
        values["visit_count"] = str(info.get("calls") or 1)
        upcoming = info.get("upcoming") or []
        values["upcoming_count"] = str(len(upcoming))
        if upcoming:
            earliest = upcoming[0]
            values["next_appointment"] = spoken_slot(
                str(earliest.get("slot") or ""), str(earliest.get("specialist_id") or "")
            )
            trace.set(next_appointment=str(earliest.get("slot") or ""))
        values["booking_enabled"] = "true" if flags.bookings_enabled else "false"
        if not flags.bookings_enabled:
            values["paused_message"] = flags.paused_message
        values["front_desk_open"] = "true" if desk_open(local_now(app)) else "false"
    return {"dynamic_variables": values}


def _welcome_line(info: Dict[str, Any]) -> str:
    """'Welcome back, {first}.' + next appointment + the question."""
    name = str(info.get("name") or "").strip()
    upcoming = info.get("upcoming") or []
    if not name and not upcoming:
        return clinic_defaults()["clinic_welcome_line"]
    parts: List[str] = []
    if name:
        parts.append(f"Welcome back, {name.split()[0]}.")
    if upcoming:
        earliest = upcoming[0]
        parts.append(
            "I see your next appointment is "
            + spoken_slot(str(earliest.get("slot") or ""), str(earliest.get("specialist_id") or ""))
            + "."
        )
    parts.append("How can I help you today?")
    return " ".join(parts)


def _triage_fallback() -> Dict[str, Any]:
    return {
        "triage_urgency": "none",
        "matched_specialty": "",
        "specialty_label": "",
        "say": "",
        "disclaimer": TRIAGE_DISCLAIMER,
        "message": (
            "I couldn't check that just now. I can take a message and have "
            "someone from the front desk call you back."
        ),
    }


async def triage_webhook(app: Any, trace: Any, body: Any) -> Dict[str, Any]:
    """`POST /clinic/tools/triage` (MCP_TOKEN): flat tool result."""
    body = body or {}
    try:
        info = await app.state.call(
            "patient", normalize_phone(body.get("caller_phone")) or "", "callInfo", str(body.get("call_id") or ""),
            trace_id=trace.trace_id,
        )
        if not info.get("known"):
            return _triage_fallback()
        age = _coerce_age(body.get("age"))
        result = await triage(
            app, trace, str(body.get("symptoms") or ""), age, bool(body.get("for_child"))
        )
        return {
            "triage_urgency": result["urgency"],
            "matched_specialty": result["specialty"],
            "specialty_label": result["specialty_label"],
            "say": result["say"],
            "disclaimer": result["disclaimer"],
            "matched_terms": result["matched_terms"],
            "red_flags": result["red_flags"],
            "source": result["source"],
            "message": result["say"],
        }
    except Exception as exc:
        trace.outcome = "degraded"
        trace.set(degraded=True, error=str(exc)[:120])
        return _triage_fallback()


def _book_fallback() -> Dict[str, Any]:
    return {
        "booking_status": "failed",
        "booking_line": "",
        "message": (
            "I couldn't book that just now. I can take a message and have "
            "someone from the front desk call you back."
        ),
    }


async def book_webhook(app: Any, trace: Any, body: Any) -> Dict[str, Any]:
    """`POST /clinic/tools/book` (MCP_TOKEN): book result + booking_line."""
    body = body or {}
    try:
        result = await book(
            app, trace,
            phone=str(body.get("caller_phone") or ""),
            slot_id=str(body.get("slot_id") or ""),
            name=str(body.get("full_name") or ""),
            reason=str(body.get("reason") or ""),
            urgency=str(body.get("urgency") or "routine"),
            source="webhook",
            call_id=str(body.get("call_id") or ""),
        )
        if result.get("booking_status") == "confirmed":
            try:
                await app.state.call(
                    "patient", normalize_phone(body.get("caller_phone")) or "", "setName",
                    str(body.get("full_name") or ""), trace_id=trace.trace_id,
                )
            except Exception:
                pass
        return result
    except Exception as exc:
        trace.outcome = "degraded"
        trace.set(degraded=True, error=str(exc)[:120])
        return _book_fallback()


# ---------------------------------------------------------------------------
# overview (the /ui console's clinic view, with masked patients)
# ---------------------------------------------------------------------------

async def overview(app: Any, trace: Any, days: int = 7) -> Dict[str, Any]:
    """Per-clinician free slots and bookings in range, flags and desk state."""
    flags = await get_flags(app, trace)
    now = local_now(app)
    today = now.replace(hour=0, minute=0)
    from_slot = slot_str(today)
    to_slot = slot_str(today + timedelta(days=max(1, days), hours=23, minutes=59))

    clinicians: List[Dict[str, Any]] = []
    for spec in CLINICIANS:
        slots = [s for s in generate_slots(app, spec, flags) if from_slot <= s <= to_slot]
        try:
            res = await app.state.call("schedule", spec["id"], "list", from_slot, to_slot, trace_id=trace.trace_id)
            bookings = res.get("bookings") or []
        except Exception as exc:
            log("warn", "clinic.overview_schedule_failed", clinician=spec["id"], error=str(exc)[:120])
            bookings = []
        taken = {str(b.get("slot")) for b in bookings}
        clinicians.append({
            "id": spec["id"],
            "name": spec["name"],
            "specialty": spec["specialty"],
            "free_slots": [s for s in slots if s not in taken],
            "bookings": [
                {
                    "id": b.get("id"),
                    "slot": b.get("slot"),
                    "patient": mask_phone(str(b.get("patient") or "")),
                    "patient_name": b.get("patient_name"),
                    "urgency": b.get("urgency"),
                    "source": b.get("source"),
                }
                for b in bookings
            ],
        })

    flags_view = flags.to_dict()
    try:
        desk_raw = await app.state.call("desk", "global", "snapshot", trace_id=trace.trace_id)
        desk = desk_raw.get("desk") or {}
        callbacks = [
            {**cb, "phone": mask_phone(str(cb.get("phone") or ""))}
            for cb in (desk.get("callbacks") or [])
        ]
        activity = [event for event in (desk.get("activity") or [])]
    except Exception as exc:
        log("warn", "clinic.overview_desk_failed", error=str(exc)[:120])
        callbacks, activity = [], []
    return {
        "clinic": {"name": CLINIC_NAME, "address": CLINIC_ADDRESS, "hours": CLINIC_HOURS_TEXT},
        "flags": flags_view,
        "today": from_slot,
        "horizon": to_slot,
        "clinicians": clinicians,
        "desk": {"callbacks": callbacks, "activity": activity},
        "desk_open": desk_open(now),
    }
