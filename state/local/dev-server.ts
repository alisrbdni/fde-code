/** Local dev server: runs the real fetch handler from src/index.ts on Node. */
import http from "node:http"
import { makeEnv } from "./env"
import handler from "../src/index"

const PORT = Number(process.env.PORT || 8787)
const env = makeEnv(process.env.STATE_TOKEN || "local-state-token", process.env.API_URL || "http://localhost:8080")

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://localhost:${PORT}`)
  const chunks: Buffer[] = []
  for await (const chunk of req) chunks.push(chunk as Buffer)
  const body = Buffer.concat(chunks)
  const headers = new Headers()
  for (const [name, value] of Object.entries(req.headers)) {
    headers.set(name, Array.isArray(value) ? value.join(", ") : String(value || ""))
  }
  const request = new Request(`http://localhost:${PORT}${url.pathname}${url.search}`, {
    method: req.method,
    headers,
    body: ["GET", "HEAD"].includes(req.method || "") ? undefined : (body as any),
  })
  try {
    const response = await handler.fetch(request, env as any)
    const responseBody = Buffer.from(await response.arrayBuffer())
    res.writeHead(response.status, Object.fromEntries(response.headers.entries()))
    res.end(responseBody)
  } catch (exc: any) {
    res.writeHead(500, { "content-type": "application/json" })
    res.end(JSON.stringify({ ok: false, error: String(exc) }))
  }
})

server.listen(PORT, () => {
  console.log(`forge-state dev server listening on http://localhost:${PORT}`)
})
