/**
 * Clinic actors: ClinicSchedule (per clinician - atomic "free? then take it"
 * booking), ClinicPatient (per phone - appointment list + upcoming limit)
 * and ClinicDesk ("global" - callbacks + activity feed for the front desk).
 */
import { StatefulActor } from "@telnyx/edge-runtime"

const hex = (n: number): string =>
  Array.from({ length: n }, () => "0123456789abcdef"[Math.floor(Math.random() * 16)]).join("")

export interface Booking {
  id: string
  patient: string
  [k: string]: any
}

export class ClinicSchedule extends StatefulActor {
  /** bookings: one slot -> one booking (atomic "free? then take it") */
  private async load(): Promise<Record<string, any>> {
    const stored = await this.ctx.storage.get("bookings")
    return stored && typeof stored === "object" ? (stored as Record<string, any>) : {}
  }

  private async save(bookings: Record<string, any>): Promise<void> {
    await this.ctx.storage.put("bookings", bookings)
  }

  async taken(from: string, to: string) {
    const bookings = await this.load()
    void from
    const slots = Object.keys(bookings).filter((s) => (!from || s >= from) && (!to || s <= to)).sort()
    return { ok: true, slots }
  }

  async book(slot: string, booking: Booking, nowSlot: string, now: number) {
    const bookings = await this.load()
    if (slot < nowSlot) {
      return { ok: false, reason: "past" }
    }
    if (bookings[slot]) {
      return { ok: false, reason: "taken" }
    }
    const existing = Object.entries(bookings).find(
      ([s, b]: [string, any]) => b.patient === booking.patient && s >= nowSlot,
    )
    if (existing) {
      return { ok: false, reason: "duplicate", existing: existing[1] }
    }
    const full = { ...booking, slot, status: "booked", createdAt: now }
    bookings[slot] = full
    await this.save(bookings)
    return { ok: true, booking: full }
  }

  async cancel(id: string, patient: string | null, now: number) {
    const bookings = await this.load()
    const entry = Object.entries(bookings).find(([, b]: [string, any]) => b.id === id)
    if (!entry) {
      return { ok: false, reason: "not_found" }
    }
    if (patient !== null && entry[1].patient !== patient) {
      return { ok: false, reason: "forbidden" }
    }
    const [slot, booking] = entry
    delete bookings[slot]
    await this.save(bookings)
    return { ok: true, booking }
  }

  async list(from: string, to: string) {
    const bookings = await this.load()
    const items = Object.entries(bookings)
      .filter(([s]) => (!from || s >= from) && (!to || s <= to))
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([slot, b]) => ({ ...b, slot }))
    return { ok: true, bookings: items }
  }

  async snapshot() {
    const bookings = await this.load()
    return { ok: true, bookings }
  }
}

export interface PatientAppointment {
  id: string
  [k: string]: any
}

export class ClinicPatient extends StatefulActor {
  private async load() {
    const stored = await this.ctx.storage.get("patient")
    if (stored) return stored as any
    return {
      phone: this.ctx.id,
      name: "",
      calls: 0,
      attested: {} as Record<string, boolean>,
      appointments: [] as PatientAppointment[],
      lastTriage: null as any,
    }
  }

  private async save(state: any): Promise<void> {
    await this.ctx.storage.put("patient", state)
  }

  async startCall(phone: string, callId: string, attested: boolean, nowSlot: string, now: number) {
    const s = await this.load()
    s.phone = phone
    s.calls += 1
    void now
    if (callId) {
      s.attested[callId] = !!attested
      const ids = Object.keys(s.attested)
      if (ids.length > 10) {
        for (const id of ids.slice(0, ids.length - 10)) delete s.attested[id]
      }
    }
    const upcoming = (s.appointments || []).filter((a: any) => a.status === "booked" && a.slot >= nowSlot)
    const result = { ok: true, returning: s.calls > 1, calls: s.calls, name: s.name, upcoming }
    await this.save(s)
    return result
  }

  async callInfo(callId: string) {
    const s = await this.load()
    return { known: Object.prototype.hasOwnProperty.call(s.attested, callId), attested: !!s.attested[callId] }
  }

  async setName(name: string) {
    const s = await this.load()
    s.name = name || ""
    await this.save(s)
    return { ok: true, name: s.name }
  }

  async recordTriage(triage: Record<string, any>) {
    const s = await this.load()
    s.lastTriage = triage
    await this.save(s)
    return { ok: true }
  }

  async addAppointment(appt: PatientAppointment, maxUpcoming: number, nowSlot: string) {
    const s = await this.load()
    const list = Array.isArray(s.appointments) ? s.appointments : []
    const upcoming = list.filter((a: any) => a.status === "booked" && a.slot >= nowSlot)
    if (upcoming.length >= maxUpcoming) {
      return { ok: false, reason: "limit" }
    }
    const full = { ...appt, status: "booked", createdAt: nowSlot }
    list.unshift(full)
    if (list.length > 20) list.length = 20
    s.appointments = list
    const patientName = (appt as any).patient_name || (appt as any).name
    if (patientName && !s.name) s.name = patientName
    await this.save(s)
    return { ok: true, appointment: full }
  }

  async cancelAppointment(id: string, now: number) {
    const s = await this.load()
    const list = Array.isArray(s.appointments) ? s.appointments : []
    const appt = list.find((a: any) => a.id === id)
    if (!appt) {
      return { ok: false, reason: "not_found" }
    }
    appt.status = "cancelled"
    appt.cancelledAt = now
    await this.save(s)
    return { ok: true, appointment: appt }
  }

  async list(nowSlot: string) {
    const s = await this.load()
    const list = Array.isArray(s.appointments) ? s.appointments : []
    const upcoming = list.filter((a: any) => a.status === "booked" && a.slot >= nowSlot)
    return { ok: true, upcoming, all: list, name: s.name }
  }

  async snapshot() {
    const s = await this.load()
    return { ok: true, patient: s }
  }
}

export class ClinicDesk extends StatefulActor {
  private async load() {
    const stored = await this.ctx.storage.get("desk")
    if (stored) return stored as { callbacks: any[]; activity: any[] }
    return { callbacks: [], activity: [] }
  }

  private async save(desk: { callbacks: any[]; activity: any[] }): Promise<void> {
    await this.ctx.storage.put("desk", desk)
  }

  async addCallback(rec: Record<string, any>, now: number) {
    const desk = await this.load()
    const callback = { id: "cb" + hex(8), at: now, done: false, ...rec }
    desk.callbacks.unshift(callback)
    if (desk.callbacks.length > 100) desk.callbacks.length = 100
    await this.save(desk)
    return { ok: true, callback }
  }

  async resolveCallback(id: string, now: number) {
    const desk = await this.load()
    const callback = desk.callbacks.find((c: any) => c.id === id)
    if (!callback) {
      return { ok: false, reason: "not_found" }
    }
    callback.done = true
    callback.resolvedAt = now
    await this.save(desk)
    return { ok: true, callback }
  }

  async log(event: Record<string, any>, now: number) {
    const desk = await this.load()
    desk.activity.unshift({ at: now, ...event })
    if (desk.activity.length > 100) desk.activity.length = 100
    await this.save(desk)
    return { ok: true }
  }

  async snapshot() {
    const desk = await this.load()
    return { ok: true, desk }
  }
}

export default { ClinicSchedule, ClinicPatient, ClinicDesk }
