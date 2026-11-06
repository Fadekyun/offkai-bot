import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'
import { NextRequest } from 'next/server.js'

function compileRoutes(output: string) {
  fs.writeFileSync(path.join(output, 'package.json'), '{"type":"module"}')
  function compile(source: string, dest: string) {
    fs.mkdirSync(dest, { recursive: true })
    for (const entry of fs.readdirSync(source, { withFileTypes: true })) {
      if (entry.isDirectory()) { compile(path.join(source, entry.name), path.join(dest, entry.name)); continue }
      if (!entry.name.endsWith('.ts') || entry.name.endsWith('.test.ts')) continue
      const code = ts.transpileModule(fs.readFileSync(path.join(source, entry.name), 'utf8'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
      }).outputText.replace(/(from\s+['"]|import\(['"])(\.[^'"]+)(['"])/g, '$1$2.js$3')
        .replaceAll("'next/server'", "'next/server.js'")
      fs.writeFileSync(path.join(dest, entry.name.replace(/\.ts$/, '.js')), code)
    }
  }
  compile('app/api', path.join(output, 'api'))
  compile('app/lib', path.join(output, 'lib'))
}

const uid = '191524132624531458'
const key = 'synthetic-checkin-key'
const eventName = 'Synthetic checkin integrity'
const stampA = '2026-10-01T00:00:00.123456+00:00'
const stampB = '2026-10-02T09:00:00.654321+09:00'
const canonical = (timestamp: string) => new Date(timestamp).toISOString()
function tokenFor(name: string, user = uid) {
  const payload = Buffer.from(`${user}:${name}`).toString('base64url')
  return `v2.${payload}.${crypto.createHmac('sha256', key).update(payload).digest('hex').slice(0, 16)}`
}
function request(url: string, body?: unknown) {
  return new NextRequest(`http://localhost${url}`, body === undefined ? {} : {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  })
}

test('actual routes bind arrivals to the current registration, not signup promises or legacy scans', async t => {
  const output = fs.mkdtempSync(path.join(process.cwd(), 'node_modules', '.checkin-routes-'))
  const oldEnv = { ...process.env }
  try {
    compileRoutes(output)
    const data = path.join(output, 'data')
    fs.mkdirSync(data)
    process.env.BOT_DATA_DIR = data
    process.env.ADMIN_KEY = key
    process.env.MOCK_MODE = 'false'
    const responsesPath = path.join(data, 'responses.json')
    const checkinsPath = path.join(data, 'checkins.json')
    fs.writeFileSync(path.join(data, 'events.json'), JSON.stringify([
      { event_name: eventName, archived: false, open: true, event_datetime: '2026-12-01T12:00:00+09:00' },
      { event_name: 'Other event', archived: false, open: true, event_datetime: '2026-12-02T12:00:00+09:00' },
    ]))
    const signup = (timestamp: string, extra_people = 0) => ({ user_id: uid, username: 'Synthetic', display_name: null,
      timestamp, arrival_confirmed: true, behavior_confirmed: true, drinks: [], extras_names: [], extra_people })
    const registrations = (timestamp: string, guests = 0, waiting = false) => fs.writeFileSync(responsesPath, JSON.stringify({
      [eventName]: { attendees: waiting ? [] : [signup(timestamp, guests)], waitlist: waiting ? [signup(timestamp, guests)] : [] },
    }))
    registrations(stampA)
    const route = await import(pathToFileURL(path.join(output, 'api/checkin/route.js')).href)
    const attendee = await import(pathToFileURL(path.join(output, 'api/attendee/route.js')).href)
    const attendees = await import(pathToFileURL(path.join(output, 'api/attendees/route.js')).href)
    const url = `/api/checkin?key=${key}&event=${encodeURIComponent(eventName)}`
    const pass = async () => (await attendee.GET(request(`/api/attendee?token=${tokenFor(eventName)}`))).json()
    const roster = async () => (await attendees.GET(request(`/api/attendees?key=${key}&event=${encodeURIComponent(eventName)}`))).json()
    const listed = async () => (await route.GET(request(url))).json()
    const manual = (timestamp: string, action?: string) => request(url, {
      event_name: eventName, user_id: uid, registration_timestamp: timestamp, ...(action ? { action } : {}),
    })
    assert.equal((await pass()).attendee.arrival_confirmed, false, 'Signup promise is not physical arrival')
    const scanned = await route.POST(request(url, { token: tokenFor(eventName), event_name: eventName }))
    assert.equal(scanned.status, 200)
    const first = await scanned.json()
    assert.equal(first.already_checked_in, false)
    assert.equal(first.record.registration_timestamp, canonical(stampA))
    assert.equal((await pass()).attendee.arrival_confirmed, true)
    assert.equal((await roster()).attendees[0].registration_timestamp, canonical(stampA))
    assert.match((await roster()).attendees[0].names_revision, /^[0-9a-f]{64}$/)
    assert.equal((await (await route.POST(manual(canonical(stampA)))).json()).already_checked_in, true)

    // Import/withdraw-and-re-register: same user, new registration and three guests.
    registrations(stampB, 3)
    assert.deepEqual(await listed(), [])
    assert.equal((await pass()).attendee.arrival_confirmed, false)
    assert.equal((await roster()).attendees[0].extra_people, 3)
    const before = fs.readFileSync(checkinsPath, 'utf8')
    assert.equal((await route.POST(manual(stampA))).status, 409)
    assert.equal((await route.POST(manual(stampA, 'checkout'))).status, 409)
    assert.equal((await route.DELETE(manual(stampA))).status, 409)
    assert.equal(fs.readFileSync(checkinsPath, 'utf8'), before, 'Stale actions cannot change current arrival')
    assert.equal((await route.POST(request(url, { user_id: uid, event_name: eventName }))).status, 400)
    assert.equal((await route.POST(manual('bad'))).status, 400)
    assert.equal((await route.POST(request(url, { user_id: uid, action: 'checkout' }))).status, 400)

    // Legacy records are never guessed onto whichever registration now exists.
    const history = JSON.parse(before)
    const legacy = { user_id: uid, event_name: eventName, name: 'Legacy', checked_in_at: '2026-10-03T00:00:00Z' }
    const other = { ...legacy, event_name: 'Other event', registration_timestamp: canonical(stampB) }
    fs.writeFileSync(checkinsPath, JSON.stringify([...history, legacy, other]))
    assert.deepEqual(await listed(), [])
    assert.equal((await pass()).attendee.arrival_confirmed, false)
    assert.equal((await route.POST(manual(stampB, 'checkout'))).status, 404)
    const newScan = await route.POST(request(url, { token: tokenFor(eventName), event_name: eventName }))
    const current = await newScan.json()
    assert.equal(current.already_checked_in, false)
    assert.equal(current.record.registration_timestamp, canonical(stampB))
    assert.equal((await listed()).length, 1)
    assert.equal((await pass()).attendee.arrival_confirmed, true)
    assert.equal((await route.POST(request(url, { token: tokenFor('Other event'), event_name: eventName }))).status, 409)

    // Waitlisting or withdrawal also invalidates reads even without delete cleanup.
    registrations(stampB, 3, true)
    assert.deepEqual(await listed(), [])
    assert.equal((await pass()).attendee.arrival_confirmed, false)
    assert.equal((await route.POST(manual(stampB))).status, 404)
    registrations(stampB, 3)
    // Checkout removes duplicate current records, not stale or other-event history.
    const stored = JSON.parse(fs.readFileSync(checkinsPath, 'utf8'))
    fs.writeFileSync(checkinsPath, JSON.stringify([...stored, current.record]))
    assert.equal((await route.DELETE(manual(canonical(stampB)))).status, 200)
    assert.deepEqual(JSON.parse(fs.readFileSync(checkinsPath, 'utf8')), [...history, legacy, other])
    assert.equal((await pass()).attendee.arrival_confirmed, false, 'Checkout clears physical arrival despite signup promise')
    assert.deepEqual(await listed(), [])

    // An interrupted replacement must leave the old JSON byte-for-byte intact.
    const unchanged = fs.readFileSync(checkinsPath, 'utf8')
    const rename = t.mock.method(fs, 'renameSync', () => { throw new Error('Synthetic rename failure') })
    const failed = await route.POST(manual(stampB))
    assert.equal(failed.status, 500)
    assert.deepEqual(await failed.json(), { error: 'database_write_error' })
    assert.equal(fs.readFileSync(checkinsPath, 'utf8'), unchanged)
    assert.equal(fs.readdirSync(data).some(file => file.endsWith('.tmp')), false)
    rename.mock.restore()
    assert.equal((await route.POST(manual(stampB))).status, 200)
    assert.equal((await listed()).length, 1)
    fs.writeFileSync(responsesPath, JSON.stringify({ [eventName]: { attendees: [], waitlist: [] } }))
    assert.deepEqual(await listed(), [])
    assert.equal((await attendee.GET(request(`/api/attendee?token=${tokenFor(eventName)}`))).status, 404)
  } finally {
    t.mock.restoreAll()
    for (const name of ['BOT_DATA_DIR', 'ADMIN_KEY', 'MOCK_MODE']) {
      if (oldEnv[name] === undefined) delete process.env[name]; else process.env[name] = oldEnv[name]
    }
    fs.rmSync(output, { recursive: true, force: true })
  }
})

test('mock routes use the same registration identity and checkout semantics', async () => {
  const output = fs.mkdtempSync(path.join(process.cwd(), 'node_modules', '.checkin-mock-'))
  const oldEnv = { ...process.env }
  try {
    compileRoutes(output)
    process.env.MOCK_MODE = 'true'
    process.env.ADMIN_KEY = key
    const route = await import(pathToFileURL(path.join(output, 'api/checkin/route.js')).href)
    const attendee = await import(pathToFileURL(path.join(output, 'api/attendee/route.js')).href)
    const roster = await import(pathToFileURL(path.join(output, 'api/attendees/route.js')).href)
    const mock = await import(pathToFileURL(path.join(output, 'api/mock.js')).href)
    const name = 'Bandori 10th Offkai'
    const url = `/api/checkin?key=${key}&event=${encodeURIComponent(name)}`
    const passUrl = `/api/attendee?token=${tokenFor(name, '123')}`
    const rows = await (await roster.GET(request(`/api/attendees?key=${key}&event=${encodeURIComponent(name)}`))).json()
    const timestamp = rows.attendees[0].registration_timestamp
    assert.equal(typeof timestamp, 'string')
    assert.equal((await (await attendee.GET(request(passUrl))).json()).attendee.arrival_confirmed, false)
    const scan = await route.POST(request(url, { token: tokenFor(name, '123'), event_name: name }))
    assert.equal((await scan.json()).record.registration_timestamp, timestamp)
    assert.equal((await (await attendee.GET(request(passUrl))).json()).attendee.arrival_confirmed, true)
    mock.MOCK_ATTENDEES[name][0].registration_timestamp = canonical(stampB)
    assert.deepEqual(await (await route.GET(request(url))).json(), [])
    assert.equal((await (await attendee.GET(request(passUrl))).json()).attendee.arrival_confirmed, false)
    assert.equal((await route.POST(request(url, { user_id: '123', registration_timestamp: timestamp }))).status, 409)
    const body = { user_id: '123', registration_timestamp: canonical(stampB) }
    assert.equal((await route.POST(request(url, body))).status, 200)
    assert.equal((await route.POST(request(url, { ...body, action: 'checkout' }))).status, 200)
    assert.equal((await (await attendee.GET(request(passUrl))).json()).attendee.arrival_confirmed, false)
  } finally {
    for (const name of ['BOT_DATA_DIR', 'ADMIN_KEY', 'MOCK_MODE']) {
      if (oldEnv[name] === undefined) delete process.env[name]; else process.env[name] = oldEnv[name]
    }
    fs.rmSync(output, { recursive: true, force: true })
  }
})
