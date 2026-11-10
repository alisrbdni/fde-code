/**
 * forge-state entry: a thin, token-protected JSON-RPC gateway to the actors.
 * POST /rpc {actor, name, method, args, trace_id}
 */
import { BuilderProfile } from "./actors/builder-profile"
import { OpsMetrics } from "./actors/ops-metrics"
import { AgentRegistry } from "./actors/agent-registry"
import { ClinicSchedule, ClinicPatient, ClinicDesk } from "./actors/clinic"
import { log } from "./log"
import { actorId, setting } from "./config"

// Actor classes are exported from the entry, where telnyx.toml's [[actors]] `type` names them.
export { BuilderProfile, OpsMetrics, AgentRegistry, ClinicSchedule, ClinicPatient, ClinicDesk }

export interface Env {
  API_URL: string
  STATE_TOKEN: string
  BUILDER: { idFromName(name: string): BuilderProfile }
  OPS: { idFromName(name: string): OpsMetrics }
  REGISTRY: { idFromName(name: string): AgentRegistry }
  SCHEDULE: { idFromName(name: string): ClinicSchedule }
  PATIENT: { idFromName(name: string): ClinicPatient }
  DESK: { idFromName(name: string): ClinicDesk }
}

const ALLOWED: Record<string, { binding: keyof Env; methods: Set<string> }> = {
  builder: {
    binding: "BUILDER",
    methods: new Set([
      "startCall",
      "callInfo",
      "saveDraft",
      "getDraft",
      "resetDraft",
      "beginBuild",
      "finishBuild",
      "listAgents",
      "requestTestCall",
      "deleteAgent",
      "addMessage",
      "listMessages",
      "snapshot",
    ]),
  },
  ops: { binding: "OPS", methods: new Set(["record", "snapshot"]) },
  registry: { binding: "REGISTRY", methods: new Set(["upsert", "setStatus", "list", "get"]) },
  schedule: { binding: "SCHEDULE", methods: new Set(["taken", "book", "cancel", "list"]) },
  patient: {
    binding: "PATIENT",
    methods: new Set([
      "startCall",
      "callInfo",
      "setName",
      "recordTriage",
      "addAppointment",
      "cancelAppointment",
      "list",
      "snapshot",
    ]),
  },
  desk: { binding: "DESK", methods: new Set(["addCallback", "resolveCallback", "log", "snapshot"]) },
}

function timingSafeEqual(a: string, b: string): boolean {
  const left = String(a)
  const right = String(b)
  const len = Math.max(left.length, right.length)
  let diff = 0
  for (let i = 0; i < len; i++) {
    diff |= (left.charCodeAt(i) || 0) ^ (right.charCodeAt(i) || 0)
  }
  return diff === 0 && left.length === right.length
}

const json = (status: number, body: Record<string, any>, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  })

/**
 * The actor runtime briefly answers "503 actor directory unavailable" (seen right after deploys). A 503 means the
 * invocation never ran, so retrying is safe; any other error is returned as-is (a method may have committed).
 */
async function invokeWithRetry<T>(fn: () => Promise<T>, attempts = 3): Promise<T> {
  for (let i = 1; ; i++) {
    try {
      return await fn()
    } catch (exc: any) {
      const msg = String(exc?.message ?? exc)
      if (i >= attempts || !/returned 503|directory unavailable/i.test(msg)) throw exc
      log("warn", "rpc.retry", { attempt: i, error: msg.slice(0, 120) })
      await new Promise((r) => setTimeout(r, 250 * i * i))
    }
  }
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const route = req.method + " " + new URL(req.url).pathname
    if (req.method === "GET" && new URL(req.url).pathname === "/health") {
      return json(200, { ok: true, service: "forge-state" })
    }
    if (req.method !== "POST" || new URL(req.url).pathname !== "/rpc") {
      return json(404, { ok: false, error: "not_found" })
    }
    const started = Date.now()
    let body: any
    try {
      body = await req.json()
    } catch {
      return json(400, { ok: false, error: "invalid_json" })
    }
    const traceId = String(body.trace_id || "")
    const stateToken = await setting(env, "STATE_TOKEN")
    if (!stateToken) {
      return json(500, { ok: false, error: "STATE_TOKEN not configured" })
    }
    const auth = req.headers.get("authorization") || ""
    const token = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7) : ""
    if (!token || !timingSafeEqual(token, stateToken)) {
      log("warn", "rpc.unauthorized", { trace_id: traceId })
      return json(401, { ok: false, error: "unauthorized" })
    }
    const spec = ALLOWED[String(body.actor)]
    const method = String(body.method || "")
    if (!spec || !body.name || !spec.methods.has(method) || typeof method !== "string" || method.startsWith("_")) {
      return json(400, { ok: false, error: "bad_request" })
    }
    const args = Array.isArray(body.args) ? body.args : []
    try {
      const namespace = (env as any)[spec.binding] as any
      const instance = namespace.idFromName(actorId(String(body.name)))
      const result = await invokeWithRetry(() => (instance as any)[method](...args))
      const ms = Date.now() - started
      log("info", "rpc.ok", { trace_id: traceId, actor: body.actor, name: body.name, method, ms })
      return json(200, { ok: true, result }, { "server-timing": `actor;dur=${ms}` })
    } catch (exc: any) {
      log("error", "rpc.failed", {
        trace_id: traceId,
        actor: String(body.actor),
        method,
        error: String(exc && exc.stack ? exc.stack : exc).slice(0, 500),
      })
      return json(500, { ok: false, error: String(exc && exc.message ? exc.message : exc) })
    }
  },
}
