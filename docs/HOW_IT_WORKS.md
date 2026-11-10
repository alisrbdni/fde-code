# How it works

Every diagram below is Mermaid, which GitHub renders directly. Workflow diagrams generated from the deployed JSON are in
[workflows.md](workflows.md) (regenerate with `python3 scripts/flow_to_mermaid.py --all > docs/workflows.md`).

## 1. The pieces

```mermaid
flowchart LR
  caller(["📞 Caller / 🌐 browser"]) --> asst
  subgraph telnyx["Telnyx AI Assistants"]
    asst["Forge / Clara / generated agent<br/>conversation workflow"]
  end
  subgraph edge["Telnyx Edge Compute"]
    api["forge-api<br/>Python Edge Function"]
    state["forge-state<br/>TypeScript Edge Function<br/>/rpc gateway"]
    subgraph actors["Stateful Actors"]
      bp[("BuilderProfile<br/>per owner")]
      sch[("ClinicSchedule<br/>per clinician")]
      pat[("ClinicPatient<br/>per patient")]
      ops[("OpsMetrics · AgentRegistry · ClinicDesk<br/>global")]
    end
    kv[("Telnyx KV<br/>flags · caches · sessions · agents")]
  end
  asst -- "dynamic variables webhook" --> api
  asst -- "MCP tools/call (/mcp, /clinic/mcp)" --> api
  asst -- "webhook tools (build, triage, book)" --> api
  api -- "REST" --> kv
  api -- "RPC + trace id" --> state --> actors
  bp -. "alarm → /internal/jobs" .-> api
  api -- "create / update / delete assistants<br/>TeXML ai_calls" --> telnyx
  api -- "Inference: persona polish, triage fallback, console chat" --> inf["Telnyx Inference"]
  api -- "SigV4" --> s3[("Cloud Storage<br/>build archives, bookings")]
```

| Piece | What it does | Why this primitive |
|---|---|---|
| **forge-api** (Edge Function, Python) | Every webhook, both MCP servers, the build pipeline, the console and its simulator, `/internal/jobs` for alarms | Stateless request handling; scales to zero |
| **forge-state** (Edge Function, TypeScript) | Thin, token-protected `/rpc` gateway in front of the actors (actors are TS classes reached through bindings) | The only way Python can reach actors |
| **BuilderProfile** actor (one per owner phone) | Draft, build lock, quota, inbox, call binding, scheduled jobs + alarm | Read-modify-write that must be single-threaded: 4 concurrent builds → 1 assistant |
| **ClinicSchedule** actor (one per clinician) | Bookings | "Is the slot free? then take it" must be atomic |
| **ClinicPatient** actor (one per phone) | Appointments, upcoming limit, last triage | Check-then-act limit |
| **OpsMetrics / AgentRegistry / ClinicDesk** (global) | Counters and latency, agent index, callbacks + activity | Concurrent appends; OpsMetrics is shared by the API (RPC) and alarms (binding) |
| **KV** | `flags/global`, `flags/clinic` · `cache/polish/*`, `cache/triage/*` · `session/chat/*` · `agents/{key}` | Read-heavy, TTL'd, single-writer data: flags, caches, sessions, published config |
| **Cloud Storage** | `forge/builds/{key}/v{n}.json`, `clinic/appointments/{id}.json` | Documents that outlive the agent |

## 2. A call starts: the dynamic variables webhook

```mermaid
sequenceDiagram
  autonumber
  participant T as Telnyx Assistant
  participant A as forge-api
  participant K as KV
  participant S as BuilderProfile actor
  T->>A: POST /webhook/dynamic-variables?token=… {caller, call_control_id, verified}
  par read flags (memoised 30 s)
    A->>K: GET flags/global
  and bind the call
    A->>S: startCall(owner, call_id, attested)
  end
  S-->>A: callCount, hasDraft, activeAgents, unread…
  A-->>T: {"dynamic_variables": {welcome_line, quota_remaining, builds_enabled, has_draft, …}}
  Note over T: welcome speak node reads {{welcome_line}};<br/>router's comparison edges read builds_enabled / quota_remaining
```

If KV or the actor fails, the webhook still answers 200 with defaults and `degraded=true`. The call carries on, and
the outcome is counted in OpsMetrics.

## 3. An MCP tool call (and why the LLM can't act for someone else)

```mermaid
sequenceDiagram
  participant T as Telnyx Assistant (node: design)
  participant A as forge-api /mcp
  participant S as BuilderProfile actor
  T->>A: tools/call save_draft {caller_phone, call_id, values}
  A->>S: callInfo(call_id)
  alt call_id was bound to this phone by the variables webhook
    S-->>A: known=true
    A->>S: saveDraft({set, append})  — single-threaded per owner
    S-->>A: draft v+1
    A-->>T: {content:[text], structuredContent:{saved, draft_complete, missing…}}
  else unknown or templated call_id
    A-->>T: isError: "I can't verify this call yet"
  end
```

Tools are scoped per workflow node (`node_tools.json`): the router has none, discover has `list_templates` and
`save_draft`, build only has `build_agent`.

## 4. Building an agent

```mermaid
sequenceDiagram
  participant T as Forge (node: build)
  participant A as forge-api /tools/build-agent
  participant S as BuilderProfile actor
  participant K as KV
  participant I as Telnyx Inference
  participant AA as Telnyx Assistants API
  participant R as AgentRegistry + Cloud Storage
  T->>A: build_agent {caller_phone, call_id}
  A->>S: beginBuild(max_agents, lease)
  S-->>A: ok (lock taken) / in_progress / already_built / quota
  A->>K: GET cache/polish/{spec hash}
  alt cache miss
    A->>I: persona + greeting (json_schema)
    A->>K: PUT cache/polish/{hash} (7 days)
  end
  Note over A: code appends the safety block + AI disclosure<br/>after any LLM text
  A->>AA: POST /ai/assistants (workflow, tools, webhooks)
  alt 400/422
    A->>AA: retry one level down the ladder<br/>full → no_workflow → no_tools → minimal
  end
  A->>K: PUT agents/{key} (ttl 25 h)
  A->>S: finishBuild → schedules the expire alarm (24 h)
  A->>R: registry upsert + archive v{n}.json (tokens redacted)
  A-->>T: {build_status: "ready", built_agent_name}
  Note over T: comparison edge build_status == ready → deliver
```

## 5. The test call and auto-expiry (actor alarms)

```mermaid
sequenceDiagram
  participant O as Owner
  participant F as Forge
  participant A as forge-api
  participant S as BuilderProfile actor
  participant X as Telnyx TeXML
  participant G as Generated agent
  O->>F: "Call me with it"
  F->>A: call_me_with_agent (private: verified caller ID)
  A->>S: requestTestCall → job at now+20 s, setAlarm
  Note over S: ⏰ alarm fires, no traffic needed
  S->>A: POST /internal/jobs (Bearer STATE_TOKEN, x-trace-id = the original call)
  A->>X: ai_calls {From: FORGE_NUMBER, To: owner, AIAssistantId}
  X->>G: outbound call to the owner
  G->>A: POST /agents/{key}/dynamic-variables
  A->>K: GET agents/{key} (memoised)
  A-->>G: is_owner=true, owner_line="This is your test call…"
  G->>A: take_message (per-agent token) → owner's inbox in BuilderProfile
  Note over S: 24 h later the expire job deletes the assistant,<br/>the KV record and marks the registry entry deleted
```

## 6. Clara books an appointment

```mermaid
sequenceDiagram
  participant P as Patient
  participant C as Clara
  participant A as forge-api
  participant Sch as ClinicSchedule (per clinician)
  participant Pat as ClinicPatient (per phone)
  P->>C: "My knee hurts when I run"
  C->>A: triage_symptoms (webhook)
  Note over A: rules decide urgency (crisis > emergency > urgent > routine);<br/>an LLM may only pick a specialty from a closed enum
  A-->>C: triage_urgency=routine, matched_specialty=orthopedics
  Note over C: comparison edge triage_urgency == routine → schedule<br/>(== emergency → 911 speak node)
  C->>A: check_availability (MCP) → generate slots − Sch.taken()
  C->>A: book_appointment {slot_id, full_name}
  A->>Sch: book(slot) — atomic: one winner per slot
  A->>Pat: addAppointment (limit) — on "limit", A cancels the slot again (compensation)
  A-->>C: booking_status=confirmed, booking_line
  Note over C: booking_status == confirmed → booked speak node
```

## 7. The browser: console chat, voice and calls

```mermaid
flowchart LR
  b(["Browser"]) -- "/ui (no login)" --> pub["/ui/api/public/chat/*<br/>fresh demo caller number per chat,<br/>rate-limited"]
  b -- "/ui + admin token" --> adm["/ui/api/chat/* + Agents, Clinic, Ops, KV browser"]
  pub & adm --> sim["simulator.py<br/>runs the real conversation_flow.json:<br/>speak nodes verbatim, comparison edges,<br/>one LLM routing call, node-scoped tools"]
  sim --> handlers["the same webhook + MCP handlers<br/>a phone call uses"]
  handlers --> kvact[("KV + actors")]
  sim -- "session" --> sess[("KV session/chat/{id}<br/>+ in-memory copy")]
  b -- "🎙 Voice button" --> speech["browser speech recognition + synthesis<br/>on top of the same chat"]
  b -- "/call" --> widget["Telnyx &lt;telnyx-ai-agent&gt; widget<br/>real WebRTC call to the deployed assistant"]
```

## 8. Following one call: the trace id

```mermaid
flowchart LR
  v["variables webhook<br/>trace = call_control_id"] --> m["MCP tool calls<br/>trace = call_id arg"] --> r["actor RPC<br/>trace_id field"] --> j["scheduled job<br/>job.traceId"] --> al["alarm → /internal/jobs<br/>x-trace-id"] --> d["job.done log line"]
```

One `grep <call_control_id>` over `telnyx-edge logs forge-api` shows the call, every tool, the build, and the
test call that happened later. Every response also carries `server-timing` with per-hop spans (KV, actor,
inference, Assistants API).
