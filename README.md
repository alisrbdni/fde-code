# Agent Forge + Clara

**Forge** is a phone voice AI that builds voice AI agents. You call it, describe your business, and it interviews
you, saving each answer as it goes. It then builds a **real Telnyx AI Assistant** with its own workflow, voice,
tools and webhooks. About 20 seconds after you hang up, **the new agent calls you back** so you can try it.
Generated agents take messages from their own callers into the owner's inbox, and delete themselves after 24 hours.

**Clara** is the assistant for the fictional **Northside Family Clinic**. She routes a caller's symptoms to the
right specialist, or to **911 / 988** by rule. She checks real availability and books without double-booking.
Forge hands medical callers off to Clara.

Both run on **Telnyx Edge Compute**: a Python Edge Function (`forge-api`) with Telnyx KV, plus a TypeScript Edge
Function (`forge-state`) hosting six **Stateful Actors**. The project was built with **OpenCode + the
`@telnyx/opencode` plugin** on Telnyx Inference (see [docs/OPENCODE_NOTES.md](docs/OPENCODE_NOTES.md)).

## Try it (live on Telnyx Edge)

| What | Where |
|---|---|
| **Call in your browser** (real voice call to the Telnyx assistants over WebRTC, no phone needed) | https://forge-api-3cb24047-7.telnyxcompute.com/call |
| **Chat in your browser** (no login: Forge and Clara tabs; the 🎙 Voice button speaks and listens) | https://forge-api-3cb24047-7.telnyxcompute.com/ui |
| Call Forge by phone | **Pending**: number `+61 7 4285 4682` is awaiting Telnyx's Australian regulatory approval ([details](#getting-a-phone-number)) |
| Call Clara by phone | **Pending**: same as above |
| Assistants on Telnyx | Forge `assistant-61126c7a-f78d-4711-b706-bfe846e2e616` · Clara `assistant-7006f7c6-203d-47e0-a3f1-2890acb8c98b` |
| API docs | https://forge-api-3cb24047-7.telnyxcompute.com/docs (OpenAPI: [/openapi.json](https://forge-api-3cb24047-7.telnyxcompute.com/openapi.json)) |
| Health (public) | https://forge-api-3cb24047-7.telnyxcompute.com/ops/health |
| Metrics (admin token) | https://forge-api-3cb24047-7.telnyxcompute.com/ops/metrics?window=5 |
| Forge MCP server | `https://forge-api-3cb24047-7.telnyxcompute.com/mcp` (POST, JSON-RPC 2.0, `Authorization: Bearer <MCP_TOKEN>`) |
| Clinic MCP server | `https://forge-api-3cb24047-7.telnyxcompute.com/clinic/mcp` (same) |
| Forge variables webhook | `https://forge-api-3cb24047-7.telnyxcompute.com/webhook/dynamic-variables` |
| Clara variables webhook | `https://forge-api-3cb24047-7.telnyxcompute.com/clinic/webhook/dynamic-variables` |
| Actor host (forge-state, RPC is token-protected) | https://forge-state-08d0ac2c-f.telnyxcompute.com/health |

Tokens for the protected routes are shared on request; they're not in the repo. The public chat and the browser
call need none: each public chat gets its own demo caller number, so visitors only ever see their own conversation.

### See inside the deployment
| What | Where |
|---|---|
| **KV contents** (flags, agent records, caches, chat sessions) | Console → **Ops** → *KV browser* (read-only, phone numbers masked) · `telnyx-edge storage kv key list c9ecd641-1027-4bef-ae25-e7eab17140a1` · `telnyx-edge storage kv key get c9ecd641-1027-4bef-ae25-e7eab17140a1 flags/global` |
| **Actor state** (drafts, agents, bookings, inbox) | Console → **Agents** / **Clinic** tabs · `telnyx-edge actors instances BuilderProfile` · `telnyx-edge actors logs BuilderProfile --tail` |
| **Logs** (one JSON line per request, with trace id) | `telnyx-edge logs forge-api --tail` · `telnyx-edge logs forge-state --tail` |
| **Metrics and health** | Console → **Ops** (refreshes every 5 s) · [/ops/health](https://forge-api-3cb24047-7.telnyxcompute.com/ops/health) · `telnyx-edge metrics forge-api` |
| **Deploys** | `telnyx-edge list` (status + URLs) · `telnyx-edge deployments forge-api` (history, why a deploy failed) |
| **API reference** | [/docs](https://forge-api-3cb24047-7.telnyxcompute.com/docs) · [/openapi.json](https://forge-api-3cb24047-7.telnyxcompute.com/openapi.json) |
| **Telnyx Portal** | [portal.telnyx.com](https://portal.telnyx.com/): AI → AI Assistants · Numbers · Edge Compute (functions, KV, secrets) · Billing |
| **All of the above in one place** | Console → **Ops** → *Links* |

**Cold starts:** Telnyx Edge scales idle functions to zero, so the first request after a quiet spell can take a few
seconds. [`.github/workflows/keep-warm.yml`](.github/workflows/keep-warm.yml) pings both functions every 5 minutes
to keep them warm. It doubles as the uptime check, because a failing `/ops/health` turns the run red and GitHub
emails you. A ship also replaces the running containers, so expect a brief blip right after a deploy.

### Getting a phone number
The assistants are created and answer browser calls already. For phone calls, the Australian number
`+61 7 4285 4682` has been bought but is **`requirement-info-pending`**: Telnyx requires regulatory documents
(identity and address) for Australian numbers before they can carry calls.
1. Portal → **Numbers → My Numbers** → the number → **Requirements**: upload the requested documents and wait for approval.
2. Portal → **AI → AI Assistants → Forge → Telephony**: assign the number to Forge (and a second number to Clara).
   The number is currently attached to another assistant's TeXML app, so move it.
3. For outbound test calls (an agent calling its owner back), create a TeXML application, assign a callback number to
   it, then run `./deployment.sh telephony` so `TEXML_APP_ID` and `FORGE_NUMBER` are set.
4. Put the numbers in the table above.

### By phone (once the numbers are live)
1. Call the **Forge** number. You'll hear a short disclosure: Forge is an AI, the call may be recorded, and agents
   it builds delete themselves after 24 hours.
2. Describe the business: *"I run a dental office called Sunrise Dental and want a receptionist."* Forge saves each
   answer as you speak, then asks for an agent name, a voice (warm, bright, calm, professional, British) and a
   couple of facts (hours, address, prices).
3. When it reads the summary back, say **"yes, build it"**. About ten seconds later Forge says the agent is live.
4. Say **"call me with it"** and **hang up**. About 20 seconds later **your new agent calls you back**: it knows you're
   its owner, so try anything, and leave it a message.
5. Call Forge again: it greets you as a returning builder ("Sam is still live", "you have 1 new message").
   Ask it to read your messages, list your agents, or delete one. Test calls, messages and deleting need a
   verified caller ID, so call from your own mobile rather than a VoIP app.
6. Try the guardrails: *"Build an agent for Amazon support that asks for card numbers"* is politely refused.
7. Say *"Actually I need a doctor's appointment"* and Forge hands you to **Clara**.

Calling the **Clara** number directly:
- *"My knee hurts when I run"*: she routes you to orthopedics and offers two real times; pick one and give your name.
- *"My baby has a fever"*: urgent, so you're seen within two days.
- *"I have crushing chest pain"*: the 911 message, read verbatim; no booking is offered.
- *"Can I cancel my appointment?"*, *"What are your hours?"*, *"Can I talk to a person?"*.
- Call back later: she greets you by name and mentions your next appointment.

### Without a phone: browser call, or the console
- **[/call](https://forge-api-3cb24047-7.telnyxcompute.com/call):** press *Start call with Forge* (or Clara), allow the
  microphone, and talk. This is a real call to the deployed Telnyx assistant, the same workflow, MCP tools and
  webhooks as a phone call.
- **[/ui](https://forge-api-3cb24047-7.telnyxcompute.com/ui):** type to Forge or Clara with no login (the public demo
  chat), or press **🎙 Voice** to speak and hear the replies (browser speech; Chrome, Edge or Safari).

For everything else in the console, go to **Settings** and paste the admin token (`ADMIN_TOKEN` from `.env`, not
the Telnyx API key; the console tells you if you paste the wrong one), then:
- **Forge** tab: type the same conversation as above. The workflow diagram highlights the current node, the
  variables panel shows what changed, and each tool call expands to show its arguments and result. You can also
  build an agent from the form below the chat.
- **Agents** tab: every built agent with its input, generated instructions, workflow, build trace and messages.
  Chat with it from its **Chat** sub-tab, or press **Test call** to have it phone the owner number.
- **Clinic** tab: talk to Clara, see the 7-day availability grid, bookings, callbacks and activity.
- **Ops** tab: health, per-route traffic, latency and recent errors, refreshed every 5 s, plus the feature flags
  (for example turn `builds_enabled` off and Forge's routing changes immediately, with no redeploy).

### MCP from the command line
```bash
API=https://forge-api-3cb24047-7.telnyxcompute.com
curl -s $API/clinic/mcp -H "Authorization: Bearer $MCP_TOKEN" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
curl -s $API/clinic/mcp -H "Authorization: Bearer $MCP_TOKEN" -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"check_availability","arguments":{"specialty":"dermatology"}}}'
```

## Quick start (local, no keys needed)
```bash
./run.sh            # TS actor host (real actors, with alarms) on :8787 + API on :8080, prints every URL
./run.sh --memory   # API only, actors emulated in Python (no Node needed)
./run.sh test       # workflow validator + 101 Python tests + 19 TS tests + contract test against the real TS actors
./run.sh smoke      # start the stack and walk a full call through it (scripts/smoke.py)
```
Python 3.11+ (standard library only at runtime) and Node 22+. Without `TELNYX_API_KEY` everything runs
offline: a fake Telnyx API, a stub chat model, and in-memory KV and storage. Copy `.env.example` to `.env` to go
live, or paste keys into the console's **Settings** tab (overrides are memory-only, never logged, never stored).

## Architecture
![architecture](docs/architecture.svg)

```
Caller ──▶ Forge (14-node workflow) ──handoff──▶ Clara (13-node workflow) ◀── Patient
              │ dyn vars · MCP /mcp · check_draft, build_agent      │ dyn vars · MCP /clinic/mcp · triage, book
              ▼                                                     ▼
     ┌──────────────────────────────────────────────┐  RPC   ┌──────────────────────────────────────┐
     │ forge-api  (Python Edge Function, stdlib)    │──────▶ │ forge-state (TS Edge Function)       │
     │  Forge + Clara webhooks, 2 MCP servers       │        │  BuilderProfile   1 per owner, alarm │
     │  build pipeline ──▶ Telnyx Assistants API    │ ◀──────│  OpsMetrics       global (shared)    │
     │  /agents/{key}/... runtime for EVERY agent   │ alarm  │  AgentRegistry    global             │
     │  /ui console + workflow simulator            │  jobs  │  ClinicSchedule   1 per clinician    │
     │  /internal/jobs ──▶ TeXML ai_calls / delete  │        │  ClinicPatient    1 per patient      │
     └──┬──────────────┬──────────────┬─────────────┘        │  ClinicDesk       global             │
        │ KV REST      │ S3 SigV4     │ Inference            └──────────────────────────────────────┘
        ▼              ▼              ▼
   Telnyx KV      Cloud Storage    persona polish · triage fallback · console chat
```
**Why two functions:** Stateful Actors are TypeScript classes reached through a binding, so `state/` is a thin,
token-protected RPC gateway to them. Everything else is Python, which reaches KV through the REST API (the env
binding is TypeScript-only). `api/function/state.py` has a Python emulator of every actor with identical
semantics. The contract step in `./run.sh test` runs the whole Python suite against the real TS actors to prove
they match.

### Choosing the primitive for each piece of state
| State | Primitive | Why |
|---|---|---|
| Draft spec, build lock, quota, inbox, call binding, scheduled jobs | Actor `BuilderProfile` (per owner phone) | read-modify-write; 4 concurrent builds must create **one** assistant (tested) |
| Test calls + auto-expiry | Actor **alarm** | runs with no traffic, retries with backoff, runs in time order |
| Clinic bookings | Actor `ClinicSchedule` (per clinician) | "is it free? then take it" must be atomic: two callers, one slot, one winner (tested) |
| Patient appointments + limit | Actor `ClinicPatient` (per phone) | check-then-act limit, with compensation if the limit is hit after the slot was taken |
| Callbacks + activity feed | Actor `ClinicDesk` (global) | concurrent appends |
| Agent index for the console | Actor `AgentRegistry` (global) | concurrent upserts |
| Counters / latency | Actor `OpsMetrics` (global), **shared**: API writes via RPC, alarms via binding | KV increments would lose updates |
| Feature flags | KV `flags/global`, `flags/clinic` | read-heavy, operator-written; 5 s memo; fail open to defaults |
| Published agent config | KV `agents/{key}` (TTL) | write once, read on every call to that agent |
| Caches | KV `cache/polish/{hash}` (7 d), `cache/triage/{hash}` (30 d) | never pay for the same inference twice |
| Console chat sessions | KV `session/chat/{id}` (TTL 2 h) | session data, single writer |
| Build archives, booking confirmations | Cloud Storage (S3, SigV4) | documents that outlive the agent |
| Validation, policy, triage rules, slot generation | plain function logic | stateless; no primitive needed |

## Conversation workflows
**Forge (14 nodes, 30 edges: 5 speak, 10 variable comparisons, 15 LLM).** `welcome` (speak: disclosure, "this
call may be recorded", auto-deletion) → `router` (no tools) → `discover` → `design` → `review` → `build` →
`deliver`, plus `manage`, `clinic_handoff`, `wrap`, and four speak nodes for verbatim outcomes:
`policy_decline`, `quota_full`, `paused`, `build_failed`.
- **Variable comparisons** are used where a tool already knows the answer: `draft_complete == "true"`
  (check_draft), `build_status == ready|blocked|quota|incomplete|paused|failed` (build_agent),
  `quota_remaining == "0"` and `builds_enabled == "false"` (variables webhook, from KV flags),
  `telnyx_conversation_duration_secs >= 840` (timeout).
- **LLM conditions** decide intent: build vs manage vs a patient who called the wrong line.
- **Dynamic variables change routing:** a returning caller hears "Welcome back. Your draft for Sunrise Dental is
  saved…"; `quota_remaining=0` sends them to `quota_full` before they waste time; a KV flag pauses building.

**Clara (13 nodes, 30 edges: 6 speak, 9 variable comparisons, 15 LLM).** `welcome` (speak: AI disclosure, not
medical advice, 911) → `intent` → `symptoms` → (`emergency` | `crisis` | `urgent_notice` | `schedule`) →
`booked`, plus `manage`, `faq`, `front_desk`, `paused`, `wrap`. **Urgency is never an LLM decision:**
`triage_symptoms` applies phrase rules (crisis > emergency > urgent > routine), and `triage_urgency` comes back
as a variable that a comparison edge routes on. The LLM may only pick a specialty from a closed enum, and only
when the rules matched none (the result is cached in KV).

**Generated agents** get their own workflow: `greet` (speak with the AI disclosure and `{{owner_line}}`) → `help`
→ `message` / `goodbye` (+ `transfer` when the owner gave a number), plus a 540 s timeout edge.
If the Assistants API rejects part of the payload, a **feature ladder** steps down
(`full → no_workflow → no_tools → minimal`), so the agent still ships. Each step is logged and counted.

## MCP servers (17 tools)
Both are stateless Streamable HTTP, JSON-RPC 2.0 (`initialize`, `ping`, `tools/list`, `tools/call`,
notifications, batches), with results as `{"content":[{"type":"text",…}], "structuredContent":…, "isError":…}`.
- **agent-forge-mcp** (`/mcp`): list_templates, list_voices, save_draft, get_draft, start_new_draft,
  list_my_agents, call_me_with_agent, list_messages, delete_agent.
- **northside-clinic-mcp** (`/clinic/mcp`): get_clinic_info, list_specialists, triage_symptoms,
  check_availability, book_appointment, list_my_appointments, cancel_appointment, request_callback.

Tools are **scoped per node** (`node_tools.json`). They never trust `caller_phone` alone: each call is checked
against the actor's `callInfo(call_id)`, which the variables webhook bound at call start. Private tools (test
calls, messages, delete) also need a verified caller ID (STIR/SHAKEN attestation).

## Safety
- Code appends the safety block and the AI disclosure to every generated agent *after* any LLM polish, so model
  output can never remove them. The polished persona is policy-checked again, and falls back to a template.
- The policy refuses agents that impersonate real organisations, collect payment, banking, password or ID
  numbers, hide that they're an AI, or threaten callers, give medical advice or cold-call people. It avoids
  false positives such as "Ups and Downs Yoga" or "we accept credit cards".
- Clara never diagnoses. 911 and 988 come from rules, and those paths are speak nodes, so the wording is verbatim.
- Logs mask phone numbers and never contain draft contents, messages or tokens. Archived payloads have their
  webhook tokens redacted.

## Observability
Full runbook and debugging trail: [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).
- **One JSON log line per request** with route, trace id, masked caller, outcome, status, latency and **spans**
  (every KV, actor, inference and Telnyx API hop).
- **One trace id per call:** the `call_control_id`. It flows through the variables webhook, every MCP and webhook
  tool, every actor RPC, the scheduled job, the actor alarm (`x-trace-id` on `/internal/jobs`) and `job.done`.
- **`server-timing`** on every response; **OpsMetrics actor** (per-route counts, errors, degraded, p50/p95,
  recent errors with trace ids); **`/ops/health`** (KV round trip, actor RPC, storage probe, error-rate gate);
  **`scripts/ops_watch.py`** (live terminal view with alerts) and the console **Ops** tab.

**How we'd know within a minute that the assistant is broken:** an uptime check on `/ops/health` every 60 s
(503 when KV or the actors fail, or when more than 20% of requests in the last 5 minutes errored), plus
`ops_watch.py`, which alerts within 5 s on any `build_agent` error, an error rate above 20% or p95 above 1.2 s.
The quiet failures are designed to keep calls working, so they're counted rather than hidden: `degraded` per
route (a webhook answered with defaults), `feature_level != full` (the ladder stepped down) and `job.failed`
(a test call or deletion didn't happen).

**What we'd look at first:** `/ops/health` (which check is red?), then `/ops/metrics` → `recent_errors` → take
the `trace_id` → search the logs for it (`telnyx-edge logs forge-api --since 15m --json`, and `telnyx-edge actors logs BuilderProfile --tail` for the actor side) → find the failing span.

## Deploy (Telnyx Edge)
`scripts/deploy.sh` prints the first-time steps (KV namespace, secrets, both functions, TeXML app and numbers,
assistants, storage). After that, run `scripts/deploy.sh ship`, which runs the validator and tests and then
ships both functions with `telnyx-edge ship`. Portal configuration (MCP servers, response → variable mappings,
per-node tools, path tests) is in [assistant/README.md](assistant/README.md).

## Repository
```
run.sh  .env.example  opencode.jsonc  AGENTS.md  DEMO_SCRIPT.md  OPENCODE_BUILD_SPEC.md
api/        Python Edge Function (func.toml, app.py, function/*.py, function/assistants/*, function/static/console.html, tests/)
state/      TypeScript Edge Function hosting the actors (src/actors/*.ts, local/ shim + dev server, test/)
assistant/  Portal setup for both assistants
scripts/    validate_workflow, create_assistant, smoke, ops_watch, set_flag, deploy.sh
docs/       OBSERVABILITY.md, OPENCODE_NOTES.md, architecture.mmd/.svg
```

## Task checklist
| Requirement | Where |
|---|---|
| AI Assistant with a multi-node Conversation Workflow, speak nodes, LLM + variable edges, callable by phone | `api/function/assistants/*`, `scripts/create_assistant.py`, `assistant/README.md` |
| Custom MCP server with ≥ 3 tools | two servers, 17 tools: `api/function/tools.py`, `api/function/clinic.py` |
| Dynamic webhook variables that change routing, served by an Edge Function | `webhook.py`, `clinic.py`, `quota_remaining` / `builds_enabled` / `triage_urgency` edges |
| Edge Function + KV (session, cache, flags) | `api/` + `kv.py` (`flags/*`, `cache/*`, `session/chat/*`, `agents/*`) |
| Stateful Actors with single-threaded read-modify-write | `state/src/actors/*` (build lock, slot booking, counters) |
| Observability: structured logs, metrics, trace, runbook | `log.py`, OpsMetrics, `/ops/*`, `ops_watch.py`, `docs/OBSERVABILITY.md` |
| Telnyx Inference via OpenCode | `opencode.jsonc` (`@telnyx/opencode` plugin), `docs/OPENCODE_NOTES.md` |
| Public deployment + docs | `scripts/deploy.sh`, this README, `/docs` |
| Stretch: multi-assistant routing, variable edges, actor alarms, Cloud Storage, KV flags, shared actor, custom routing variables, distributed tracing | handoff to Clara · comparison + timeout edges · BuilderProfile alarm · `storage.py` · `set_flag.py` · OpsMetrics written by API and alarm · `quota_remaining`, `triage_urgency` · `call_control_id` trace |
