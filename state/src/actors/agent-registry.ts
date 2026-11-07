/**
 * AgentRegistry - one "global" instance; the console's agent index.
 * Newest first, max 200 records; concurrent upserts are serialised by the
 * actor runtime so nothing is lost.
 */
import { StatefulActor } from "@telnyx/edge-runtime"

export interface RegistryRecord {
  key: string
  owner?: string
  name: string
  business?: string
  assistantId?: string
  level?: string
  status: string
  source?: string
  expiresAt?: number
  createdAt?: number
  updatedAt?: number
  builds?: number
  lastTraceId?: string
  lastVersion?: number
  archive?: Record<string, any>
  [k: string]: any
}

const MAX_AGENTS = 200

export class AgentRegistry extends StatefulActor {
  private async load(): Promise<RegistryRecord[]> {
    const stored = await this.ctx.storage.get("agents")
    return Array.isArray(stored) ? (stored as RegistryRecord[]) : []
  }

  private async save(agents: RegistryRecord[]): Promise<void> {
    await this.ctx.storage.put("agents", agents)
  }

  async upsert(rec: RegistryRecord, now: number) {
    const agents = await this.load()
    const idx = agents.findIndex((a) => a.key === rec.key)
    let merged: RegistryRecord
    if (idx >= 0) {
      merged = { ...agents[idx], ...rec, createdAt: agents[idx].createdAt, builds: (agents[idx].builds || 0) + 1, updatedAt: now }
    } else {
      merged = { ...rec, createdAt: now, builds: 1, updatedAt: now }
      agents.unshift(merged)
    }
    merged.updatedAt = now
    const without = agents.filter((a) => a.key !== rec.key)
    const next = idx >= 0 ? [merged, ...without] : [merged, ...without]
    if (next.length > MAX_AGENTS) next.length = MAX_AGENTS
    await this.save(next)
    return { ok: true, agent: merged }
  }

  async setStatus(key: string, status: string, now: number) {
    const agents = await this.load()
    const agent = agents.find((a) => a.key === key)
    if (!agent) return { ok: false, reason: "not_found" }
    agent.status = status
    agent.updatedAt = now
    await this.save(agents)
    return { ok: true, agent }
  }

  async list(limit: number) {
    const agents = await this.load()
    return { ok: true, agents: agents.slice(0, limit) }
  }

  async get(key: string) {
    const agents = await this.load()
    const agent = agents.find((a) => a.key === key)
    return { ok: true, agent: agent || null }
  }
}

export default AgentRegistry
