"""The build pipeline: flags -> draft -> assessment -> lock -> polish -> Telnyx
(with the feature ladder) -> KV/registry/archive -> finishBuild.

`build` is the only place that creates assistants. Any exception releases the
build lock (finishBuild with ok=False), so a crashed build can never wedge the
draft. `run_jobs` executes alarm jobs (test calls, expiry) and `record_build`
archives a finished build (registry + storage, best effort). Never logs full
phone numbers or unredacted payloads.
"""
from __future__ import annotations

import copy
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from . import generator, log
from .config import mask_phone
from .drafts import assess
from .state import StateError
from .telnyx_api import TelnyxError

BUILD_LEASE_MS = 60_000  # finishBuild normally releases far sooner


def _ms() -> int:
    return int(time.time() * 1000)


class PermanentError(Exception):
    """A job failure that must never be retried (e.g. missing TeXML config)."""


def _display_name(fields: Dict[str, Any]) -> str:
    agent = str(fields.get("agent_name") or "").strip()
    business = str(fields.get("business_name") or "").strip()
    if agent and business:
        return f"{agent} - {business}"
    return agent or business or "your agent"


async def build(app: Any, trace: Any, owner: str, call_id: str = "") -> Dict[str, Any]:
    """Build (or rebuild) the caller's drafted agent. Never raises."""
    try:
        return await _build(app, trace, owner, call_id)
    except Exception as exc:
        log("error", "agent.build_failed", owner=mask_phone(owner), error=str(exc)[:200])
        return {
            "build_status": "failed",
            "message": "Something went wrong while building. Your draft is saved, "
            "so please try again in a minute or two.",
        }


async def _build(app: Any, trace: Any, owner: str, call_id: str) -> Dict[str, Any]:
    flags, _ = await app.flags.get(trace)
    if not flags.builds_enabled:
        return {"build_status": "paused", "message": flags.maintenance_message}

    now = _ms()
    async with trace.span("actor builder.getDraft"):
        got = await app.state.call("builder", owner, "getDraft", trace_id=trace.trace_id)
    draft = got.get("draft") or {}
    fields = draft.get("fields") or {}
    assessed = assess(fields)
    if assessed["policy_blocked"]:
        reason = assessed["policy_reason"]
        return {"build_status": "blocked", "policy_reason": reason,
                "message": f"I can't build that, because {reason}."}
    if assessed["missing"]:
        missing = assessed["missing"]
        return {"build_status": "incomplete", "missing_fields": missing,
                "message": "The draft isn't complete yet. I still need " + ", ".join(missing) + "."}

    async with trace.span("actor builder.beginBuild"):
        begin = await app.state.call(
            "builder", owner, "beginBuild",
            flags.max_agents_per_caller, BUILD_LEASE_MS, now, trace_id=trace.trace_id,
        )
    if not begin.get("ok"):
        return _begin_refused(begin, flags, now)

    draft = begin.get("draft") or {}
    version = int(draft.get("buildVersion") or 0)
    rebuild_of = str(begin.get("rebuildOf") or "")
    key = rebuild_of or ("ag" + secrets.token_hex(4))
    assistant_id_for_update = ""
    if rebuild_of:
        async with trace.span("actor builder.listAgents"):
            listed = await app.state.call("builder", owner, "listAgents", now, trace_id=trace.trace_id)
        assistant_id_for_update = str(
            next((a.get("assistantId") for a in listed.get("agents") or []
                  if a.get("key") == rebuild_of), "")
        )

    try:
        async with trace.span("polish"):
            persona, greeting, persona_source = await generator.polish(app, trace, fields, flags)
        payload = generator.full_payload(key, fields, app.settings, persona, greeting)
        async with trace.span("telnyx.create_assistant"):
            result = await create_with_ladder(
                app, trace, payload=payload, fields=fields, greeting=greeting,
                key=key, rebuild_assistant_id=assistant_id_for_update,
            )
        assistant_id, level, warnings, applied = result
    except Exception as exc:
        log("error", "agent.build_failed", owner=mask_phone(owner), error=str(exc)[:200])
        await _release_lock(app, trace, owner, version, str(exc)[:200])
        return {
            "build_status": "failed",
            "message": "Something went wrong while building. Your draft is saved, "
            "so please try again in a minute or two.",
        }

    ttl_ms = flags.agent_ttl_hours * 3_600_000
    display = _display_name(fields)
    kv_record = {
        "key": key,
        "owner": owner,
        "assistant_id": assistant_id,
        "name": display,
        "business": fields.get("business_name") or "",
        "level": level,
        "created_at": now,
    }
    try:
        async with trace.span("kv.put agents"):
            await app.kv.put(f"agents/{key}", _json(kv_record), ttl_secs=ttl_ms // 1000 + 3600)
    except Exception as exc:
        log("warn", "kv.agents_put_failed", agent_key=key, error=str(exc)[:160])

    finish_result = {
        "ok": True, "key": key, "assistantId": assistant_id, "name": display,
        "business": fields.get("business_name") or "", "level": level,
        "traceId": trace.trace_id,
    }
    async with trace.span("actor builder.finishBuild"):
        finish = await app.state.call(
            "builder", owner, "finishBuild",
            owner, version, finish_result, ttl_ms, now, trace_id=trace.trace_id,
        )
    if not finish.get("ok"):
        log("warn", "agent.finish_stale", owner=mask_phone(owner), agent_key=key)
        return {
            "build_status": "failed",
            "message": "Something went wrong while building. Your draft is saved, "
            "so please try again in a minute or two.",
        }

    expires_at = int(finish.get("expiresAt") or (now + ttl_ms))
    hours = max(0, round((expires_at - now) / 3_600_000))
    await record_build(
        app, trace,
        key=key, owner=owner, call_id=call_id, fields=fields, name=display,
        payload=applied, persona=persona, greeting=greeting, level=level,
        warnings=warnings, persona_source=persona_source, rebuild=bool(rebuild_of),
        assistant_id=assistant_id, expires_at=expires_at, ttl_ms=ttl_ms,
    )
    log(
        "info", "agent.built",
        owner=mask_phone(owner), agent_key=key, assistant_id=assistant_id,
        feature_level=level, persona_source=persona_source, ladder_warnings=warnings,
    )
    return {
        "build_status": "ready",
        "agent_name": display,
        "agent_key": key,
        "level": level,
        "updated": draft.get("updatedAt"),
        "expires_in_hours": hours,
        "message": f"{display} is live and will answer your callers for about {hours} hours.",
    }


def _begin_refused(begin: Dict[str, Any], flags: Any, now: int) -> Dict[str, Any]:
    reason = str(begin.get("reason") or "")
    if reason == "already_built":
        agent = begin.get("agent") or {}
        hours = max(0, round((int(agent.get("expiresAt") or now) - now) / 3_600_000))
        return {
            "build_status": "ready",
            "duplicate": True,
            "agent_key": agent.get("key"),
            "agent_name": agent.get("name"),
            "level": agent.get("level"),
            "expires_in_hours": hours,
            "message": f"{agent.get('name') or 'Your agent'} is already live with your latest saved draft.",
        }
    if reason == "in_progress":
        return {
            "build_status": "building",
            "message": "I'm already building your agent from this draft. Give me a few seconds, then ask again.",
        }
    if reason == "quota":
        return {
            "build_status": "quota",
            "message": (
                f"You already have {flags.max_agents_per_caller} live agents, which is the limit "
                "on this line. I can delete one for you, or you can wait for one to expire."
            ),
        }
    raise StateError(f"beginBuild refused: {reason}")


async def _release_lock(app: Any, trace: Any, owner: str, version: int, error: str) -> None:
    """Always release the build lease, even if releasing itself fails."""
    try:
        async with trace.span("actor builder.finishBuild"):
            await app.state.call(
                "builder", owner, "finishBuild",
                owner, version, {"ok": False, "error": error}, 0, _ms(),
                trace_id=trace.trace_id,
            )
    except Exception as exc:
        log("error", "agent.finish_failed", owner=mask_phone(owner), error=str(exc)[:160])


def _json(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


async def create_with_ladder(
    app: Any,
    trace: Any,
    *,
    payload: Dict[str, Any],
    fields: Dict[str, Any],
    greeting: str,
    key: str,
    rebuild_assistant_id: str,
) -> Tuple[str, str, List[str], Dict[str, Any]]:
    """Create (or update, for a rebuild) the assistant, walking the feature
    ladder down when the API rejects a shape.

    Returns (assistant_id, level_reached, warnings, applied_payload).
    - 400/422 (validation): drop one level and try that shape.
    - 429/5xx/network: retry the same shape once, then treat like validation
      and drop a level rather than fail the build.
    - A rebuild updates the existing assistant; if it vanished (404) we create.
    """

    async def _submit(level: str) -> str:
        candidate = generator.at_level(payload, fields, greeting, level)
        if rebuild_assistant_id:
            try:
                await app.telnyx.update_assistant(rebuild_assistant_id, candidate)
                return rebuild_assistant_id
            except TelnyxError as exc:
                if exc.status != 404:
                    raise
        data = await app.telnyx.create_assistant(candidate)
        return str(data)

    warnings: List[str] = []
    retried = False
    idx = 0
    levels = list(generator.LADDER)
    while idx < len(levels):
        level = levels[idx]
        try:
            assistant_id = await _submit(level)
            applied = generator.at_level(payload, fields, greeting, level)
            return assistant_id, level, warnings, applied
        except TelnyxError as exc:
            if exc.is_retryable and not retried:
                retried = True
                warnings.append(f"transient {exc.status} at level {level}; retried")
                continue
            if exc.is_validation and idx + 1 < len(levels):
                nxt = levels[idx + 1]
                warnings.append(f"level {level} rejected ({exc.status}); dropped to {nxt}")
                idx += 1
                continue
            raise
    raise TelnyxError(0, "build failed at every ladder level", "create_with_ladder")


async def run_jobs(app: Any, trace: Any, jobs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run due jobs in time order. 4xx (except 429) are permanent; 429, 5xx and
    network errors come back non-permanent so the actor requeues them."""
    results: List[Dict[str, Any]] = []
    for job in sorted(list(jobs), key=lambda j: int(j.get("at") or 0)):
        jid = str(job.get("id"))
        jtype = str(job.get("type"))
        try:
            if jtype == "test_call":
                if not (app.settings.texml_app_id and app.settings.forge_number):
                    raise PermanentError("outbound test calls are not configured")
                async with trace.span("telnyx.ai_call"):
                    await app.telnyx.ai_call(
                        app.settings.texml_app_id,
                        app.settings.forge_number,
                        str(job.get("owner")),
                        str(job.get("assistantId")),
                    )
            elif jtype == "expire":
                assistant_id = str(job.get("assistantId") or "")
                if assistant_id:
                    async with trace.span("telnyx.delete_assistant"):
                        await app.telnyx.delete_assistant(assistant_id)
                await _expire_local(app, trace, job)
            else:
                raise PermanentError(f"unknown job type {jtype!r}")
            results.append({"id": jid, "ok": True})
            log("info", "job.done", job_id=jid, type=jtype, agent_key=job.get("agentKey"),
                trace_id=job.get("traceId"))
        except PermanentError as exc:
            results.append({"id": jid, "ok": False, "permanent": True, "error": str(exc)[:160]})
            log("warn", "job.failed", job_id=jid, type=jtype, permanent=True,
                error=str(exc)[:160], trace_id=job.get("traceId"))
        except TelnyxError as exc:
            permanent = 400 <= exc.status < 500 and exc.status != 429
            results.append({"id": jid, "ok": False, "permanent": permanent, "error": str(exc)[:160]})
            log("warn", "job.failed", job_id=jid, type=jtype, permanent=permanent,
                status=exc.status, trace_id=job.get("traceId"))
        except Exception as exc:
            results.append({"id": jid, "ok": False, "error": str(exc)[:160]})
            log("warn", "job.failed", job_id=jid, type=jtype, error=str(exc)[:160],
                trace_id=job.get("traceId"))
    return results


async def _expire_local(app: Any, trace: Any, job: Dict[str, Any]) -> None:
    """KV + registry cleanup for an expiry (or an explicit delete)."""
    key = str(job.get("agentKey"))
    now = _ms()
    try:
        async with trace.span("kv.delete agents"):
            await app.kv.delete(f"agents/{key}")
    except Exception as exc:
        log("warn", "kv.delete_failed", agent_key=key, error=str(exc)[:160])
    try:
        async with trace.span("actor registry.setStatus"):
            await app.state.call("registry", "global", "setStatus", key, "deleted", now)
    except Exception as exc:
        log("warn", "registry.set_status_failed", agent_key=key, error=str(exc)[:160])


async def run_due_jobs(app: Any, trace: Any, owner: str) -> Dict[str, Any]:
    """The local alarm: take due jobs, run them, apply results, record ops."""
    now = _ms()
    async with trace.span("actor builder.takeDueJobs"):
        due = await app.state.call("builder", owner, "takeDueJobs", now, trace_id=trace.trace_id)
    if not due:
        return {"ran": 0, "results": []}
    results = await run_jobs(app, trace, due)
    async with trace.span("actor builder.applyJobResults"):
        await app.state.call(
            "builder", owner, "applyJobResults", due, results, _ms(), trace_id=trace.trace_id
        )
    outcome = "ok" if all(r.get("ok") for r in results) else "error"
    try:
        async with trace.span("actor ops.record alarm"):
            await app.state.call(
                "ops", "global", "record", "alarm", outcome,
                round(trace.latency_ms()), _ms(), trace.trace_id,
            )
    except Exception as exc:
        log("warn", "ops.record_failed", route="alarm", error=str(exc)[:160])
    return {"ran": len(due), "results": results}
