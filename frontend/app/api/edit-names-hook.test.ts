import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'

const tick = () => new Promise<void>(resolve => setImmediate(resolve))
const uid = '191524132624531458'
const timestamp = '2026-10-01T00:00:00.000Z'
const event = 'Event A'
const attendee = { user_id: uid, username: 'Synthetic', display_name: 'Before', extra_people: 2,
  extras_names: ['Alice', null], drinks: [], attendee_number: 1, extras_attendee_numbers: [2, 3],
  status: 'attending', registration_timestamp: timestamp }
const expected = { display_name: 'Before', extra_people: 2, extras_names: ['Alice', null] }
const changes = { display_name: 'After', extras_names: [{ index: 1, value: 'Bea' }] }
const savedRegistration = { user_id: uid, registration_timestamp: timestamp, display_name: 'After',
  extra_people: 2, extras_names: ['Alice', 'Bea'] }

async function loadHook() {
  const dir = fs.mkdtempSync(path.join(process.cwd(), 'node_modules', '.names-hook-'))
  fs.writeFileSync(path.join(dir, 'package.json'), '{"type":"module"}')
  fs.writeFileSync(path.join(dir, 'react.js'), `
    const states = []; const refs = []; let index = 0; let refIndex = 0; let effects = [];
    export function begin(seed = {}, selectedEvent, refSeed = {}) { index = 0; refIndex = 0; effects = [];
      for (const [key, value] of Object.entries(seed)) states[Number(key)] = value;
      if (selectedEvent !== undefined) refs[0] = { current: selectedEvent };
      for (const [key, value] of Object.entries(refSeed)) refs[Number(key)] = { current: value }; }
    export function useState(initial) { const key = index++; if (!(key in states)) states[key] = initial;
      return [states[key], value => { states[key] = typeof value === 'function' ? value(states[key]) : value; }]; }
    export function useRef(initial) { const key = refIndex++; return refs[key] ??= { current: initial }; }
    export function useCallback(fn) { return fn; }
    export function useEffect(fn) { effects.push(fn); }
    export function runEffects() { for (const fn of effects) fn(); }
  `)
  const code = ts.transpileModule(fs.readFileSync('app/hooks/useAdminData.ts', 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
  }).outputText.replaceAll("from 'react'", "from './react.js'").replaceAll("from '../lib/checkin'", "from './checkin.js'")
  fs.writeFileSync(path.join(dir, 'checkin.js'), ts.transpileModule(
    fs.readFileSync('app/lib/checkin.ts', 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText)
  fs.writeFileSync(path.join(dir, 'hook.js'), code)
  const react = await import(pathToFileURL(path.join(dir, 'react.js')).href)
  const { useAdminData } = await import(pathToFileURL(path.join(dir, 'hook.js')).href)
  return { ...react, useAdminData, cleanup: () => fs.rmSync(dir, { recursive: true, force: true }) }
}

const seed = () => ({ 0: 'synthetic-key', 1: true, 4: event, 6: event, 7: [attendee] })

test('name save shows persisted names even when refresh fails, and distinguishes conflict from uncertainty', async () => {
  const hookModule = await loadHook()
  const previousFetch = globalThis.fetch
  try {
    const { begin, useAdminData } = hookModule
    let posts = 0
    let gets = 0
    globalThis.fetch = async (url, options) => {
      if (options?.method === 'POST') {
        posts++
        assert.equal(String(url), '/api/registration/names?key=synthetic-key')
        assert.deepEqual(JSON.parse(options.body as string),
          { event_name: event, user_id: uid, registration_timestamp: timestamp, expected, changes })
        return Response.json({ saved: true, registration: savedRegistration })
      }
      gets++
      throw new Error('Network disconnected during refresh')
    }
    begin(seed(), event, { 3: [attendee] })
    const hook = useAdminData()
    assert.equal(await hook.saveAttendeeNames(event, uid, timestamp, expected, changes), 'saved')
    assert.equal(posts, 1)
    assert.equal(gets, 1)
    begin()
    const after = useAdminData()
    assert.equal(after.attendees[0].display_name, 'After')
    assert.deepEqual(after.attendees[0].extras_names, ['Alice', 'Bea'])

    // A conflict refreshes the roster and never reports success.
    posts = 0
    gets = 0
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') { posts++; return Response.json(
        { error: 'registration_changed', saved: false }, { status: 409 }) }
      gets++
      return Response.json({ event_name: event, attendees: [{ ...attendee, display_name: 'Other admin' }] })
    }
    begin(seed(), event, { 3: [attendee] })
    const conflicted = useAdminData()
    assert.equal(await conflicted.saveAttendeeNames(event, uid, timestamp, expected, changes), 'conflict')
    assert.equal(posts, 1)
    assert.equal(gets, 1)

    // A timeout/lost response refreshes once, never retries the mutation, and does not claim success.
    posts = 0
    gets = 0
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') { posts++; throw new Error('response lost') }
      gets++
      return Response.json({ event_name: event, attendees: [{ ...attendee, display_name: 'Persisted elsewhere' }] })
    }
    begin(seed(), event, { 3: [attendee] })
    const uncertain = useAdminData()
    assert.equal(await uncertain.saveAttendeeNames(event, uid, timestamp, expected, changes), 'unconfirmed')
    assert.equal(posts, 1)
    assert.equal(gets, 1)

    // Validation errors do not refresh or mutate anything.
    posts = 0
    gets = 0
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') { posts++; return Response.json({ error: 'invalid_request', saved: false }, { status: 400 }) }
      gets++
      return Response.json({ event_name: event, attendees: [attendee] })
    }
    begin(seed(), event, { 3: [attendee] })
    const invalid = useAdminData()
    assert.equal(await invalid.saveAttendeeNames(event, uid, timestamp, expected, { display_name: '  ' }), 'invalid')
    assert.equal(posts, 1)
    assert.equal(gets, 0)

    // A partial/malformed success reply is not applied and never becomes a success claim.
    posts = 0
    gets = 0
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') {
        posts++
        return Response.json({ saved: true, registration: {
          user_id: uid, registration_timestamp: timestamp, display_name: 'Bad', extra_people: 2,
        } })
      }
      gets++
      return Response.json({ event_name: event, attendees: [attendee] })
    }
    begin(seed(), event, { 3: [attendee] })
    const partial = useAdminData()
    assert.equal(await partial.saveAttendeeNames(event, uid, timestamp, expected, changes), 'unconfirmed')
    assert.equal(posts, 1)
    assert.equal(gets, 1)
    begin()
    const unchanged = useAdminData()
    assert.equal(unchanged.attendees[0].display_name, 'Before')
    assert.deepEqual(unchanged.attendees[0].extras_names, ['Alice', null])
  } finally {
    globalThis.fetch = previousFetch
    hookModule.cleanup()
  }
})

test('name save is stale after an event switch and pre-save polls cannot revert confirmed names', async () => {
  const hookModule = await loadHook()
  const previousFetch = globalThis.fetch
  const originalInterval = globalThis.setInterval
  const timers: ReturnType<typeof setInterval>[] = []
  const polls: (() => void)[] = []
  try {
    const { begin, runEffects, useAdminData } = hookModule
    const pending: { resolve: (value: Response) => void }[] = []
    let posts = 0
    globalThis.fetch = async (url, options) => {
      if (options?.method === 'POST') {
        posts++
        return Response.json({ saved: true, registration: savedRegistration })
      }
      const parsed = new URL(String(url), 'http://localhost')
      if (parsed.pathname === '/api/checkin') return Response.json([])
      return new Promise<Response>(resolve => pending.push({ resolve }))
    }
    globalThis.setInterval = ((callback: () => void) => {
      polls.push(callback)
      const timer = originalInterval(() => {}, 100_000)
      timers.push(timer)
      return timer
    }) as typeof setInterval

    begin(seed(), event, { 3: [attendee] })
    const hook = useAdminData()
    runEffects()
    assert.equal(pending.length, 1) // initial load for the selected event
    polls[0]() // a poll that starts before the save and will return pre-save names
    assert.equal(pending.length, 2)
    const saving = hook.saveAttendeeNames(event, uid, timestamp, expected, changes)
    await tick()
    assert.equal(pending.length, 3) // the post-save refresh
    pending[2].resolve(Response.json({ event_name: event, attendees: [{
      ...attendee, display_name: 'After', extras_names: ['Alice', 'Bea'],
    }] }))
    await tick()
    assert.equal(await saving, 'saved')
    pending[1].resolve(Response.json({ event_name: event, attendees: [attendee] }))
    pending[0].resolve(Response.json({ event_name: event, attendees: [attendee] }))
    await tick()
    begin()
    const settled = useAdminData()
    assert.equal(settled.attendees[0].display_name, 'After')
    assert.deepEqual(settled.attendees[0].extras_names, ['Alice', 'Bea'])

    // A response that lands after switching events is ignored and does not refresh.
    let resolvePost!: (value: Response) => void
    let getCalls = 0
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') {
        posts++
        return new Promise<Response>(resolve => { resolvePost = resolve })
      }
      getCalls++
      return Response.json({ event_name: 'Event B', attendees: [] })
    }
    begin(seed(), event, { 3: [attendee] })
    const switching = useAdminData()
    const late = switching.saveAttendeeNames(event, uid, timestamp, expected, changes)
    switching.changeEvent('Event B')
    resolvePost(Response.json({ saved: true, registration: savedRegistration }))
    assert.equal(await late, 'stale')
    assert.equal(posts, 2)
    assert.equal(getCalls, 0)
    begin()
    const afterSwitch = useAdminData()
    assert.equal(afterSwitch.selectedEvent, 'Event B')
    assert.equal(afterSwitch.attendees[0].display_name, 'Before')

    // Even after the POST is confirmed, an event switch during the refresh suppresses success.
    let resolveConfirmedPost!: (value: Response) => void
    let resolveRefresh!: (value: Response) => void
    let refreshCalls = 0
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') {
        posts++
        return new Promise<Response>(resolve => { resolveConfirmedPost = resolve })
      }
      refreshCalls++
      return new Promise<Response>(resolve => { resolveRefresh = resolve })
    }
    begin(seed(), event, { 3: [attendee] })
    const switchDuringRefresh = useAdminData()
    const pendingSave = switchDuringRefresh.saveAttendeeNames(event, uid, timestamp, expected, changes)
    await tick()
    resolveConfirmedPost(Response.json({ saved: true, registration: savedRegistration }))
    await tick()
    assert.equal(refreshCalls, 1)
    switchDuringRefresh.changeEvent('Event B')
    resolveRefresh(Response.json({ event_name: event, attendees: [{
      ...attendee, display_name: 'After', extras_names: ['Alice', 'Bea'],
    }] }))
    assert.equal(await pendingSave, 'stale')
    begin()
    assert.equal(useAdminData().selectedEvent, 'Event B')

    // Calling with a different selected event is refused before any request.
    begin(seed(), event, { 3: [attendee] })
    const wrongEvent = useAdminData()
    const before = posts
    assert.equal(await wrongEvent.saveAttendeeNames('Event C', uid, timestamp, expected, changes), 'stale')
    assert.equal(posts, before)
  } finally {
    globalThis.fetch = previousFetch
    globalThis.setInterval = originalInterval
    for (const timer of timers) clearInterval(timer)
    hookModule.cleanup()
  }
})
