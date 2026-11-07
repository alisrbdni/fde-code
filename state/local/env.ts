/** Local-only: build an Env with all six actor bindings. */
import { actorNamespace, type ActorNamespace } from "./edge-runtime-shim"
import { BuilderProfile } from "../src/actors/builder-profile"
import { OpsMetrics } from "../src/actors/ops-metrics"
import { AgentRegistry } from "../src/actors/agent-registry"
import { ClinicSchedule, ClinicPatient, ClinicDesk } from "../src/actors/clinic"

export interface LocalEnv {
  API_URL: string
  STATE_TOKEN: string
  BUILDER: ActorNamespace<BuilderProfile>
  OPS: ActorNamespace<OpsMetrics>
  REGISTRY: ActorNamespace<AgentRegistry>
  SCHEDULE: ActorNamespace<ClinicSchedule>
  PATIENT: ActorNamespace<ClinicPatient>
  DESK: ActorNamespace<ClinicDesk>
}

export function makeEnv(stateToken = "local-state-token", apiUrl = "http://localhost:8080"): LocalEnv {
  const env: Record<string, any> = {
    API_URL: apiUrl,
    STATE_TOKEN: stateToken,
  }
  // actors receive the same env object, so one actor can reach another
  // through its binding (BuilderProfile.alarm -> OPS, for example).
  env.BUILDER = actorNamespace(BuilderProfile, env)
  env.OPS = actorNamespace(OpsMetrics, env)
  env.REGISTRY = actorNamespace(AgentRegistry, env)
  env.SCHEDULE = actorNamespace(ClinicSchedule, env)
  env.PATIENT = actorNamespace(ClinicPatient, env)
  env.DESK = actorNamespace(ClinicDesk, env)
  return env as LocalEnv
}

export function allNamespaces(env: LocalEnv) {
  return [env.BUILDER, env.OPS, env.REGISTRY, env.SCHEDULE, env.PATIENT, env.DESK]
}
