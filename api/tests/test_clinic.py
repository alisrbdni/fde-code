"""Clara / Northside Family Clinic: rule triage, slots, booking, MCP and the
workflow (spec Phase 10). The clinic clock is pinned to Monday 08:00."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from conftest import OTHER, PATIENT, Client
from function import clinic
from function.clinic_catalog import CLINICIANS, LUNCH_HOUR
from function.log import Trace
from function.telnyx_api import FakeTelnyx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import validate_workflow  # noqa: E402

MONDAY_8AM = datetime(2026, 11, 9, 8, 0)
VARS = "/clinic/webhook/dynamic-variables"
MCP = "/clinic/mcp"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(clinic, "local_now", lambda app: MONDAY_8AM)


async def _call(client: Client, phone: str = PATIENT, attested: bool = True):
    return await client.start_call(phone, attested=attested, path=VARS)


async def _ctool(client: Client, name: str, call, phone: str = PATIENT, **args):
    return await client.tool(name, path=MCP, caller_phone=phone, call_id=call["_call_id"], **args)


async def _first_option(client: Client, **query):
    res = await client.tool("check_availability", path=MCP, **query)
    assert res["options"], res
    return res["options"][0]


# ---------------------------------------------------------------------------
# triage: rules decide urgency
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,urgency", [
    ("I have crushing chest pain", "emergency"),
    ("her face is drooping and she has slurred speech", "emergency"),
    ("I can’t breathe properly", "emergency"),
    ("I want to kill myself", "crisis"),
    ("I think my wrist is broken", "urgent"),
    ("my baby has a fever", "urgent"),
    ("I'd like my annual check-up", "routine"),
])
def test_triage_urgency(text, urgency):
    assert clinic.triage_rules(text)["urgency"] == urgency


@pytest.mark.parametrize("text,specialty", [
    ("my knee hurts when I climb stairs", "orthopedics"),
    ("I have an itchy rash on my arm", "dermatology"),
    ("I keep getting heartburn after dinner", "gastroenterology"),
    ("migraines and tingling in my hands", "neurology"),
    ("my son has an earache", "pediatrics"),
    ("I've had palpitations this week", "cardiology"),
    ("I've been feeling anxious lately", "behavioral_health"),
])
def test_triage_specialty(text, specialty):
    assert clinic.triage_rules(text)["specialty"] == specialty


def test_pediatrics_overrides_by_age():
    assert clinic.triage_rules("an itchy rash", age=7)["specialty"] == "pediatrics"
    assert clinic.triage_rules("an itchy rash", for_child=True)["specialty"] == "pediatrics"


async def test_triage_never_diagnoses(app):
    for text in ("crushing chest pain", "itchy rash", "I feel tired all the time", "my knee hurts"):
        result = await clinic.triage(app, Trace("t", "t"), text)
        spoken = (result["say"] + " " + result["disclaimer"]).lower()
        assert "you have" not in spoken and "probably" not in spoken and "diagnos" not in spoken.replace("not a diagnosis", "")
        assert result["disclaimer"] == "This is not a diagnosis, just who to see and how soon."
        assert set(result) == {"urgency", "specialty", "specialty_label", "matched_terms", "red_flags",
                               "source", "say", "disclaimer"}


async def test_unmatched_symptoms_use_inference_once_then_cache(app):
    calls = []

    class Counting(FakeTelnyx):
        async def chat_json(self, model, messages, schema):
            calls.append(schema)
            return await super().chat_json(model, messages, schema)

    app.llm = Counting()
    app.llm.triage_specialty = "neurology"
    first = await clinic.triage(app, Trace("t", "t"), "Something just feels off")
    assert (first["specialty"], first["source"]) == ("neurology", "inference")
    second = await clinic.triage(app, Trace("t", "t"), "something   just feels OFF")
    assert (second["specialty"], second["source"]) == ("neurology", "inference_cache")
    assert len(calls) == 1
    assert calls[0]["properties"]["specialty"]["enum"][0] == "family_medicine"
    # urgency never comes from the LLM, and an emergency never asks it
    await clinic.triage(app, Trace("t", "t"), "crushing chest pain and something odd")
    assert len(calls) == 1


async def test_inference_off_or_failing_uses_family_medicine(app):
    await app.clinic_flags.update({"llm_triage": False})
    off = await clinic.triage(app, Trace("t", "t"), "Something feels off")
    assert (off["specialty"], off["source"]) == ("family_medicine", "default")


# ---------------------------------------------------------------------------
# webhooks
# ---------------------------------------------------------------------------

async def test_variables_webhook_new_and_returning_patient(client: Client):
    first = await _call(client)
    assert first["patient_known"] == "false" and first["upcoming_count"] == "0"
    assert first["clinic_welcome_line"].startswith("How can I help you today?")
    assert first["front_desk_open"] == "true"  # Monday 08:00
    option = await _first_option(client, specialty="dermatology")
    booked = await _ctool(client, "book_appointment", first, slot_id=option["slot_id"], full_name="Ana Diaz",
                          reason="rash")
    assert booked["booking_status"] == "confirmed", booked
    again = await _call(client)
    assert again["patient_known"] == "true" and again["patient_name"] == "Ana Diaz"
    assert again["upcoming_count"] == "1"
    assert again["clinic_welcome_line"].startswith("Welcome back, Ana.")
    assert "Dr. Priya Raman" in again["next_appointment"]


async def test_variables_webhook_degrades(client: Client, app):
    async def down(*a, **kw):
        raise RuntimeError("actors down")

    app.state.call = down
    v = await _call(client)
    assert v["degraded"] == "true" and v["triage_urgency"] == "none"


async def test_emergency_sets_variable_that_routes_to_911(client: Client):
    call = await _call(client)
    resp = await client.post("/clinic/tools/triage", {
        "caller_phone": PATIENT, "call_id": call["_call_id"], "symptoms": "crushing chest pain since an hour"})
    assert resp.status == 200
    assert resp.json["triage_urgency"] == "emergency"
    assert resp.headers["x-trace-id"] == call["_call_id"]
    flow = _flow()
    edge = next(e for e in flow["edges"] if e["condition"].get("type") == "expression"
                and e["start_node_id"] == "symptoms"
                and e["condition"]["expression"]["right"]["value"] == resp.json["triage_urgency"])
    target = next(n for n in flow["nodes"] if n["id"] == edge["target"]["node_id"])
    assert target["type"] == "speak" and "dial 911" in target["message"]


async def test_triage_rejects_unbound_call(client: Client):
    resp = await client.post("/clinic/tools/triage", {"caller_phone": PATIENT, "call_id": "v3:never-seen",
                                                      "symptoms": "itchy rash"})
    assert resp.json["triage_urgency"] == "none" and "call you back" in resp.json["message"]
    call = await _call(client)
    other = await client.tool("triage_symptoms", path=MCP, caller_phone=OTHER, call_id=call["_call_id"],
                              symptoms="itchy rash")
    assert other["_is_error"]


# ---------------------------------------------------------------------------
# availability and booking
# ---------------------------------------------------------------------------

async def test_availability_respects_schedule_and_notice(client: Client, app):
    flags = await clinic.get_flags(app)
    for spec in CLINICIANS:
        for slot in clinic.generate_slots(app, spec, flags):
            dt = clinic.parse_slot(slot)
            assert dt.weekday() in spec["days"] and dt.hour != LUNCH_HOUR
            assert slot >= "2026-11-09T09:00"  # 60 min notice from 08:00
            assert slot <= "2026-11-22T23:59"  # 14-day window
    today = await client.tool("check_availability", path=MCP, specialist="fm-okafor", preferred_day="today")
    assert today["options"][0]["slot"] == "2026-11-09T09:00"
    assert today["options"][0]["spoken"] == "Monday, November 9 at 9 AM with Dr. Maya Okafor"
    derm = await client.tool("check_availability", path=MCP, specialty="dermatology")
    days = [o["slot"][:10] for o in derm["options"]]
    assert days == ["2026-11-10", "2026-11-11", "2026-11-12"]  # spread across days, Tue-Thu
    assert derm["say"].startswith("The earliest times are:")
    afternoon = await client.tool("check_availability", path=MCP, specialty="cardiology", preferred_day="wed",
                                  part_of_day="afternoon")
    assert all(o["slot"].startswith("2026-11-11T1") and int(o["slot"][11:13]) >= 12 for o in afternoon["options"])
    urgent = await client.tool("check_availability", path=MCP, specialty="neurology", urgency="urgent")
    assert all(o["slot"] <= "2026-11-11T23:59" for o in urgent["options"])
    spanish = await client.tool("check_availability", path=MCP, specialty="family_medicine", language="Spanish",
                                preferred_day="tuesday")
    assert spanish["options"][0]["specialist_id"] in ("fm-carter", "fm-okafor")


async def test_book_list_cancel_roundtrip(client: Client, app):
    call = await _call(client)
    option = await _first_option(client, specialty="orthopedics")
    booked = await _ctool(client, "book_appointment", call, slot_id=option["slot_id"], full_name="Ana Diaz",
                          reason="knee")
    assert booked["booking_status"] == "confirmed"
    assert booked["appointment_id"].startswith("ap") and len(booked["appointment_id"]) == 10
    assert booked["booking_line"] == f"You're booked with Dr. Tom Becker on {clinic.spoken_slot(option['slot'])}."
    listed = await _ctool(client, "list_my_appointments", call)
    assert listed["count"] == 1 and listed["upcoming"][0]["appointment_id"] == booked["appointment_id"]
    cancelled = await _ctool(client, "cancel_appointment", call, which="Becker")
    assert cancelled["booking_status"] == "cancelled"
    assert (await _ctool(client, "list_my_appointments", call))["count"] == 0
    taken = await app.state.call("schedule", "ortho-becker", "taken", "", "")
    assert taken["slots"] == []
    desk = (await app.state.call("desk", "global", "snapshot"))["desk"]
    assert [a["event"] for a in desk["activity"]][:2] == ["cancelled", "booked"]


async def test_concurrent_booking_same_slot_one_wins(client: Client):
    a = await _call(client, PATIENT)
    b = await _call(client, OTHER)
    option = await _first_option(client, specialist="ent-novak")
    results = await asyncio.gather(
        _ctool(client, "book_appointment", a, PATIENT, slot_id=option["slot_id"], full_name="Ana Diaz"),
        _ctool(client, "book_appointment", b, OTHER, slot_id=option["slot_id"], full_name="Bo Lee"),
    )
    statuses = sorted(r["booking_status"] for r in results)
    assert statuses == ["confirmed", "slot_taken"]


async def test_taken_slot_offers_alternatives(client: Client):
    a = await _call(client, PATIENT)
    b = await _call(client, OTHER)
    option = await _first_option(client, specialist="gi-kim")
    await _ctool(client, "book_appointment", a, PATIENT, slot_id=option["slot_id"], full_name="Ana Diaz")
    lost = await _ctool(client, "book_appointment", b, OTHER, slot_id=option["slot_id"], full_name="Bo Lee")
    assert lost["booking_status"] == "slot_taken"
    assert len(lost["alternatives"]) == 2
    assert all(alt["slot_id"] != option["slot_id"] and alt["specialist_id"] == "gi-kim" for alt in lost["alternatives"])
    assert "Dr. Samuel Kim" in lost["message"]


async def test_duplicate_and_limit_rules(client: Client, app):
    call = await _call(client)
    first, second = (await client.tool("check_availability", path=MCP, specialist="derm-raman"))["options"][:2]
    assert (await _ctool(client, "book_appointment", call, slot_id=first["slot_id"], full_name="Ana Diaz"))[
        "booking_status"] == "confirmed"
    dup = await _ctool(client, "book_appointment", call, slot_id=second["slot_id"], full_name="Ana Diaz")
    assert dup["booking_status"] == "duplicate"
    await app.clinic_flags.update({"max_upcoming_per_patient": 1})
    other = await _first_option(client, specialist="card-reyes")
    limited = await _ctool(client, "book_appointment", call, slot_id=other["slot_id"], full_name="Ana Diaz")
    assert limited["booking_status"] == "limit"
    # compensation: the cardiology slot was given back
    taken = await app.state.call("schedule", "card-reyes", "taken", "", "")
    assert taken["slots"] == []


async def test_bookings_paused_by_kv_flag(client: Client, app):
    await app.clinic_flags.update({"bookings_enabled": False})
    call = await _call(client)
    assert call["booking_enabled"] == "false" and call["paused_message"].startswith("Online booking is paused")
    option = await _first_option(client, specialty="ent")
    res = await _ctool(client, "book_appointment", call, slot_id=option["slot_id"], full_name="Ana Diaz")
    assert res["booking_status"] == "paused"
    raw = await app.kv.get("flags/clinic")
    assert '"bookings_enabled": false' in raw


async def test_invalid_slot_rejected(client: Client):
    call = await _call(client)
    for slot_id in ("fm-okafor@2020-01-01T09:00", "nobody@2026-11-10T09:00", "fm-okafor@2026-11-10T12:00",
                    "fm-okafor@2026-11-10T09:10", "garbage"):
        res = await _ctool(client, "book_appointment", call, slot_id=slot_id, full_name="Ana Diaz")
        assert res["booking_status"] == "invalid", slot_id
    option = await _first_option(client, specialty="ent")
    nameless = await _ctool(client, "book_appointment", call, slot_id=option["slot_id"], full_name="  ")
    assert nameless["booking_status"] == "need_name"


async def test_callback_request_lands_at_desk(client: Client, app):
    call = await _call(client)
    res = await _ctool(client, "request_callback", call, full_name="Ana Diaz", reason="billing question")
    assert res["requested"] and res["when"] == "today"
    desk = (await app.state.call("desk", "global", "snapshot"))["desk"]
    (cb,) = desk["callbacks"]
    assert cb["phone"] == PATIENT and cb["reason"] == "billing question" and cb["done"] is False


async def test_booking_confirmation_archived(client: Client, app):
    call = await _call(client)
    option = await _first_option(client, specialty="womens_health")
    booked = await _ctool(client, "book_appointment", call, slot_id=option["slot_id"], full_name="Ana Diaz")
    doc = await app.storage.get_json(f"clinic/appointments/{booked['appointment_id']}.json")
    assert doc["patient"] == "+15***3001" and doc["specialist_id"] == "wh-weiss"
    assert PATIENT not in str(doc)


async def test_book_webhook_flat_result(client: Client):
    call = await _call(client)
    option = await _first_option(client, specialty="ent")
    resp = await client.post("/clinic/tools/book", {"caller_phone": PATIENT, "call_id": call["_call_id"],
                                                    "slot_id": option["slot_id"], "full_name": "Ana Diaz"})
    assert resp.json["booking_status"] == "confirmed" and resp.json["booking_line"].startswith("You're booked")


async def test_clinic_mcp_lists_8_tools(client: Client):
    init = await client.rpc("initialize", path=MCP)
    assert init.json["result"]["serverInfo"]["name"] == "northside-clinic-mcp"
    tools = (await client.rpc("tools/list", path=MCP)).json["result"]["tools"]
    assert len(tools) == 8 and all("inputSchema" in t for t in tools)
    info = await client.tool("get_clinic_info", path=MCP)
    assert "200 Harbor Avenue" in info["say"]


def test_clinic_workflow_valid():
    directory = Path(validate_workflow.ASSISTANTS_DIR) / "clinic"
    assert validate_workflow.check("clinic", directory) == []
    summary = validate_workflow.summarise(_flow())
    assert summary == "13 nodes, 30 edges, 6 speak nodes, 9 variable-comparison edges, 15 LLM edges"


def _flow():
    import json

    return json.loads((Path(validate_workflow.ASSISTANTS_DIR) / "clinic" / "conversation_flow.json").read_text())



async def test_triage_is_recorded_on_the_patient(client: Client, app):
    call = await _call(client)
    res = await _ctool(client, "triage_symptoms", call, symptoms="I have an itchy rash")
    assert res["triage_urgency"] == "routine"
    patient = (await app.state.call("patient", PATIENT, "snapshot"))["patient"]
    assert patient["lastTriage"]["specialty"] == "dermatology"
    assert patient["lastTriage"]["urgency"] == "routine" and patient["lastTriage"]["at"] > 0


@pytest.mark.parametrize("text,urgency", [
    ("I've had a cough for a week, no fever, no chest pain", "routine"),
    ("no fever or trouble breathing, just a cough", "routine"),
    ("I don't have chest pain, just a sore throat", "routine"),
    ("no fever but chest pain since this morning", "emergency"),
    ("I'm not sure if it's chest pain or heartburn", "emergency"),
    ("I said no, I have chest pain", "emergency"),
    ("I can't breathe", "emergency"),
    ("I don't want to die", "crisis"),
    ("no high fever, but a deep cut that needs stitches", "urgent"),
])
def test_triage_handles_negation(text, urgency):
    assert clinic.triage_rules(text)["urgency"] == urgency
