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
const batchId = 'c'.repeat(32)
const attendee = { user_id: uid, username: 'Synthetic', display_name: 'Before', extra_people: 2,
  extras_names: ['Alice', null], names_revision: revision, drinks: [], attendee_number: 1,
  extras_attendee_numbers: [2, 3], status: 'attending', registration_timestamp: timestamp }
const changes = { display_name: 'After', extras_names: [{ index: 1, value: 'Bea' }] }
const savedRegistration = { user_id: uid, registration_timestamp: timestamp, display_name: 'After',
  extra_people: 2, extras_names: ['Alice', 'Bea'], names_revision: 'd'.repeat(64) }

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

test('one save stages every bounded part then commits once and applies returned names', async () => {
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
        const body = JSON.parse(text) as Record<string, unknown>
        posts.push(body)
        if (body.operation === 'stage') {
          return Response.json({ batch_id: batchId, staged: body.field, complete: false })
        }
        return Response.json({ saved: true, warnings: ['Notification DM failed'], registration: savedRegistration })
      }
      gets++
      throw new Error('Network disconnected during refresh')
    }
    begin(seed(), event, { 3: [attendee] })
    const hook = useAdminData()
    assert.equal(await hook.saveAttendeeNames(event, uid, timestamp, revision, changes), 'saved')
    assert.equal(posts.length, 3)
    assert.ok(sizes.every(size => size <= 4096), sizes.join(','))
    assert.deepEqual(posts[0], {
      operation: 'stage', event_name: event, user_id: uid, registration_timestamp: timestamp,
      expected_revision: revision, manifest: ['display_name', 'guest:1'], field: 'display_name', value: 'After',
    })
    assert.deepEqual(posts[1], {
      operation: 'stage', event_name: event, user_id: uid, registration_timestamp: timestamp,
      expected_revision: revision, manifest: ['display_name', 'guest:1'], field: 'guest:1', value: 'Bea',
      batch_id: batchId,
    })
    assert.deepEqual(posts[2], {
      operation: 'commit', event_name: event, user_id: uid, registration_timestamp: timestamp,
      expected_revision: revision, manifest: ['display_name', 'guest:1'], batch_id: batchId,
    })
    assert.equal(gets, 1)
    begin()
    const after = useAdminData()
    assert.equal(after.attendees[0].display_name, 'After')
    assert.deepEqual(after.attendees[0].extras_names, ['Alice', 'Bea'])
    assert.equal(after.attendees[0].names_revision, 'd'.repeat(64))
    assert.deepEqual(after.actionWarnings, ['Notification DM failed'])

    // The returned revision is applied with the names, so a second edit still
    // works even though the post-save refresh failed.
    posts.length = 0
    const secondRevision = 'e'.repeat(64)
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') {
        const body = JSON.parse(options.body as string) as Record<string, unknown>
        posts.push(body)
        return body.operation === 'stage'
          ? Response.json({ batch_id: batchId, staged: body.field, complete: true })
          : Response.json({ saved: true, registration: {
            ...savedRegistration, display_name: 'Second', names_revision: secondRevision,
          } })
      }
      gets++
      throw new Error('Network disconnected during refresh')
    }
    begin()
    const secondEdit = useAdminData()
    assert.equal(await secondEdit.saveAttendeeNames(
      event, uid, timestamp, after.attendees[0].names_revision!, { display_name: 'Second' }), 'saved')
    assert.equal((posts[0] as { expected_revision?: string }).expected_revision, 'd'.repeat(64))
    begin()
    const afterSecond = useAdminData()
    assert.equal(afterSecond.attendees[0].display_name, 'Second')
    assert.equal(afterSecond.attendees[0].names_revision, secondRevision)

    // A conflict is reported only after refreshing and never as success.
    posts.length = 0
    let conflictGets = 0
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') {
        const body = JSON.parse(options.body as string) as Record<string, unknown>
        posts.push(body)
        return body.operation === 'stage'
          ? Response.json({ batch_id: batchId, staged: body.field, complete: true })
          : Response.json({ error: 'registration_changed', saved: false }, { status: 409 })
      }
      conflictGets++
      return Response.json({ event_name: event, attendees: [{ ...attendee, display_name: 'Other admin' }] })
    }
    begin(seed(), event, { 3: [attendee] })
    const conflicted = useAdminData()
    assert.equal(await conflicted.saveAttendeeNames(event, uid, timestamp, revision, changes), 'conflict')
    assert.equal(posts.length, 3)
    assert.equal(conflictGets, 1)
  } finally {
    globalThis.fetch = previousFetch
    hookModule.cleanup()
  }
})

test('staging stops before commit, errors stay truthful, and an unknown final response is never retried', async () => {
  const hookModule = await loadHook()
  const previousFetch = globalThis.fetch
  try {
    const { begin, useAdminData } = hookModule
    let posts: Record<string, unknown>[] = []
    let gets = 0
    const route = (stage: () => Response, commit: () => Response | Promise<Response>) => (
      async (_url: string | URL | Request, options?: RequestInit) => {
        if (options?.method !== 'POST') {
          gets++
          throw new Error('refresh failed')
        }
        const body = JSON.parse(options.body as string) as Record<string, unknown>
        posts.push(body)
        return body.operation === 'stage' ? stage() : commit()
      }
    ) as typeof fetch

    globalThis.fetch = route(
      () => Response.json({ error: 'batch_busy', saved: false }, { status: 409 }),
      () => { throw new Error('commit must not run') },
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'busy')
    assert.equal(posts.length, 1)

    posts = []
    globalThis.fetch = route(
      () => Response.json({ error: 'batch_unknown', saved: false }, { status: 409 }),
      () => { throw new Error('commit must not run') },
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'expired')
    assert.equal(posts.length, 1)

    posts = []
    globalThis.fetch = route(
      () => Response.json({ error: 'request_too_large', saved: false }, { status: 413 }),
      () => { throw new Error('commit must not run') },
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'too_large')
    assert.equal(posts.length, 1)

    // A lost final response refreshes once and never retries the commit.
    posts = []
    gets = 0
    globalThis.fetch = route(
      () => Response.json({ batch_id: batchId, staged: 'display_name', complete: false }),
      () => { throw new Error('response lost after commit') },
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'unconfirmed')
    assert.equal(posts.length, 3)
    assert.equal(posts.filter(post => post.operation === 'commit').length, 1)
    assert.equal(gets, 1)

    posts = []
    gets = 0
    globalThis.fetch = route(
      () => Response.json({ batch_id: batchId, staged: 'display_name', complete: false }),
      () => Response.json({ error: 'batch_unknown', unconfirmed: true, saved: false }, { status: 409 }),
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'unconfirmed')
    assert.equal(posts.filter(post => post.operation === 'commit').length, 1)
    assert.equal(gets, 1)

    // A malformed success body is unconfirmed, not success.
    posts = []
    gets = 0
    globalThis.fetch = route(
      () => Response.json({ batch_id: batchId, staged: 'display_name', complete: false }),
      () => Response.json({ saved: true, registration: {
        user_id: uid, registration_timestamp: timestamp, display_name: 'Bad', extra_people: 2,
      } }),
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'unconfirmed')
    const badGuestCount = { ...savedRegistration, extra_people: 6 }
    globalThis.fetch = route(
      () => Response.json({ batch_id: batchId, staged: 'display_name', complete: false }),
      () => Response.json({ saved: true, registration: badGuestCount }),
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'unconfirmed')

    const booleanGuestCount = { ...savedRegistration, extra_people: true }
    globalThis.fetch = route(
      () => Response.json({ batch_id: batchId, staged: 'display_name', complete: false }),
      () => Response.json({ saved: true, registration: booleanGuestCount }),
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'unconfirmed')

    const badRevision = { ...savedRegistration, names_revision: 'not-a-revision' }
    globalThis.fetch = route(
      () => Response.json({ batch_id: batchId, staged: 'display_name', complete: false }),
      () => Response.json({ saved: true, registration: badRevision }),
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'unconfirmed')

    const missingRevision = { ...savedRegistration, names_revision: undefined }
    globalThis.fetch = route(
      () => Response.json({ batch_id: batchId, staged: 'display_name', complete: false }),
      () => Response.json({ saved: true, registration: missingRevision }),
    )
    begin(seed(), event, { 3: [attendee] })
    assert.equal(await useAdminData().saveAttendeeNames(event, uid, timestamp, revision, changes), 'unconfirmed')
    begin()
    const unchanged = useAdminData()
    assert.equal(unchanged.attendees[0].display_name, 'Before')
    assert.deepEqual(unchanged.attendees[0].extras_names, ['Alice', null])
  } finally {
    globalThis.fetch = previousFetch
    hookModule.cleanup()
  }
})

test('event switches and pre-save polls cannot overwrite or leak a completed save', async () => {
  const hookModule = await loadHook()
  const previousFetch = globalThis.fetch
  const originalSetInterval = globalThis.setInterval
  const timers: ReturnType<typeof setInterval>[] = []
  const polls: (() => void)[] = []
  try {
    const { begin, runEffects, useAdminData } = hookModule
    const pending: { resolve: (value: Response) => void }[] = []
    let resolveCommit!: (value: Response) => void
    let commitStarted = false
    globalThis.fetch = async (url, options) => {
      if (options?.method === 'POST') {
        const body = JSON.parse(options.body as string) as Record<string, unknown>
        if (body.operation === 'stage') {
          return Response.json({ batch_id: batchId, staged: body.field, complete: true })
        }
        commitStarted = true
        return new Promise<Response>(resolve => { resolveCommit = resolve })
      }
      const parsed = new URL(String(url), 'http://localhost')
      if (parsed.pathname === '/api/checkin') return Response.json([])
      return new Promise<Response>(resolve => pending.push({ resolve }))
    }
    globalThis.setInterval = ((callback: () => void) => {
      polls.push(callback)
      const timer = originalSetInterval(() => {}, 100_000)
      timers.push(timer)
      return timer
    }) as typeof setInterval

    begin(seed(), event, { 3: [attendee] })
    const hook = useAdminData()
    runEffects()
    assert.equal(pending.length, 1) // initial load for the selected event
    polls[0]() // a poll that starts before the save and will return pre-save names
    assert.equal(pending.length, 2)
    const saving = hook.saveAttendeeNames(event, uid, timestamp, revision, changes)
    for (let count = 0; count < 20 && !commitStarted; count++) await tick()
    assert.equal(commitStarted, true)
    assert.equal(pending.length, 2)
    resolveCommit(Response.json({ saved: true, registration: savedRegistration }))
    await tick()
    assert.equal(pending.length, 3) // the post-commit refresh
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

    // A commit response that lands after switching events is ignored.
    let posts = 0
    let resolveLate!: (value: Response) => void
    let lateStage = 0
    globalThis.fetch = async (_url, options) => {
      if (options?.method === 'POST') {
        posts++
        const body = JSON.parse(options.body as string) as Record<string, unknown>
        if (body.operation === 'stage') {
          lateStage++
          return Response.json({ batch_id: batchId, staged: body.field, complete: true })
        }
        return new Promise<Response>(resolve => { resolveLate = resolve })
      }
      throw new Error('no refresh expected after switching events')
    }
    begin(seed(), event, { 3: [attendee] })
    const switching = useAdminData()
    const late = switching.saveAttendeeNames(event, uid, timestamp, revision, changes)
    for (let count = 0; count < 20 && !resolveLate; count++) await tick()
    assert.ok(resolveLate)
    switching.changeEvent('Event B')
    resolveLate(Response.json({ saved: true, registration: savedRegistration }))
    assert.equal(await late, 'stale')
    assert.equal(posts, 3)
    assert.equal(lateStage, 2)
    begin()
    const afterSwitch = useAdminData()
    assert.equal(afterSwitch.selectedEvent, 'Event B')
    assert.equal(afterSwitch.attendees[0].display_name, 'Before')

    // Calling with a different selected event is refused before any request.
    begin(seed(), event, { 3: [attendee] })
    const wrongEvent = useAdminData()
    const before = posts
    assert.equal(await wrongEvent.saveAttendeeNames('Event C', uid, timestamp, revision, changes), 'stale')
    assert.equal(posts, before)
  } finally {
    globalThis.fetch = previousFetch
    globalThis.setInterval = originalSetInterval
    for (const timer of timers) clearInterval(timer)
    hookModule.cleanup()
  }
})

test('a delayed save cannot overwrite newer names but still applies over unrelated bot updates', async () => {
  const hookModule = await loadHook()
  const previousFetch = globalThis.fetch
  const originalSetInterval = globalThis.setInterval
  const timers: ReturnType<typeof setInterval>[] = []
  try {
    const { begin, runEffects, useAdminData } = hookModule
    const polls: (() => void)[] = []
    let pendingRoster: { resolve: (value: Response) => void }[] = []
    let failRoster = false
    let resolveCommit!: (value: Response) => void
    let commitStarted = false
    globalThis.fetch = async (url, options) => {
      if (options?.method === 'POST') {
        const body = JSON.parse(options.body as string) as Record<string, unknown>
        if (body.operation === 'stage') {
          return Response.json({ batch_id: batchId, staged: body.field, complete: true })
        }
        commitStarted = true
        return new Promise<Response>(resolve => { resolveCommit = resolve })
      }
      const parsed = new URL(String(url), 'http://localhost')
      if (parsed.pathname === '/api/checkin') return Response.json([])
      if (failRoster) throw new Error('refresh failed')
      return new Promise<Response>(resolve => pendingRoster.push({ resolve }))
    }
    globalThis.setInterval = ((callback: () => void) => {
      polls.push(callback)
      const timer = originalSetInterval(() => {}, 100_000)
      timers.push(timer)
      return timer
    }) as typeof setInterval

    // Poll sees another admin's newer names revision while the older save's
    // commit response is still awaited. The older names must not overwrite it.
    begin(seed(), event, { 3: [attendee] })
    const olderSave = useAdminData()
    runEffects()
    assert.equal(pendingRoster.length, 1)
    pendingRoster[0].resolve(Response.json({ event_name: event, attendees: [attendee] }))
    await tick()
    const saving = olderSave.saveAttendeeNames(event, uid, timestamp, revision, { display_name: 'After' })
    for (let count = 0; count < 20 && !commitStarted; count++) await tick()
    assert.equal(commitStarted, true)
    polls[0]()
    assert.equal(pendingRoster.length, 2)
    const newer = { ...attendee, display_name: 'Newer admin', names_revision: 'e'.repeat(64) }
    pendingRoster[1].resolve(Response.json({ event_name: event, attendees: [newer] }))
    await tick()
    failRoster = true
    resolveCommit(Response.json({ saved: true, registration: savedRegistration }))
    assert.equal(await saving, 'saved')
    begin()
    const afterNewer = useAdminData()
    assert.equal(afterNewer.attendees[0].display_name, 'Newer admin')
    assert.equal(afterNewer.attendees[0].names_revision, 'e'.repeat(64))

    // An unrelated payment update keeps the same names revision, so the saved
    // names still apply while the unrelated field is preserved.
    pendingRoster = []
    failRoster = false
    commitStarted = false
    begin(seed(), event, { 3: [attendee] })
    const unrelatedSave = useAdminData()
    runEffects()
    pendingRoster[0].resolve(Response.json({ event_name: event, attendees: [attendee] }))
    await tick()
    const savingUnrelated = unrelatedSave.saveAttendeeNames(
      event, uid, timestamp, revision, { display_name: 'After' })
    for (let count = 0; count < 20 && !commitStarted; count++) await tick()
    assert.equal(commitStarted, true)
    polls[0]()
    const paid = { ...attendee, payment_method: 'PayNow' }
    pendingRoster[1].resolve(Response.json({ event_name: event, attendees: [paid] }))
    await tick()
    failRoster = true
    resolveCommit(Response.json({ saved: true, registration: savedRegistration }))
    assert.equal(await savingUnrelated, 'saved')
    begin()
    const afterUnrelated = useAdminData()
    assert.equal(afterUnrelated.attendees[0].display_name, 'After')
    assert.equal(afterUnrelated.attendees[0].names_revision, 'd'.repeat(64))
    assert.equal(afterUnrelated.attendees[0].payment_method, 'PayNow')
  } finally {
    globalThis.fetch = previousFetch
    globalThis.setInterval = originalSetInterval
    for (const timer of timers) clearInterval(timer)
    hookModule.cleanup()
  }
})
