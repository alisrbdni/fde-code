/** Single-line JSON logs with a fixed service name. */
export function log(level: "debug" | "info" | "warn" | "error", event: string, fields: Record<string, any> = {}): void {
  const line = JSON.stringify({ ts: Date.now(), level, event, service: "forge-state", ...fields })
  if (level === "warn" || level === "error") {
    console.error(line)
  } else {
    console.log(line)
  }
}
