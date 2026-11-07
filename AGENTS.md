# Repo rules for AI coding agents

## Layout
- `api/` is a Python Edge Function, **standard library only** at runtime
  (`cryptography` optional for Ed25519 signature checks). Entry:
  `function/func.py::new()` returns an object with an ASGI 3 `handle(scope, receive, send)`.
- `state/` is the TypeScript Edge Function hosting the Stateful Actors. Keep it thin.
- `api/function/state.py` contains a **Python emulator of every actor** with identical
  semantics. When you change an actor in `state/src/actors/*.ts`, change the mirror in
  `api/function/state.py` **in the same commit**, and keep
  `state/test/actors.test.ts` and the Python tests green.

## Contracts that must not drift
- KV keys use `/`, never `:` (Telnyx KV forbids colons; `MemoryKV` enforces it).
- Variables webhooks must return `{"dynamic_variables": {...}}`; a flat object is ignored
  by the platform and by our tests.
- Every `{{variable}}` used in a workflow needs a default in the assistant's
  `dynamic_variables` list. `python3 scripts/validate_workflow.py` proves it.
- MCP tool results use `{"content": [{"type": "text", "text": ...}], "structuredContent": ...}`.
- `{{telnyx_end_user_target}}` / `{{call_control_id}}` are always passed as
  `caller_phone` / `call_id` tool args, but tools never trust `caller_phone` alone:
  verify the call with the actor's `callInfo(call_id)` first.

## Safety rules (never regress these)
- Never let LLM output remove the safety block or the AI disclosure from generated
  agents; the code appends them after any LLM polish.
- Urgency (911/988/urgent) for the clinic is decided by **rules**, never by an LLM.
  The LLM may only pick a specialty from the closed enum.
- The policy block in `policy.py` must keep blocking impersonation, payment/ID
  collection, AI-hiding and deception.
- Console overrides are memory-only: never log their values, never write them to KV.

## Logging
- One JSON line per request via `log.py`. Never log full phone numbers (use
  `mask_phone`), draft contents, messages or tokens. Never name a log field `level`.

## Before you finish ANY task
Run `./run.sh test`. Ask for the exact failing output if handed a partial log.
