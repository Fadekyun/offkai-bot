import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'
import { NextRequest } from 'next/server.js'

async function loadRoute() {
  const dir = fs.mkdtempSync(path.join(process.cwd(), 'node_modules', '.names-route-'))
  fs.writeFileSync(path.join(dir, 'package.json'), '{"type":"module"}')
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
  compile('app/api', path.join(dir, 'api'))
  const { POST } = await import(pathToFileURL(path.join(dir, 'api/registration/names/route.js')).href)
  return { POST, cleanup: () => fs.rmSync(dir, { recursive: true, force: true }) }
}

const identity = {
  event_name: 'Test event',
  user_id: '191524132624531458',
  registration_timestamp: '2026-10-01T00:00:00.123456Z',
}
const expected = {
  display_name: 'Before',
  extra_people: 2,
  extras_names: ['Alice', null] as (string | null)[],
}
const changes = { display_name: 'After', extras_names: [{ index: 0, value: '  Alicia  ' }] }
const body = { ...identity, expected, changes }

test('names route requires organizer auth and validates malformed payloads before forwarding', async () => {
  const route = await loadRoute()
  const previousFetch = globalThis.fetch
  const previousKey = process.env.ADMIN_KEY
  const previousBotUrl = process.env.BOT_ADMIN_URL
  try {
    process.env.ADMIN_KEY = 'synthetic-admin'
    process.env.BOT_ADMIN_URL = 'http://synthetic-bot.invalid'
    const request = (key = 'synthetic-admin', payload: unknown = body) => new NextRequest(
      `http://localhost/api/registration/names?key=${key}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      })
    const posts: { url: string; init: RequestInit }[] = []
    globalThis.fetch = async (url, init) => {
      posts.push({ url: String(url), init: init! })
      return Response.json({ saved: true, registration: { ...identity, ...changes } })
    }

    assert.equal((await route.POST(request('user-token'))).status, 401)
    const invalidPayloads = [
      null,
      { ...body, user_id: 123 },
      { ...body, user_id: '123' },
      { ...body, registration_timestamp: 'not-a-time' },
      { ...body, unexpected: true },
      { ...body, expected: { ...expected, extra_people: 1 } },
      { ...body, expected: { ...expected, extras_names: ['Alice', 3] } },
      { ...body, changes: {} },
      { ...body, changes: { display_name: '   ' } },
      { ...body, changes: { display_name: 'x'.repeat(33) } },
      { ...body, changes: { extras_names: [{ index: 2, value: 'Nope' }] } },
      { ...body, changes: { extras_names: [{ index: 0, value: 'One' }, { index: 0, value: 'Two' }] } },
      { ...body, changes: { extras_names: [{ index: 0, value: 'x'.repeat(161) }] } },
      { ...body, changes: { unknown: 'value' } },
    ]
    for (const payload of invalidPayloads) {
      const response = await route.POST(request('synthetic-admin', payload))
      assert.equal(response.status, 400, JSON.stringify(payload))
      assert.deepEqual(await response.json(), { error: 'invalid_request', saved: false })
    }
    assert.equal(posts.length, 0)

    const success = await route.POST(request())
    assert.equal(success.status, 200)
    assert.deepEqual(await success.json(), { saved: true, registration: { ...identity, ...changes } })
    assert.equal(posts.length, 1)
    assert.equal(posts[0].url, 'http://synthetic-bot.invalid/registrations/names')
    assert.equal(new Headers(posts[0].init.headers).get('authorization'), 'Bearer synthetic-admin')
    assert.deepEqual(JSON.parse(posts[0].init.body as string), {
      ...identity,
      registration_timestamp: '2026-10-01T00:00:00.123Z',
      expected,
      changes,
    })
    // Surplus legacy guest entries are part of the conflict snapshot and are forwarded verbatim.
    const surplusExpected = { ...expected, extras_names: ['Alice', null, 'Legacy surplus'] }
    const surplus = await route.POST(request('synthetic-admin', { ...body, expected: surplusExpected }))
    assert.equal(surplus.status, 200)
    assert.equal(posts.length, 2)
    assert.deepEqual(JSON.parse(posts[1].init.body as string).expected, surplusExpected)
  } finally {
    globalThis.fetch = previousFetch
    if (previousKey === undefined) delete process.env.ADMIN_KEY; else process.env.ADMIN_KEY = previousKey
    if (previousBotUrl === undefined) delete process.env.BOT_ADMIN_URL; else process.env.BOT_ADMIN_URL = previousBotUrl
    route.cleanup()
  }
})

test('names route forwards conflicts unchanged and never retries an uncertain commit', async () => {
  const route = await loadRoute()
  const previousFetch = globalThis.fetch
  const previousKey = process.env.ADMIN_KEY
  const previousBotUrl = process.env.BOT_ADMIN_URL
  try {
    process.env.ADMIN_KEY = 'synthetic-admin'
    process.env.BOT_ADMIN_URL = 'http://synthetic-bot.invalid'
    const request = () => new NextRequest('http://localhost/api/registration/names?key=synthetic-admin', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    let calls = 0
    globalThis.fetch = async () => {
      calls++
      return Response.json({ error: 'registration_changed', saved: false }, { status: 409 })
    }
    const conflict = await route.POST(request())
    assert.equal(conflict.status, 409)
    assert.deepEqual(await conflict.json(), { error: 'registration_changed', saved: false })
    assert.equal(calls, 1)

    globalThis.fetch = async () => { calls++; throw new Error('Response lost after commit') }
    const uncertain = await route.POST(request())
    assert.equal(uncertain.status, 502)
    assert.deepEqual(await uncertain.json(), { error: 'names_unconfirmed' })
    assert.equal(calls, 2)

    globalThis.fetch = async () => { calls++; return new Response('not json', { status: 200 }) }
    const malformed = await route.POST(request())
    assert.equal(malformed.status, 502)
    assert.deepEqual(await malformed.json(), { error: 'names_unconfirmed' })
    assert.equal(calls, 3)
  } finally {
    globalThis.fetch = previousFetch
    if (previousKey === undefined) delete process.env.ADMIN_KEY; else process.env.ADMIN_KEY = previousKey
    if (previousBotUrl === undefined) delete process.env.BOT_ADMIN_URL; else process.env.BOT_ADMIN_URL = previousBotUrl
    route.cleanup()
  }
})
