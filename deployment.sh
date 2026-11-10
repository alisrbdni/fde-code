#!/usr/bin/env bash
# =============================================================================================
# deployment.sh: interactive deploy of Agent Forge + Clara to Telnyx Edge Compute
# =============================================================================================
#
#   ./deployment.sh              run every step in order, asking before each one
#   ./deployment.sh kv secrets   run only the named steps (see STEPS below)
#   ./deployment.sh --list       list the steps
#   ASSUME_YES=1 ./deployment.sh ...   don't ask "run this step?" (prompts for missing values still appear)
#
# Every value you enter or the script discovers (ids, URLs, numbers, tokens) is saved to .env, and the
# non-secret ones also go into api/func.toml, so the script is safe to re-run: finished steps detect their
# result and skip. Secrets are read with hidden input and never printed.
#
# AWS analogies, for orientation:
#   Edge Function = Lambda + Function URL · KV = DynamoDB with TTL · Stateful Actor = Durable Object
#   (no AWS twin) · secrets = Secrets Manager · AI Assistant + workflow = Lex bot on an Amazon Connect flow
# =============================================================================================
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"
ENV_FILE="$ROOT/.env"
API_TOML="$ROOT/api/func.toml"
STATE_TOML="$ROOT/state/telnyx.toml"
SCAFFOLD_DIR="${SCAFFOLD_DIR:-$HOME/edge-scaffold}"
KV_NAME="agent-forge"

STEPS=(preflight apikey tokens cli kv functions secrets tests ship_state ship_api telephony assistants smoke summary)

# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------
c_green=$'\033[1;32m'; c_yellow=$'\033[1;33m'; c_red=$'\033[1;31m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say()  { printf '%s==>%s %s\n' "$c_green" "$c_off" "$*"; }
info() { printf '    %s%s%s\n' "$c_dim" "$*" "$c_off"; }
warn() { printf '%s!!%s %s\n' "$c_yellow" "$c_off" "$*"; }
die()  { printf '%sxx%s %s\n' "$c_red" "$c_off" "$*" >&2; exit 1; }

# Read .env into the environment, without echoing anything.
load_env() { set -a; # shellcheck disable=SC1090
  . "$ENV_FILE"; set +a; }

# set_env NAME VALUE: write NAME=VALUE into .env (replacing an existing line) and export it.
set_env() {
  python3 - "$ENV_FILE" "$1" "$2" <<'PY'
import re, sys
path, name, value = sys.argv[1:]
text = open(path).read() if __import__("os").path.exists(path) else ""
line = f"{name}={value}"
if re.search(rf"^{re.escape(name)}=.*$", text, re.M):
    text = re.sub(rf"^{re.escape(name)}=.*$", lambda m: line, text, count=1, flags=re.M)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
open(path, "w").write(text)
PY
  export "$1=$2"
}

# toml_set FILE SECTION KEY VALUE: set KEY = "VALUE" inside [SECTION] (adds the key if missing).
toml_set() {
  python3 - "$@" <<'PY'
import re, sys
path, section, key, value = sys.argv[1:]
lines = open(path).read().split("\n")
header = f"[{section}]"
try:
    start = next(i for i, l in enumerate(lines) if l.strip() == header)
except StopIteration:
    lines += ["", header]; start = len(lines) - 1
end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
new = f'{key} = "{value}"'
for i in range(start + 1, end):
    if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i]):
        lines[i] = new; break
else:
    insert = end
    while insert > start + 1 and not lines[insert - 1].strip():
        insert -= 1
    lines.insert(insert, new)
open(path, "w").write("\n".join(lines))
PY
}

# Copy the current .env values of every [env_vars] key into api/func.toml (non-secrets only).
sync_api_toml() {
  local k
  for k in API_URL STATE_URL KV_NAMESPACE_ID TEXML_APP_ID FORGE_NUMBER ASSISTANT_MODEL POLISH_MODEL CHAT_MODEL \
           STORAGE_ENDPOINT STORAGE_BUCKET STORAGE_REGION CLINIC_ASSISTANT_ID CLINIC_FRONT_DESK_NUMBER CLINIC_UTC_OFFSET \
           FORGE_ASSISTANT_ID REQUIRE_TOKENS; do
    [[ -n "${!k:-}" ]] || continue
    # never copy a local URL (http://localhost...) into the deployed manifest
    [[ "$k" == API_URL || "$k" == STATE_URL ]] && [[ "${!k}" != https://* ]] && continue
    toml_set "$API_TOML" env_vars "$k" "${!k}"
  done
  [[ -n "${KV_NAMESPACE_ID:-}" ]] && toml_set "$API_TOML" storage.kv.FORGE_KV id "$KV_NAMESPACE_ID"
  return 0
}

# ask VAR "question" [default] [secret]: prompt for a value; secret=1 hides the input.
ask() {
  local var="$1" question="$2" default="${3:-}" secret="${4:-0}" answer=""
  local hint=""; [[ -n "$default" && "$secret" != 1 ]] && hint=" [$default]"
  [[ -n "$default" && "$secret" == 1 ]] && hint=" [keep current]"
  if [[ "$secret" == 1 ]]; then read -r -s -p "    $question$hint: " answer; echo
  else read -r -p "    $question$hint: " answer; fi
  printf -v "$var" '%s' "${answer:-$default}"
}

# confirm "question": yes/no, default yes (ASSUME_YES=1 answers yes).
confirm() {
  [[ "${ASSUME_YES:-0}" == 1 ]] && return 0
  local a; read -r -p "    $1 [Y/n] " a
  [[ -z "$a" || "$a" =~ ^[Yy] ]]
}

need() { command -v "$1" >/dev/null || die "$1 is required ($2)"; }
is_placeholder() { [[ -z "${1:-}" || "$1" == REPLACE* || "$1" == *REPLACE* || "$1" == "dev-token" ]]; }
toml_get() { grep -E "^\s*$2\s*=" "$1" 2>/dev/null | head -1 | cut -d'"' -f2; }
first_url() { grep -oE 'https://[A-Za-z0-9.-]+\.telnyxcompute\.com' | head -1 || true; }

# ---------------------------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------------------------

# preflight: tools and .env exist. (AWS: having the CLI, Python and Node installed.)
step_preflight() {
  need python3 "3.11+"; need curl "downloads and API checks"; need node "22+, for the actor host"; need openssl "token generation"
  [[ -f "$ENV_FILE" ]] || { cp .env.example "$ENV_FILE"; chmod 600 "$ENV_FILE"; say "created .env from .env.example"; }
  git ls-files --error-unmatch .env >/dev/null 2>&1 && die ".env is tracked by git; remove it (git rm --cached .env) before deploying"
  [[ -d state/node_modules ]] || (cd state && npm install --silent)
  info "tools ok, .env present and not tracked by git"
}

# apikey: one Telnyx API key is used for the CLI, KV REST, Assistants, TeXML and Inference.
# (AWS: an IAM access key.) Also checks the configured models exist on your account (AWS: Bedrock model access).
step_apikey() {
  load_env
  if [[ -z "${TELNYX_API_KEY:-}" ]] || ! confirm "Use the TELNYX_API_KEY already in .env?"; then
    info "Portal → Account → API Keys → Create API Key"
    local key; ask key "Telnyx API key" "${TELNYX_API_KEY:-}" 1
    [[ -n "$key" ]] || die "an API key is required"
    set_env TELNYX_API_KEY "$key"
  fi
  local models
  models="$(curl -fsS -H "Authorization: Bearer $TELNYX_API_KEY" https://api.telnyx.com/v2/ai/models)" \
    || die "the API key was rejected (GET /v2/ai/models failed)"
  info "API key works"
  local var model
  for var in ASSISTANT_MODEL POLISH_MODEL; do
    model="${!var:-}"
    # Assistants (Forge, Clara and every generated agent) only accept models flagged recommended_for_assistants.
    if [[ "$var" == ASSISTANT_MODEL && -n "$model" ]] && ! python3 -c 'import json,sys; m=sys.argv[1]; sys.exit(0 if any(x["id"]==m and x.get("recommended_for_assistants") for x in json.load(sys.stdin)["data"]) else 1)' "$model" <<<"$models"; then
      warn "$var=$model can't be used by AI Assistants. Assistant-capable models:"
      python3 -c 'import json,sys; print("\n".join("      "+m["id"] for m in json.load(sys.stdin)["data"] if m.get("recommended_for_assistants")))' <<<"$models"
      local pick2; ask pick2 "Model to use for $var" "moonshotai/Kimi-K2.6"
      set_env "$var" "$pick2"; continue
    fi
    if [[ -n "$model" ]] && ! grep -q "\"$model\"" <<<"$models"; then
      warn "$var=$model is not available on this account. Some that are:"
      python3 -c 'import json,sys; print("\n".join("      "+m["id"] for m in json.load(sys.stdin)["data"]))' <<<"$models" | head -20
      local pick; ask pick "Model to use for $var" "moonshotai/Kimi-K3"
      set_env "$var" "$pick"
    else
      info "$var=$model available"
    fi
  done
}

# tokens: shared secrets between the assistants, the two functions and the console.
# MCP_TOKEN = webhook tools + MCP servers, ADMIN_TOKEN = /ui and /ops, STATE_TOKEN = forge-api ↔ forge-state.
step_tokens() {
  load_env
  local t
  for t in MCP_TOKEN ADMIN_TOKEN STATE_TOKEN; do
    if is_placeholder "${!t:-}"; then set_env "$t" "$(openssl rand -hex 24)"; info "generated $t"; else info "$t already set"; fi
  done
}

# cli: install telnyx-edge (versioned release asset) and log it in with the API key. (AWS: `aws configure`.)
step_cli() {
  load_env
  if ! command -v telnyx-edge >/dev/null; then
    local tag arch="linux-amd64"
    [[ "$(uname -s)" == Darwin ]] && arch="macos-$( [[ "$(uname -m)" == arm64 ]] && echo arm64 || echo amd64 )"
    [[ "$(uname -s)" == Linux && "$(uname -m)" == aarch64 ]] && arch="linux-arm64"
    tag="$(curl -fsS https://api.github.com/repos/team-telnyx/edge-compute/releases/latest | python3 -c 'import json,sys;print(json.load(sys.stdin)["tag_name"])')"
    say "installing telnyx-edge $tag ($arch)"
    local tmp; tmp="$(mktemp -d)"
    curl -fsSL "https://github.com/team-telnyx/edge-compute/releases/download/$tag/telnyx-edge-$tag-$arch.tar.gz" | tar xz -C "$tmp"
    sudo mv "$tmp/telnyx-edge-$tag-$arch/telnyx-edge" /usr/local/bin/ && rm -rf "$tmp"
  fi
  info "$(telnyx-edge --version)"
  # The CLI does not read TELNYX_API_KEY from the environment; store it in ~/.telnyx-edge/config.toml.
  telnyx-edge auth api-key set "$TELNYX_API_KEY" >/dev/null
  telnyx-edge auth status | grep -q "Authenticated" || die "telnyx-edge auth failed"
  info "CLI authenticated"
}

# kv: the namespace for flags/*, cache/*, session/chat/*, agents/*. (AWS: a DynamoDB table with TTL.)
step_kv() {
  load_env
  local id
  id="$(telnyx-edge storage kv list 2>/dev/null | awk -v n="$KV_NAME" '$2==n {print $1; exit}')"
  if [[ -z "$id" ]]; then
    say "creating KV namespace $KV_NAME"
    telnyx-edge storage kv create --name "$KV_NAME"
    sleep 3
    id="$(telnyx-edge storage kv list | awk -v n="$KV_NAME" '$2==n {print $1; exit}')"
  fi
  [[ -n "$id" ]] || die "could not find the KV namespace id (telnyx-edge storage kv list)"
  set_env KV_NAMESPACE_ID "$id"; sync_api_toml
  # Round trip through the REST API exactly as the Python function does (raw body, ?ttl_secs).
  local code
  curl -fsS -X PUT "https://api.telnyx.com/v2/storage/kvs/$id/keys/health%2Fdeploy?ttl_secs=60" \
    -H "Authorization: Bearer $TELNYX_API_KEY" -H "content-type: text/plain" --data-binary "ok" >/dev/null
  code="$(curl -s -o /dev/null -w '%{http_code}' "https://api.telnyx.com/v2/storage/kvs/$id/keys/health%2Fdeploy" \
    -H "Authorization: Bearer $TELNYX_API_KEY")"
  [[ "$code" == 200 ]] || die "KV REST read failed (HTTP $code)"
  info "KV $KV_NAME = $id (REST put/get ok)"
}

# functions: register forge-state (TypeScript, actors) and forge-api (Python) and record their func_ids.
# new-func writes a new directory, so it runs in a scratch folder; only the ids are copied back.
# (AWS: `aws lambda create-function` ×2.)
step_functions() {
  load_env
  mkdir -p "$SCAFFOLD_DIR"
  local name lang dir target id flags
  for name in forge-state forge-api; do
    if [[ "$name" == forge-state ]]; then lang=ts; flags="--actor"; target="$STATE_TOML"
    else lang=python; flags=""; target="$API_TOML"; fi
    id="$(toml_get "$target" func_id)"
    if ! is_placeholder "$id"; then info "$name already has func_id $id"; continue; fi
    dir="$SCAFFOLD_DIR/$name"
    if [[ ! -d "$dir" ]]; then
      say "telnyx-edge new-func -l $lang -n $name $flags"
      # shellcheck disable=SC2086
      (cd "$SCAFFOLD_DIR" && telnyx-edge new-func -l "$lang" -n "$name" $flags)
    fi
    id="$(grep -rhoE 'func_id\s*=\s*"[^"]+"' "$dir" --include='*.toml' 2>/dev/null | head -1 | cut -d'"' -f2 || true)"
    if is_placeholder "$id"; then
      warn "couldn't read func_id from $dir; see: telnyx-edge list"
      ask id "func_id for $name"
    fi
    [[ -n "$id" ]] || die "no func_id for $name"
    toml_set "$target" edge_compute func_id "$id"
    info "$name func_id = $id (saved to ${target#"$ROOT"/})"
  done
  info "compare the scaffolds with ours once: ls $SCAFFOLD_DIR/forge-api $SCAFFOLD_DIR/forge-state"
}

# secrets: org-wide secrets, injected into every function as env vars (AWS: Secrets Manager), plus the
# API-key binding that the [telnyx] section uses to give forge-api a Telnyx credential (AWS: an execution role).
step_secrets() {
  load_env
  local s
  for s in STATE_TOKEN MCP_TOKEN ADMIN_TOKEN; do
    is_placeholder "${!s:-}" && die "$s is empty; run: ./deployment.sh tokens"
    telnyx-edge secrets add "$s" "${!s}" >/dev/null && info "secret $s set"
  done
  [[ -n "${API_URL:-}" && "$API_URL" == https://*telnyxcompute.com ]] && telnyx-edge secrets add API_URL "$API_URL" >/dev/null \
    && info "secret API_URL set"
  if telnyx-edge bindings get >/dev/null 2>&1; then info "API key binding exists"
  else say "creating the API key binding"; telnyx-edge bindings create; fi
  if confirm "Also store TELNYX_API_KEY as a secret? (fallback if the binding's temporary token can't call the Assistants API)"; then
    telnyx-edge secrets add TELNYX_API_KEY "$TELNYX_API_KEY" >/dev/null && info "secret TELNYX_API_KEY set"
  fi
}

# tests: never ship a red build. (AWS: the test stage of a CodePipeline.)
step_tests() {
  python3 scripts/validate_workflow.py
  (cd api && python3 -m pytest -q)
  (cd state && npm test --silent >/dev/null && npm run --silent typecheck) && info "TypeScript tests + typecheck ok"
}

# ship_one NAME DIR: deploy one function and print its public URL. (AWS: `sam deploy`.)
ship_one() {
  local name="$1" dir="$2" out url
  say "telnyx-edge ship --from-dir $dir"
  out="$(telnyx-edge ship --from-dir "$dir" --yes 2>&1 | tee /dev/stderr)" \
    || { warn "ship failed; deploy history:"; telnyx-edge deployments "$name" 2>&1 | tail -20; die "ship $name failed"; }
  url="$(first_url <<<"$out")"
  [[ -n "$url" ]] || url="$(telnyx-edge inspect "$name" 2>/dev/null | first_url)"
  printf '%s' "$url"
}

# wait_health URL: poll /health until it answers (cold start). (AWS: waiting for a Lambda to be Active.)
wait_health() {
  local url="$1" i
  for i in $(seq 1 30); do curl -fsS "$url/health" >/dev/null 2>&1 && return 0; sleep 2; done
  return 1
}

# ship_state: the actor host first, because forge-api needs its URL (STATE_URL).
step_ship_state() {
  load_env
  local url; url="$(ship_one forge-state state | tail -1)"
  [[ -n "$url" ]] || ask url "forge-state URL (https://forge-state-<org>.telnyxcompute.com)" "${STATE_URL:-}"
  set_env STATE_URL "$url"; sync_api_toml
  if wait_health "$url"; then info "forge-state healthy: $url"
  else warn "forge-state /health didn't answer; check: telnyx-edge logs forge-state --since 10m"; fi
}

# ship_api: deploy forge-api, record API_URL, give forge-state the API_URL (alarms POST /internal/jobs),
# and reship so both functions see the final URLs.
step_ship_api() {
  load_env
  is_placeholder "${STATE_URL:-}" && die "STATE_URL is empty; run: ./deployment.sh ship_state"
  sync_api_toml
  local url; url="$(ship_one forge-api api | tail -1)"
  [[ -n "$url" ]] || ask url "forge-api URL (https://forge-api-<org>.telnyxcompute.com)" "${API_URL:-}"
  local changed=0; [[ "${API_URL:-}" != "$url" ]] && changed=1
  set_env API_URL "$url"; sync_api_toml
  telnyx-edge secrets add API_URL "$url" >/dev/null && info "secret API_URL set (used by actor alarms)"
  if [[ "$changed" == 1 ]]; then
    say "reshipping both with the final API_URL"
    ship_one forge-state state >/dev/null; ship_one forge-api api >/dev/null
  fi
  wait_health "$url" || warn "forge-api /health didn't answer yet"
  local health; health="$(curl -s "$url/ops/health" || true)"
  echo "$health" | python3 -m json.tool 2>/dev/null || echo "$health"
  grep -q '"ok": *true' <<<"$health" && info "forge-api healthy: $url" \
    || warn "unhealthy: see the failing check above, then: telnyx-edge logs forge-api --since 10m"
}

# telephony: numbers + the TeXML app generated agents call owners back from. Bought in the Portal.
# (AWS: claiming numbers in Amazon Connect and an outbound contact flow.)
step_telephony() {
  load_env
  info "In the Portal: Numbers → Search & Buy (3 numbers: Forge, Clara, callbacks)."
  info "Voice → TeXML Applications → Create 'agent-forge-callbacks'; assign the callbacks number to it."
  local v
  ask v "TeXML application id (TEXML_APP_ID)" "${TEXML_APP_ID:-}"; [[ -n "$v" ]] && set_env TEXML_APP_ID "$v"
  ask v "Callbacks number on that TeXML app, E.164 (FORGE_NUMBER)" "${FORGE_NUMBER:-}"; [[ -n "$v" ]] && set_env FORGE_NUMBER "$v"
  ask v "Clinic front desk number, optional (CLINIC_FRONT_DESK_NUMBER)" "${CLINIC_FRONT_DESK_NUMBER:-}"
  [[ -n "$v" ]] && set_env CLINIC_FRONT_DESK_NUMBER "$v"
  ask v "Number for Forge callers (FORGE_PHONE, used in the README)" "${FORGE_PHONE:-}"; [[ -n "$v" ]] && set_env FORGE_PHONE "$v"
  ask v "Number for Clara callers (CLARA_PHONE)" "${CLARA_PHONE:-}"; [[ -n "$v" ]] && set_env CLARA_PHONE "$v"
  sync_api_toml
  if confirm "Reship forge-api with these values now?"; then ship_one forge-api api >/dev/null; fi
}

# create_assistant NAME: run scripts/create_assistant.py and return the new id.
create_assistant() {
  local name="$1" out id
  out="$(python3 scripts/create_assistant.py "$name" 2>&1 | tee /dev/stderr)" || return 1
  id="$(grep -oE '^assistant_id=.+' <<<"$out" | cut -d= -f2)"
  printf '%s' "$id"
}

# assistants: create Clara, then Forge (Forge's handoff tool needs Clara's id).
# (AWS: deploying the Lex bots and their flows from templates.)
step_assistants() {
  load_env
  is_placeholder "${API_URL:-}" && die "API_URL is empty; ship first"
  local id
  if [[ -z "${CLINIC_ASSISTANT_ID:-}" ]]; then
    say "creating Clara"
    id="$(create_assistant clinic | tail -1)" || die "creating Clara failed; fix the field named in the error in api/function/assistants/clinic/assistant.json"
    [[ -n "$id" ]] || ask id "Clara's assistant id (from the output above)"
    set_env CLINIC_ASSISTANT_ID "$id"; sync_api_toml
  else info "Clara exists: $CLINIC_ASSISTANT_ID (update: python3 scripts/create_assistant.py clinic --update $CLINIC_ASSISTANT_ID)"; fi
  if [[ -z "${FORGE_ASSISTANT_ID:-}" ]]; then
    say "creating Forge"
    id="$(create_assistant forge | tail -1)" || die "creating Forge failed; fix the field named in the error in api/function/assistants/forge/assistant.json"
    [[ -n "$id" ]] || ask id "Forge's assistant id (from the output above)"
    set_env FORGE_ASSISTANT_ID "$id"
  else info "Forge exists: $FORGE_ASSISTANT_ID"; fi
  confirm "Reship forge-api so it knows CLINIC_ASSISTANT_ID?" && ship_one forge-api api >/dev/null
  cat <<EOF

    Finish in the Portal (AI → AI Assistants), for Clara and then Forge (details: assistant/README.md):
      1. MCP servers: $API_URL/clinic/mcp and $API_URL/mcp, header Authorization: Bearer <MCP_TOKEN in .env>
      2. Webhook tools → map responses to variables: draft_complete · build_status, built_agent_name ·
         triage_urgency, matched_specialty, specialty_label · booking_status, booking_line
      3. Tools per node from node_tools.json; handoff only on clinic_handoff; transfer only on front_desk
      4. Telephony: assign ${FORGE_PHONE:-<Forge number>} to Forge and ${CLARA_PHONE:-<Clara number>} to Clara
EOF
}

# smoke: walk a full call against the live URL, optionally with a real callback to your phone.
step_smoke() {
  load_env
  local args=(--base "$API_URL" --mcp-token "$MCP_TOKEN" --admin-token "$ADMIN_TOKEN")
  if confirm "Include a real outbound test call to your phone?"; then
    local phone; ask phone "Your mobile number, E.164" "${OWNER_PHONE:-}"
    [[ -n "$phone" ]] && set_env OWNER_PHONE "$phone"
    args+=(--phone "$phone" --test-call --wait 60)
  fi
  python3 scripts/smoke.py "${args[@]}"
}

step_summary() {
  load_env
  cat <<EOF

  Agent Forge on Telnyx Edge
  Console        ${API_URL:-?}/ui        (ADMIN_TOKEN is in .env)
  API docs       ${API_URL:-?}/docs
  Forge MCP      ${API_URL:-?}/mcp       Clinic MCP  ${API_URL:-?}/clinic/mcp
  Health         ${API_URL:-?}/ops/health
  Actor host     ${STATE_URL:-?}
  Phones         Forge ${FORGE_PHONE:-?}   Clara ${CLARA_PHONE:-?}   your phone ${OWNER_PHONE:-?}
  Logs           telnyx-edge logs forge-api --tail
                 telnyx-edge actors logs BuilderProfile --tail
  Watch          python3 scripts/ops_watch.py --base ${API_URL:-?} --admin-token "\$ADMIN_TOKEN"

  Next: fill the README "Try it" table, set an uptime check on /ops/health, commit and push.
EOF
}

# ---------------------------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------------------------
describe() {
  case "$1" in
    preflight) echo "check tools, create .env if missing";;
    apikey) echo "Telnyx API key + model availability";;
    tokens) echo "generate MCP_TOKEN, ADMIN_TOKEN, STATE_TOKEN";;
    cli) echo "install + authenticate telnyx-edge";;
    kv) echo "find/create the KV namespace, test REST read/write";;
    functions) echo "register forge-state and forge-api, record func_ids";;
    secrets) echo "org secrets + API key binding";;
    tests) echo "validator, Python and TypeScript tests";;
    ship_state) echo "deploy the actor host, record STATE_URL";;
    ship_api) echo "deploy the API, record API_URL, health check";;
    telephony) echo "TeXML app + phone numbers (from the Portal)";;
    assistants) echo "create Clara and Forge, Portal checklist";;
    smoke) echo "live end-to-end smoke test";;
    summary) echo "print all URLs";;
  esac
}

if [[ "${1:-}" == "--list" || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  for s in "${STEPS[@]}"; do printf '  %-11s %s\n' "$s" "$(describe "$s")"; done; exit 0
fi
selected=("$@"); [[ ${#selected[@]} -eq 0 ]] && selected=("${STEPS[@]}")
for s in "${selected[@]}"; do
  declare -F "step_$s" >/dev/null || die "unknown step '$s' (./deployment.sh --list)"
done
for s in "${selected[@]}"; do
  echo; say "step: $s ($(describe "$s"))"
  if [[ ${#selected[@]} -gt 1 && "$s" != preflight && "$s" != summary ]] && ! confirm "Run this step?"; then
    info "skipped"; continue
  fi
  "step_$s"
done
