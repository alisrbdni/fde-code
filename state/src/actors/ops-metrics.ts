/**
 * OpsMetrics - one "global" instance shared by the API (over RPC) and by
 * BuilderProfile alarms (through the OPS binding). Per-minute buckets of
 * requests/errors/degraded per route, a latency sample list, and a ring
 * buffer of recent errors and degraded responses.
 */
import { StatefulActor } from "@telnyx/edge-runtime"

export interface Bucket {
  count: number
  errors: number
  routes: Record<string, { count: number; errors: number; degraded: number }>
  lat: number[]
}

export interface MetricsState {
  buckets: Record<string, Bucket>
  recent: { at: number; route: string; outcome: string; trace_id: string; error?: string }[]
}

const MAX_BUCKET_AGE_MIN = 120
const MAX_LAT = 500
const MAX_RECENT = 20

export class OpsMetrics extends StatefulActor {
  private async load(): Promise<MetricsState> {
    const stored = await this.ctx.storage.get("state")
    if (stored) return stored as MetricsState
    return { buckets: {}, recent: [] }
  }

  private async save(state: MetricsState): Promise<void> {
    await this.ctx.storage.put("state", state)
  }

  async record(route: string, outcome: string, ms: number, now: number, trace_id: string, error?: string) {
    const s = await this.load()
    const bucketKey = String(Math.floor(now / 60000))
    const bucket: Bucket = s.buckets[bucketKey] || { count: 0, errors: 0, routes: {}, lat: [] }
    bucket.count += 1
    const routeStats = bucket.routes[route] || { count: 0, errors: 0, degraded: 0 }
    routeStats.count += 1
    if (outcome === "error") {
      bucket.errors += 1
      routeStats.errors += 1
    } else if (outcome === "degraded") {
      routeStats.degraded += 1
    }
    bucket.routes[route] = routeStats
    bucket.lat.push(ms)
    if (bucket.lat.length > MAX_LAT) bucket.lat.shift()
    s.buckets[bucketKey] = bucket
    if (outcome === "error" || outcome === "degraded") {
      s.recent.push({ at: now, route, outcome, trace_id, error })
      if (s.recent.length > MAX_RECENT) s.recent.shift()
    }
    const cutoff = Math.floor(now / 60000) - MAX_BUCKET_AGE_MIN
    for (const key of Object.keys(s.buckets)) {
      if (Number(key) < cutoff) delete s.buckets[key]
    }
    await this.save(s)
    return { ok: true }
  }

  async snapshot(windowMinutes: number, now: number) {
    const s = await this.load()
    const maxBucket = Math.floor(now / 60000)
    const minBucket = maxBucket - (windowMinutes - 1)
    let requests = 0
    let errors = 0
    const lat: number[] = []
    const byRoute: Record<string, { count: number; errors: number; degraded: number }> = {}
    for (const [key, bucket] of Object.entries(s.buckets)) {
      const minute = Number(key)
      if (minute < minBucket || minute > maxBucket) continue
      requests += bucket.count
      errors += bucket.errors
      lat.push(...bucket.lat)
      for (const [route, stats] of Object.entries(bucket.routes)) {
        const agg = byRoute[route] || { count: 0, errors: 0, degraded: 0 }
        agg.count += stats.count
        agg.errors += stats.errors
        agg.degraded += stats.degraded
        byRoute[route] = agg
      }
    }
    lat.sort((a, b) => a - b)
    const pick = (p: number) => (lat.length ? Math.round(lat[Math.min(lat.length - 1, Math.floor((lat.length - 1) * p))]) : 0)
    const recentErrors = [...s.recent].reverse().slice(0, 10)
    return {
      window_minutes: windowMinutes,
      requests,
      errors,
      error_rate: requests ? Math.round((errors / requests) * 10000) / 10000 : 0,
      latency_p50_ms: pick(0.5),
      latency_p95_ms: pick(0.95),
      by_route: byRoute,
      recent_errors: recentErrors,
    }
  }
}

export default OpsMetrics
