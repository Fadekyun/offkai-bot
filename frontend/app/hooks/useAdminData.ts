'use client'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Attendee, AdminFilter, CheckinRecord } from '../lib/types'
import { matchesCheckin } from '../lib/checkin'

const groupSize = (a: Attendee) => 1 + (a.extra_people ?? 0)

export type NamesSaveOutcome = 'saved' | 'conflict' | 'invalid' | 'unconfirmed' | 'error' | 'stale'
export type NamesExpected = { display_name: string | null; extra_people: number; extras_names: (string | null)[] }
export type NamesChanges = { display_name?: string; extras_names?: { index: number; value: string }[] }

// Owns admin auth + the selected event's attendees/check-ins, the manual
// check-in/out actions, and the filtered/derived view data. The scanner hook is
// kept separate and feeds successful scans back via `applyScanCheckin`.
export function useAdminData() {
  const selectedEventRef = useRef('')
  const eventGeneration = useRef(0)
  // Invalidates an in-flight attendee fetch when a name save crosses it, so
  // pre-save polling data cannot replace the names that were just persisted.
  const attendeeLoadId = useRef(0)
  const attendeesRef = useRef<Attendee[]>([])
  const [key, setKey] = useState('')
  const [authed, setAuthed] = useState(false)
  const [keyInput, setKeyInput] = useState('')
  const [loginError, setLoginError] = useState('')
  const [eventName, setEventName] = useState('')
  const [events, setEvents] = useState<{ event_name: string; event_datetime: string | null; open: boolean }[]>([])
  const [selectedEvent, setSelectedEvent] = useState('')
  const [attendees, setAttendees] = useState<Attendee[]>([])
  const [checkins, setCheckins] = useState<Record<string, CheckinRecord>>({})
  const [filter, setFilter] = useState<AdminFilter>('all')
  const [search, setSearch] = useState('')
  // Rows the admin just checked in/out — kept visible regardless of the active
  // filter so a mistaken action can be undone in place (e.g. on the Pending tab).
  const [stickyIds, setStickyIds] = useState<Set<string>>(new Set())
  const [actionWarnings, setActionWarnings] = useState<string[]>([])

  useEffect(() => { attendeesRef.current = attendees }, [attendees])

  const loadCheckins = useCallback(async (adminKey: string, ev: string) => {
    const generation = eventGeneration.current
    if (selectedEventRef.current !== ev) return
    const evParam = ev ? `&event=${encodeURIComponent(ev)}` : ''
    const res = await fetch(`/api/checkin?key=${encodeURIComponent(adminKey)}${evParam}`)
    if (!res.ok) return
    const chk: CheckinRecord[] = await res.json()
    if (generation !== eventGeneration.current || selectedEventRef.current !== ev) return
    setCheckins(Object.fromEntries(chk.map(c => [c.user_id, c])))
  }, [])

  const loadAttendees = useCallback(async (adminKey: string, ev: string) => {
    const generation = eventGeneration.current
    if (selectedEventRef.current !== ev) return false
    const loadId = ++attendeeLoadId.current
    const evParam = ev ? `&event=${encodeURIComponent(ev)}` : ''
    const res = await fetch(`/api/attendees?key=${encodeURIComponent(adminKey)}${evParam}`)
    if (!res.ok) return false
    const { event_name, attendees: att } = await res.json()
    if (loadId !== attendeeLoadId.current || generation !== eventGeneration.current ||
        selectedEventRef.current !== ev) return false
    setEventName(event_name)
    attendeesRef.current = att
    setAttendees(att)
    return true
  }, [])

  // Initial load after login: fetch event list + default, then its data.
  const initialLoad = useCallback(async (adminKey: string) => {
    const evRes = await fetch(`/api/events?key=${encodeURIComponent(adminKey)}`)
    if (!evRes.ok) return false
    const { events: evs, default_event_name } = await evRes.json()
    const startEvent: string = default_event_name || (evs[0]?.event_name ?? '')
    setEvents(evs)
    selectedEventRef.current = startEvent
    eventGeneration.current += 1
    setSelectedEvent(startEvent)
    const ok = await loadAttendees(adminKey, startEvent)
    if (!ok) return false
    await loadCheckins(adminKey, startEvent)
    return true
  }, [loadAttendees, loadCheckins])

  const handleLogin = useCallback(async () => {
    const ok = await initialLoad(keyInput)
    if (ok) { setKey(keyInput); setAuthed(true); setLoginError('') }
    else setLoginError('Invalid key. Check with the event host.')
  }, [keyInput, initialLoad])

  // Refresh attendee list + check-ins whenever the selected event changes.
  useEffect(() => {
    if (!authed || !selectedEvent) return
    let cancelled = false
    const run = async () => {
      await loadAttendees(key, selectedEvent)
      if (!cancelled) await loadCheckins(key, selectedEvent)
    }
    run()
    return () => { cancelled = true }
  }, [authed, selectedEvent, key, loadAttendees, loadCheckins])

  // Poll check-ins for the selected event every 10s.
  useEffect(() => {
    if (!authed || !selectedEvent) return
    const id = setInterval(() => {
      loadCheckins(key, selectedEvent)
      loadAttendees(key, selectedEvent)
    }, 10_000)
    return () => clearInterval(id)
  }, [authed, selectedEvent, key, loadCheckins, loadAttendees])

  // Switch the viewed event (resets transient view state at the source, not in
  // an effect, per react-hooks guidance).
  const changeEvent = useCallback((next: string) => {
    selectedEventRef.current = next
    eventGeneration.current += 1
    setSelectedEvent(next)
    setStickyIds(new Set())
    setActionWarnings([])
  }, [])

  const changeFilter = useCallback((next: AdminFilter) => {
    setFilter(next)
    setStickyIds(new Set())
  }, [])

  // Manual check-in (admin button) — no QR needed, admin is already authed.
  const manualCheckin = useCallback(async (userId: string, timestamp: string) => {
    if (!eventName || eventName !== selectedEvent || selectedEventRef.current !== eventName ||
        !attendeesRef.current.some(a => a.status === 'attending' && a.user_id === userId && a.registration_timestamp === timestamp)) return
    const generation = eventGeneration.current
    const res = await fetch(`/api/checkin?key=${encodeURIComponent(key)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, event_name: eventName, registration_timestamp: timestamp }),
    })
    const data = await res.json().catch(() => ({}))
    if (generation !== eventGeneration.current || selectedEventRef.current !== eventName) return
    if (!attendeesRef.current.some(a => a.user_id === userId && a.registration_timestamp === timestamp)) return
    if (res.ok && matchesCheckin(data.record, eventName, userId, timestamp)) {
      setCheckins(prev => ({ ...prev, [userId]: data.record }))
      setStickyIds(prev => new Set(prev).add(userId))
    } else if (res.status === 409) {
      await loadAttendees(key, eventName)
      await loadCheckins(key, eventName)
    }
  }, [key, selectedEvent, eventName, loadAttendees, loadCheckins])

  // Manual check-out (admin button) — removes the check-in for this event.
  // Uses POST with action=checkout because some proxies reject the DELETE method.
  const manualCheckout = useCallback(async (userId: string, timestamp: string) => {
    if (!eventName || eventName !== selectedEvent || selectedEventRef.current !== eventName ||
        !attendeesRef.current.some(a => a.status === 'attending' && a.user_id === userId && a.registration_timestamp === timestamp)) return
    const generation = eventGeneration.current
    const res = await fetch(`/api/checkin?key=${encodeURIComponent(key)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'checkout', user_id: userId, event_name: eventName, registration_timestamp: timestamp }),
    })
    if (generation !== eventGeneration.current || selectedEventRef.current !== eventName) return
    if (!attendeesRef.current.some(a => a.user_id === userId && a.registration_timestamp === timestamp)) return
    if (res.ok) {
      setCheckins(prev => {
        if (!matchesCheckin(prev[userId], eventName, userId, timestamp)) return prev
        const next = { ...prev }
        delete next[userId]
        return next
      })
      setStickyIds(prev => new Set(prev).add(userId))
    } else if (res.status === 409) {
      await loadAttendees(key, eventName)
      await loadCheckins(key, eventName)
    }
  }, [key, selectedEvent, eventName, loadAttendees, loadCheckins])

  const updatePayment = useCallback(async (userId: string, paid: boolean, timestamp: string) => {
    // An event switch can leave the previous rows visible while the new list loads.
    if (!eventName || eventName !== selectedEvent || selectedEventRef.current !== eventName) return false
    const generation = eventGeneration.current
    const res = await fetch('/api/payment?key=' + encodeURIComponent(key), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, event_name: eventName, paid, registration_timestamp: timestamp }),
    })
    const result = await res.json().catch(() => ({}))
    if (!res.ok) {
      if (res.status === 409) await loadAttendees(key, eventName)
      return false
    }
    await loadAttendees(key, selectedEvent)
    if (generation === eventGeneration.current && selectedEventRef.current === eventName) {
      setActionWarnings(result.warning ? [result.warning] : [])
    }
    return true
  }, [key, selectedEvent, eventName, loadAttendees])

  // Persist only the edited name fields using the raw snapshot captured when the
  // editor opened. The bot rechecks that snapshot immediately before its atomic
  // write; polling never updates this snapshot. A lost response refreshes to
  // confirm, and the mutation is never retried automatically.
  const saveAttendeeNames = useCallback(async (
    requestedEvent: string,
    userId: string,
    timestamp: string,
    expected: NamesExpected,
    changes: NamesChanges,
  ): Promise<NamesSaveOutcome> => {
    if (!eventName || eventName !== selectedEvent || selectedEventRef.current !== eventName ||
        requestedEvent !== eventName) return 'stale'
    if (!attendeesRef.current.some(a => a.status === 'attending' && a.user_id === userId &&
        a.registration_timestamp === timestamp)) return 'stale'
    const generation = eventGeneration.current
    const stillCurrent = () => generation === eventGeneration.current && selectedEventRef.current === eventName
    let response: Response | undefined
    let result: {
      saved?: boolean
      error?: string
      registration?: { user_id?: string; registration_timestamp?: string; display_name?: string | null
        extra_people?: number; extras_names?: string[] }
    } = {}
    try {
      response = await fetch('/api/registration/names?key=' + encodeURIComponent(key), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event_name: eventName, user_id: userId, registration_timestamp: timestamp,
          expected, changes }),
      })
      result = await response.json().catch(() => ({}))
    } catch {
      // A timeout can follow a committed save. Refresh to confirm; never retry it automatically.
    }
    if (!stillCurrent()) return 'stale'

    // A refresh failure or timeout must never turn a confirmed save into a reported failure.
    const refresh = async () => {
      let timer: ReturnType<typeof setTimeout> | undefined
      try {
        await Promise.race([
          loadAttendees(key, eventName),
          new Promise<void>(resolve => { timer = setTimeout(resolve, 10_000) }),
        ])
      } catch { /* keep the confirmed result */ } finally {
        if (timer) clearTimeout(timer)
      }
    }
    const registration = result.registration
    const returnedDisplayName = registration?.display_name
    const returnedExtrasNames = registration?.extras_names
    const validRegistration = !!registration &&
      registration.user_id === userId &&
      typeof registration.registration_timestamp === 'string' &&
      Date.parse(registration.registration_timestamp) === Date.parse(timestamp) &&
      registration.extra_people === expected.extra_people &&
      (returnedDisplayName === null || typeof returnedDisplayName === 'string') &&
      Array.isArray(returnedExtrasNames) &&
      returnedExtrasNames.every(name => typeof name === 'string')
    if (response?.ok && result.saved === true && validRegistration &&
        (returnedDisplayName === null || typeof returnedDisplayName === 'string') &&
        Array.isArray(returnedExtrasNames)) {
      // Invalidate any poll that began before this confirmed write, then show the
      // persisted names immediately even if the follow-up refresh fails.
      attendeeLoadId.current += 1
      const names: Pick<Attendee, 'display_name' | 'extras_names'> = {
        display_name: returnedDisplayName,
        extras_names: [...returnedExtrasNames],
      }
      const applyNames = (list: Attendee[]) => list.map(a =>
        a.status === 'attending' && a.user_id === userId && a.registration_timestamp === timestamp
          ? { ...a, ...names }
          : a)
      setAttendees(applyNames)
      attendeesRef.current = applyNames(attendeesRef.current)
      await refresh()
      return stillCurrent() ? 'saved' : 'stale'
    }
    if (response?.status === 400) return 'invalid'
    if (response?.status === 401) return 'error'
    if (response?.status === 500 && result.error === 'write_failed') return 'error'
    if (response?.status === 409 || response?.status === 404) {
      await refresh()
      return stillCurrent() ? 'conflict' : 'stale'
    }
    // Unknown or ambiguous transport outcome: refresh without claiming a result.
    await refresh()
    return stillCurrent() ? 'unconfirmed' : 'stale'
  }, [key, selectedEvent, eventName, loadAttendees])

  const removeRegistration = useCallback(async (userId: string, timestamp: string) => {
    if (!eventName || eventName !== selectedEvent || selectedEventRef.current !== eventName) return false
    const generation = eventGeneration.current
    const response = await fetch('/api/registration/remove?key=' + encodeURIComponent(key), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, event_name: eventName, registration_timestamp: timestamp }),
    })
    const result = await response.json().catch(() => ({}))
    // Refresh even after an ambiguous failure; a persisted removal cannot be retried blindly.
    await loadAttendees(key, eventName)
    if (generation === eventGeneration.current && selectedEventRef.current === eventName) {
      setActionWarnings(response.ok ? result.warnings ?? [] : [])
    }
    if (response.status === 409) throw new Error('registration_changed')
    if (!response.ok && result.removed === false) throw new Error('not_removed')
    return response.ok && result.removed === true
  }, [key, selectedEvent, eventName, loadAttendees])

  const promoteRegistration = useCallback(async (userId: string, timestamp: string) => {
    if (!eventName || eventName !== selectedEvent || selectedEventRef.current !== eventName) return false
    const generation = eventGeneration.current
    let response: Response | undefined
    let result: { warnings?: string[]; promoted?: boolean } = {}
    try {
      response = await fetch('/api/registration/promote?key=' + encodeURIComponent(key), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id: userId, event_name: eventName, registration_timestamp: timestamp }),
      })
      result = await response.json().catch(() => ({}))
    } finally {
      // A timeout can follow a committed promotion. Refresh; never retry it automatically.
      await loadAttendees(key, eventName).catch(() => false)
    }
    if (generation === eventGeneration.current && selectedEventRef.current === eventName) {
      setActionWarnings(response?.ok ? result.warnings ?? [] : [])
    }
    if (response?.status === 409) throw new Error('registration_changed')
    if (!response?.ok && result.promoted === false) throw new Error('not_promoted')
    return response?.ok === true && result.promoted === true
  }, [key, selectedEvent, eventName, loadAttendees])

  // Record a successful camera scan into the check-in map (no sticky — scans
  // aren't undo-in-place like the manual buttons).
  const applyScanCheckin = useCallback((record: CheckinRecord) => {
    if (eventName !== selectedEvent || selectedEventRef.current !== eventName ||
        !attendeesRef.current.some(a => a.status === 'attending' &&
          matchesCheckin(record, eventName, a.user_id, a.registration_timestamp))) return
    setCheckins(prev => ({ ...prev, [record.user_id]: record }))
  }, [eventName, selectedEvent])

  // Header counters reflect physical people, not RSVP records: each attending
  // record is the primary attendee plus their extra_people guests, who check in
  // as one group via the primary's single QR / manual action (issue #80).
  const attending = attendees.filter(a => a.status === 'attending')
  // Polls and mutations can straddle a registration replacement independently.
  // Never expose an old record to rows, filters, or group-size counters.
  const currentCheckins = Object.fromEntries(attending
    .filter(a => matchesCheckin(checkins[a.user_id], eventName, a.user_id, a.registration_timestamp))
    .map(a => [a.user_id, checkins[a.user_id]]))
  const attendingCount = attending.reduce((total, a) => total + groupSize(a), 0)
  const checkedInCount = attending
    .filter(a => currentCheckins[a.user_id])
    .reduce((total, a) => total + groupSize(a), 0)
  const pendingCount = attendingCount - checkedInCount
  const waitlist = attendees.filter(a => a.status === 'waitlist')

  const filtered = attending
    .filter(a => {
      if (stickyIds.has(a.user_id)) return true
      if (filter === 'checked') return !!currentCheckins[a.user_id]
      if (filter === 'pending') return !currentCheckins[a.user_id]
      return true
    })
    .filter(a => {
      if (!search) return true
      const q = search.toLowerCase().replace(/^[@#]/, '')
      const numbers = [a.attendee_number, ...(a.extras_attendee_numbers ?? [])].filter(n => n != null).map(String)
      return [a.display_name || '', a.username, a.user_id, ...a.drinks, ...a.extras_names, ...numbers]
        .some(v => v.toLowerCase().includes(q))
    })

  return {
    key, authed, keyInput, setKeyInput, loginError, handleLogin,
    eventName, events, selectedEvent, changeEvent,
    updatePayment, attendees, checkins: currentCheckins, applyScanCheckin, manualCheckin, manualCheckout,
    saveAttendeeNames, removeRegistration, promoteRegistration, actionWarnings,
    filter, changeFilter, search, setSearch,
    filtered, waitlist, pendingCount, checkedInCount, attendingCount,
    waitlistCount: waitlist.length,
  }
}
