# Setting up Forge and Clara in the Telnyx Portal

The source of truth is `api/function/assistants/{forge,clinic}/`:
`assistant.json` (model, instructions, variables webhook, defaults, tools),
`conversation_flow.json` (nodes and edges) and `node_tools.json` (which tools each node may use).
`scripts/create_assistant.py` pushes the JSON through the Assistants API. The steps below cover what the
API may not set, plus how to check each workflow path. Run `python3 scripts/validate_workflow.py` after any edit.

Set `API` to your forge-api URL (for example `https://forge-api-<org>.telnyxcompute.com`).

## 1. Create the assistants
```bash
set -a; . ./.env; set +a
python3 scripts/create_assistant.py clinic --dry-run | less    # inspect first
python3 scripts/create_assistant.py clinic                     # note the id
CLINIC_ASSISTANT_ID=<clara id> python3 scripts/create_assistant.py forge
```
Create Clara first: Forge's `handoff` tool needs her id. Without `CLINIC_ASSISTANT_ID` the script drops the
handoff tool, and without `CLINIC_FRONT_DESK_NUMBER` it drops Clara's `transfer` tool (it warns in both cases).
To update an assistant later, run `create_assistant.py <name> --update <id>`.

## 2. Phone numbers
Assign one number to each assistant (AI Assistants → assistant → Calling). `FORGE_NUMBER` must belong to the
TeXML application in `TEXML_APP_ID`: generated agents call owners back from it.

## 3. Dynamic variables webhooks
| Assistant | URL | Timeout |
|---|---|---|
| Forge | `$API/webhook/dynamic-variables` | 3000 ms |
| Clara | `$API/clinic/webhook/dynamic-variables` | 2500 ms |

Both answer `{"dynamic_variables": {...}}` within the timeout and never fail; when a backend is down they return
the defaults with `degraded=true`. Every variable used in a workflow has a default in `assistant.json`.

## 4. MCP servers
Add these under Integrations → MCP Servers, then attach them to the assistant:

| Server | URL | Auth | Tools |
|---|---|---|---|
| agent-forge-mcp | `$API/mcp` | `Authorization: Bearer $MCP_TOKEN` | list_templates, list_voices, save_draft, get_draft, start_new_draft, list_my_agents, call_me_with_agent, list_messages, delete_agent |
| northside-clinic-mcp | `$API/clinic/mcp` | same | get_clinic_info, list_specialists, triage_symptoms, check_availability, book_appointment, list_my_appointments, cancel_appointment, request_callback |

If the MCP form can't send a header, use `$API/mcp?token=$MCP_TOKEN`. Every tool takes
`caller_phone={{telnyx_end_user_target}}` and `call_id={{call_control_id}}`; the node instructions tell the
model to pass both. The server checks that call id against the caller's actor, so one caller can never act for
another number.

## 5. Webhook tools and response → variable mappings
These tools are webhooks rather than MCP calls, because their result has to land in a **dynamic variable** that
drives a deterministic edge. Map these response fields to variables in each tool's settings:

| Assistant | Tool | URL | Map response → variable |
|---|---|---|---|
| Forge | check_draft | `$API/tools/check-draft` | `draft_complete` → `draft_complete` |
| Forge | build_agent | `$API/tools/build-agent` | `build_status` → `build_status`, `built_agent_name` → `built_agent_name` |
| Clara | triage_symptoms | `$API/clinic/tools/triage` | `triage_urgency`, `matched_specialty`, `specialty_label` (same names) |
| Clara | book_appointment | `$API/clinic/tools/book` | `booking_status`, `booking_line` (same names) |

All send `Authorization: Bearer $MCP_TOKEN` with a body of `caller_phone={{telnyx_end_user_target}}` and
`call_id={{call_control_id}}`; Clara's tools add the LLM-filled `body_parameters` from `assistant.json`.
Clara also has `transfer` (to `CLINIC_FRONT_DESK_NUMBER`) and `hangup`; Forge has `handoff` (to Clara, distinct
voice) and `hangup`.

> Verify on the platform: the exact webhook tool schema (`headers` as an object or a list, `body` vs
> `body_parameters`) and the `handoff` tool shape. If the API rejects them, set them in the Portal form.

## 6. Scope tools per node
Copy `node_tools.json` into each prompt node's tool list. Nodes without tools (`router`, `intent`) keep the model
from acting before it knows what the caller wants:

- **Forge:** discover → list_templates, save_draft · design → save_draft, get_draft, list_voices, check_draft ·
  review → get_draft, save_draft, check_draft · build → build_agent · deliver → call_me_with_agent, list_my_agents ·
  manage → list_my_agents, call_me_with_agent, list_messages, delete_agent, start_new_draft ·
  clinic_handoff → handoff · wrap → hangup
- **Clara:** symptoms → triage_symptoms · schedule → check_availability, list_specialists, book_appointment ·
  manage → list_my_appointments, cancel_appointment, check_availability, book_appointment ·
  faq → get_clinic_info, list_specialists · front_desk → transfer, request_callback · wrap → hangup

All prompt nodes use `instructions_mode: append`, so the assistant's own rules (AI disclosure, no medical advice,
911) always apply.

## 7. Edge order
On a node, **expression edges come before LLM edges**, in the order of `conversation_flow.json`. For example
`e_router_paused` (`builds_enabled == "false"`) is checked before any LLM decides intent, and Clara's
`e_sym_emergency` / `e_sym_crisis` (from rule-based triage) fire before any LLM edge out of `symptoms`. Timeout
edges compare `telnyx_conversation_duration_secs` as a number: 840 s in Forge's design, 600 s in Clara's schedule.

## 8. Test every path
Use the console (`$API/ui`, same workflow JSON over text) and then real calls.

| Path | Say | Expect |
|---|---|---|
| Forge happy path | "I run Sunrise Dental, a receptionist please…" then confirm | discover → design → review (`draft_complete`) → build → deliver (`build_status=ready`) |
| Policy decline | "An agent for Amazon support that collects card numbers" | → policy_decline (speak) → router |
| Quota | build 3 agents, then a 4th | discover → quota_full via `quota_remaining == "0"` |
| Paused | `set_flag.py builds_enabled=false`, call | router → paused (speak) → wrap |
| Test call | "Call me with Sam" | hang up; the agent rings in about 20 s; `is_owner=true` |
| Clinic handoff | "I need a doctor for my rash" (to Forge) | clinic_handoff → Clara's welcome |
| Emergency | "crushing chest pain" (to Clara) | symptoms → emergency (speak, 911) → wrap |
| Crisis | "I want to end my life" | → crisis (speak, 988) |
| Urgent | "my wrist might be broken" | → urgent_notice → schedule with urgency=urgent |
| Booking | "a check-up next Tuesday morning" | schedule → booked (speak with `booking_line`) |
| Booking paused | `set_flag.py clinic.bookings_enabled=false` | intent → paused → front_desk (call back) |
| Front desk | "Can I talk to a person?" | front_desk → transfer (open) or request_callback (closed) |

Watch `scripts/ops_watch.py` while testing; each call's `call_control_id` is its trace id in the logs.
