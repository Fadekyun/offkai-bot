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
const revision = 'a'.repeat(64)
const attendee = { user_id: uid, username: 'Synthetic', display_name: 'Before', extra_people: 2,
  extras_names: ['Alice', null], names_revision: revision, drinks: [], attendee_number: 1,
  extras_attendee_numbers: [2, 3], status: 'attending', registration_timestamp: timestamp }
const changes = { display_name: 'After', extras_names: [{ index: 1, value: 'Bea' }] }
const savedBody = {
  event_name: event, user_id: uid, registration_timestamp: timestamp, expected_revision: revision,
  display_name: 'After', guest_names: { 1: 'Bea' },
}
const refreshed = { ...attendee, display_name: 'After', extras_names: ['Alice', 'Bea'],
  names_revision: 'd'.repeat(64) }

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

const seed = () => ({ 0: 'synthetic-key', 1: true, 4: event, 6: event, 7: [attendee], 12: [] })
const roster = () => Response.json({ event_name: event, attendees: [refreshed] })

test('one save sends one request, refreshes, and reports persisted warnings', async () => {
  const hookModule = await loadHook()
  const previousFetch = globalThis.fetch
  try {
    const { begin, useAdminData } = hookModule
    const posts: Record<string, unknown>[] = []
    const sizes: number[] = []
    let gets = 0
    globalThis.fetch = async (url, options) => {
      if (options?.method === 'POST') {
        assert.equal(String(url), '/api/registration/names?key=synthetic-key')
        const text = options.body as string
        sizes.push(Buffer.byteLength(text, 'utf8'))
        posts.push(JSON.parse(text) as Record<string, unknown>)
        return Response.json({ saved: true, warnings: ['Notification DM failed'], registration: refreshed })
      }
      gets++
      return roster()
    }
    begin(seed(), event, { 2: [attendee] })
    const hook = useAdminData()
    assert.equal(await hook.saveAttendeeNames(event, uid, timestamp, revision, changes), 'saved')
    assert.equal(posts.length, 1)
    assert.deepEqual(posts[0], savedBody)
    assert.ok(sizes[0] <= 4096, String(sizes[0]))
    assert.equal(gets, 1)
    begin()
    const after = useAdminData()
    assert.equal(after.attendees[0].display_name, 'After')
    assert.deepEqual(after.attendees[0].extras_names, ['Alice', 'Bea'])
    assert.equal(after.attendees[0].names_revision, 'd'.repeat(64))
    assert.deepEqual(after.actionWarnings, ['Notification DM failed'])

    // A refresh failure must not turn a confirmed save into a failure.
    begin(seed(), event, { 2: [attendee] })
    const failingRefresh = useAdminData()
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') return Response.json({ saved: true, warnings: [], registration: refreshed })
      throw new Error('refresh failed')
    }
    assert.equal(await failingRefresh.saveAttendeeNames(event, uid, timestamp, revision, changes), 'saved')
  } finally {
    globalThis.fetch = previousFetch
    hookModule.cleanup()
  }
})

test('refresh-only reconciliation never merges the save response into newer roster data', async () => {
  const hookModule = await loadHook()
  const previousFetch = globalThis.fetch
  try {
    const { begin, useAdminData } = hookModule
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') return Response.json({ saved: true, warnings: [], registration: refreshed })
      return Response.json({ event_name: event, attendees: [{ ...attendee, display_name: 'Newer admin',
        names_revision: 'e'.repeat(64) }] })
    }
    begin(seed(), event, { 2: [attendee] })
    const hook = useAdminData()
    assert.equal(await hook.saveAttendeeNames(event, uid, timestamp, revision, changes), 'saved')
    begin()
    const after = useAdminData()
    assert.equal(after.attendees[0].display_name, 'Newer admin')
    assert.equal(after.attendees[0].names_revision, 'e'.repeat(64))
  } finally {
    globalThis.fetch = previousFetch
    hookModule.cleanup()
  }
})

test('save outcomes stay truthful without retrying the single request', async () => {
  const hookModule = await loadHook()
  const previousFetch = globalThis.fetch
  try {
    const { begin, useAdminData } = hookModule
    let posts = 0
    let gets = 0
    const setRoute = (reply: () => Response | Promise<Response>) => {
      globalThis.fetch = (async (_url, options) => {
        if (options?.method === 'POST') { posts++; return reply() }
        gets++
        return roster()
      }) as typeof fetch
    }

    begin(seed(), event, { 2: [attendee] })
    setRoute(() => Response.json({ error: 'invalid_request', saved: false }, { status: 400 }))
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'invalid')
    assert.equal(posts, 1)
    assert.equal(gets, 1)

    begin(seed(), event, { 2: [attendee] })
    setRoute(() => Response.json({ error: 'registration_changed', saved: false }, { status: 409 }))
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'conflict')
    assert.equal(posts, 2)
    assert.equal(gets, 2)

    begin(seed(), event, { 2: [attendee] })
    setRoute(() => Response.json({ error: 'request_too_large', saved: false }, { status: 413 }))
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'error')
    assert.equal(posts, 3)

    begin(seed(), event, { 2: [attendee] })
    setRoute(() => Response.json({ error: 'write_failed', saved: false }, { status: 500 }))
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'error')
    assert.equal(posts, 4)

    begin(seed(), event, { 2: [attendee] })
    setRoute(() => Response.json({ error: 'names_unconfirmed' }, { status: 502 }))
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'unconfirmed')
    assert.equal(posts, 5)
    assert.equal(gets, 5)

    begin(seed(), event, { 2: [attendee] })
    setRoute(() => { throw new Error('response lost after save') })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'unconfirmed')
    assert.equal(posts, 6)
    assert.equal(gets, 6)
  } finally {
    globalThis.fetch = previousFetch
    hookModule.cleanup()
  }
})

test('late saves after an event or attendee identity switch are stale and leak no result', async () => {
  const hookModule = await loadHook()
  const previousFetch = globalThis.fetch
  try {
    const { begin, useAdminData } = hookModule
    let posts = 0
    let gets = 0
    let resolveSave!: (value: Response) => void
    globalThis.fetch = (async (_url, options) => {
      if (options?.method === 'POST') {
        posts++
        return new Promise<Response>(resolve => { resolveSave = resolve })
      }
      gets++
      return Response.json({ event_name: event, attendees: [{ ...attendee,
        registration_timestamp: '2026-10-02T00:00:00.000Z' }] })
    }) as typeof fetch

    begin(seed(), event, { 2: [attendee] })
    const switching = useAdminData()
    const late = switching.saveAttendeeNames(event, uid, timestamp, revision, changes)
    for (let count = 0; count < 20 && !resolveSave; count++) await tick()
    switching.changeEvent('Event B')
    resolveSave(Response.json({ saved: true, warnings: ['must not leak'], registration: refreshed }))
    assert.equal(await late, 'stale')
    assert.equal(posts, 1)
    assert.equal(gets, 0)
    begin()
    const afterSwitch = useAdminData()
    assert.equal(afterSwitch.selectedEvent, 'Event B')
    assert.deepEqual(afterSwitch.actionWarnings, [])

    // Same event, but the refresh shows a replaced registration identity.
    resolveSave = undefined as unknown as (value: Response) => void
    globalThis.fetch = (async (_url, options) => {
      if (options?.method === 'POST') {
        posts++
        return new Promise<Response>(resolve => { resolveSave = resolve })
      }
      gets++
      return Response.json({ event_name: event, attendees: [{ ...attendee,
        registration_timestamp: '2026-10-02T00:00:00.000Z' }] })
    }) as typeof fetch
    begin(seed(), event, { 2: [attendee] })
    const replaced = useAdminData()
    const replacedSave = replaced.saveAttendeeNames(event, uid, timestamp, revision, changes)
    for (let count = 0; count < 20 && !resolveSave; count++) await tick()
    resolveSave(Response.json({ saved: true, warnings: ['must not leak'], registration: refreshed }))
    assert.equal(await replacedSave, 'stale')
    assert.equal(posts, 2)
    assert.equal(gets, 1)
    begin()
    assert.deepEqual(useAdminData().actionWarnings, [])

    // Calling for a different selected event is refused before any request.
    begin(seed(), event, { 2: [attendee] })
    const wrongEvent = useAdminData()
    assert.equal(await wrongEvent.saveAttendeeNames('Event C', uid, timestamp, revision, changes), 'stale')
    assert.equal(posts, 2)
  } finally {
    globalThis.fetch = previousFetch
    hookModule.cleanup()
  }
})
