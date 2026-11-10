/**
 * Read a setting the way the platform may provide it: as a direct env property (local dev server),
 * through the secrets binding (`env.SECRETS.get(binding)`, per the Edge docs), or as a process
 * environment variable (`[env_vars]` in func.toml). The first non-empty value wins.
 */
export async function setting(env: any, name: string): Promise<string> {
  const direct = env?.[name]
  if (typeof direct === "string" && direct) return direct
  try {
    const fromSecrets = await env?.SECRETS?.get?.(name)
    if (typeof fromSecrets === "string" && fromSecrets) return fromSecrets
  } catch {
    // no secrets binding, or the secret isn't set
  }
  const proc = (globalThis as any).process
  const fromProcess = proc?.env?.[name]
  return typeof fromProcess === "string" ? fromProcess : ""
}

/**
 * Map any actor name (a phone number, "global", "fm-okafor", a test prefix) to an id the platform accepts.
 * Actor alarms are stored as reminders named after the instance id, and that name must be a lowercase
 * RFC 1123 label: "+15550100556" is rejected. Lowercase letters and digits pass through; every other
 * character, '-' included, becomes "-" plus two hex digits, so the mapping is reversible and collision-free.
 * The leading "a" guarantees the label starts with a letter.
 */
export function actorId(name: string): string {
  let out = "a"
  for (const ch of name) {
    if (/[a-z0-9]/.test(ch)) out += ch
    else out += "-" + ch.codePointAt(0)!.toString(16).padStart(2, "0")
  }
  return out
}

export const RFC1123_LABEL = /^[a-z0-9]([-a-z0-9]*[a-z0-9])?$/
