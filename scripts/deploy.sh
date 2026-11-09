#!/usr/bin/env bash
# Deploy Agent Forge to Telnyx Edge Compute.
#
#   scripts/deploy.sh        print the first-time setup steps
#   scripts/deploy.sh ship   validate + test, then ship forge-state and forge-api
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" == "ship" ]]; then
  command -v telnyx-edge >/dev/null || { echo "telnyx-edge CLI not found (see README: Deploy)"; exit 1; }
  python3 scripts/validate_workflow.py
  (cd api && python3 -m pytest -q)
  (cd state && npm test --silent && npm run --silent typecheck)
  echo "==> shipping forge-state (actors)"; telnyx-edge ship --from-dir state
  echo "==> shipping forge-api";           telnyx-edge ship --from-dir api
  echo "==> done. Check: curl -fsS \"\$API_URL/ops/health\""
  exit 0
fi

cat <<'STEPS'
First-time deployment: run ./deployment.sh (interactive; does all of this for you).
Manual equivalent, in order:

 1. CLI + auth (release assets are versioned; check github.com/team-telnyx/edge-compute/releases)
      V=v0.5.17
      curl -fsSL "https://github.com/team-telnyx/edge-compute/releases/download/$V/telnyx-edge-$V-linux-amd64.tar.gz" | tar xz
      sudo mv "telnyx-edge-$V-linux-amd64/telnyx-edge" /usr/local/bin/
      telnyx-edge auth api-key set "$TELNYX_API_KEY" && telnyx-edge auth status

 2. KV namespace (copy its id into api/func.toml: KV_NAMESPACE_ID and [storage.kv.FORGE_KV] id)
      telnyx-edge storage kv create --name agent-forge

 3. Create both functions in a scratch folder; copy each func_id into state/func.toml and api/func.toml
      mkdir -p ~/edge-scaffold && cd ~/edge-scaffold
      telnyx-edge new-func -l ts -n forge-state
      telnyx-edge new-func -l python -n forge-api

 4. Secrets (org-wide; the value is an argument)
      telnyx-edge secrets add STATE_TOKEN "$STATE_TOKEN"
      telnyx-edge secrets add MCP_TOKEN "$MCP_TOKEN"
      telnyx-edge secrets add ADMIN_TOKEN "$ADMIN_TOKEN"
      telnyx-edge secrets add API_URL "https://forge-api-<org>.telnyxcompute.com"   # after step 5

 5. Ship the actor host first, then the API
      (cd state && telnyx-edge ship)     # note its URL -> STATE_URL in api/func.toml
      (cd api && telnyx-edge ship)       # note its URL -> API_URL (both functions)
    forge-state needs API_URL (the forge-api URL) so actor alarms can POST /internal/jobs.

 6. Telephony
      Portal: create a TeXML application (TEXML_APP_ID) and buy two numbers.
      FORGE_NUMBER must be on that TeXML app (it is the From of outbound test calls).

 7. Assistants (Clara first, so Forge can hand off to her)
      set -a; . ./.env; set +a
      python3 scripts/create_assistant.py clinic          # -> id = CLINIC_ASSISTANT_ID
      CLINIC_ASSISTANT_ID=<id> python3 scripts/create_assistant.py forge
    Then finish in the Portal (assistant/README.md): assign numbers, add the MCP servers
    ($API_URL/mcp and $API_URL/clinic/mcp with Bearer MCP_TOKEN), map webhook-tool
    responses to variables, and scope tools per node from node_tools.json.

 8. Cloud Storage (optional): create the bucket in the Portal; set STORAGE_ENDPOINT.

 9. Verify
      python3 scripts/smoke.py --base "$API_URL" --mcp-token "$MCP_TOKEN" --admin-token "$ADMIN_TOKEN" --test-call
      python3 scripts/ops_watch.py --base "$API_URL" --admin-token "$ADMIN_TOKEN"
      open "$API_URL/ui"   (console)   and   "$API_URL/docs"

Later deploys:  scripts/deploy.sh ship
STEPS
