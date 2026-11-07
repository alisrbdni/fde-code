/**
 * BuilderProfile - one actor instance per owner phone.
 * Holds the draft spec, build lock, quota, inbox, call bindings and the
 * scheduled jobs (test calls + agent expiry) with an alarm that delivers
 * due jobs to the API's /internal/jobs endpoint.
 */
import { StatefulActor } from "@telnyx/edge-runtime"
import { log } from "../log"

export interface Job {
  id: string
  type: "test_call" | "expire"
  agentKey: string
  assistantId: string
  owner: string
  at: number
  attempts: number
  traceId: string
}

export interface TestCall {
  at: number
  traceId: string
}

export interface AgentRecord {
  key: string
  assistantId: string
  name: string
  business: string
  level: string
  status: "active" | "deleting" | "deleted"
  createdAt: number
  updatedAt: number
  expiresAt: number
  testCalls: TestCall[]
}

export interface Message {
  id: string
  agentKey: string
  agentName: string
  from: string
  caller_name: string
  callback_number: string
  message: string
  at: number
  read: boolean
}

export interface Draft {
  id: string
  fields: Record<string, any>
  version: number
  status: "editing" | "building" | "built"
  buildVersion: number
  builtVersion: number
  leaseUntil: number
  agentKey: string
  lastError: string
  updatedAt: number
}

export interface Profile {
  owner: string
  callCount: number
  lastCallAt: number
  attested: Record<string, boolean>
  draft: Draft
  agents: AgentRecord[]
  messages: Message[]
  jobs: Job[]
  alarmAt: number | null
}

export interface JobResult {
  id: string
  ok: boolean
  permanent?: boolean
  error?: string
}

const hex = (n: number): string =>
  Array.from({ length: n }, () => "0123456789abcdef"[Math.floor(Math.random() * 16)]).join("")

export function freshDraft(now: number): Draft {
  return {
    id: "df" + hex(10),
    fields: {},
    version: 1,
    status: "editing",
    buildVersion: 0,
    builtVersion: 0,
    leaseUntil: 0,
    agentKey: "",
    lastError: "",
    updatedAt: now,
  }
}

export class BuilderProfile extends StatefulActor {
  private async load(): Promise<Profile> {
    const stored = await this.ctx.storage.get("profile")
    if (stored) return stored as Profile
    return {
      owner: this.ctx.id,
      callCount: 0,
      lastCallAt: 0,
      attested: {},
      draft: freshDraft(Date.now()),
      agents: [],
      messages: [],
      jobs: [],
      alarmAt: null,
    }
  }

  /** Persist and re-arm the alarm from the earliest pending job. */
  private async save(state: Profile): Promise<void> {
    const next = state.jobs.length ? Math.min(...state.jobs.map((j) => j.at)) : null
    state.alarmAt = next
    await this.ctx.storage.put("profile", state)
    this.ctx.storage.setAlarm(next)
  }

  async startCall(owner: string, callId: string, attested: boolean, now: number) {
    const s = await this.load()
    s.owner = owner
    s.callCount += 1
    s.lastCallAt = now
    if (callId) {
      s.attested[callId] = !!attested
      const ids = Object.keys(s.attested)
      if (ids.length > 10) {
        for (const id of ids.slice(0, ids.length - 10)) delete s.attested[id]
      }
    }
    const hasDraft = Object.keys(s.draft.fields).length > 0 && s.draft.status !== "built"
    const active = s.agents.filter((a) => a.status === "active")
    const result = {
      callCount: s.callCount,
      returning: s.callCount > 1,
      hasDraft,
      draftBusiness: hasDraft ? s.draft.fields.business_name || "" : "",
      draftStatus: hasDraft ? s.draft.status : "none",
      activeAgents: active.length,
      lastAgentName: active.length ? active[active.length - 1].name : "",
      unreadMessages: s.messages.filter((m) => !m.read).length,
    }
    await this.save(s)
    return { ok: true, ...result }
  }

  async callInfo(callId: string) {
    const s = await this.load()
    return { known: Object.prototype.hasOwnProperty.call(s.attested, callId), attested: !!s.attested[callId] }
  }

  async saveDraft(patch: { set?: Record<string, any> | null; append?: Record<string, any[]> | null }, now: number) {
    const s = await this.load()
    if (s.draft.status === "building") {
      return { ok: false, reason: "building" }
    }
    const set = patch.set || {}
    for (const [name, value] of Object.entries(set)) {
      if (value === null || value === "") {
        delete s.draft.fields[name]
      } else {
        s.draft.fields[name] = value
      }
    }
    if (patch.append) {
      for (const name of ["tasks", "facts"]) {
        const additions = (patch.append as Record<string, any[]>)[name]
        if (!Array.isArray(additions)) continue
        const list: any[] = Array.isArray(s.draft.fields[name]) ? s.draft.fields[name] : []
        const lower = new Set(list.map((v) => String(v).toLowerCase()))
        for (const value of additions) {
          if (value === null || value === "") continue
          if (list.length >= 12) break
          if (lower.has(String(value).toLowerCase())) continue
          list.push(value)
          lower.add(String(value).toLowerCase())
        }
        s.draft.fields[name] = list
      }
    }
    s.draft.version += 1
    s.draft.status = "editing"
    s.draft.updatedAt = now
    await this.save(s)
    return { ok: true, draft: s.draft }
  }

  async getDraft() {
    const s = await this.load()
    return { ok: true, draft: s.draft }
  }

  async resetDraft(now: number) {
    const s = await this.load()
    s.draft = freshDraft(now)
    await this.save(s)
    return { ok: true, draft: s.draft }
  }

  async beginBuild(maxAgents: number, leaseMs: number, now: number) {
    const s = await this.load()
    const d = s.draft
    if (d.status === "building" && d.leaseUntil > now) {
      return { ok: false, reason: "in_progress", draft: d }
    }
    if (d.status === "built" && d.builtVersion === d.version) {
      const agent = s.agents.find((a) => a.key === d.agentKey) || null
      return { ok: false, reason: "already_built", agent, draft: d }
    }
    const rebuildOf = d.agentKey && s.agents.some((a) => a.key === d.agentKey && a.status === "active") ? d.agentKey : ""
    if (!rebuildOf) {
      const active = s.agents.filter((a) => a.status === "active")
      if (active.length >= maxAgents) {
        return { ok: false, reason: "quota", draft: d }
      }
    }
    d.status = "building"
    d.leaseUntil = now + leaseMs
    d.buildVersion = d.version
    d.updatedAt = now
    await this.save(s)
    return { ok: true, draft: d, rebuildOf }
  }

  async finishBuild(
    owner: string,
    version: number,
    result: { ok: boolean; key?: string; assistantId?: string; name?: string; business?: string; level?: string; error?: string; traceId?: string },
    ttlMs: number,
    now: number,
  ) {
    const s = await this.load()
    const d = s.draft
    if (d.buildVersion !== version) {
      return { ok: false, reason: "stale", draft: d }
    }
    if (!result.ok) {
      d.status = "editing"
      d.leaseUntil = 0
      d.lastError = result.error || "build failed"
      d.updatedAt = now
      await this.save(s)
      return { ok: false, draft: d }
    }
    const key = result.key || d.agentKey
    const prev = s.agents.find((a) => a.key === key)
    const expiresAt = now + ttlMs
    const agent: AgentRecord = {
      key,
      assistantId: result.assistantId || (prev ? prev.assistantId : ""),
      name: result.name || "",
      business: result.business || "",
      level: result.level || "full",
      status: "active",
      createdAt: prev ? prev.createdAt : now,
      updatedAt: now,
      expiresAt,
      testCalls: prev ? prev.testCalls : [],
    }
    s.agents = s.agents.filter((a) => a.key !== key)
    s.agents.push(agent)
    d.status = "built"
    d.builtVersion = d.version
    d.agentKey = key
    d.leaseUntil = 0
    d.lastError = ""
    d.updatedAt = now
    s.jobs = s.jobs.filter((j) => !(j.type === "expire" && j.agentKey === key))
    s.jobs.push({
      id: "jb" + hex(10),
      type: "expire",
      agentKey: key,
      assistantId: agent.assistantId,
      owner,
      at: expiresAt,
      attempts: 0,
      traceId: result.traceId || "",
    })
    await this.save(s)
    return { ok: true, agent, expiresAt }
  }

  async listAgents(now: number) {
    const s = await this.load()
    void now
    const active = s.agents.filter((a) => a.status === "active")
    return { ok: true, agents: active }
  }

  async requestTestCall(agentKey: string | null, delayMs: number, maxPerHour: number, now: number, traceId: string) {
    const s = await this.load()
    const active = s.agents.filter((a) => a.status === "active")
    const agent = agentKey ? active.find((a) => a.key === agentKey) : active.length ? active[active.length - 1] : null
    if (!agent) {
      return { ok: false, reason: "no_agent" }
    }
    const recent = agent.testCalls.filter((tc) => tc.at > now - 3600000).length
    if (recent >= maxPerHour) {
      return { ok: false, reason: "rate_limited", agent: { key: agent.key, name: agent.name } }
    }
    agent.testCalls.push({ at: now, traceId })
    if (agent.testCalls.length > 50) agent.testCalls = agent.testCalls.slice(-50)
    const job: Job = {
      id: "jb" + hex(10),
      type: "test_call",
      agentKey: agent.key,
      assistantId: agent.assistantId,
      owner: s.owner,
      at: now + delayMs,
      attempts: 0,
      traceId: traceId || "",
    }
    s.jobs.push(job)
    await this.save(s)
    return { ok: true, agentKey: agent.key, name: agent.name, at: job.at, delayMs }
  }

  async deleteAgent(key: string, now: number) {
    const s = await this.load()
    const agent = s.agents.find((a) => a.key === key)
    if (!agent || agent.status === "deleted") {
      return { ok: false, reason: "not_found" }
    }
    agent.status = "deleting"
    agent.updatedAt = now
    s.jobs = s.jobs.filter((j) => j.agentKey !== key)
    s.jobs.push({
      id: "jb" + hex(10),
      type: "expire",
      agentKey: key,
      assistantId: agent.assistantId,
      owner: s.owner,
      at: now,
      attempts: 0,
      traceId: "",
    })
    if (s.draft.agentKey === key) {
      s.draft.agentKey = ""
    }
    await this.save(s)
    return { ok: true, agent: { key: agent.key, name: agent.name } }
  }

  async addMessage(agentKey: string, msg: Record<string, any>, now: number) {
    const s = await this.load()
    const agent = s.agents.find((a) => a.key === agentKey)
    const message: Message = {
      id: "mg" + hex(10),
      agentKey,
      agentName: agent ? agent.name : msg.agent_name || "",
      from: msg.from || "",
      caller_name: msg.caller_name || "",
      callback_number: msg.callback_number || "",
      message: msg.message || "",
      at: now,
      read: false,
    }
    s.messages.unshift(message)
    if (s.messages.length > 50) s.messages.length = 50
    await this.save(s)
    return { ok: true, id: message.id, unread: s.messages.filter((m) => !m.read).length }
  }

  async listMessages(markRead: boolean) {
    const s = await this.load()
    if (markRead) {
      for (const m of s.messages) m.read = true
      await this.save(s)
    }
    return {
      ok: true,
      messages: s.messages,
      unread: s.messages.filter((m) => !m.read).length,
    }
  }

  /** Remove and return all due jobs, sorted by their scheduled time. */
  async takeDueJobs(now: number) {
    const s = await this.load()
    const due = s.jobs.filter((j) => j.at <= now)
    due.sort((a, b) => a.at - b.at)
    const ids = new Set(due.map((j) => j.id))
    s.jobs = s.jobs.filter((j) => !ids.has(j.id))
    await this.save(s)
    return due
  }

  async applyJobResults(jobs: Job[], results: JobResult[], now: number) {
    const s = await this.load()
    for (const job of jobs) {
      const res = results.find((r: JobResult) => r.id === job.id) || { id: job.id, ok: false }
      if (res.ok) {
        if (job.type === "expire") {
          const agent = s.agents.find((a) => a.key === job.agentKey)
          if (agent) {
            agent.status = "deleted"
            agent.updatedAt = now
          }
        }
        continue
      }
      if (res.permanent) continue
      const attempts = (job.attempts || 0) + 1
      if (attempts >= 5) continue
      s.jobs.push({ ...job, attempts, at: now + 60000 * attempts })
    }
    await this.save(s)
    return { ok: true }
  }

  /** Deliver due jobs to the API; called by the platform alarm. */
  async alarm(): Promise<void> {
    const now = Date.now()
    const jobs = await this.takeDueJobs(now)
    if (jobs.length === 0) {
      await this.save(await this.load())
      return
    }
    const started = Date.now()
    const apiBase = (this.env?.API_URL || "").replace(/\/$/, "")
    const token = this.env?.STATE_TOKEN || ""
    let results: JobResult[] = []
    let outcome: "ok" | "degraded" | "error" = "ok"
    let errorText: string | undefined
    try {
      const resp = await fetch(apiBase + "/internal/jobs", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: "Bearer " + token,
          "x-trace-id": jobs[0].traceId || "",
        },
        body: JSON.stringify({ jobs }),
      })
      const data: any = await resp.json().catch(() => null)
      if (resp.ok && data && Array.isArray(data.results)) {
        results = data.results
        outcome = data.results.every((r: JobResult) => r.ok) ? "ok" : "degraded"
      } else {
        outcome = "error"
        errorText = "POST /internal/jobs -> " + resp.status
      }
    } catch (exc: any) {
      outcome = "error"
      errorText = String(exc && exc.message ? exc.message : exc)
    }
    await this.applyJobResults(jobs, results, Date.now())
    await this.save(await this.load())
    const ms = Date.now() - started
    try {
      await this.env.OPS.idFromName("global").record("alarm", outcome, ms, Date.now(), jobs[0].traceId || "", errorText)
    } catch (exc: any) {
      log("warn", "alarm.ops_record_failed", { error: String(exc && exc.message ? exc.message : exc) })
    }
    log(outcome === "ok" ? "info" : "warn", "alarm.delivered", {
      actor: "BuilderProfile",
      id: this.ctx.id,
      jobs: jobs.length,
      outcome,
      ms,
    })
  }

  async snapshot() {
    const s = await this.load()
    return s
  }
}

export default BuilderProfile
