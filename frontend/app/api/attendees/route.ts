import { NextRequest, NextResponse } from 'next/server'
import { readEvents, readResponses, getDefaultEvent, isSelectableForCheckin } from '../db'
import { paymentFromSnapshot, readPaymentSnapshot, registrationTimestamp } from '../payments'
import { paymentEnabled, paymentMethodRequired } from '../payment-access'
import { parseEventParam } from '../validation'
import { namesRevision } from '../../lib/namesRevision'
import { MOCK_EVENTS, MOCK_ATTENDEES } from '../mock'

const MOCK_MODE = process.env.MOCK_MODE === 'true'
const ADMIN_KEY = process.env.ADMIN_KEY ?? ''

export async function GET(request: NextRequest) {
  const key = request.nextUrl.searchParams.get('key')
  if (!ADMIN_KEY || key !== ADMIN_KEY) {
    return NextResponse.json({ error: 'unauthorized' }, { status: 401 })
  }

  const requestedEvent = parseEventParam(request.nextUrl.searchParams.get('event'))
  if (requestedEvent === false) {
    return NextResponse.json({ error: 'invalid_event' }, { status: 400 })
  }

  if (MOCK_MODE) {
    const activeEvent = requestedEvent
      ? MOCK_EVENTS.find(e => e.event_name === requestedEvent && isSelectableForCheckin(e))
      : getDefaultEvent(MOCK_EVENTS)
    if (!activeEvent) {
      if (requestedEvent) return NextResponse.json({ error: 'event_not_found' }, { status: 404 })
      return NextResponse.json({ event_name: 'No Active Event', attendees: [] })
    }
    const eventName = activeEvent.event_name
    if (!MOCK_ATTENDEES[eventName]) {
      return NextResponse.json({ error: 'event_not_found' }, { status: 404 })
    }
    return NextResponse.json({
      event_name: eventName,
      attendees: MOCK_ATTENDEES[eventName].map(a => ({
        ...a,
        names_revision: namesRevision(a.display_name ?? null, a.extra_people ?? 0, a.extras_names ?? []),
      })),
    })
  }

  const events = readEvents()

  // Resolve which event to show: an explicit (validated) selection, else default.
  let activeEvent
  if (requestedEvent) {
    activeEvent = events.find(e => e.event_name === requestedEvent && isSelectableForCheckin(e))
    if (!activeEvent) {
      return NextResponse.json({ error: 'event_not_found' }, { status: 404 })
    }
  } else {
    activeEvent = getDefaultEvent(events)
  }

  if (!activeEvent) {
    return NextResponse.json({ event_name: 'No Active Event', attendees: [] })
  }

  const responses = readResponses()
  const eventResponses = responses[activeEvent.event_name]

  if (!eventResponses) {
    return NextResponse.json({ event_name: activeEvent.event_name, attendees: [] })
  }

  const enabled = paymentEnabled(activeEvent)
  const snapshot = enabled ? readPaymentSnapshot() : {}
  const paymentFields = (a: import('../db').BotAttendee) => {
    let payment = null
    let unavailable = snapshot === null
    if (enabled && snapshot) {
      try { payment = paymentFromSnapshot(snapshot, activeEvent.event_name, a.user_id, a.timestamp) }
      catch { unavailable = true }
    }
    return { registration_timestamp: a.timestamp && Number.isFinite(Date.parse(a.timestamp)) ? registrationTimestamp(a.timestamp) : null,
      payment_enabled: enabled, payment_method_required: paymentMethodRequired(activeEvent, a),
      payment_unavailable: unavailable, payment }
  }

  const attendeesList = (eventResponses.attendees || []).map(a => ({
    payment_method: a.payment_method ?? null,
    payment_instructions: a.payment_method ? activeEvent.signup_form?.payment_methods[a.payment_method] ?? null : null,
    ...paymentFields(a),
    user_id: a.user_id,
    username: a.username,
    display_name: a.display_name,
    drinks: a.drinks || [],
    extra_people: a.extra_people || 0,
    extras_names: a.extras_names || [],
    names_revision: namesRevision(a.display_name ?? null, a.extra_people || 0, a.extras_names || []),
    attendee_number: a.attendee_number ?? null,
    extras_attendee_numbers: a.extras_attendee_numbers || [],
    status: 'attending' as const
  }))

  const waitlistList = (eventResponses.waitlist || []).map(a => ({
    payment_method: a.payment_method ?? null,
    payment_instructions: a.payment_method ? activeEvent.signup_form?.payment_methods[a.payment_method] ?? null : null,
    ...paymentFields(a),
    user_id: a.user_id,
    username: a.username,
    display_name: a.display_name,
    drinks: a.drinks || [],
    extra_people: a.extra_people || 0,
    extras_names: a.extras_names || [],
    names_revision: namesRevision(a.display_name ?? null, a.extra_people || 0, a.extras_names || []),
    attendee_number: null,
    extras_attendee_numbers: [] as number[],
    status: 'waitlist' as const
  }))

  const combined = [...attendeesList, ...waitlistList]

  return NextResponse.json({
    event_name: activeEvent.event_name,
    attendees: combined
  })
}
