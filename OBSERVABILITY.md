# Observability and runbook

## What is instrumented
**1. One JSON log line per request** (API function), for example a build:
```json
{"level":"info","event":"request","trace_id":"v3:b1ea...","route":"build_agent","outcome":"ok","latency_ms":2140,
 "caller":"+15***2001","node":"build","tool":"build_agent","agent_key":"agc7239537","rebuild":false,
 "build":"ready","feature_level":"full","persona_source":"inference",
 "spans":[{"name":"actor builder.getDraft","ms":6},{"name":"actor builder.beginBuild","ms":7},
          {"name":"kv.get polish","ms":18},{"name":"backend polish","ms":910},{"name":"kv.put","ms":15},
          {"name":"telnyx.create full","ms":1120},{"name":"kv.put agent","ms":14},{"name":"actor builder.finishBuild","ms":8}]}
```
(Example values. Run `smoke.py` against your deployment to get real numbers.) Every webhook, MCP tool, generated-agent webhook and alarm job writes such a line: **caller (masked), node, tool, outcome, latency, spans**. No draft contents, messages or full phone numbers are logged.

**2. One trace across the whole system.** `trace_id` = the Forge call's `call_control_id`. It flows into every MCP tool (`call_id`), every actor RPC (`x-trace-id`), **into scheduled jobs**, out through the actor **alarm** (`x-trace-id` on `/internal/jobs`) and into `job.done`. One search shows the call, the build, the Telnyx API calls and the outbound test call that happened later.

**3. `Server-Timing` header** on every response, so per-hop latency is visible from `smoke.py` or `curl -i`.

**4. OpsMetrics actor:** per-minute requests, errors and degraded responses **per route** (`forge_variables`, `build_agent`, `mcp`, `agent_take_message`, `jobs`…), p50/p95, and the last 20 errors with trace IDs. Written **after** the response is sent, so it never adds latency or fails a call.

**5. `/ops/health`:** KV round-trip plus actor RPC (and a best-effort storage probe); 503 on failure or when the 5-minute error rate is above 20%. **6. `scripts/ops_watch.py`:** live dashboard with alerts (error rate, p95, **any failed build**). **7. Console Ops tab (`/ui#ops`):** the same health, metrics, per-route table and recent errors with trace IDs, refreshed every 5 s, next to the flag switches.

**Shared OpsMetrics actor.** The `alarm` route in the metrics is written by forge-state itself: `BuilderProfile.alarm()` records each delivery to `/internal/jobs` (ok, degraded when a job failed, error when the API was unreachable) straight into `OpsMetrics("global")` through its own binding. That's the same instance forge-api writes to over RPC. Alarm failures show up next to webhook traffic, even though no HTTP request to forge-api ever happened.

**Clinic signals:** routes `clinic_variables`, `clinic_triage`, `clinic_book`, `clinic_mcp`; log events `clinic.booked`, `triage.llm_failed`, `clinic.desk_log_failed`, `storage.put_failed`. Spans name the clinician actor (`actor schedule.book ortho-becker`), so a slow or contended clinician is visible. Emergency and crisis triage results are also written to the ClinicDesk activity feed (masked), so the front desk sees them.

**Console traces:** console chats use a `web_…` call ID as their trace ID, so a typed conversation is traced exactly like a phone call (variables webhook → MCP tools → actor RPCs → build).

**Build-specific signals:** `agent.built` (feature level reached, persona source, ladder warnings), `build.ladder_step` (the API rejected a payload level), `polish.failed` (inference fell back to the template), `job.failed` (an outbound call or delete failed; `permanent` tells you whether it will retry), `alarm.jobs_failed` (the actor couldn't reach the API).

## How we'd know within a minute that it's broken
| Signal | Catches | Speed |
|---|---|---|
| Uptime check on `/ops/health` every 60 s | function, KV or actor host down; error spike | ≤ 1 min |
| `ops_watch.py` | any `build_agent` error, error rate > 20%, p95 > 1.2 s | 5 s |
| `feature_level != "full"` in `agent.built` | the Assistants API started rejecting part of our payload (silent degradation) | per build |
| `job.failed` / `alarm.jobs_failed` | test calls not going out, agents not being deleted (cost leak) | per job |
| `degraded` per route | Forge running on default variables | 5 s |

**The silent failures matter most:** if the variables webhook dies, Forge still answers using defaults. If the API rejects workflows, the ladder still ships a simpler agent. Both are designed to keep calls working, so both are **counted and alerted on** rather than hidden.

## What to look at first
1. `/ops/health`: which check is red?  2. `/ops/metrics` → `recent_errors` → take a `trace_id`.
3. Logs for that trace ID (`telnyx-edge ship status forge-api --logs`, and `telnyx-edge actors logs BuilderProfile --type runtime --tail` for the actor side): which span failed?
4. For builds: `build.ladder_step` lines show exactly which field the Assistants API rejected.
5. Portal conversation history for the call: which node and edge fired?

## Debugging trail (real bugs from building this, with the signal that found each)

### 1. Every build failed with "Something went wrong", found by the structured log
- **Symptom:** 14 tests failed; `build_agent` returned `failed`.
- **Signal:** the `request` log line for the build showed **every span `ok: true`** (create, KV put, finishBuild), then `"error":"TypeError: log() got multiple values for argument 'level'"`.
- **Cause:** after a successful build I logged `log("info", "agent.built", ..., level=level)`. The field name collided with the logger's own `level` parameter, so the build *succeeded at Telnyx* and then crashed while logging, which also released the lock as a failure.
- **Fix:** renamed the field to `feature_level`. **Lesson:** the spans proved the external calls had worked, which pointed straight at our own code after them.

### 2. A test call could ring after its agent had already been deleted
- **Signal:** the regression test `test_due_jobs_run_in_time_order`. When the expiry and a test call were both due, the log showed `job.done type=expire` and then `job.failed type=test_call status=404 "assistant not found"`.
- **Cause:** due jobs ran in **insertion order**. The 24 h expiry is scheduled at build time, before any test call, so after a delayed alarm (an outage, or retries) the agent was deleted first.
- **Fix:** sort due jobs by `at` in both actor implementations, plus a test that asserts `ai_call` comes before `delete_assistant`.

### 3. The trace broke at the alarm hop
- **Signal:** grepping the smoke run's trace ID showed every hop **except** the outbound call: `job.done` had a fresh `tr_…` ID.
- **Cause:** jobs didn't carry the originating call's ID, and the actor's alarm `fetch` sent no `x-trace-id`.
- **Fix:** store `traceId` on each job, send it as `x-trace-id` from the alarm, and log `job.done` with it. Now one search for a call ID runs from "I'd like a receptionist" to the moment the agent rang back.

### 4. The policy blocked "Ups and Downs Yoga"
- **Signal:** `test_policy_allows_normal_businesses` failed: `it would present itself as Ups`.
- **Fix:** short brand names (UPS, IRS, DHL) only match when the business name *is* the brand (plus support-style suffixes). Tests cover both the scams and the false positives.

### Template for your live bug (add one from your deployment for demo day)
```
Symptom / first signal (paste the log line or alert):
trace_id:
Spans showing the bad hop:
Root cause:
Fix (commit) and the guard (test / alert) added:
```

## Known gotchas
- The variables webhook response must be `{"dynamic_variables": {...}}`. A flat object is silently ignored.
- KV keys can't contain `:` (MemoryKV enforces this too).
- Outbound AI calls need the assistant's `telephony_settings.default_texml_app_id`, and the `From` number must be on that TeXML app.
- Generated assistants cost money while they exist, so watch for `job.failed type=expire`.
