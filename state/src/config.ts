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
