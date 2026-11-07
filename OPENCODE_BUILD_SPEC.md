# Agent Forge: build spec for OpenCode (Telnyx Inference)

This document is a complete, phase-by-phase specification for building **Agent Forge** and **Clara** from an empty
folder with **OpenCode + the `@telnyx/opencode` plugin**, using Telnyx-hosted models. It describes every part of the
current codebase: what each module does, its contracts and data, the exact workflow graphs, and the tests that prove
each phase works.

Work through it one phase at a time. Each phase ends with **acceptance checks**. Don't start the next phase until
they pass.

---

## 0. How to use this with OpenCode

### 0.1 Setup
```bash
opencode plugin @telnyx/opencode
opencode auth login --provider telnyx --method "API Key"
opencode auth list                                   # telnyx credential present
opencode run --model 'telnyx/moonshotai/Kimi-K3' 'Say hello in one sentence.'
```
`opencode.jsonc` at the repo root (create it in Phase 1):
```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@telnyx/opencode"],
  "model": "telnyx/moonshotai/Kimi-K3",
  "small_model": "telnyx/zai-org/GLM-5.3",
  "instructions": ["AGENTS.md"]
}
```
Model IDs change. Check `/telnyx` in the TUI or `~/.config/opencode/telnyx-models.json`. Suggested split: a strong
coding model (Kimi-K3, DeepSeek-V4, GLM-5.x) for phases, the small model for quick edits.

### 0.2 Working pattern per phase
1. Start a fresh OpenCode session (or `/compact`) at each phase, so the context stays small.
2. Paste the phase's **prompt** (each phase starts with one), and tell it to read this file section.
3. Let it write the code and tests, then run the acceptance commands **yourself** and paste failures back.
4. Commit after each phase: `git commit -m "phase N: ..."`.
5. Keep a running log in `docs/OPENCODE_NOTES.md`: model used, what worked, where it struggled (e.g. inventing
   Telnyx API fields), and the fix. The interview asks about this. Keep it honest and specific.

### 0.3 Tips that matter with hosted models
- Paste the **exact data tables** from the appendix when a phase needs them. Don't let the model invent lists.
- When the model guesses a Telnyx API field, paste the relevant Telnyx doc page into the prompt.
- Ask for small files. If a file passes about 300 lines, split the request ("write only the clinic MCP tools").
- Always ask it to run the tests (`cd api && python3 -m pytest -q`) before saying it's done.

---

## 1. What we're building

**Forge** is a phone voice AI that builds other voice AI agents. A caller describes their business; Forge interviews
them (saving a draft field by field), builds a **real Telnyx AI Assistant** (its own workflow, voice, tools, webhooks),
and that new agent **calls the owner back** about 20 s after hang-up. Generated agents take messages from their own
callers into the owner's inbox. Agents delete themselves after 24 h.

**Clara** is the assistant for the fictional **Northside Family Clinic**: she routes the caller's symptoms to a
specialist (or to **911 / 988** by rule), checks availability and books, without double-booking. Forge hands medical
callers off to Clara.

**Console** (`/ui`): type to Forge, Clara or any generated agent (the real workflows run over text), see flow
diagrams, agent internals, clinic bookings, ops metrics, KV flags, and override tokens on top of `.env`.

### 1.1 Architecture
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
Why two languages: Stateful Actors must be TypeScript classes reached through a binding from a TypeScript function.
Everything else is Python. `state/` is a thin, token-protected RPC gateway to the actors. Python reaches KV through
the REST API (the env binding is TypeScript-only).

### 1.2 Primitive per piece of state (the core design decision; keep it)
| State | Primitive | Why |
|---|---|---|
| Draft spec, build lock, quota, inbox, call binding, scheduled jobs | Actor `BuilderProfile` (per owner phone) | read-modify-write; concurrent builds must create ONE assistant |
| Test calls + auto-expiry | Actor **alarm** | runs with no traffic; retries with backoff; time-ordered |
| Clinic bookings | Actor `ClinicSchedule` (per clinician) | "free? then take it" must be atomic |
| Patient appointments + limit | Actor `ClinicPatient` (per phone) | check-then-act limit, with compensation |
| Callbacks + activity feed | Actor `ClinicDesk` ("global") | concurrent appends |
| Agent index for the console | Actor `AgentRegistry` ("global") | concurrent upserts |
| Counters / latency | Actor `OpsMetrics` ("global"), **shared** with alarms | KV increments lose updates |
| Feature flags | KV `flags/global`, `flags/clinic` | read-heavy, operator-written |
| Published agent config | KV `agents/{key}` (TTL) | write-once read-many |
| Caches | KV `cache/polish/{hash}`, `cache/triage/{hash}` | avoid paying inference twice |
| Console chat sessions | KV `session/chat/{id}` (TTL 2 h) | session data, single writer |
| Build archives, booking docs | Cloud Storage (S3) | documents that outlive the agent |

---

## 2. Ground rules (write these into `AGENTS.md` in Phase 1)
- `api/` is a Python Edge Function, **standard library only** at runtime (`cryptography` optional for signatures).
  Entry: `function/func.py::new()` returns an object with an ASGI 3 `handle(scope, receive, send)`.
- `state/` is the TypeScript function hosting the actors. Keep it thin.
- `api/function/state.py` contains a **Python emulator of every actor** with identical semantics. Change both
  together; the contract test runs the Python suite against the real TS actors.
- Never let LLM output remove the safety block or the AI disclosure from generated agents; code appends them.
- Tools never trust `caller_phone` alone: check the actor's `callInfo(call_id)`.
- KV keys use `/`, never `:`. Variables webhooks return `{"dynamic_variables": {...}}` (a flat object is ignored).
- Every `{{variable}}` in a workflow needs a default; run `scripts/validate_workflow.py`.
- Never log full phone numbers (use `mask_phone`), draft contents, messages or tokens. Don't name a log field `level`.
- Clara never diagnoses. Urgency (911/988/urgent) is decided by rules, never by an LLM.
- Console overrides are memory-only: never log values, never write them to KV.
- Before finishing any task: `./run.sh test`.

---

## 3. Repository layout (final)
```
run.sh                       start everything locally + print URLs (modes: full, --memory, test, smoke)
.env.example  .gitignore  opencode.jsonc  AGENTS.md  README.md  DEMO_SCRIPT.md
api/
  app.py                     local entry: loads ../.env, then app = new().handle
  func.toml                  Edge manifest (env vars, [telnyx] binding)
  pyproject.toml             pytest config: testpaths=["tests"], pythonpath=["."]
  function/
    __init__.py              from .func import new
    config.py  log.py  http.py  signature.py
    kv.py  state.py  storage.py  telnyx_api.py
    catalog.py  drafts.py  policy.py  tools.py  mcp.py  webhook.py  generator.py  builder.py
    clinic_catalog.py  clinic.py
    simulator.py  ui_api.py  openapi.py  func.py
    static/console.html
    assistants/forge/{assistant.json,conversation_flow.json,node_tools.json}
    assistants/clinic/{assistant.json,conversation_flow.json,node_tools.json}
  tests/  conftest.py  test_flows.py  test_clinic.py  test_console.py
state/
  telnyx.toml  package.json  tsconfig.json  tsconfig.local.json
  src/index.ts  src/log.ts
  src/actors/{builder-profile.ts,ops-metrics.ts,agent-registry.ts,clinic.ts}
  local/{edge-runtime-shim.ts,env.ts,dev-server.ts}
  test/actors.test.ts
assistant/README.md          Portal setup for both assistants
scripts/{deploy.sh,smoke.py,ops_watch.py,set_flag.py,validate_workflow.py,create_assistant.py}
docs/{OBSERVABILITY.md,OPENCODE_NOTES.md,architecture.mmd,architecture.svg}
```

---

## Phase 1: Python skeleton, config, logging, router, test harness

**Prompt:** "Read docs/OPENCODE_BUILD_SPEC.md sections 2, 3 and Phase 1. Create the Python function skeleton exactly
as specified, with the test harness, and make the acceptance tests pass. Standard library only."

### config.py
- `@dataclass(frozen=True) Settings` with: `telnyx_api_key, telnyx_api_base, kv_namespace_id, kv_base_url, state_url,
  state_token, mcp_token, admin_token, public_api_url, texml_app_id, forge_number, assistant_model, polish_model,
  chat_model, dry_run, log_level, verify_signatures, telnyx_public_key, service_name, storage_endpoint, storage_bucket,
  storage_region, storage_access_key, storage_secret_key, clinic_assistant_id, clinic_front_desk_number,
  clinic_utc_offset`.
  Properties: `local_kv = not (kv_namespace_id and telnyx_api_key)`, `local_state = not state_url`,
  `local_storage = not (storage_endpoint and storage_access_key)`.
- `DEFAULTS`: TELNYX_API_BASE `https://api.telnyx.com/v2`, KV_BASE_URL `https://api.telnyx.com/v2/storage/kvs`,
  API_URL `http://localhost:8080`, ASSISTANT_MODEL `moonshotai/Kimi-K3`, POLISH_MODEL
  `meta-llama/Meta-Llama-3.1-8B-Instruct`, LOG_LEVEL info, VERIFY_SIGNATURES false, SERVICE_NAME forge-api,
  STORAGE_BUCKET agent-forge, STORAGE_REGION us-central-1, STORAGE_SECRET_KEY telnyx, CLINIC_UTC_OFFSET `-05:00`.
- `OVERRIDABLE` dict name → {secret, label} for: TELNYX_API_KEY*, MCP_TOKEN*, STATE_URL, STATE_TOKEN*, KV_NAMESPACE_ID,
  API_URL, TEXML_APP_ID, FORGE_NUMBER, ASSISTANT_MODEL, POLISH_MODEL, CHAT_MODEL, FORGE_DRY_RUN, STORAGE_ENDPOINT,
  STORAGE_BUCKET, STORAGE_REGION, STORAGE_ACCESS_KEY*, STORAGE_SECRET_KEY*, CLINIC_ASSISTANT_ID,
  CLINIC_FRONT_DESK_NUMBER (* = secret). **ADMIN_TOKEN is never overridable.**
- `lookup(name, overrides) -> (value, source)` with source `override | env | default | unset`.
- `load_settings(overrides=None)`: precedence override > env > default. `chat_model` defaults to assistant_model;
  `storage_access_key` defaults to telnyx_api_key; `dry_run = FORGE_DRY_RUN=="true" or no key`;
  `admin_token = os.environ["ADMIN_TOKEN"]` only.
- `load_dotenv(path)`: KEY=VALUE lines, `#` comments, optional quotes and `export `; existing env vars win.
- `normalize_phone`: strips `sip:` and `@host`; 10 digits → `+1…`; 11–15 digits → `+…`; else None.
- `mask_phone("+15550102001") == "+15***2001"`; `mask_secret` shows `******` + last 4.

### log.py
- `log(level, event, **fields)` prints ONE line of JSON (`ts, level, event, ...`) to stdout (stderr for warn/error).
- `Trace(trace_id, route)`: `.set(**fields)`, `async with trace.span(name)` records `{name, ms, ok, note?}`,
  `.server_timing()` → `name;dur=ms, ..., total;dur=..`, `.end(outcome, level, **extra)` logs event `request` with
  all fields + spans. `trace.outcome` can be set by handlers (`degraded`, `client_error`, `error`).

### http.py
`async request(method, url, headers=, body=bytes|str|dict, timeout=)` → `HttpResponse(status, body)` with `.json()`
and `.text`; urllib in `asyncio.to_thread`; HTTP errors return a response, not an exception.

### func.py (skeleton now, routes added per phase)
- `App` holds settings + clients (built in `_wire(settings, previous=None)`; Phase 13 adds `reconfigure`).
- `Request(scope, body)`: method, path (trailing `/` stripped), query dict, lower-cased headers, `bearer()`.
- `_token_ok(expected, req, allow_query=False)`: no expected token → allowed (local dev); constant-time compare;
  `?token=` accepted only when `allow_query`.
- `Function.handle`: reads the body (413 over 256 KB), builds a `Trace` (id from `x-trace-id` or generated), calls
  `route()`, returns JSON with `x-trace-id` and `server-timing` headers. JSON decode errors → 400. Any exception →
  500, **except** webhooks the assistant depends on, which always answer usably (Phase 6 and 10 list the fallbacks).
  After the response is sent: `trace.end(...)` and record the request in OpsMetrics (skip `health`, `ops`,
  `ops_health`, `console` routes). Metric failures are logged and swallowed.
- `GET /` and `/health` → `{"ok": true, "service", "dry_run"}`.
- `new()` returns `Function()` (App built lazily on first request).

### tests/conftest.py
- `OWNER="+15550102001"`, `OTHER="+15550102002"`, `SUNRISE` fields (Appendix A.7).
- `Client(fn)`: drives the ASGI app in-process; `post/get/request`; parses JSON only when content-type is JSON.
  Helpers mirror Telnyx: `start_call(phone, call_id, attested)` posts the variables webhook payload
  (`data.payload.telnyx_end_user_target`, `call_control_id`, `telnyx_end_user_target_verified`), `tool(name, **args)`
  posts MCP `tools/call`, `save`, `check`, `build`, `ready_draft`.
- `make_app()` clears KV/STATE/token env vars, sets `TEXML_APP_ID, FORGE_NUMBER, API_URL`, returns
  `App(settings, kv=MemoryKV(), state=LocalState() or Isolated(RemoteState(STATE_URL_TEST)), telnyx=FakeTelnyx())`.
  `Isolated` prefixes actor names per test (for the contract test).

**Acceptance:** `GET /health` returns 200 with JSON; a log line is printed per request; tests run.

---

## Phase 2: KV, flags, cache

**Prompt:** "Implement api/function/kv.py per Phase 2 of the spec, with tests."

- `RestKV(base_url, namespace_id, api_key)`: `GET/PUT/DELETE {base}/{ns}/keys/{quoted key}`; `PUT ?ttl_secs=N`;
  404 on get → None; errors raise.
- `MemoryKV`: same interface, honours TTL, raises on `:` in keys, `fail=True` simulates an outage.
- `Flags` dataclass (Appendix A.5): `from_dict` (bools parsed, ints clamped), `to_dict`.
- `FlagStore(kv, key="flags/global", cls=Flags)`: `get(trace=None) -> (flags, source)` with a 5 s per-instance memo,
  source `memo|kv|default|default_on_error`; **fails open to defaults** if KV errors. `update(patch)` merges, writes,
  clears the memo. The clinic uses `FlagStore(kv, "flags/clinic", ClinicFlags)`.
- `cached(kv, trace, key, ttl, load) -> (value, hit)`: cache-aside, `{"v": value}` or `{"none": true}`, read/write
  errors logged and swallowed (cache is never a dependency), spans `kv.get <label>`, `backend <label>`, `kv.put`.

---

## Phase 3: Stateful Actors (TypeScript) + Python emulator + RPC

**Prompt:** "Build state/ (TypeScript actor host) and api/function/state.py (RPC client + emulator) for
BuilderProfile and OpsMetrics per Phase 3. Then write the TS tests and make npm test and typecheck pass."

### 3.1 state/ package
- `package.json`: type module; deps `@telnyx/edge-runtime`; dev deps `tsx`, `typescript`, `@types/node`. Scripts:
  `dev = tsx --tsconfig tsconfig.local.json local/dev-server.ts`, `test = tsx --tsconfig tsconfig.local.json --test test/*.test.ts`,
  `typecheck = tsc -p tsconfig.local.json --noEmit`, `ship = telnyx-edge ship`.
- `tsconfig.json`: ES2022, ESNext modules, Bundler resolution, lib `["ES2022","DOM","DOM.Iterable"]`, strict.
  `tsconfig.local.json` extends it with `types:["node"]` and `paths: {"@telnyx/edge-runtime": ["./local/edge-runtime-shim.ts"]}`.
- `telnyx.toml`: `name="forge-state"`, `main="src/index.ts"`, one `[[actors]]` block per binding:
  BUILDER/BuilderProfile, OPS/OpsMetrics, REGISTRY/AgentRegistry, SCHEDULE/ClinicSchedule, PATIENT/ClinicPatient,
  DESK/ClinicDesk; `[edge_compute] func_id, func_name="forge-state"`.
- `local/edge-runtime-shim.ts` (local only): `StatefulActor` base with `ctx` (`id`, `storage`) and `env`;
  `MemoryStorage` (`get/put/delete/list`, structured-clone values, `setAlarm/getAlarm`, test helper `fireAlarmNow`);
  `actorNamespace(Cls, env)` → `idFromName(name)` returns a Proxy whose method calls are **serialised per instance**
  (a promise queue), results structured-cloned; `__storage` exposes storage for tests; alarms enqueue `inst.alarm()`.
- `local/env.ts`: `makeEnv(token)` builds an Env with all six bindings; actors receive the same env object, so one
  actor can call another through a binding.
- `local/dev-server.ts`: Node http server on `PORT` (8787) calling the real fetch handler.
- `src/log.ts`: single-line JSON logs with `service: "forge-state"`.

### 3.2 Gateway `src/index.ts`
`POST /rpc {actor, name, method, args, trace_id}` with `Authorization: Bearer STATE_TOKEN` (500 if not configured,
401 if wrong, constant-time compare). Whitelist (400 otherwise):
- builder: startCall, callInfo, saveDraft, getDraft, resetDraft, beginBuild, finishBuild, listAgents, requestTestCall,
  deleteAgent, addMessage, listMessages, snapshot (**not** takeDueJobs / applyJobResults: alarm-internal)
- ops: record, snapshot
- registry: upsert, setStatus, list, get
- schedule: taken, book, cancel, list
- patient: startCall, callInfo, setName, recordTriage, addAppointment, cancelAppointment, list, snapshot
- desk: addCallback, resolveCallback, log, snapshot

Response `{ok:true, result}` with `server-timing: actor;dur=ms`; errors `{ok:false, error}` 500. Each RPC is logged
with `trace_id`. `GET /health` → `{ok, service}`.

### 3.3 BuilderProfile (one per OWNER phone) — storage key `profile`
State: `owner, callCount, lastCallAt, attested{callId: bool} (keep last 10), draft, agents[], messages[] (max 50),
jobs[], alarmAt`. Draft: `id, fields{}, version, status editing|building|built, buildVersion, builtVersion,
leaseUntil, agentKey, lastError, updatedAt`. Agent: `key, assistantId, name, business, level, status
active|deleting|deleted, createdAt, updatedAt, expiresAt, testCalls[]`. Job: `id, type test_call|expire, agentKey,
assistantId, owner, at, attempts, traceId`.
`save()` sets `alarmAt = min(job.at)` and calls `storage.setAlarm(alarmAt)`.
Methods:
- `startCall(owner, callId, attested, now)` → `{callCount, returning, hasDraft (fields and not built), draftBusiness,
  draftStatus, activeAgents, lastAgentName, unreadMessages}`.
- `callInfo(callId)` → `{known, attested}`.
- `saveDraft({set, append}, now)`: refused while building (`{ok:false, reason:"building"}`); `set` null/"" deletes a
  field; `append` only for `tasks`/`facts`, deduped, max 12; bumps version; status editing.
- `getDraft()`, `resetDraft(now)`.
- `beginBuild(maxAgents, leaseMs, now)`: `in_progress` if building and lease valid; `already_built` (+agent) if
  builtVersion == version; a rebuild (draft.agentKey active) skips quota; else `quota` if active >= max; otherwise
  status building, lease, buildVersion=version → `{ok, draft, rebuildOf}`.
- `finishBuild(owner, version, result, ttlMs, now)`: failure → editing + lastError; success → upsert agent record
  (keep createdAt and testCalls on rebuild), draft built, replace the agent's expire job with a new one at expiresAt.
- `listAgents(now)` (active only), `requestTestCall(agentKey|null, delayMs, maxPerHour, now, traceId)` (rate limit on
  testCalls within the last hour; schedules test_call), `deleteAgent(key, now)` (status deleting; removes its jobs;
  schedules an immediate expire; unlinks the draft).
- `addMessage(agentKey, msg, now)` (prepend, cap 50), `listMessages(markRead)`.
- `takeDueJobs(now)` → due jobs **sorted by `at`**; `applyJobResults(jobs, results, now)`: ok expire → agent deleted;
  failures requeued with `attempts+1` at `now + 60s × attempts` unless `permanent` or attempts reach 5.
- `alarm()`: take due jobs; POST `{jobs}` to `${API_URL}/internal/jobs` with `Bearer STATE_TOKEN` and
  `x-trace-id: jobs[0].traceId`; apply results (empty results on failure → requeue); then **record into
  `env.OPS.idFromName("global").record("alarm", ok|degraded|error, ms, now, traceId, error)`** (shared actor; best effort).
- `snapshot()`.

### 3.4 OpsMetrics ("global") — storage key `state`
Per-minute buckets `{count, errors, routes{route:{count,errors,degraded}}, lat[] (max 500)}`; ring buffer of the last
20 errors/degraded `{at, route, outcome, trace_id, error}`; outcomes `ok|degraded|client_error` aren't errors; drop
buckets older than 120 min. `snapshot(windowMinutes, now)` → `{window_minutes, requests, errors, error_rate,
latency_p50_ms, latency_p95_ms, by_route, recent_errors (10)}`.

### 3.5 Python `state.py`
- `RemoteState(base_url, token).call(actor, name, method, *args, trace_id=)` → POST `/rpc`; raises `StateError`.
- `LocalState`: `TYPES = {builder, ops, registry, schedule, patient, desk}`; one instance and one `asyncio.Lock`
  per `(actor, name)`; deep-copies args and results; refuses `_private` methods. Python classes mirror the TS ones
  exactly (Phase 10 and 11 add the rest).

**Acceptance (TS tests):** draft merge and dedupe; 5 concurrent `beginBuild` → exactly 1 ok; lease expiry frees a
crashed build; finishBuild + already_built + rebuild skips quota + quota; callInfo binding; test call rate limit +
alarm posts jobs + applies results (expire job remains); failed delivery requeues with backoff; 10 concurrent
messages all land; ops metrics aggregate; gateway auth + whitelist (takeDueJobs and constructor rejected).
`npm test && npm run typecheck` green.

---

## Phase 4: Forge catalog, draft validation, policy

**Prompt:** "Implement catalog.py, drafts.py and policy.py using the exact data in Appendix A.1–A.4. Add the policy
tests from Phase 4."

- `catalog.py`: TEMPLATES, VOICES, TONES, FIELDS, REQUIRED (Appendix A.1–A.2).
- `drafts.build_patch(args, current) -> ({"set","append"}, notes)`: strip `{}<>` and collapse whitespace, cut to
  `max`; validate tone/voice/template/language (else a note, field dropped); escalation number normalised (else a note);
  lower-case enum fields; a template pre-fills `purpose` and `tasks` if not set; `clear: [fields]` sets None.
- `drafts.assess(fields)` → `{complete, missing, policy_blocked, policy_reason, suggestions, summary}`. Missing =
  required fields + "at least one task or fact" unless tasks, facts or template. Complete only if nothing is missing
  and policy allows. Suggestions: template `ask_for` facts if fewer than 2 facts; voices if no voice.
- `drafts.summarize(f)`: one spoken paragraph ("Sam, the AI assistant for Sunrise Dental whose job is to …").
- `policy.check(fields) -> Verdict(allowed, reasons)` (Appendix A.3): impersonation (short brand names only when the
  business name IS the brand, optionally with support suffixes; or "we are / this is / calling from / on behalf of
  <brand>"), sensitive-data collection (verbs near items, or items near "over the phone/from callers"), deception,
  harm.

**Acceptance:** blocks "IRS Refund Department", "Amazon Support", "ask callers for their credit card number",
"pretend to be a human", "threaten to arrest"; allows "Ups and Downs Yoga", "we accept credit cards",
"Sunrise Dental".

---

## Phase 5: MCP server + Forge tools

**Prompt:** "Implement mcp.py (generic Server) and tools.py (9 Forge tools) per Phase 5."

### mcp.py
`Server(name, instructions, tool_defs, handlers, crash_hint)`. `handle_http(app, raw, trace, server)`:
stateless Streamable HTTP, JSON only. Supports `initialize` (negotiate among `2025-06-18, 2025-03-26, 2024-11-05`;
returns capabilities.tools, serverInfo, instructions), `notifications/*` (→ 202 no body), `ping`, `tools/list`,
`tools/call`, batches. Bad JSON → -32700; invalid request → -32600; unknown method → -32601. Tool results:
`{"content":[{"type":"text","text":json}], "structuredContent": data, "isError": bool}`. `ToolError` → isError with
its message; other exceptions → isError with `crash_hint`, logged. `trace.trace_id = args.call_id` when present.
`FORGE = Server("agent-forge-mcp", ...)`. GET → 405.

### tools.py (9 tools; schemas in code)
Common args `caller_phone` ("Always {{telnyx_end_user_target}}") and `call_id` ("Always {{call_control_id}}").
`_owner(app, trace, args, private)`: normalise phone, reject missing or templated call_id, `builder.callInfo` must be
known; private tools also need `attested` when flag `require_attestation_for_private`.
| Tool | Behaviour |
|---|---|
| list_templates | templates with label, purpose, ask_for |
| list_voices | voices + tones |
| save_draft | build_patch → saveDraft → assess; returns `saved, draft_complete, missing, suggestions, notes`, plus `policy_blocked` and `say` when blocked; "nothing to save" → ToolError |
| get_draft | fields, status + assess |
| start_new_draft | resetDraft; `say` |
| list_my_agents | name, business, "expires in about N hours", features |
| call_me_with_agent (private) | find agent by name (default newest); delay default flag (5..86400); requestTestCall; `app.local_alarm(owner, at)`; `say` "… will call you in about N seconds. Hang up now…" |
| list_messages (private) | 10 newest, marks read |
| delete_agent (private) | deleteAgent; registry setStatus deleting (best effort); local_alarm now |

**Acceptance:** `initialize`, `tools/list` (9 tools), errors as specified; the LLM can't act for another number.

---

## Phase 6: Forge webhooks, generator, build pipeline, Telnyx client

**Prompt:** "Implement telnyx_api.py (client + FakeTelnyx), generator.py, builder.py and webhook.py per Phase 6. Then
add the build tests listed."

### telnyx_api.py
`TelnyxError(status, body, op)` with `is_validation` (400/422) and `is_retryable` (0, 429, 5xx).
`TelnyxClient(api_key, base)`: `create_assistant(payload)→id` (POST /ai/assistants), `update_assistant(id, payload)`
(POST /ai/assistants/{id}), `delete_assistant(id)` (404 = success), `ai_call(connection_id, from, to, assistant_id)`
(POST /texml/ai_calls/{connection_id} `{From, To, AIAssistantId}`), `chat_json(model, messages, schema)` (POST
/ai/openai/chat/completions with `response_format: json_schema strict`), `chat(model, messages, tools)` (tool calling,
returns the assistant message), `get_assistant(id)`, `list_models()`.
`FakeTelnyx(reject=set, fail_chat=False)`: records `calls`; `reject` keys → 422; `fail_next` queue of errors;
`assistants` dict; `chat_json` is schema-aware (`choice` → `route_choices.pop(0)` or 0; `specialty` →
`triage_specialty`; else a persona from the spec); `chat` pops `chat_script` or returns an "offline stub" message.

### generator.py
- `SAFETY` and `OWNER_RULE` text (Appendix A.6). `agent_token(secret, key)` = HMAC-SHA256(secret or "dev-secret",
  key)[:32].
- `polish(app, trace, f, flags)` → `(persona, greeting, source)`: if `llm_polish`, Telnyx Inference with
  POLISH_SCHEMA `{persona, greeting}` cached in KV `cache/polish/{spec_hash}` for 7 days; output cleaned and re-checked
  by policy; any failure → template persona `"You are {agent}, the {tone} AI phone assistant for {business}. Your job
  is to {purpose}. You are speaking with {audience}."` and source `template`.
- `build_instructions(f, persona)`: persona, "What you do:" tasks (template tasks if none), "Facts you may share:"
  (hours first), transfer line if escalation_number, take-message line, Spanish line if `language == es`, SAFETY,
  OWNER_RULE.
- `build_flow(f, greeting)` (Appendix B.3): greet (speak, "Hi, thanks for calling {business}. I'm {agent}, an AI
  assistant. {{owner_line}} {greeting}") → help → message / goodbye (+ transfer when escalation number); timeout edge
  `telnyx_conversation_duration_secs >= 540`.
- `build_tools`: take_message webhook (`{API_URL}/agents/{key}/tools/take-message`, header
  `Authorization: Bearer <agent_token>`, body caller_name, callback_number, message, call_id), hangup, transfer (only
  with escalation number and FORGE_NUMBER).
- `build_payload`: `name "{agent} - {business} [forge {key}]"`, instructions, model, `greeting ""`,
  `voice_settings.voice`, telephony (`time_limit_secs 600, user_idle_timeout_secs 60, default_texml_app_id`),
  `dynamic_variables_webhook_url {API_URL}/agents/{key}/dynamic-variables`, timeout 2000, defaults, tools,
  conversation_flow.
- `LADDER`: `full` → `no_workflow` (greeting = speak text without owner_line) → `no_tools` → `minimal` (name,
  instructions, telephony + greeting).

### builder.py
`build(app, trace, owner, call_id)`: flags (paused → `build_status=paused`) → getDraft → assess (blocked /
incomplete) → `beginBuild` (already_built → ready + duplicate; in_progress → building; quota) → key `ag` + 8 hex (or
the rebuild key) → polish → payload → `create_with_ladder` (walk the ladder on 400/422, retry once on retryable,
update for a rebuild with 404 → create) → KV `agents/{key}` = `{key, owner, assistant_id, name, business, level,
created_at}` with TTL `ttl+1h` → finishBuild → `record_build` (Phase 11) → log `agent.built` with
`feature_level, persona_source, warnings` → `{build_status:"ready", agent_name, agent_key, level, updated,
expires_in_hours, message}`. Any exception → finishBuild failure (**always release the lock**) and `failed`.
`run_jobs(app, jobs, trace)`: test_call → `ai_call(TEXML_APP_ID, FORGE_NUMBER, owner, assistantId)` (missing config
= `PermanentError`); expire → delete assistant + KV key + registry `deleted`; results `{id, ok, permanent?, error?}`;
4xx except 429 are permanent; logs `job.done` / `job.failed` with the job's traceId.

### webhook.py
- `forge_defaults(trace_id)` (Appendix A.8). `forge_variables`: trace id = call_control_id; caller masked; flags and
  `startCall` in parallel (`asyncio.gather(return_exceptions=True)`); fill vars; `welcome_line` (paused message;
  "Welcome back."; "Your draft for X is saved…"; "X is still live."; "Your agents took N new messages."; closing
  question); failures → `degraded=true`, outcome degraded; ALWAYS 200 `{"dynamic_variables": v}`.
- `check_draft` → `{draft_complete "true|false", policy_blocked, missing_fields, suggestions, summary, message}`.
- `build_agent` → builder.build + `built_agent_name`.
- Generated agents: `agent_variables(key)` → `is_owner`, `owner_line "This is your test call, so try anything you
  like."` when the caller is the owner (from KV); `take_message(key)` checks the per-agent token (401), saves into the
  owner's inbox via `builder.addMessage`.
- Router fallbacks on exception: forge_variables → defaults + degraded; agent_variables → `is_owner false`;
  check_draft/build_agent → `build_status failed` with "The draft is saved."

### Routes added
`POST /webhook/dynamic-variables` (optional Ed25519 signature check, `signature.py`), `POST /tools/check-draft` and
`/tools/build-agent` (MCP_TOKEN, `?token=` allowed), `POST /mcp`, `POST /agents/{ag[0-9a-f]{8}}/(dynamic-variables|tools/take-message)`.

**Acceptance (tests):** new builder defaults and wrapper; returning builder hears the saved draft; garbage body still
returns defaults; actor outage degrades, doesn't fail; draft accumulates and completes; invalid values explained;
template injection stripped; blocked draft never builds; build creates a real assistant with a workflow;
**4 concurrent builds → 1 assistant**; rebuild updates the same assistant; quota; ladder degrades on a rejected
workflow; ladder all the way down; transient error retried; failed build releases the lock; inference failure falls
back; polish cached by spec; builds paused by flag.

---

## Phase 7: Alarm jobs, test calls, expiry, generated agent runtime

**Prompt:** "Add /internal/jobs, local alarm emulation and the tests for test calls, expiry, delete, ordering and
retries, per Phase 7."

- `POST /internal/jobs` requires STATE_TOKEN (500 if unset on the edge) → `{"results": run_jobs(...)}`.
- `App.local_alarm(owner, at_ms)`: only with LocalState: an asyncio task sleeps until `at`, then
  `run_due_jobs(owner)` = takeDueJobs → run_jobs → applyJobResults (what the TS alarm does).

**Acceptance:** call-me schedules an outbound call to the owner only; test calls rate-limited; private tools need a
verified caller ID; agent auto-expires; delete works; due jobs run in time order (ai_call before delete); retryable
failure requeued; generated agent knows its owner and takes messages; 8 concurrent messages all land; generated
workflow passes the validator (with and without escalation).

---

## Phase 8: Forge workflow JSON, validator, create_assistant

**Prompt:** "Write api/function/assistants/forge/*.json exactly from Appendix B.1, and scripts/validate_workflow.py and
scripts/create_assistant.py per Phase 8."

- `assistant.json` (Appendix B.1): name, model `${ASSISTANT_MODEL}`, instructions, empty greeting, variables webhook
  `${API_URL}/webhook/dynamic-variables` (3000 ms), `dynamic_variables` = every key of `forge_defaults`, telephony
  (`default_texml_app_id ${TEXML_APP_ID}`, 1200 s, idle 60), tools: webhook `check_draft` and `build_agent` (header
  `Bearer ${MCP_TOKEN}`, body caller_phone + call_id), `handoff` (`ai_assistants: [{id: ${CLINIC_ASSISTANT_ID}, name}]`,
  `voice_mode: distinct`), hangup; `"conversation_flow": "@conversation_flow.json"`.
- `conversation_flow.json`: `{start_node_id, nodes[], edges[]}`; node `{id, name, type speak|prompt, message |
  instructions + instructions_mode append}`; edge `{id, start_node_id, target:{type:"node", node_id}, condition}` with
  condition `{type:"default"}`, `{type:"llm", prompt}` or `{type:"expression", expression:{type:"comparison", op,
  left:{type:"variable", name}, right:{type:"string_literal"|"number_literal", value}}}`.
- `node_tools.json`: node → tool names (Appendix B.1).
- `validate_workflow.py`: `validate(flow, defaults)` checks duplicate ids, the start node, unknown sources/targets,
  default edges only from speak/tool nodes, expression variables have defaults (or are system vars), each speak node
  has exactly ONE default edge and no other edges, every `{{var}}` in messages and instructions has a default,
  reachability. `check(name)` also checks node_tools refer to real nodes and tools. CLI: all assistants or a named one.
  System vars: telnyx_current_time, telnyx_conversation_channel, telnyx_agent_target, telnyx_end_user_target,
  telnyx_shaken_stir_attestation, call_control_id, telnyx_conversation_duration_secs, telnyx_current_date,
  telnyx_current_weekday, telnyx_last_tool_status_code.
- `create_assistant.py forge|clinic [--dry-run] [--update ID]`: substitute `${VARS}` from the environment, inline the
  flow, **drop** the handoff tool if CLINIC_ASSISTANT_ID is empty and the transfer tool if the front desk number is
  empty (warn), POST to the Assistants API.

**Acceptance:** `python3 scripts/validate_workflow.py` → `forge: 14 nodes, 30 edges, 5 speak nodes, 10
variable-comparison edges, 15 LLM edges -> OK`.

---

## Phase 9: Observability + ops scripts

**Prompt:** "Add the /ops routes, deep health and the scripts (smoke, ops_watch, set_flag) per Phase 9."

- `GET /ops/health` (no auth): KV put + get probe, actor `ops.snapshot`, telnyx mode, storage probe (reported, never
  fails health); 503 if a check fails or (≥5 requests and error rate > 20%).
- Admin (ADMIN_TOKEN): `GET /ops/metrics?window=1..120`, `GET/POST /ops/flags`, `GET /ops/builder/{phone}` (path
  masked in logs).
- `scripts/smoke.py --base --mcp-token --admin-token --phone --test-call --wait`: walks a real call (variables → MCP
  initialize/list → save_draft ×3 → check → build → agent variables → take_message bad token 401 → list agents →
  [test call + wait + no pending job] → clinic variables → triage emergency + dermatology → availability → book →
  cancel → health → metrics), printing PASS/FAIL with ms; exit code = failures.
- `scripts/ops_watch.py`: every 5 s prints health, requests, error rate, p50/p95 per route, recent errors; ALERT lines
  + bell on unhealthy, error rate > 20%, p95 > 1200 ms, any build_agent error.
- `scripts/set_flag.py --base --admin-token k=v ...`.
- `docs/OBSERVABILITY.md`: what's instrumented, "how we'd know within a minute" table, what to look at first, the
  debugging trail (write your own real bugs here).

**Acceptance:** metrics, health and log line test; internal jobs require a token.

---

## Phase 10: Clara, the clinic assistant

**Prompt:** "Implement clinic_catalog.py with the exact data in Appendix C, the clinic actors (TS + Python mirror),
clinic.py and the clinic workflow per Phase 10. Then write tests/test_clinic.py."

### 10.1 Actors (TS `src/actors/clinic.ts` + Python mirrors)
- **ClinicSchedule** (per clinician id), storage `bookings{slot: booking}`; slots are clinic-local `YYYY-MM-DDTHH:MM`
  strings (they sort lexicographically).
  `taken(from, to)` → sorted slots; `book(slot, booking{id, patient,...}, nowSlot, now)` → `past` if slot < nowSlot,
  `taken`, `duplicate` (+existing) if the patient already has a future booking with this clinician, else stored with
  `status booked, createdAt`; `cancel(id, patient|null, now)` → `not_found | forbidden`, else removes and returns it;
  `list(from, to)`.
- **ClinicPatient** (per phone), storage `patient {phone, name, calls, attested (last 10), appointments[] (max 20),
  lastTriage}`. `startCall(phone, callId, attested, nowSlot, now)` → `{returning, calls, name, upcoming}`;
  `callInfo`; `setName`; `recordTriage`; `addAppointment(appt, maxUpcoming, nowSlot)` → `limit` if upcoming >= max,
  else prepend with `status booked` (and set name); `cancelAppointment(id, now)`; `list(nowSlot)` →
  `{upcoming, all, name}`; `snapshot`.
- **ClinicDesk** ("global"), storage `desk {callbacks[], activity[]}` (max 100 each): `addCallback(rec, now)`
  (id, at, done false), `resolveCallback(id, now)`, `log(event, now)`, `snapshot`.

### 10.2 clinic.py
- `ClinicFlags` (Appendix C.5) + `FlagStore(kv, "flags/clinic", ClinicFlags)`.
- Time: `tz(app)` from CLINIC_UTC_OFFSET; `local_now(app)` (naive, clinic-local); `slot_str`, `parse_slot`;
  `spoken_slot(slot, specialist_id)` → "Tuesday, November 10 at 9:30 AM with Dr. Maya Okafor" (on the hour → "10 AM");
  `desk_open(t)` from desk hours.
- **Triage** `triage_rules(symptoms, age, for_child)`: lower-case; phrase match with letter boundaries
  `(?<![a-z])phrase(?![a-z])`; urgency = crisis > emergency > urgent > routine; specialty = the one with the most
  keyword hits (excluding pediatrics); **pediatrics overrides** when for_child, age < 18 or a child keyword matched;
  returns `{urgency, specialty|None, matched_terms, red_flags, source}`.
  `triage(app, trace, …)`: if no specialty, urgency routine/urgent and flag `llm_triage` → Inference picks ONE from the
  closed enum (json_schema), cached in KV `cache/triage/{sha256(normalised text)[:24]}` for 30 days
  (`source inference|inference_cache`); else `family_medicine` (`default`). Adds `specialty_label`, `say` per urgency
  (emergency "What you describe could be an emergency."; crisis "Thank you for telling me. You deserve support right
  now."; urgent "Based on what you told me, it's best to be seen within a day or two, by {who}. I'll look for the
  earliest time."; routine "Based on what you told me, the right person to see is {who}."), `disclaimer` "This is not
  a diagnosis, just who to see and how soon."
- **Slots** `generate_slots(app, specialist, flags)`: for today … +window days, on the clinician's weekdays, from start
  in steps of slot_min while `t + slot_min <= end`, skipping starts in lunch 12:00–13:00 and anything before
  `now + min_notice`.
- **Availability** `availability(app, trace, specialty, specialist_id, preferred_day, part_of_day, urgency, language,
  limit=3)`: resolve clinicians (by id or name substring, else by specialty, prefer language); day filter (any/earliest
  → none; today; tomorrow; next week = next Monday +7; this week; ISO date; weekday names or 3-letter prefixes, "next"
  on the same weekday = +7); urgent → only the next 3 days; part of day (morning < 12, afternoon 12–17, evening ≥ 16);
  remove `taken` slots (one actor call per clinician, span `actor schedule.taken <id>`); sort; **spread across days**
  (first pick one per day, then fill). Each option `{slot_id "<specialistId>@<slot>", specialist_id, specialist, slot,
  spoken}`; `say` "The earliest times are: A; or B. Which works best?" or a no-openings sentence.
- **Book** `book(app, trace, phone, slot_id, name, reason, urgency, source, call_id)` → `booking_status`:
  `paused` (flag) · `invalid` (bad slot id, or not in generate_slots) · `need_name` · schedule.book →
  `slot_taken` + 2 alternatives for that clinician · `duplicate` · then patient.addAppointment → `limit` with
  **compensation** (schedule.cancel) · `confirmed` with `appointment_id "ap"+8 hex`, `booking_line "You're booked with
  {name} on {spoken}."`; desk log `booked`; archive `clinic/appointments/{id}.json` (masked patient); log `clinic.booked`.
- **Cancel** `cancel(app, trace, phone, appointment_id|which)`: from upcoming; by id, by "which" (clinician, date words,
  specialty) or the only one; schedule.cancel (forbidden → ToolError) + patient.cancelAppointment + desk log.
- **MCP** `SERVER = Server("northside-clinic-mcp", …)` with 8 tools: get_clinic_info, list_specialists,
  triage_symptoms (binds the call, records triage, desk-logs emergency/crisis), check_availability, book_appointment,
  list_my_appointments (private), cancel_appointment (private), request_callback (desk callback; "today" or "on the
  next business day"). `_patient()` = the same binding check as Forge's `_owner()`, using the patient actor.
- **Webhooks**: `clinic_defaults(trace_id)` (Appendix C.6); `variables()` (patient.startCall; `clinic_welcome_line`
  = "Welcome back, {first name}." + "I see your next appointment is …" + the question; degraded on failure);
  `triage_webhook` → flat `{triage_urgency, matched_specialty, specialty_label, say, disclaimer, message}`;
  `book_webhook` → book result + `booking_line`.
- `overview(app, trace, days=7)` for the console: per clinician the free slots and bookings (masked) in range, flags,
  desk snapshot (masked).
- Routes: `POST /clinic/webhook/dynamic-variables` (optional signature), `POST /clinic/tools/triage|book`
  (MCP_TOKEN), `POST /clinic/mcp`. Router fallbacks: variables → defaults + degraded; triage → `triage_urgency none`
  + offer a call back; book → `booking_status failed` + offer a call back.

### 10.3 Clara workflow (Appendix B.2) and assistant.json
Webhook tools `triage_symptoms` (`/clinic/tools/triage`) and `book_appointment` (`/clinic/tools/book`), transfer to the
front desk (`${CLINIC_FRONT_DESK_NUMBER}`, from `${FORGE_NUMBER}`), hangup. Variables webhook
`/clinic/webhook/dynamic-variables` (2500 ms). Time limit 900 s, idle 45 s.

**Acceptance:** `validate_workflow.py` → `clinic: 13 nodes, 30 edges, 6 speak nodes, 9 variable-comparison edges,
15 LLM edges -> OK`; `test_clinic.py` (Phase 14 list); TS tests for the clinic actors.

---

## Phase 11: Registry, Cloud Storage archives, shared actor

**Prompt:** "Add AgentRegistry (TS + Python), storage.py (SigV4 S3 + memory) and record_build in builder.py per Phase
11."

- **AgentRegistry** ("global"), storage `agents[]` (max 200, newest first): `upsert(rec, now)` merges with the
  previous record (keeps createdAt, `builds` +1, `updatedAt`), `setStatus(key, status, now)`, `list(limit)`, `get(key)`.
- **storage.py**: `S3Storage(endpoint, bucket, region, access_key, secret_key)`, path-style; AWS Signature V4 on the
  stdlib (canonical request with host, x-amz-content-sha256, x-amz-date; scope `{day}/{region}/s3/aws4_request`);
  `put_json` (on 404 NoSuchBucket: PUT the bucket and retry), `get_json` (404 → None), `list(prefix, limit)`
  (ListObjectsV2 XML). `MemoryStorage` (same interface, `fail` flag). `make_storage(settings)`.
  `redact_payload(payload)` replaces every webhook Authorization header value with `Bearer <redacted>`.
  Telnyx Cloud Storage: endpoint like `https://us-central-1.telnyxcloudstorage.com`; access key = Telnyx API key
  (verify against your account).
- **builder.record_build(...)** after finishBuild, best-effort: registry upsert `{key, owner, name, business,
  assistantId, level, status active, expiresAt, source console|phone (web_ call id → console), lastTraceId,
  lastVersion, archive}`; storage `forge/builds/{key}/v{n}.json` and `latest.json` = `{agent_key, version, built_at,
  trace_id, owner (masked), assistant_id, rebuild, feature_level, ladder_warnings, persona_source, persona, greeting,
  input (fields), payload (redacted), spans}`. `run_jobs` expire → registry `deleted`; delete_agent → `deleting`.
- **Shared actor**: BuilderProfile.alarm records into OpsMetrics via the OPS binding (Phase 3.3).

**Acceptance:** SigV4 headers for AWS's example credentials (credential scope string and signed headers);
redaction doesn't mutate the input; a storage outage never fails a build; registry tracks delete; TS test "alarm
writes into the same OpsMetrics('global')" (`by_route.alarm.count == 1`).

---

## Phase 12: Workflow simulator (console chat)

**Prompt:** "Implement simulator.py per Phase 12: it runs an assistant's real workflow JSON over text, with the
scripted-model tests from Phase 14."

- `AssistantDef(id, name, instructions, defaults, flow, node_tools|None, tools{name: ToolSpec}, init, meta)`;
  `ToolSpec(name, kind mcp|webhook|hangup|transfer|handoff, description, parameters, run, var_map)`.
- `forge_def()`: MCP tools from `tools.TOOL_DEFS/HANDLERS`; webhook tools check_draft (`draft_complete`) and build_agent
  (`build_status`, `built_agent_name`) call the webhook functions in-process; builtins hangup, handoff; `init` = the
  Forge variables webhook. `clinic_def()`: clinic MCP tools **except** triage_symptoms and book_appointment, which are
  webhook tools (var maps `triage_urgency, matched_specialty, specialty_label` and `booking_status, booking_line`);
  transfer (simulated); hangup. `agent_def(key)`: payload from the storage archive (else registry → Telnyx
  get_assistant); a no-workflow payload becomes greet (speak) → main (prompt); take_message calls the webhook
  in-process with the correct per-agent token; node_tools None = every tool everywhere.
- Parameters shown to the LLM drop `caller_phone`/`call_id`; the simulator injects them.
- `_init_body(phone, call_id)` = a Telnyx-style payload with `telnyx_end_user_target_verified: true` (console requires
  the admin token) and channel `web_chat`.
- **Engine**: `start(assistant, phone)` → call id `web_` + 16 hex; vars = defaults + init result (event `variables`);
  `_enter` says speak nodes verbatim (substituting `{{vars}}` incl. system vars, e.g. duration from start) and follows
  their default edge. `send(s, text)`: append the user message; `_advance(use_llm=True)`: expression edges first
  (numeric compare when both sides parse), else ONE LLM routing call (`chat_json` with schema `{choice: integer
  0..n}`, the last 8 turns, "pick the ONE condition clearly true … 0 if none"), at most one LLM hop per turn; then
  `_generate` in the current prompt node: system = assistant instructions + node instructions (append; replace mode =
  node only) + a console note; tools = node-scoped; loop up to 6 rounds: call `llm.chat`; record the content; run each
  tool call (not allowed → error result "isn't available in this step"); apply var_map; hangup ends the call; handoff
  switches to Clara (push to `chain`, re-init variables, enter her start node); after tools, `_advance(use_llm=False)`
  so variable edges fire right away, then keep generating in the new node.
- Events: `user, assistant, speak (node, node_name), transition (edge, frm, to, condition, label), tool (name, kind,
  args, result|error, vars_set, ms), variables, handoff, end, system`.
- Session in KV `session/chat/{12 hex}` TTL 7200, history trimmed to 40 at a user-message boundary, events capped at
  300. `public(s)` masks the phone.

---

## Phase 13: Console API + UI, settings overrides

**Prompt:** "Implement ui_api.py and static/console.html per Phase 13. Add App.reconfigure and Function.overrides.
Then the console tests."

### App changes
`App(settings, kv=, state=, telnyx=, storage=, llm=)`: pinned clients win; `_wire(settings, previous)` keeps the
previous **in-memory** KV/state/storage when the new settings are still local (so changing a token doesn't wipe
local data); `llm` = a real TelnyxClient whenever a key exists (independent of FORGE_DRY_RUN), else the fake;
`flags` and `clinic_flags`. `reconfigure(overrides)` rebuilds from `load_settings(overrides)`.
`Function.overrides: dict` (memory only).

### Routes (all `/ui/api/*` need ADMIN_TOKEN when set; responses mask phone numbers)
`GET /ui` serves `static/console.html` with a strict CSP (`default-src 'self'; script-src 'unsafe-inline'; style-src
'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'`), `x-frame-options DENY`; `GET /favicon.ico` → 204.
| Route | Does |
|---|---|
| GET overview | mode (telnyx api live/dry, llm, kv, actors, storage, texml, clinic handoff), active overrides, both flag sets, endpoints |
| GET/POST settings | list OVERRIDABLE with masked value + source; POST `{values:{NAME: value|null}}` (unknown → 400; logs names only) → reconfigure |
| GET/POST flags/{forge,clinic} | read / patch |
| GET flow/{forge,clinic,agKEY} | flow, node_tools, defaults, tools, instructions |
| POST chat/start, chat/send; GET chat/{id} | simulator; responses `{session (+node_tools, draft for forge, patient for clinic), events}`; ended → 409 |
| GET agents; GET agents/{key}; POST agents/{key}/test-call, /delete | registry list (owner masked); detail = registry + archive + versions + actor agent record + messages + jobs + current draft |
| POST forge/build | form build: variables webhook → (reset) → save_draft → check_draft → build_agent; returns steps and spans |
| GET owner/{phone} | BuilderProfile snapshot (masked, no attested) |
| GET clinic; POST clinic/triage, availability, book, cancel (by phone or by specialist_id + appointment_id), callback-done; GET clinic/patient/{phone} | clinic operations |

### console.html (single file, no external resources, light + dark via CSS variables)
Header with tabs (Forge, Agents, Clinic, Ops, Settings; `#hash` routing) and mode chips. A reusable **chat component**
(caller ID input, start, hang up, transcript: user/assistant bubbles, speak bubbles marked "speak node · name",
transitions with condition badges, collapsible tool calls with `var=value` chips, variables, hand-off banner).
A **flow diagram renderer** in SVG: BFS levels from the start node, rows centred, speak nodes amber, prompt nodes blue,
cubic edges (forward: bottom → top; back: curve on the right), default grey, expression green dashed with labels
placed along the curve, llm purple; current node outlined, visited nodes bold, last 3 edges thick; click a node →
instructions/message, tools in this node, outgoing edges.
Tabs: Forge (chat + flow + variables with changed values highlighted + draft panel + build-from-form);
Agents (table + detail sub-tabs Overview, Input, Generated, Flow, Build trace span bars, Messages, Chat, Raw; test
call, delete); Clinic (chat + flow + variables + patient panel; a 7-day grid clinicians × days with booked/free counts
using UTC date math; day slots; booking table with cancel; triage → availability → book form; callbacks; activity);
Ops (health chips, stat tiles, per-route table, recent errors, flag forms; refresh every 5 s while visible);
Settings (admin token in localStorage; overrides table with source badges; "remember in this browser" re-applies
overrides when the server reports none).

---

## Phase 14: API docs, run.sh, deploy, docs, final tests

**Prompt:** "Add openapi.py (/openapi.json, /docs), run.sh, deploy.sh, .env.example and update README per Phase 14.
Make ./run.sh test pass."

- `openapi.py`: `spec(base_url)` OpenAPI 3.1 for every route (tags Forge, Clinic, Generated agents, Console, Ops,
  Internal; bearer security; request/response examples; MCP tool lists generated from the registries).
  `DOCS_HTML`: a self-contained page that renders `/openapi.json` grouped by tag.
- `run.sh`: modes `full` (default: actor host on STATE_PORT 8787 + API on API_PORT 8080 with STATE_URL set),
  `--memory`, `test` (validator, pytest, npm test + typecheck, then the contract test against a fresh actor host),
  `smoke`. Installs uvicorn if missing; npm install if needed; waits for /health; prints the URL banner
  (console, docs, openapi, health, metrics, both MCPs, both variables webhooks, actor host, admin token, mode, logs);
  Ctrl+C stops everything.
- `api/app.py`: `load_dotenv(repo/.env)` then `app = new().handle`.
- `api/func.toml` env vars incl. storage and clinic settings; secrets via `telnyx-edge secrets add`.
- `scripts/deploy.sh`: prints the first-time steps (KV namespace, secrets, new-func ×2, ship state then API,
  API_URL secret for alarms, TeXML app + numbers, `create_assistant.py clinic` then forge with CLINIC_ASSISTANT_ID,
  storage, console); `deploy.sh ship` = validator + tests + ship both.
- `assistant/README.md`: Portal steps for both assistants (numbers, variables webhooks, webhook tools and
  **response → variable mappings**, MCP servers, per-node tool scoping, edge order, path tests).
- README: quick start, URL table, console, architecture, primitive table, both workflows, MCP, safety, deploy,
  observability, task checklist.

### Final test list (all must pass; 91 Python + 16 TS)
`test_flows.py` (Phases 5–9): new_builder_gets_defaults_and_wrapper, returning_builder_hears_saved_draft,
garbage_body_still_returns_defaults, actor_outage_degrades_not_fails, draft_accumulates_and_completes,
invalid_values_are_explained, template_injection_is_stripped, llm_cannot_act_for_another_number, policy_blocks[…],
policy_allows_normal_businesses[…], blocked_draft_never_builds, build_creates_real_assistant_with_workflow,
double_build_creates_one_assistant, rebuild_updates_same_assistant, quota_enforced,
ladder_degrades_when_api_rejects_workflow, ladder_all_the_way_down, transient_api_error_is_retried,
failed_build_releases_lock, inference_failure_falls_back_to_template, polish_is_cached_by_spec, builds_paused_by_flag,
call_me_schedules_outbound_call_to_owner_only, test_calls_rate_limited, private_tools_need_verified_caller_id,
agent_auto_expires, delete_agent, due_jobs_run_in_time_order, retryable_job_failure_is_requeued,
generated_agent_knows_its_owner_and_takes_messages, concurrent_messages_all_land, mcp_initialize_and_list, mcp_errors,
internal_jobs_requires_token, metrics_health_and_log_line, generated_workflow_passes_validator[escalation on/off].

`test_clinic.py`: triage_urgency (7 cases: chest pain, stroke signs, can't breathe → emergency; "kill myself" →
crisis; broken wrist, baby fever → urgent; check-up → routine), triage_specialty (7 cases: knee → orthopedics, itchy
rash → dermatology, heartburn → gastroenterology, migraines + tingling → neurology, son's earache → pediatrics,
palpitations → cardiology, anxious → behavioral_health), triage_never_diagnoses,
unmatched_symptoms_use_inference_once_then_cache, variables_webhook_new_and_returning_patient,
emergency_sets_variable_that_routes_to_911, triage_rejects_unbound_call, availability_respects_schedule_and_notice,
book_list_cancel_roundtrip, concurrent_booking_same_slot_one_wins, taken_slot_offers_alternatives,
duplicate_and_limit_rules (incl. compensation), bookings_paused_by_kv_flag, invalid_slot_rejected,
callback_request_lands_at_desk, booking_confirmation_archived, clinic_mcp_lists_8_tools, clinic_workflow_valid.

`test_console.py` (scripted FakeTelnyx: `route_choices`, `chat_script`): forge_chat_builds_an_agent_end_to_end
(router→discover→design→review via the `draft_complete` edge→build→deliver via `build_status`, then the registry +
archive with redacted tokens and spans), tool_scoping_is_enforced_per_node, forge_hands_off_to_clara,
clinic_chat_emergency_goes_to_911_speak_node, clinic_chat_books, chat_with_generated_agent_takes_message,
ended_session_refuses_input, console_requires_admin_token, settings_override_on_top_of_env (source changes, masking,
new MCP token enforced at once, ADMIN_TOKEN refused), reconfigure_keeps_local_state, form_build_steps_and_flags,
flow_endpoint_and_docs, sigv4_matches_aws_reference_vector, redact_payload, storage_outage_never_fails_a_build,
registry_tracks_delete (local only).

TS `actors.test.ts`: the 10 from Phase 3 + clinic schedule concurrency, schedule duplicate/past/cancel ownership,
patient binding/limit/cancel, registry + desk concurrent writes, shared OpsMetrics from the alarm, gateway exposes the
clinic and registry actors (and rejects non-whitelisted methods).

**Done when:** `./run.sh test` prints `all green`, `./run.sh smoke` passes, and `/ui` + `/docs` load.

---

# Appendix A: Forge data

### A.1 Templates
| id | label | purpose | tasks | ask_for |
|---|---|---|---|---|
| receptionist | Front-desk receptionist | answer calls, share business information and take messages | answer questions about hours, location and services; take a message with name and callback number; transfer urgent calls to the owner if a transfer number is set | opening hours, address, main services, prices or price ranges |
| appointments | Appointment request line | collect appointment requests and answer questions about visits | collect the caller's name, callback number, preferred day and time and reason for visit; explain what to bring and how to prepare; answer questions about cancellations | opening hours, cancellation policy, what to bring |
| faq | Information hotline | answer frequently asked questions | answer frequently asked questions; take a message when the answer is unknown | the five questions callers ask most, with answers |
| restaurant | Restaurant line | answer menu and hours questions and take reservation requests | answer questions about the menu, dietary options and hours; take reservation requests with name, party size, date and time and callback number | opening hours, menu highlights, dietary options, largest party size |
| events | Event information line | tell callers about an event and take questions | explain the date, time, venue and schedule; answer questions about tickets and parking | date and time, venue address, ticket prices, parking |
| lead_intake | New customer intake | learn what a prospective customer needs and pass the details to the team | ask what the caller needs, their timeline and budget range; collect name and callback number; promise a call back from the team | services offered, service area, typical response time |

### A.2 Voices, tones, fields
Voices: warm `Telnyx.KokoroTTS.af_heart` (warm and friendly, female) · bright `Telnyx.KokoroTTS.af_bella` (bright and
upbeat, female) · calm `Telnyx.KokoroTTS.am_michael` (calm and steady, male) · professional `Telnyx.KokoroTTS.am_adam`
(crisp and professional, male) · british `Telnyx.KokoroTTS.bf_emma` (British English, female).
Tones: friendly, professional, calm, upbeat, formal.
Fields (required, max length): business_name (yes, 80), agent_name (yes, 40), purpose (yes, 200), template (20),
tone (20), voice (20), greeting (200), audience (120), hours (200), tasks (list, 160 each), facts (list, 240 each),
escalation_number (20, E.164), language (10, en|es).

### A.3 Policy lists
Impersonation: irs, internal revenue, social security administration, medicare, medicaid, fbi, police department,
sheriff, uscis, dmv, amazon, apple support, microsoft support, google support, paypal, venmo, zelle, wells fargo, chase
bank, bank of america, citibank, american express, mastercard, fedex, ups, usps, dhl, netflix, comcast, verizon, at&t,
t-mobile, geek squad, norton, mcafee, coinbase, binance. Names of ≤ 4 characters match only as the whole business name
(optionally "the …" and suffixes support, service(s), security, customer care/service, delivery, department, office,
agency, help desk, billing, refund(s)).
Sensitive items: credit card (numbers), debit card (numbers), card numbers, cvv, cvc, payment(s), bank login/password/
account (numbers), account numbers, routing numbers, social security (numbers), ssn(s), password(s), pin(s), one-time
code(s)/password(s), otp(s), verification codes, gift cards, crypto wallets, seed phrases. Collection verbs: ask,
collect, take, get, request, record, capture, confirm, verify, read back, note, gather, process (+s/ed/ing) within 50
chars before an item; or an item followed within 30 chars by "over the phone" / "from (the) caller(s)".
Deception: "pretend to be/you are (a) human/real person", "deny/never say/don't say/do not say (that) you are/you're/
being (an) ai/bot/robot", "say you are (a) human/real person".
Harm: threaten, arrest, lawsuit against, debt collect, act now or, final notice, diagnos, prescribe, instead of
(calling) 911, emergency (medical) advice, (vote|voting) for, robocall, cold-call, call (a|this|my) list.
Reasons: "it would present itself as {Name}, a real organisation you don't own" · "it would ask callers for payment,
banking, password or ID numbers" · "it would hide that it is an AI" · "it would threaten callers, give medical or
emergency advice, persuade voters, or cold-call people".

### A.4 Validator system vars
See Phase 8.

### A.5 Forge flags (`flags/global`)
builds_enabled true · max_agents_per_caller 3 (1–20) · agent_ttl_hours 24 (1–720) · max_test_calls_per_hour 3 (0–20)
· test_call_delay_secs 20 (5–3600) · llm_polish true · require_attestation_for_private true · maintenance_message
"Agent building is paused for maintenance right now. Your draft is saved, so please call back in a little while."

### A.6 Generated-agent text
SAFETY: "Rules you must always follow. You are an AI assistant: if anyone asks, say so, and never claim to be a person.
Never ask for or accept payment card numbers, bank details, passwords, PINs, one-time codes or social security numbers;
if a caller starts to share one, stop them politely. Do not give medical, legal or financial advice. If someone
describes an emergency, tell them to hang up and dial 911. Only share the facts listed above; if you don't know
something, say so and offer to take a message. Never promise prices, bookings or availability that aren't in the
facts. Keep every reply to one or two short sentences, ask one question at a time, and never read out lists, symbols or
web links."
OWNER_RULE: "If {{is_owner}} is true, the caller is the person who built you and is testing you: help them try
anything, and before saying goodbye ask what they would change, then save their answer with take_message as feedback."
Polish system prompt: "You write the persona paragraph for a phone AI assistant. Return JSON with 'persona' (2-3
sentences, second person 'You are ...', describe role, tone and audience only; no facts, prices, promises or rules)
and 'greeting' (one short spoken sentence that asks how to help; do not greet with the business name, it is said
before)."

### A.7 Test fixture `SUNRISE`
`{"business_name": "Sunrise Dental", "agent_name": "Sam", "template": "receptionist", "tone": "friendly", "voice":
"warm", "hours": "Monday to Friday, 8 AM to 5 PM", "facts": ["We are at 12 Oak Street", "New patient cleanings are 99
dollars"], "escalation_number": "+15550109999"}`

### A.8 Forge variable defaults
welcome_line "Tell me about the business or project you want an agent for, and I'll build it while we talk.",
caller_phone "", caller_id_verified "false", returning_builder "false", call_count "1", has_draft "false",
draft_business "", active_agents "0", last_agent_name "", unread_messages "0", max_agents "3", quota_remaining "3",
builds_enabled "true", maintenance_message "", agent_ttl_hours "24", draft_complete "false", build_status "none",
built_agent_name "", degraded "false", trace_id.

---

# Appendix B: Workflows

### B.1 Forge (14 nodes, 30 edges)
Forge instructions: "You are Forge, a voice assistant that builds voice AI agents for callers on the Telnyx platform.
You interview the caller about their business, save their answers, build a real agent, and arrange a test call. This
is a phone call: use short sentences, ask one question at a time, never read lists or symbols, and confirm what you
heard. Save every detail with save_draft as soon as you hear it. Never promise features you don't have. You only build
agents for the caller's own business or project; you never build agents that impersonate real organisations, collect
payment or ID numbers, hide that they are an AI, or pressure people. The current time is {{telnyx_current_time}}."

Nodes (prompt nodes use `instructions_mode: append`; most end with "Always pass caller_phone={{telnyx_end_user_target}}
and call_id={{call_control_id}} to tools."):
| id | type | name | text (speak = verbatim) / instructions gist | tools |
|---|---|---|---|---|
| welcome | speak | Welcome & disclosure | "Welcome to Agent Forge, powered by Telnyx. I'm Forge, an AI that builds voice AI agents while you wait. This call may be recorded. Every agent I build tells its callers that it's an AI, and it is deleted automatically after {{agent_ttl_hours}} hours. {{welcome_line}}" | |
| router | prompt | What would you like to do? | find out: build new / continue draft ({{has_draft}}, {{draft_business}}) / test / messages ({{unread_messages}} unread) / list or delete; explain how it works in two sentences if asked; no tools | none |
| discover | prompt | Discover the business | business name, then what the agent does; offer a matching template; save_draft after each answer (business_name, purpose, template); read `say` if policy_blocked | list_templates, save_draft |
| design | prompt | Design the agent | one question at a time, save each: agent name, tone, voice (list_voices, offer 2–3), 2–4 facts, special tasks, optional transfer number; when done call check_draft and follow its message | save_draft, get_draft, list_voices, check_draft |
| review | prompt | Review & confirm | read the summary back, ask "shall I build it?"; changes → save_draft + check_draft | get_draft, save_draft, check_draft |
| build | prompt | Build | say "Great, building it now. This takes about ten seconds." then call build_agent ONCE | build_agent |
| deliver | prompt | Deliver & test | {{built_agent_name}} is live; offer a test (call_me_with_agent, read `say`, tell them to hang up); messages; deleted after {{agent_ttl_hours}} hours | call_me_with_agent, list_my_agents |
| manage | prompt | Manage agents | list_my_agents, call_me_with_agent, list_messages (read callback digit by digit), delete_agent after confirming, start_new_draft | list_my_agents, call_me_with_agent, list_messages, delete_agent, start_new_draft |
| clinic_handoff | prompt | Hand off to the clinic (Clara) | one sentence: you're Forge, an agent builder; Clara at Northside Family Clinic can book a visit; if they agree use the handoff tool; never give medical advice | handoff |
| policy_decline | speak | Policy decline | "I can't build that one. I don't build agents that pretend to be a real company or a government office, ask callers for payment, banking, password or ID numbers, hide that they're an AI, or pressure people. I'm happy to build something else." | |
| quota_full | speak | Quota reached | "You already have {{max_agents}} live agents, which is the limit on this line. I can delete one for you, or you can wait for one to expire." | |
| paused | speak | Builds paused | "{{maintenance_message}}" | |
| build_failed | speak | Build failed | "Something went wrong while building. Your draft is saved, so you can try again in a minute or two." | |
| wrap | prompt | Wrap up | summarise in one sentence, ask if anything else; else goodbye + hangup; remind them to hang up if a test call is coming | hangup |

Edges, **in this order** (expression edges before LLM edges on the same node):
| id | from → to | condition |
|---|---|---|
| e_welcome | welcome → router | default |
| e_router_paused | router → paused | builds_enabled == "false" |
| e_router_build | router → discover | llm: The caller wants to build a new agent or continue their saved draft. |
| e_router_manage | router → manage | llm: The caller wants to test an agent, hear messages, or list or delete their agents. |
| e_router_clinic | router → clinic_handoff | llm: The caller is a patient who wants to book, change or ask about a medical appointment, or describes their own symptoms, rather than building an agent. |
| e_router_wrap | router → wrap | llm: The caller has nothing else they want to do. |
| e_discover_quota | discover → quota_full | quota_remaining == "0" |
| e_discover_policy | discover → policy_decline | llm: The caller wants an agent that impersonates a real company or government office, collects payment, banking, password or ID numbers, hides that it is an AI, or threatens or pressures people. |
| e_discover_design | discover → design | llm: The business name and what the agent should do are both known and saved. |
| e_design_review | design → review | draft_complete == "true" |
| e_design_timeout | design → review | telnyx_conversation_duration_secs >= 840 (number) |
| e_design_policy | design → policy_decline | llm: The caller asks for an agent that impersonates a real organisation, collects payment or ID numbers, hides that it is an AI, or pressures people. |
| e_review_build | review → build | llm: The caller clearly confirms they want the agent built now. |
| e_review_design | review → design | llm: The caller wants to change or add something substantial. |
| e_build_ready / _blocked / _quota / _incomplete / _paused / _failed | build → deliver / policy_decline / quota_full / design / paused / build_failed | build_status == "ready" / "blocked" / "quota" / "incomplete" / "paused" / "failed" |
| e_deliver_wrap | deliver → wrap | llm: A test call has been scheduled, or the caller doesn't want one. |
| e_deliver_design | deliver → design | llm: The caller wants to change something about the agent. |
| e_manage_build | manage → discover | llm: The caller wants to build a new agent. |
| e_manage_wrap | manage → wrap | llm: The caller has finished managing their agents. |
| e_policy_router | policy_decline → router | default |
| e_quota_manage | quota_full → manage | default |
| e_paused_wrap | paused → wrap | default |
| e_failed_wrap | build_failed → wrap | default |
| e_wrap_router | wrap → router | llm: The caller has another request. |
| e_clinic_router | clinic_handoff → router | llm: The caller doesn't want to be transferred, or actually wants to build an agent. |

### B.2 Clara (13 nodes, 30 edges)
Clara instructions: "You are Clara, the AI phone assistant for Northside Family Clinic, a multi-specialty clinic at 200
Harbor Avenue, Suite 3. You help callers find the right clinician for what they describe, check availability and book,
change or cancel appointments. This is a phone call: short sentences, one question at a time, no lists or symbols, read
times naturally. You are an AI and you say so if asked. You never diagnose, never name a likely condition, never
suggest treatments or medication doses, and never ask for insurance numbers, card numbers or social security numbers.
Triage tools decide urgency; follow their result. If anyone describes an emergency, tell them to hang up and dial 911.
The current time is {{telnyx_current_time}}."

| id | type | text / instructions gist | tools |
|---|---|---|---|
| welcome | speak | "Thank you for calling Northside Family Clinic. I'm Clara, the clinic's AI assistant. I can help you find the right clinician and book a visit, but I can't give medical advice. If this is an emergency, please hang up and dial 911. This call may be recorded. {{clinic_welcome_line}}" | |
| intent | prompt | new appointment / change or cancel ({{upcoming_count}} upcoming; next: {{next_appointment}}) / clinic question / a person; one short question; no tools | none |
| symptoms | prompt | ask what's going on and for how long, for them or a child (age); never suggest a condition; call triage_symptoms with their words; if routine/urgent read `say` + `disclaimer` | triage_symptoms |
| emergency | speak | "What you're describing could be a medical emergency. Please hang up now and dial 911, or go to the nearest emergency room. I can't book this as a regular appointment. If you're not sure, the 911 operator can help you decide." | |
| crisis | speak | "I'm really sorry you're going through this, and I'm glad you reached out. You can call or text 988, the Suicide and Crisis Lifeline, any time, day or night, to talk to someone right now. If you are in immediate danger, please dial 911." | |
| urgent_notice | speak | "Because of what you described, you should be seen within a day or two. I'll look for the earliest opening. If things get worse before then, please go to urgent care or dial 911." | |
| schedule | prompt | area {{specialty_label}} ({{matched_specialty}}); ask day/time; check_availability (urgency=urgent if {{triage_urgency}} is urgent); offer at most two `spoken` options; confirm full name ({{patient_name}}) and reason; book_appointment with slot_id; on slot taken offer alternatives | check_availability, list_specialists, book_appointment |
| booked | speak | "{{booking_line}} Please arrive 15 minutes early and bring your insurance card and a photo ID. If you need to cancel, call us at least 24 hours ahead." | |
| manage | prompt | list_my_appointments; cancel after confirming; reschedule = cancel (after agreement) + check_availability + book_appointment | list_my_appointments, cancel_appointment, check_availability, book_appointment |
| faq | prompt | answer with get_clinic_info and list_specialists; never medical advice; offer to book | get_clinic_info, list_specialists |
| front_desk | prompt | if {{front_desk_open}} is true say you're connecting and use transfer; if closed or failed take a call-back (name, reason) with request_callback | transfer, request_callback |
| paused | speak | "{{paused_message}}" | |
| wrap | prompt | confirm what was done, anything else? else goodbye + hangup | hangup |

Edges in order: e_welcome welcome→intent default · e_intent_paused intent→paused booking_enabled=="false" ·
e_intent_book →symptoms llm "The caller wants a new appointment, a check-up, or describes symptoms." · e_intent_manage
→manage llm "…check, change, reschedule or cancel an existing appointment." · e_intent_faq →faq llm "…a question
about the clinic itself, such as hours, address, insurance or parking." · e_intent_desk →front_desk llm "…asks for a
person, the front desk, a nurse, or their doctor directly." · e_intent_wrap →wrap llm "…nothing else they need." ·
e_sym_emergency symptoms→emergency triage_urgency=="emergency" · e_sym_crisis →crisis =="crisis" · e_sym_urgent
→urgent_notice =="urgent" · e_sym_routine →schedule =="routine" · e_sym_desk →front_desk llm "…refuses to describe
their symptoms and wants a person." · e_emergency_wrap, e_crisis_wrap →wrap default · e_urgent_schedule
urgent_notice→schedule default · e_sched_booked schedule→booked booking_status=="confirmed" · e_sched_paused →paused
=="paused" · e_sched_timeout →front_desk telnyx_conversation_duration_secs >= 600 · e_sched_symptoms →symptoms llm
"…describes different or new symptoms, or wants a different kind of clinician." · e_sched_desk →front_desk llm "None
of the offered times work and the caller wants a person or a call back." · e_booked_wrap booked→wrap default ·
e_manage_booked manage→booked booking_status=="confirmed" · e_manage_symptoms →symptoms llm "…a new appointment for a
new problem." · e_manage_wrap →wrap llm "…done with their appointments." · e_faq_book faq→symptoms llm "…now wants to
book an appointment." · e_faq_desk →front_desk llm "…wants to talk to a person." · e_faq_wrap →wrap llm "…questions
are answered and they need nothing else." · e_desk_wrap front_desk→wrap llm "The transfer was started or a call back
was requested." · e_paused_desk paused→front_desk default · e_wrap_intent wrap→intent llm "The caller has another
request."

### B.3 Generated agent flow
Nodes: greet (speak, disclosure + {{owner_line}} + greeting) · help ("Answer using only the facts in your
instructions. Ask one question at a time.") · message ("Collect the caller's name, a callback number and their
message, one at a time. Read the number back digit by digit. Then call take_message with call_id={{call_control_id}}
and confirm it was saved.") · goodbye ("Ask if there is anything else. If not, thank them, say goodbye and use the
hangup tool.") · [transfer ("Say one short sentence that you are connecting them, then use the transfer tool.")].
Edges: e_greet greet→help default · e_timeout help→goodbye duration ≥ 540 · [e_help_xfer help→transfer llm "asks for
a person or says it is urgent"] · e_help_msg help→message llm "wants to leave a message … or asks something the facts
don't cover" · e_help_bye help→goodbye llm "questions are answered" · e_msg_bye message→goodbye llm "saved with
take_message" · e_msg_help message→help llm "another question instead" · e_bye_help goodbye→help llm "another
question" · [e_xfer_msg transfer→message llm "transfer failed or would rather leave a message"].

---

# Appendix C: Clinic data

### C.1 Clinic facts
Name Northside Family Clinic · agent Clara · address 200 Harbor Avenue, Suite 3 · hours "Monday to Friday 8 AM to 6
PM, and Saturday 9 AM to 1 PM" · desk hours Mon–Fri 08:00–18:00, Sat 09:00–13:00 · facts: "We accept most major
insurance plans; please bring your insurance card and a photo ID." · "New patients should arrive 15 minutes early to
fill in forms." · "Please give at least 24 hours notice to cancel or change an appointment." · "Free parking is behind
the building; the entrance is step-free." · "Lab tests and vaccinations are done on site, no appointment needed for flu
shots on weekdays." · "Prescription refill requests are handled by your doctor's team within two business days."

### C.2 Specialties (id: label · who · keywords)
- family_medicine: family medicine · a family doctor · check up, checkup, check-up, physical, annual exam, cold, flu,
  fever, cough, fatigue, tired, vaccination, vaccine, shot, blood pressure check, prescription, refill, diabetes,
  thyroid, weight, general, not feeling well, sick, infection, urinary, burning when i pee, allergies, sleep
- cardiology: cardiology · a heart specialist · palpitations, heart racing, racing heart, irregular heartbeat, skipped
  beats, heart flutter, high blood pressure, hypertension, heart murmur, cholesterol, swollen ankles, ankle swelling,
  short of breath when walking, heart
- dermatology: dermatology · a skin specialist · rash, acne, mole, eczema, psoriasis, itchy skin, itchy, skin, hives,
  hair loss, wart, spot on my, sunburn, dry skin, nail fungus, freckle
- orthopedics: orthopedics · a bone and joint specialist · knee, back pain, lower back, shoulder, joint, sprain,
  sprained, hip, ankle, wrist, elbow, sports injury, twisted, fracture, broken, neck pain, arthritis, tendon, muscle
  pain, pulled muscle
- ent: ear, nose and throat · an ear, nose and throat specialist · ear, earache, hearing, sinus, sore throat, tonsil,
  nosebleed, hoarse, ringing in my ears, tinnitus, blocked nose, congestion, snoring, throat
- gastroenterology: gastroenterology · a stomach and digestion specialist · stomach, abdominal, belly, heartburn,
  reflux, acid, nausea, diarrhea, diarrhoea, constipation, bloating, indigestion, bowel, vomiting, gas
- neurology: neurology · a neurologist · headache, migraine, numbness, tingling, pins and needles, tremor, memory,
  dizziness, dizzy, vertigo, seizure history, nerve pain
- pediatrics: pediatrics · a children's doctor · my child, my son, my daughter, my baby, my kid, toddler, infant,
  newborn, child, kids, teenager, school physical
- womens_health: women's health · a women's health specialist · pregnant, pregnancy, period, menstrual, pelvic,
  contraception, birth control, menopause, pap smear, prenatal, breast
- behavioral_health: behavioral health · a mental health clinician · anxiety, anxious, depressed, depression, stress,
  panic, mood, can't sleep, insomnia, grief, therapy, counseling, counselling

### C.3 Clinicians (id · name · specialty · days 0=Mon · start–end · slot min · languages)
fm-okafor · Dr. Maya Okafor · family_medicine · 0–4 · 08:30–17:00 · 30 · English
fm-carter · Dr. Ben Carter · family_medicine · 1,3,5 · 09:00–13:00 · 20 · English, Spanish
card-reyes · Dr. Daniel Reyes · cardiology · 0,2,4 · 09:00–16:00 · 30 · English, Spanish
derm-raman · Dr. Priya Raman · dermatology · 1,2,3 · 10:00–17:00 · 30 · English, Hindi
ortho-becker · Dr. Tom Becker · orthopedics · 0,1,3 · 08:00–15:00 · 30 · English, German
ent-novak · Dr. Lena Novak · ent · 2,4 · 09:00–17:00 · 30 · English
gi-kim · Dr. Samuel Kim · gastroenterology · 0,3 · 09:00–16:00 · 30 · English, Korean
neuro-liu · Dr. Grace Liu · neurology · 1,4 · 09:00–16:00 · 45 · English, Mandarin
peds-haddad · Dr. Omar Haddad · pediatrics · 0–4 · 08:30–16:30 · 30 · English, Arabic
wh-weiss · Dr. Hannah Weiss · womens_health · 0,2,4 · 09:00–17:00 · 30 · English
bh-alvarez · Ruth Alvarez, LCSW · behavioral_health · 1–4 · 11:00–19:00 · 50 · English, Spanish
Lunch: no slot starts 12:00–12:59.

### C.4 Red-flag phrases
EMERGENCY: chest pain, chest pressure, crushing chest, tight chest, pain in my chest, heart attack, can't breathe,
cannot breathe, can not breathe, trouble breathing, difficulty breathing, struggling to breathe, severe shortness of
breath, choking, lips turning blue, turning blue, face drooping, face is drooping, slurred speech, slurring, one side of
my body, sudden numbness, sudden weakness, can't move my arm, stroke, unconscious, passed out, fainted, unresponsive,
won't wake up, severe bleeding, bleeding heavily, won't stop bleeding, bleeding a lot, coughing up blood, vomiting
blood, throat closing, throat is closing, tongue swelling, swollen tongue, anaphylaxis, allergic reaction and swelling,
overdose, overdosed, took too many pills, poisoned, poisoning, seizure right now, having a seizure, is seizing, worst
headache of my life, sudden severe headache, stiff neck and fever, head injury, hit my head, car accident, bone
sticking out, pregnant and bleeding, severe burn.
CRISIS: suicide, suicidal, kill myself, end my life, want to die, don't want to live, self harm, self-harm, hurt
myself, hurting myself, cutting myself.
URGENT: high fever, fever for three days, fever for 3 days, fever over 103, fever of 103, fever of 104, broken,
fracture, deep cut, needs stitches, burn, infection is spreading, spreading redness, severe pain, really bad pain, blood
in my stool, blood in stool, blood in my urine, vomiting for, can't keep anything down, dehydrated, baby has a fever,
child has a fever, eye injury, something in my eye, swollen and hot, getting worse quickly, shortness of breath, short
of breath.
(Normalise curly apostrophes to `'` before matching.)

### C.5 Clinic flags (`flags/clinic`)
bookings_enabled true · booking_window_days 14 (1–60) · min_notice_minutes 60 (0–10080) · max_upcoming_per_patient 3
(1–10) · llm_triage true · require_attestation_for_private false · paused_message "Online booking is paused right now.
I can take your details and the front desk will call you back."

### C.6 Clinic variable defaults
clinic_welcome_line "How can I help you today? I can book a visit, change an appointment, or answer questions about the
clinic.", caller_phone "", patient_known "false", patient_name "", visit_count "1", upcoming_count "0",
next_appointment "", booking_enabled "true", paused_message "", front_desk_open "true", triage_urgency "none",
matched_specialty "", specialty_label "", booking_status "none", booking_line "", degraded "false", trace_id.

---

# Appendix D: Things to verify on the real platform
- The `handoff` tool shape (`{"type":"handoff","handoff":{"ai_assistants":[{"id","name"}],"voice_mode"}}`). If the API
  rejects it, configure the hand-off in the Portal.
- Telnyx Cloud Storage auth (API key as the S3 access key id) and endpoint/region names.
- Webhook-tool **response → dynamic variable** mapping is set in the Portal (`draft_complete`, `build_status`,
  `built_agent_name`, `triage_urgency`, `matched_specialty`, `specialty_label`, `booking_status`, `booking_line`).
- Outbound AI calls need `telephony_settings.default_texml_app_id`, and `From` must be a number on that TeXML app.
- Model availability for ASSISTANT_MODEL / POLISH_MODEL / CHAT_MODEL in your account (`GET /v2/ai/models`), and
  whether your chosen chat model supports tool calling on `/ai/openai/chat/completions`.
