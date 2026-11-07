/**
 * Local-only shim for `@telnyx/edge-runtime` Stateful Actors.
 *
 * Mirrors the pieces our actors use: `StatefulActor` base class with
 * `ctx: { id, storage }` + `env`, storage with get/put/delete/list and
 * alarms (setAlarm/getAlarm), and actor namespaces whose instances have
 * **per-instance serialised method calls** (a promise queue) with
 * structured-clone semantics for args/results — the same guarantees the
 * platform gives us.
 */

export interface ActorCtx {
  id: string
  storage: MemoryStorage
}

export class StatefulActor {
  ctx: ActorCtx
  env: any

  constructor(ctx: ActorCtx, env: any) {
    this.ctx = ctx
    this.env = env
  }
}

const clone = (value: any): any =>
  typeof structuredClone === "function" ? structuredClone(value) : JSON.parse(JSON.stringify(value))

const ids = (): string => Math.random().toString(16).slice(2, 12)

export class MemoryStorage {
  private data = new Map<string, any>()
  public alarmAt: number | null = null
  private alarmTimer: any = null
  /** wired by actorNamespace: runs the instance's alarm() through its queue */
  onAlarm: (() => void) | null = null

  async get(key: string): Promise<any> {
    const value = this.data.get(key)
    return value === undefined ? undefined : clone(value)
  }

  async put(key: string, value: any): Promise<void> {
    this.data.set(key, clone(value))
  }

  async delete(key: string): Promise<boolean> {
    return this.data.delete(key)
  }

  async list(): Promise<string[]> {
    return [...this.data.keys()]
  }

  /** Wire a manual alarm trigger for tests / dev (runs regardless of due time when forced). */
  setAlarm(at: number | null): void {
    this.alarmAt = at
    if (this.alarmTimer) {
      clearTimeout(this.alarmTimer)
      this.alarmTimer = null
    }
    if (at !== null && at !== undefined && this.onAlarm) {
      const delay = Math.max(0, at - Date.now())
      this.alarmTimer = setTimeout(() => {
        if (this.onAlarm) this.onAlarm()
      }, delay)
      if (typeof this.alarmTimer.unref === "function") this.alarmTimer.unref()
    }
  }

  getAlarm(): number | null {
    return this.alarmAt
  }
}

interface Entry {
  instance: any
  queue: Promise<any>
  storage: MemoryStorage
}

export interface ActorNamespace<T> {
  idFromName(name: string): T
  /** test helper: raw storage of one instance */
  rawStorage(name: string): MemoryStorage
  __instances: Map<string, Entry>
}

export function actorNamespace<Cls extends new (ctx: ActorCtx, env: any) => any>(
  Cls: Cls,
  env: any,
): ActorNamespace<InstanceType<Cls>> {
  const instances = new Map<string, Entry>()

  const entryFor = (name: string): Entry => {
    let entry = instances.get(name)
    if (!entry) {
      const storage = new MemoryStorage()
      const instance = new Cls({ id: name, storage }, env)
      entry = { instance, queue: Promise.resolve(), storage }
      storage.onAlarm = () => {
        // run through the same serialised queue as every other call
        const run = async () => {
          if (typeof instance.alarm === "function") {
            await instance.alarm()
          }
        }
        entry!.queue = entry!.queue.then(run, run)
      }
      instances.set(name, entry)
    }
    return entry
  }

  const ns: any = {
    idFromName(name: string) {
      const entry = entryFor(name)
      const stub: any = {}
      return new Proxy(stub, {
        get(_target, prop: string) {
          if (typeof (entry.instance as any)[prop] === "function") {
            const fn = (entry.instance as any)[prop].bind(entry.instance)
            return (...args: any[]) => {
              const call = () => Promise.resolve().then(() => fn(...clone(args)))
              const result = entry.queue.then(call, call)
              entry.queue = result.then(
                () => undefined,
                () => undefined,
              )
              return result.then((value: any) => clone(value))
            }
          }
          return (entry.instance as any)[prop]
        },
      }) as InstanceType<Cls>
    },
    rawStorage(name: string) {
      return entryFor(name).storage
    },
    __instances: instances,
  }
  return ns
}

/** Fire every due alarm (alarmAt <= now) across all given namespaces. */
export async function fireAlarmNow(namespaces: ActorNamespace<any>[], force = false): Promise<void> {
  const now = Date.now()
  for (const ns of namespaces) {
    for (const entry of ns.__instances.values()) {
      if (entry.storage.alarmAt !== null && (force || entry.storage.alarmAt <= now)) {
        const at = entry.storage.alarmAt
        entry.storage.alarmAt = null
        const run = async () => {
          if (typeof (entry.instance as any).alarm === "function") {
            await (entry.instance as any).alarm()
          }
        }
        entry.queue = entry.queue.then(run, run)
        await entry.queue
        if (entry.storage.alarmAt === null && at !== null) {
          // alarm() that found nothing may not re-arm; leave cleared
        }
      }
    }
  }
}
