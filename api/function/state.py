"""Stateful Actor access: RemoteState (RPC to the TS host) and LocalState,
an in-process Python emulator of every actor.

The Python classes below mirror `state/src/actors/*.ts` **exactly** — same
storage shapes, same method names, same reasons. Change both sides together;
`api/tests` runs against whichever backend `make_app()` picks (the contract
test runs the same suite against the real TS actors over RPC).
"""
from __future__ import annotations

import asyncio
import copy
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from .http import HttpResponse, join_url, request


class StateError(Exception):
    pass


def _hex(n: int) -> str:
    return "".join(secrets.choice("0123456789abcdef") for _ in range(n))


class RemoteState:
    """RPC client for the forge-state gateway."""

    local = False

    def __init__(self, base_url: str, token: str) -> None:
        self.base = base_url.rstrip("/")
        self.token = token

    async def call(self, actor: str, name: str, method: str, *args: Any, trace_id: str = "") -> Any:
        payload = {"actor": actor, "name": name, "method": method, "args": list(args), "trace_id": trace_id}
        headers: Dict[str, str] = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        resp: HttpResponse = await request("POST", join_url(self.base, "/rpc"), headers=headers, body=payload)
        data = resp.json()
        if resp.status != 200 or not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error") if isinstance(data, dict) else None
            raise StateError(f"state rpc {actor}.{name}.{method} -> {resp.status} {str(error or resp.text)[:120]}")
        return data.get("result")

    async def health(self) -> bool:
        resp = await request("GET", join_url(self.base, "/health"))
        return resp.ok


class Isolated:
    """Per-test actor-name prefix on a shared RemoteState, so the contract
    test sees fresh instances."""

    def __init__(self, inner: RemoteState, prefix: Optional[str] = None) -> None:
        self.inner = inner
        self.prefix = prefix or _hex(8)
        self.local = getattr(inner, "local", False)

    def fresh(self) -> "Isolated":
        """Rotate the prefix (a new set of isolated instances)."""
        self.prefix = _hex(8)
        return self

    async def call(self, actor: str, name: str, method: str, *args: Any, trace_id: str = "") -> Any:
        return await self.inner.call(actor, self.prefix + "/" + name, method, *args, trace_id=trace_id)


# ---------------------------------------------------------------------------
# Python actor emulator. One instance per (actor, name); calls are serialised
# per instance and args/results deep-copied — same guarantees as the platform.
# ---------------------------------------------------------------------------


class _Actor:
    """Shared in-memory storage semantics (structured-clone on read/write)."""

    storage_key = "state"

    def __init__(self, name: str) -> None:
        self.actor_name = name
        self.data: Dict[str, Any] = {}

    async def _load(self, key: Optional[str] = None) -> Any:
        key = key or self.storage_key
        value = self.data.get(key)
        return copy.deepcopy(value) if value is not None else None

    async def _put(self, key: Optional[str], value: Any) -> None:
        self.data[key or self.storage_key] = copy.deepcopy(value)


def _fresh_draft(now: int) -> Dict[str, Any]:
    return {
        "id": "df" + _hex(10),
        "fields": {},
        "version": 1,
        "status": "editing",
        "buildVersion": 0,
        "builtVersion": 0,
        "leaseUntil": 0,
        "agentKey": "",
        "lastError": "",
        "updatedAt": now,
    }


class BuilderProfile(_Actor):
    """Mirror of state/src/actors/builder-profile.ts (one instance per owner)."""

    storage_key = "profile"

    async def _load_state(self) -> Dict[str, Any]:
        stored = await self._load()
        if stored is not None:
            return stored
        return {
            "owner": self.actor_name,
            "callCount": 0,
            "lastCallAt": 0,
            "attested": {},
            "draft": _fresh_draft(int(time.time() * 1000)),
            "agents": [],
            "messages": [],
            "jobs": [],
            "alarmAt": None,
        }

    async def _save_state(self, state: Dict[str, Any]) -> None:
        jobs = state.get("jobs") or []
        state["alarmAt"] = min((j["at"] for j in jobs), default=None)
        await self._put(None, state)

    # -- call binding ------------------------------------------------------

    async def startCall(self, owner: str, call_id: str, attested: bool, now: int) -> Dict[str, Any]:
        s = await self._load_state()
        s["owner"] = owner
        s["callCount"] += 1
        s["lastCallAt"] = now
        if call_id:
            s["attested"][call_id] = bool(attested)
            ids = list(s["attested"].keys())
            for old in ids[: len(ids) - 10]:
                del s["attested"][old]
        draft = s["draft"]
        has_draft = bool(draft["fields"]) and draft["status"] != "built"
        active = [a for a in s["agents"] if a["status"] == "active"]
        result = {
            "ok": True,
            "callCount": s["callCount"],
            "returning": s["callCount"] > 1,
            "hasDraft": has_draft,
            "draftBusiness": draft["fields"].get("business_name", "") if has_draft else "",
            "draftStatus": draft["status"] if has_draft else "none",
            "activeAgents": len(active),
            "lastAgentName": active[-1]["name"] if active else "",
            "unreadMessages": len([m for m in s["messages"] if not m.get("read")]),
        }
        await self._save_state(s)
        return result

    async def callInfo(self, call_id: str) -> Dict[str, Any]:
        s = await self._load_state()
        return {"ok": True, "known": call_id in s["attested"], "attested": bool(s["attested"].get(call_id))}

    # -- draft -------------------------------------------------------------

    async def saveDraft(self, patch: Dict[str, Any], now: int) -> Dict[str, Any]:
        s = await self._load_state()
        draft = s["draft"]
        if draft["status"] == "building":
            return {"ok": False, "reason": "building"}
        for name, value in (patch.get("set") or {}).items():
            if value is None or value == "":
                draft["fields"].pop(name, None)
            else:
                draft["fields"][name] = value
        for name in ("tasks", "facts"):
            additions = (patch.get("append") or {}).get(name)
            if not isinstance(additions, list):
                continue
            items: List[Any] = draft["fields"].get(name) if isinstance(draft["fields"].get(name), list) else []
            lower = {str(v).lower() for v in items}
            for value in additions:
                if value is None or value == "":
                    continue
                if len(items) >= 12:
                    break
                if str(value).lower() in lower:
                    continue
                items.append(value)
                lower.add(str(value).lower())
            draft["fields"][name] = items
        draft["version"] += 1
        draft["status"] = "editing"
        draft["updatedAt"] = now
        await self._save_state(s)
        return {"ok": True, "draft": draft}

    async def getDraft(self) -> Dict[str, Any]:
        s = await self._load_state()
        return {"ok": True, "draft": s["draft"]}

    async def resetDraft(self, now: int) -> Dict[str, Any]:
        s = await self._load_state()
        s["draft"] = _fresh_draft(now)
        await self._save_state(s)
        return {"ok": True, "draft": s["draft"]}

    # -- build lock, quota, agents -----------------------------------------

    async def beginBuild(self, max_agents: int, lease_ms: int, now: int) -> Dict[str, Any]:
        s = await self._load_state()
        d = s["draft"]
        if d["status"] == "building" and d["leaseUntil"] > now:
            return {"ok": False, "reason": "in_progress", "draft": d}
        if d["status"] == "built" and d["builtVersion"] == d["version"]:
            agent = next((a for a in s["agents"] if a["key"] == d["agentKey"]), None)
            return {"ok": False, "reason": "already_built", "agent": agent, "draft": d}
        rebuild_of = ""
        if d["agentKey"] and any(a["key"] == d["agentKey"] and a["status"] == "active" for a in s["agents"]):
            rebuild_of = d["agentKey"]
        if not rebuild_of:
            active = [a for a in s["agents"] if a["status"] == "active"]
            if len(active) >= max_agents:
                return {"ok": False, "reason": "quota", "draft": d}
        d["status"] = "building"
        d["leaseUntil"] = now + lease_ms
        d["buildVersion"] = d["version"]
        d["updatedAt"] = now
        await self._save_state(s)
        return {"ok": True, "draft": d, "rebuildOf": rebuild_of}

    async def finishBuild(
        self, owner: str, version: int, result: Dict[str, Any], ttl_ms: int, now: int
    ) -> Dict[str, Any]:
        s = await self._load_state()
        d = s["draft"]
        if d["buildVersion"] != version:
            return {"ok": False, "reason": "stale", "draft": d}
        if not result.get("ok"):
            d["status"] = "editing"
            d["leaseUntil"] = 0
            d["lastError"] = result.get("error") or "build failed"
            d["updatedAt"] = now
            await self._save_state(s)
            return {"ok": False, "draft": d}
        key = result.get("key") or d["agentKey"]
        prev = next((a for a in s["agents"] if a["key"] == key), None)
        expires_at = now + ttl_ms
        agent = {
            "key": key,
            "assistantId": result.get("assistantId") or (prev["assistantId"] if prev else ""),
            "name": result.get("name", ""),
            "business": result.get("business", ""),
            "level": result.get("level", "full"),
            "status": "active",
            "createdAt": prev["createdAt"] if prev else now,
            "updatedAt": now,
            "expiresAt": expires_at,
            "testCalls": prev["testCalls"] if prev else [],
        }
        s["agents"] = [a for a in s["agents"] if a["key"] != key]
        s["agents"].append(agent)
        d["status"] = "built"
        d["builtVersion"] = d["version"]
        d["agentKey"] = key
        d["leaseUntil"] = 0
        d["lastError"] = ""
        d["updatedAt"] = now
        s["jobs"] = [j for j in s["jobs"] if not (j["type"] == "expire" and j["agentKey"] == key)]
        s["jobs"].append(
            {
                "id": "jb" + _hex(10),
                "type": "expire",
                "agentKey": key,
                "assistantId": agent["assistantId"],
                "owner": owner,
                "at": expires_at,
                "attempts": 0,
                "traceId": result.get("traceId", ""),
            }
        )
        await self._save_state(s)
        return {"ok": True, "agent": agent, "expiresAt": expires_at}

    async def listAgents(self, now: int) -> Dict[str, Any]:
        s = await self._load_state()
        return {"ok": True, "agents": [a for a in s["agents"] if a["status"] == "active"]}

    async def requestTestCall(
        self, agent_key: Optional[str], delay_ms: int, max_per_hour: int, now: int, trace_id: str
    ) -> Dict[str, Any]:
        s = await self._load_state()
        active = [a for a in s["agents"] if a["status"] == "active"]
        agent = None
        if agent_key:
            agent = next((a for a in active if a["key"] == agent_key), None)
        elif active:
            agent = active[-1]
        if not agent:
            return {"ok": False, "reason": "no_agent"}
        recent = len([tc for tc in agent["testCalls"] if tc["at"] > now - 3600000])
        if recent >= max_per_hour:
            return {"ok": False, "reason": "rate_limited", "agent": {"key": agent["key"], "name": agent["name"]}}
        agent["testCalls"].append({"at": now, "traceId": trace_id})
        if len(agent["testCalls"]) > 50:
            agent["testCalls"] = agent["testCalls"][-50:]
        job = {
            "id": "jb" + _hex(10),
            "type": "test_call",
            "agentKey": agent["key"],
            "assistantId": agent["assistantId"],
            "owner": s["owner"],
            "at": now + delay_ms,
            "attempts": 0,
            "traceId": trace_id or "",
        }
        s["jobs"].append(job)
        await self._save_state(s)
        return {"ok": True, "agentKey": agent["key"], "name": agent["name"], "at": job["at"], "delayMs": delay_ms}

    async def deleteAgent(self, key: str, now: int) -> Dict[str, Any]:
        s = await self._load_state()
        agent = next((a for a in s["agents"] if a["key"] == key), None)
        if not agent or agent["status"] == "deleted":
            return {"ok": False, "reason": "not_found"}
        agent["status"] = "deleting"
        agent["updatedAt"] = now
        s["jobs"] = [j for j in s["jobs"] if j["agentKey"] != key]
        s["jobs"].append(
            {
                "id": "jb" + _hex(10),
                "type": "expire",
                "agentKey": key,
                "assistantId": agent["assistantId"],
                "owner": s["owner"],
                "at": now,
                "attempts": 0,
                "traceId": "",
            }
        )
        if s["draft"]["agentKey"] == key:
            s["draft"]["agentKey"] = ""
        await self._save_state(s)
        return {"ok": True, "agent": {"key": agent["key"], "name": agent["name"]}}

    # -- inbox --------------------------------------------------------------

    async def addMessage(self, agent_key: str, msg: Dict[str, Any], now: int) -> Dict[str, Any]:
        s = await self._load_state()
        agent = next((a for a in s["agents"] if a["key"] == agent_key), None)
        message = {
            "id": "mg" + _hex(10),
            "agentKey": agent_key,
            "agentName": agent["name"] if agent else msg.get("agent_name", ""),
            "from": msg.get("from", ""),
            "caller_name": msg.get("caller_name", ""),
            "callback_number": msg.get("callback_number", ""),
            "message": msg.get("message", ""),
            "at": now,
            "read": False,
        }
        s["messages"].insert(0, message)
        del s["messages"][50:]
        unread = len([m for m in s["messages"] if not m.get("read")])
        await self._save_state(s)
        return {"ok": True, "id": message["id"], "unread": unread}

    async def listMessages(self, mark_read: bool) -> Dict[str, Any]:
        s = await self._load_state()
        if mark_read:
            for m in s["messages"]:
                m["read"] = True
            await self._save_state(s)
        unread = len([m for m in s["messages"] if not m.get("read")])
        return {"ok": True, "messages": s["messages"], "unread": unread}

    # -- alarm jobs (off the RPC whitelist; LocalState uses them directly) --

    async def takeDueJobs(self, now: int) -> List[Dict[str, Any]]:
        s = await self._load_state()
        due = sorted([j for j in s["jobs"] if j["at"] <= now], key=lambda j: j["at"])
        due_ids = {j["id"] for j in due}
        s["jobs"] = [j for j in s["jobs"] if j["id"] not in due_ids]
        await self._save_state(s)
        return due

    async def applyJobResults(self, jobs: List[Dict[str, Any]], results: List[Dict[str, Any]], now: int) -> Dict[str, Any]:
        s = await self._load_state()
        by_id = {r["id"]: r for r in results if isinstance(r, dict)}
        for job in jobs:
            res = by_id.get(job["id"], {"id": job["id"], "ok": False})
            if res.get("ok"):
                if job["type"] == "expire":
                    agent = next((a for a in s["agents"] if a["key"] == job["agentKey"]), None)
                    if agent:
                        agent["status"] = "deleted"
                        agent["updatedAt"] = now
                continue
            if res.get("permanent"):
                continue
            attempts = (job.get("attempts") or 0) + 1
            if attempts >= 5:
                continue
            requeued = copy.deepcopy(job)
            requeued["attempts"] = attempts
            requeued["at"] = now + 60000 * attempts
            s["jobs"].append(requeued)
        await self._save_state(s)
        return {"ok": True}

    async def snapshot(self) -> Dict[str, Any]:
        s = await self._load_state()
        return {"ok": True, "profile": s}


class OpsMetrics(_Actor):
    """Mirror of state/src/actors/ops-metrics.ts ("global" instance)."""

    storage_key = "state"

    async def _load_state(self) -> Dict[str, Any]:
        stored = await self._load()
        return stored if stored is not None else {"buckets": {}, "recent": []}

    async def record(
        self, route: str, outcome: str, ms: int, now: int, trace_id: str, error: Optional[str] = None
    ) -> Dict[str, Any]:
        s = await self._load_state()
        bucket_key = str(now // 60000)
        bucket = s["buckets"].setdefault(bucket_key, {"count": 0, "errors": 0, "routes": {}, "lat": []})
        bucket["count"] += 1
        stats = bucket["routes"].setdefault(route, {"count": 0, "errors": 0, "degraded": 0})
        stats["count"] += 1
        if outcome == "error":
            bucket["errors"] += 1
            stats["errors"] += 1
        elif outcome == "degraded":
            stats["degraded"] += 1
        bucket["lat"].append(ms)
        if len(bucket["lat"]) > 500:
            del bucket["lat"][: len(bucket["lat"]) - 500]
        if outcome in ("error", "degraded"):
            s["recent"].append({"at": now, "route": route, "outcome": outcome, "trace_id": trace_id, "error": error})
            if len(s["recent"]) > 20:
                del s["recent"][: len(s["recent"]) - 20]
        cutoff = now // 60000 - 120
        for key in [k for k in s["buckets"] if int(k) < cutoff]:
            del s["buckets"][key]
        await self._put(None, s)
        return {"ok": True}

    async def snapshot(self, window_minutes: int, now: int) -> Dict[str, Any]:
        s = await self._load_state()
        max_bucket = now // 60000
        min_bucket = max_bucket - (window_minutes - 1)
        requests = 0
        errors = 0
        lat: List[int] = []
        by_route: Dict[str, Dict[str, int]] = {}
        for key, bucket in s["buckets"].items():
            minute = int(key)
            if minute < min_bucket or minute > max_bucket:
                continue
            requests += bucket["count"]
            errors += bucket["errors"]
            lat.extend(bucket["lat"])
            for route, stats in bucket["routes"].items():
                agg = by_route.setdefault(route, {"count": 0, "errors": 0, "degraded": 0})
                agg["count"] += stats["count"]
                agg["errors"] += stats["errors"]
                agg["degraded"] += stats["degraded"]
        lat.sort()

        def pick(p: float) -> int:
            if not lat:
                return 0
            return round(lat[min(len(lat) - 1, int((len(lat) - 1) * p))])

        return {
            "ok": True,
            "window_minutes": window_minutes,
            "requests": requests,
            "errors": errors,
            "error_rate": round(errors / requests, 4) if requests else 0,
            "latency_p50_ms": pick(0.5),
            "latency_p95_ms": pick(0.95),
            "by_route": by_route,
            "recent_errors": list(reversed(s["recent"]))[:10],
        }


class AgentRegistry(_Actor):
    """Mirror of state/src/actors/agent-registry.ts ("global" instance)."""

    storage_key = "agents"
    MAX_AGENTS = 200

    async def _load_list(self) -> List[Dict[str, Any]]:
        stored = await self._load()
        return stored if isinstance(stored, list) else []

    async def upsert(self, rec: Dict[str, Any], now: int) -> Dict[str, Any]:
        agents = await self._load_list()
        idx = next((i for i, a in enumerate(agents) if a["key"] == rec["key"]), -1)
        if idx >= 0:
            merged = {**agents[idx], **rec}
            merged["createdAt"] = agents[idx].get("createdAt")
            merged["builds"] = (agents[idx].get("builds") or 0) + 1
            merged["updatedAt"] = now
        else:
            merged = {**rec, "createdAt": now, "builds": 1, "updatedAt": now}
        without = [a for a in agents if a["key"] != rec["key"]]
        agents = [merged, *without]
        del agents[self.MAX_AGENTS :]
        await self._put(None, agents)
        return {"ok": True, "agent": merged}

    async def setStatus(self, key: str, status: str, now: int) -> Dict[str, Any]:
        agents = await self._load_list()
        agent = next((a for a in agents if a["key"] == key), None)
        if not agent:
            return {"ok": False, "reason": "not_found"}
        agent["status"] = status
        agent["updatedAt"] = now
        await self._put(None, agents)
        return {"ok": True, "agent": agent}

    async def list(self, limit: int) -> Dict[str, Any]:
        agents = await self._load_list()
        return {"ok": True, "agents": agents[:limit]}

    async def get(self, key: str) -> Dict[str, Any]:
        agents = await self._load_list()
        agent = next((a for a in agents if a["key"] == key), None)
        return {"ok": True, "agent": agent}


class ClinicSchedule(_Actor):
    """Mirror of state/src/actors/clinic.ts ClinicSchedule (per clinician id).
    Slots are clinic-local `YYYY-MM-DDTHH:MM` strings; they sort lexicographically."""

    storage_key = "bookings"

    async def _bookings(self) -> Dict[str, Dict[str, Any]]:
        stored = await self._load()
        return stored if isinstance(stored, dict) else {}

    async def taken(self, from_slot: str, to_slot: str) -> Dict[str, Any]:
        bookings = await self._bookings()
        slots = sorted(
            s for s in bookings if (not from_slot or s >= from_slot) and (not to_slot or s <= to_slot)
        )
        return {"ok": True, "slots": slots}

    async def book(self, slot: str, booking: Dict[str, Any], now_slot: str, now: int) -> Dict[str, Any]:
        bookings = await self._bookings()
        if slot < now_slot:
            return {"ok": False, "reason": "past"}
        if slot in bookings:
            return {"ok": False, "reason": "taken"}
        existing = next(
            (
                (s, b)
                for s, b in bookings.items()
                if b.get("patient") == booking.get("patient") and s >= now_slot
            ),
            None,
        )
        if existing:
            return {"ok": False, "reason": "duplicate", "existing": existing[1]}
        full = {**booking, "slot": slot, "status": "booked", "createdAt": now}
        bookings[slot] = full
        await self._put(None, bookings)
        return {"ok": True, "booking": full}

    async def cancel(self, booking_id: str, patient: Optional[str], now: int) -> Dict[str, Any]:
        bookings = await self._bookings()
        found = next(((s, b) for s, b in bookings.items() if b.get("id") == booking_id), None)
        if not found:
            return {"ok": False, "reason": "not_found"}
        slot, booking = found
        if patient is not None and booking.get("patient") != patient:
            return {"ok": False, "reason": "forbidden"}
        del bookings[slot]
        await self._put(None, bookings)
        return {"ok": True, "booking": booking}

    async def list(self, from_slot: str, to_slot: str) -> Dict[str, Any]:
        bookings = await self._bookings()
        items = [
            {**b, "slot": s}
            for s, b in sorted(bookings.items())
            if (not from_slot or s >= from_slot) and (not to_slot or s <= to_slot)
        ]
        return {"ok": True, "bookings": items}

    async def snapshot(self) -> Dict[str, Any]:
        bookings = await self._bookings()
        return {"ok": True, "bookings": bookings}


class ClinicPatient(_Actor):
    """Mirror of ClinicPatient (per phone)."""

    storage_key = "patient"

    async def _load_state(self) -> Dict[str, Any]:
        stored = await self._load()
        if stored is not None:
            return stored
        return {
            "phone": self.actor_name,
            "name": "",
            "calls": 0,
            "attested": {},
            "appointments": [],
            "lastTriage": None,
        }

    async def _save_state(self, state: Dict[str, Any]) -> None:
        await self._put(None, state)

    async def startCall(self, phone: str, call_id: str, attested: bool, now_slot: str, now: int) -> Dict[str, Any]:
        s = await self._load_state()
        s["phone"] = phone
        s["calls"] += 1
        if call_id:
            s["attested"][call_id] = bool(attested)
            ids = list(s["attested"].keys())
            for old in ids[: len(ids) - 10]:
                del s["attested"][old]
        upcoming = [a for a in s["appointments"] if a.get("status") == "booked" and a.get("slot", "") >= now_slot]
        await self._save_state(s)
        return {
            "ok": True,
            "returning": s["calls"] > 1,
            "calls": s["calls"],
            "name": s["name"],
            "upcoming": upcoming,
        }

    async def callInfo(self, call_id: str) -> Dict[str, Any]:
        s = await self._load_state()
        return {"ok": True, "known": call_id in s["attested"], "attested": bool(s["attested"].get(call_id))}

    async def setName(self, name: str) -> Dict[str, Any]:
        s = await self._load_state()
        s["name"] = name or ""
        await self._save_state(s)
        return {"ok": True, "name": s["name"]}

    async def recordTriage(self, triage: Dict[str, Any]) -> Dict[str, Any]:
        s = await self._load_state()
        s["lastTriage"] = triage
        await self._save_state(s)
        return {"ok": True}

    async def addAppointment(self, appt: Dict[str, Any], max_upcoming: int, now_slot: str) -> Dict[str, Any]:
        s = await self._load_state()
        items = s["appointments"] if isinstance(s["appointments"], list) else []
        upcoming = [a for a in items if a.get("status") == "booked" and a.get("slot", "") >= now_slot]
        if len(upcoming) >= max_upcoming:
            return {"ok": False, "reason": "limit"}
        full = {**appt, "status": "booked", "createdAt": now_slot}
        items.insert(0, full)
        del items[20:]
        s["appointments"] = items
        if not s["name"] and (appt.get("patient_name") or appt.get("name")):
            s["name"] = appt.get("patient_name") or appt.get("name")
        await self._save_state(s)
        return {"ok": True, "appointment": full}

    async def cancelAppointment(self, appointment_id: str, now: int) -> Dict[str, Any]:
        s = await self._load_state()
        items = s["appointments"] if isinstance(s["appointments"], list) else []
        appt = next((a for a in items if a.get("id") == appointment_id), None)
        if not appt:
            return {"ok": False, "reason": "not_found"}
        appt["status"] = "cancelled"
        appt["cancelledAt"] = now
        await self._save_state(s)
        return {"ok": True, "appointment": appt}

    async def list(self, now_slot: str) -> Dict[str, Any]:
        s = await self._load_state()
        items = s["appointments"] if isinstance(s["appointments"], list) else []
        upcoming = [a for a in items if a.get("status") == "booked" and a.get("slot", "") >= now_slot]
        return {"ok": True, "upcoming": upcoming, "all": items, "name": s["name"]}

    async def snapshot(self) -> Dict[str, Any]:
        s = await self._load_state()
        return {"ok": True, "patient": s}


class ClinicDesk(_Actor):
    """Mirror of ClinicDesk ("global"): callbacks + activity for the front desk."""

    storage_key = "desk"

    async def _load_state(self) -> Dict[str, Any]:
        stored = await self._load()
        return stored if stored is not None else {"callbacks": [], "activity": []}

    async def _save_state(self, state: Dict[str, Any]) -> None:
        await self._put(None, state)

    async def addCallback(self, rec: Dict[str, Any], now: int) -> Dict[str, Any]:
        s = await self._load_state()
        callback = {"id": "cb" + _hex(8), "at": now, "done": False, **rec}
        s["callbacks"].insert(0, callback)
        del s["callbacks"][100:]
        await self._save_state(s)
        return {"ok": True, "callback": callback}

    async def resolveCallback(self, callback_id: str, now: int) -> Dict[str, Any]:
        s = await self._load_state()
        callback = next((c for c in s["callbacks"] if c.get("id") == callback_id), None)
        if not callback:
            return {"ok": False, "reason": "not_found"}
        callback["done"] = True
        callback["resolvedAt"] = now
        await self._save_state(s)
        return {"ok": True, "callback": callback}

    async def log(self, event: Dict[str, Any], now: int) -> Dict[str, Any]:
        s = await self._load_state()
        s["activity"].insert(0, {"at": now, **event})
        del s["activity"][100:]
        await self._save_state(s)
        return {"ok": True}

    async def snapshot(self) -> Dict[str, Any]:
        s = await self._load_state()
        return {"ok": True, "desk": s}


class LocalState:
    """In-process emulator: one instance + one asyncio.Lock per (actor, name),
    deep-copied args/results, private methods refused."""

    local = True

    TYPES: Dict[str, type] = {
        "builder": BuilderProfile,
        "ops": OpsMetrics,
        "registry": AgentRegistry,
        "schedule": ClinicSchedule,
        "patient": ClinicPatient,
        "desk": ClinicDesk,
    }

    def __init__(self) -> None:
        self.instances: Dict[Tuple[str, str], _Actor] = {}
        self.locks: Dict[Tuple[str, str], asyncio.Lock] = {}

    def instance(self, actor: str, name: str) -> _Actor:
        key = (actor, name)
        if key not in self.instances:
            cls = self.TYPES.get(actor)
            if cls is None:
                raise StateError(f"unknown actor {actor!r}")
            self.instances[key] = cls(name)
        return self.instances[key]

    async def call(self, actor: str, name: str, method: str, *args: Any, trace_id: str = "") -> Any:
        if actor not in self.TYPES:
            raise StateError(f"unknown actor {actor!r}")
        if method.startswith("_"):
            raise StateError(f"private method {actor}.{method} refused")
        inst = self.instance(actor, name)
        fn = getattr(inst, method, None)
        if not callable(fn):
            raise StateError(f"unknown method {actor}.{method}")
        lock = self.locks.setdefault((actor, name), asyncio.Lock())
        call_args = copy.deepcopy(list(args))
        async with lock:
            result = fn(*call_args)
            if asyncio.iscoroutine(result):
                result = await result
            return copy.deepcopy(result)
