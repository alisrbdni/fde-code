/**
 * Actor + gateway tests. Run with: npm test (state/)
 */
import test from "node:test"
import assert from "node:assert/strict"
import http from "node:http"
import { makeEnv, type LocalEnv } from "../local/env"
import { fireAlarmNow } from "../local/edge-runtime-shim"
import handler from "../src/index"
import { actorId } from "../src/config"

const OWNER = "+15550102001"
const NOW = Date.now()

function builder(env: LocalEnv, owner = OWNER) {
  return env.BUILDER.idFromName(actorId(owner))
}

async function fillDraft(env: LocalEnv, owner = OWNER, fields: Record<string, any> = {}) {
  const b = builder(env, owner)
  await b.startCall(owner, `call-${Math.random().toString(16).slice(2)}`, true, Date.now())
  await b.saveDraft(
    {
      set: {
        business_name: "Sunrise Dental",
        agent_name: "Sam",
        purpose: "answer calls and take messages",
        ...fields,
      },
    },
    Date.now(),
  )
  return b
}

async function buildOnce(env: LocalEnv, owner: string, key: string) {
  const b = builder(env, owner)
  const begin = await b.beginBuild(3, 30000, Date.now())
  assert.equal(begin.ok, true, `beginBuild should be ok: ${JSON.stringify(begin)}`)
  const fin = await b.finishBuild(owner, (begin as any).draft.version, { ok: true, key, assistantId: "ASST-" + key, name: "Sam", business: "Sunrise Dental", level: "full" }, 24 * 3600 * 1000, Date.now())
  assert.equal(fin.ok, true)
  return b
}

test("builder: draft merge and dedupe", async () => {
  const env = makeEnv()
  const b = builder(env)
  await b.startCall(OWNER, "call-1", true, Date.now())
  await b.saveDraft({ set: { business_name: "Sunrise Dental", agent_name: "Sam" } }, Date.now())
  await b.saveDraft({ set: { purpose: "take messages" } }, Date.now())
  await b.saveDraft({ append: { facts: ["We are at 12 Oak Street", "we are at 12 oak street"] } }, Date.now())
  const got = await b.getDraft()
  assert.equal((got as any).draft.fields.business_name, "Sunrise Dental")
  assert.equal((got as any).draft.fields.facts.length, 1, "deduped case-insensitively")
  await b.saveDraft({ set: { business_name: "" } }, Date.now())
  const cleared = await b.getDraft()
  assert.equal((cleared as any).draft.fields.business_name, undefined, "empty set deletes the field")
})

test("builder: 5 concurrent beginBuild -> exactly 1 ok", async () => {
  const env = makeEnv()
  const b = await fillDraft(env)
  const calls = Array.from({ length: 5 }, () => b.beginBuild(3, 30000, Date.now()))
  const results = await Promise.all(calls)
  const okCount = results.filter((r: any) => r.ok).length
  const inProgress = results.filter((r: any) => r.reason === "in_progress").length
  assert.equal(okCount, 1)
  assert.equal(inProgress, 4)
})

test("builder: lease expiry frees a crashed build", async () => {
  const env = makeEnv()
  const b = await fillDraft(env)
  const first = await b.beginBuild(3, 30, Date.now())
  assert.equal(first.ok, true)
  await new Promise((resolve) => setTimeout(resolve, 40))
  const second = await b.beginBuild(3, 30000, Date.now())
  assert.equal((second as any).ok, true, "expired lease is reclaimable")
})

test("builder: rebuild skips quota, fresh draft hits quota", async () => {
  const env = makeEnv()
  const b = builder(env)
  await fillDraft(env, OWNER, { business_name: "Alpha", agent_name: "A" })
  await buildOnce(env, OWNER, "ag11111111")
  await b.resetDraft(Date.now())
  await fillDraft(env, OWNER, { business_name: "Beta", agent_name: "B" })
  await buildOnce(env, OWNER, "ag22222222")
  // quota (2 active) is full for a brand-new draft...
  await b.resetDraft(Date.now())
  await fillDraft(env, OWNER, { business_name: "Gamma", agent_name: "G" })
  const quota: any = await builder(env).beginBuild(2, 30000, Date.now())
  assert.equal(quota.reason, "quota")
})

test("builder: rebuild of the built draft skips quota", async () => {
  const env = makeEnv()
  const b = builder(env)
  await fillDraft(env, OWNER, { business_name: "Alpha", agent_name: "A" })
  await buildOnce(env, OWNER, "ag11111111")
  await b.resetDraft(Date.now())
  await fillDraft(env, OWNER, { business_name: "Beta", agent_name: "B" })
  await buildOnce(env, OWNER, "ag22222222")
  // editing the current (built) draft is a REBUILD of ag22222222: allowed at quota
  await b.saveDraft({ set: { hours: "Sat only" } }, Date.now())
  const rebuild: any = await builder(env).beginBuild(2, 30000, Date.now())
  assert(rebuild.ok, `rebuild expected ok: ${JSON.stringify(rebuild)}`)
  assert.equal(rebuild.rebuildOf, "ag22222222")
})

test("builder: already_built + rebuild", async () => {
  const env = makeEnv()
  await fillDraft(env, OWNER)
  const b = await buildOnce(env, OWNER, "ag11111111")
  const again: any = await b.beginBuild(3, 30000, Date.now())
  assert.equal(again.reason, "already_built")
  assert.equal(again.agent.key, "ag11111111")
  await b.saveDraft({ set: { hours: "Mon-Fri" } }, Date.now())
  const rebuild: any = await b.beginBuild(3, 30000, Date.now())
  assert.equal(rebuild.ok, true)
  assert.equal(rebuild.rebuildOf, "ag11111111")
  const fin: any = await b.finishBuild(OWNER, rebuild.draft.version, { ok: true, key: "ag11111111", assistantId: "ASST-x", name: "Sam", business: "Sunrise Dental", level: "full" }, 24 * 3600 * 1000, Date.now())
  assert.equal(fin.ok, true)
  const agents: any = await b.listAgents(Date.now())
  assert.equal(agents.agents.length, 1, "rebuild keeps one agent")
})

test("builder: callInfo binding keeps last 10", async () => {
  const env = makeEnv()
  const b = builder(env)
  for (let i = 0; i < 12; i++) {
    await b.startCall(OWNER, `call-${i}`, i % 2 === 0, Date.now())
  }
  const knownOld = await b.callInfo("call-0")
  assert.equal((knownOld as any).known, false, "older than the last 10 bindings")
  const fresh = await b.callInfo("call-11")
  assert.equal((fresh as any).known, true)
  assert.equal((fresh as any).attested, false)
})

test("builder: test call rate limit", async () => {
  const env = makeEnv()
  const b = await buildOnce(env, OWNER, "ag11111111")
  const now = Date.now()
  const one = await b.requestTestCall("ag11111111", 1000, 2, now, "trace-1")
  assert.equal((one as any).ok, true)
  const two = await b.requestTestCall(null, 1000, 2, now, "trace-2")
  assert.equal((two as any).ok, true, "null key picks the newest active agent")
  const three = await b.requestTestCall("ag11111111", 1000, 2, now, "trace-3")
  assert.equal((three as any).reason, "rate_limited")
})

async function startMockApi(resultFor: (body: any, headers: Record<string, string>) => any) {
  const seen: { body: any; headers: Record<string, string> }[] = []
  const server = http.createServer((req, res) => {
    const chunks: Buffer[] = []
    req.on("data", (c) => chunks.push(c as Buffer))
    req.on("end", () => {
      const body = JSON.parse(Buffer.concat(chunks).toString() || "{}")
      seen.push({ body, headers: req.headers as Record<string, string> })
      res.writeHead(200, { "content-type": "application/json" })
      res.end(JSON.stringify(resultFor(body, req.headers as Record<string, string>)))
    })
  })
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve))
  const address = server.address() as any
  return { server, url: `http://127.0.0.1:${address.port}`, seen }
}

test("alarm: posts due jobs, applies results, expire job remains, ops records alarm", async () => {
  const mock = await startMockApi((body) => ({
    results: (body.jobs || []).map((j: any) => ({ id: j.id, ok: true })),
  }))
  const env = makeEnv("tok", mock.url)
  const b = builder(env)
  await fillDraft(env)
  await buildOnce(env, OWNER, "ag11111111")
  const past = Date.now() - 20000
  await b.requestTestCall(null, 1000, 3, past, "trace-alarm-1")
  await fireAlarmNow([env.BUILDER])
  await new Promise((resolve) => setTimeout(resolve, 30))
  assert.equal(mock.seen.length, 1, "alarm POSTs due jobs exactly once")
  const job = mock.seen[0].body.jobs[0]
  assert.equal(job.type, "test_call")
  assert.equal(job.agentKey, "ag11111111")
  assert.equal(mock.seen[0].headers["x-trace-id"], "trace-alarm-1")
  const profile: any = await env.BUILDER.rawStorage(actorId(OWNER)).get("profile")
  const remaining = profile.jobs.filter((j: any) => j.type === "expire")
  assert.equal(remaining.length, 1, "expire job remains after the test call ran")
  // shared OpsMetrics through the binding
  const ops = env.OPS.idFromName(actorId("global"))
  const snap: any = await ops.snapshot(10, Date.now())
  assert.equal(snap.by_route.alarm.count, 1)
  mock.server.close()
})

test("alarm: failed delivery requeues with backoff", async () => {
  const mock = await startMockApi(() => null)
  mock.server.close()
  const closedUrl = mock.url
  const env = makeEnv("tok", closedUrl)
  const b = builder(env)
  await fillDraft(env)
  await buildOnce(env, OWNER, "ag11111111")
  const past = Date.now() - 20000
  await b.requestTestCall(null, 1000, 3, past, "trace-alarm-2")
  const profileBefore: any = await env.BUILDER.rawStorage(actorId(OWNER)).get("profile")
  assert.equal(profileBefore.jobs.length, 2)
  await fireAlarmNow([env.BUILDER], true)
  await new Promise((resolve) => setTimeout(resolve, 30))
  const profileAfter: any = await env.BUILDER.rawStorage(actorId(OWNER)).get("profile")
  const requeued = profileAfter.jobs.filter((j: any) => j.type === "test_call")
  assert.equal(requeued.length, 1, "failed job is requeued")
  assert.equal(requeued[0].attempts, 1)
  assert.ok(requeued[0].at > Date.now() + 30000, "requeued at now + 60s x attempts")
})

test("builder: 10 concurrent messages all land", async () => {
  const env = makeEnv()
  const b = builder(env)
  await Promise.all(
    Array.from({ length: 10 }, (_, i) => b.addMessage("ag11111111", { caller_name: `Caller ${i}`, message: `msg ${i}`, from: "+1555000000" }, Date.now())),
  )
  const listed: any = await b.listMessages(false)
  assert.equal(listed.messages.length, 10)
  const ids = new Set(listed.messages.map((m: any) => m.id))
  assert.equal(ids.size, 10)
})

test("ops: metrics aggregate", async () => {
  const env = makeEnv()
  const ops = env.OPS.idFromName(actorId("global"))
  const now = Date.now()
  await ops.record("forge_variables", "ok", 20, now, "t1")
  await ops.record("forge_variables", "ok", 40, now, "t2")
  await ops.record("build_agent", "error", 900, now, "t3", "boom")
  await ops.record("forge_variables", "degraded", 30, now, "t4")
  const snap: any = await ops.snapshot(30, now)
  assert.equal(snap.requests, 4)
  assert.equal(snap.errors, 1)
  assert.equal(snap.error_rate, 0.25)
  assert.equal(snap.by_route.forge_variables.count, 3)
  assert.equal(snap.by_route.forge_variables.degraded, 1)
  assert.equal(snap.recent_errors.length, 2)
  assert.ok(snap.latency_p95_ms >= snap.latency_p50_ms)
})

test("gateway: auth and whitelist", async () => {
  const env = makeEnv("secret-token")
  const call = async (body: any, token?: string) => {
    const headers: Record<string, string> = { "content-type": "application/json" }
    if (token) headers.authorization = "Bearer " + token
    const resp = await (handler as any).fetch(
      new Request("http://x/rpc", { method: "POST", headers, body: JSON.stringify(body) }),
      env as any,
    )
    return { status: resp.status, json: await resp.json() }
  }
  assert.equal((await call({ actor: "ops", name: "global", method: "record", args: [] })).status, 401)
  assert.equal((await call({ actor: "ops", name: "global", method: "record", args: [] }, "wrong")).status, 401)
  const rejected = await call({ actor: "builder", name: "g", method: "takeDueJobs", args: [] }, "secret-token")
  assert.equal(rejected.status, 400)
  const ctor = await call({ actor: "builder", name: "g", method: "constructor", args: [] }, "secret-token")
  assert.equal(ctor.status, 400)
  const ok = await call({ actor: "builder", name: OWNER, method: "callInfo", args: ["call-9"] }, "secret-token")
  assert.equal(ok.status, 200)
  assert.equal(ok.json.ok, true)
})

test("gateway: exposes clinic and registry actors", async () => {
  const env = makeEnv("secret-token")
  const call = async (body: any) => {
    const resp = await (handler as any).fetch(
      new Request("http://x/rpc", {
        method: "POST",
        headers: { "content-type": "application/json", authorization: "Bearer secret-token" },
        body: JSON.stringify(body),
      }),
      env as any,
    )
    return { status: resp.status, json: await resp.json() }
  }
  const health = await (handler as any).fetch(new Request("http://x/health"), env as any)
  assert.equal(health.status, 200)
  const reg = await call({ actor: "registry", name: "global", method: "upsert", args: [{ key: "ag11111111", name: "Sam" }, Date.now()] })
  assert.equal(reg.status, 200)
  const list = await call({ actor: "registry", name: "global", method: "list", args: [50] })
  assert.equal(list.json.result.agents.length, 1)
  const taken = await call({ actor: "schedule", name: "fm-okafor", method: "taken", args: ["", ""] })
  assert.equal(taken.status, 200)
  const desk = await call({ actor: "desk", name: "global", method: "snapshot", args: [] })
  assert.equal(desk.status, 200)
  const patient = await call({ actor: "patient", name: OWNER, method: "list", args: ["2026-01-01T00:00"] })
  assert.equal(patient.status, 200)
  const badMethod = await call({ actor: "schedule", name: "fm-okafor", method: "destroyEverything", args: [] })
  assert.equal(badMethod.status, 400)
})

test("clinic schedule: concurrent booking of the same slot - one wins", async () => {
  const env = makeEnv()
  const s = env.SCHEDULE.idFromName(actorId("fm-okafor"))
  const slot = "2026-11-10T09:30"
  const nowSlot = "2026-11-07T10:00"
  const two = await Promise.all([
    s.book(slot, { id: "ap1", patient: "+15550000001" }, nowSlot, NOW),
    s.book(slot, { id: "ap2", patient: "+15550000002" }, nowSlot, NOW),
  ])
  assert.equal(two.filter((r: any) => r.ok).length, 1)
  assert.equal(two.filter((r: any) => r.reason === "taken").length, 1)
})

test("clinic schedule: duplicate, past, cancel ownership", async () => {
  const env = makeEnv()
  const s = env.SCHEDULE.idFromName(actorId("derm-raman"))
  const nowSlot = "2026-11-07T10:00"
  const past = await s.book("2026-11-06T15:00", { id: "ap0", patient: "+15550000001" }, nowSlot, NOW)
  assert.equal((past as any).reason, "past")
  const ok = await s.book("2026-11-10T10:00", { id: "ap1", patient: "+15550000001" }, nowSlot, NOW)
  assert.equal(ok.ok, true)
  const dup = await s.book("2026-11-13T10:00", { id: "ap2", patient: "+15550000001" }, nowSlot, NOW)
  assert.equal((dup as any).reason, "duplicate")
  assert.ok((dup as any).existing)
  const forbidden = await s.cancel("ap1", "+15550000002", NOW)
  assert.equal((forbidden as any).reason, "forbidden")
  const owner = await s.cancel("ap1", "+15550000001", NOW)
  assert.equal(owner.ok, true)
  const gone = await s.cancel("ap1", null, NOW)
  assert.equal((gone as any).reason, "not_found")
})

test("clinic patient: binding, limit, cancel", async () => {
  const env = makeEnv()
  const p = env.PATIENT.idFromName(actorId("+15550000001"))
  const unknown = await p.callInfo("call-x")
  assert.equal((unknown as any).known, false)
  const start: any = await p.startCall("+15550000001", "call-1", true, "2026-11-07T10:00", NOW)
  assert.equal(start.returning, false)
  const bound = await p.callInfo("call-1")
  assert.equal((bound as any).known, true)
  for (let i = 0; i < 3; i++) {
    const added = await p.addAppointment({ id: "apt" + i, slot: `2026-11-1${i}T10:00`, specialist_id: "fm-okafor" }, 3, "2026-11-07T10:00")
    assert.equal((added as any).ok, true)
  }
  const limit = await p.addAppointment({ id: "apt3", slot: "2026-11-12T10:00", specialist_id: "fm-okafor" }, 3, "2026-11-07T10:00")
  assert.equal((limit as any).reason, "limit")
  await p.cancelAppointment("apt1", NOW)
  const listed: any = await p.list("2026-11-07T10:00")
  assert.equal(listed.upcoming.length, 2)
})

test("registry + desk: concurrent writes land", async () => {
  const env = makeEnv()
  const reg = env.REGISTRY.idFromName(actorId("global"))
  const desk = env.DESK.idFromName(actorId("global"))
  await Promise.all(
    Array.from({ length: 20 }, (_, i) => reg.upsert({ key: "reg" + i, name: "Agent " + i, status: "active" }, NOW)),
  )
  const listed: any = await reg.list(500)
  assert.equal(listed.agents.length, 20)
  await Promise.all(
    Array.from({ length: 30 }, (_, i) => desk.log({ event: "booked", agent: i % 2 ? "a" : "b", phone: "+1555***0001" }, NOW)),
  )
  const deskSnap: any = await desk.snapshot()
  assert.equal(deskSnap.desk.activity.length, 30)
})

test("registry: upsert merges, keeps createdAt, builds++", async () => {
  const env = makeEnv()
  const reg = env.REGISTRY.idFromName(actorId("global"))
  const first: any = await reg.upsert({ key: "ag11111111", name: "Sam", status: "active" }, NOW)
  const created = first.agent.createdAt
  const second: any = await reg.upsert({ key: "ag11111111", name: "Sammy", status: "active" }, NOW + 100)
  assert.equal(second.agent.createdAt, created)
  assert.equal(second.agent.builds, 2)
  assert.equal(second.agent.name, "Sammy")
  const status: any = await reg.setStatus("ag11111111", "deleting", NOW + 200)
  assert.equal(status.agent.status, "deleting")
  const fetched: any = await reg.get("ag11111111")
  assert.equal(fetched.agent.status, "deleting")
})


test("actor ids: alarm-safe RFC 1123 labels, reversible and collision-free", async () => {
  const label = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/
  for (const name of ["+15550102001", "global", "fm-okafor", "abc12345/+15550102001", "Foo"]) {
    assert.match(actorId(name), label, name)
  }
  assert.equal(actorId("+15550102001"), "a-2b15550102001")
  assert.notEqual(actorId("a-b"), actorId("a_b"))
  // the strict shim rejects an alarm on a raw phone-number id, exactly like the platform
  const env = makeEnv("t")
  const raw = env.BUILDER.idFromName("+15550109999")
  await assert.rejects(raw.saveDraft({ set: { business_name: "X" } }, Date.now()), /RFC 1123/)
})
