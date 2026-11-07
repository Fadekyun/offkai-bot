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

const body = {
  event_name: 'Test event',
  user_id: '191524132624531458',
  registration_timestamp: '2026-10-01T00:00:00.123456Z',
  expected_revision: 'a'.repeat(64),
  display_name: 'After',
  guest_names: { 1: 'Bea' },
}

test('names route requires organizer auth and forwards one request unchanged', async () => {
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
      return Response.json({ saved: true, warnings: [] })
    }

    assert.equal((await route.POST(request('user-token'))).status, 401)
    assert.equal(posts.length, 0)
    const saved = await route.POST(request())
    assert.equal(saved.status, 200)
    assert.deepEqual(await saved.json(), { saved: true, warnings: [] })
    assert.equal(posts.length, 1)
    assert.equal(posts[0].url, 'http://synthetic-bot.invalid/registrations/names')
    assert.equal(new Headers(posts[0].init.headers).get('authorization'), 'Bearer synthetic-admin')
    assert.deepEqual(JSON.parse(posts[0].init.body as string), body)

    // The bot, not this route, owns field validation. Its rejection is forwarded.
    globalThis.fetch = async (_url, init) => {
      posts.push({ url: 'invalid', init: init! })
      return Response.json({ error: 'invalid_request', saved: false }, { status: 400 })
    }
    const rejected = await route.POST(request('synthetic-admin', { ...body, unexpected: true }))
    assert.equal(rejected.status, 400)
    assert.deepEqual(await rejected.json(), { error: 'invalid_request', saved: false })
    assert.deepEqual(JSON.parse(posts[1].init.body as string), { ...body, unexpected: true })
  } finally {
    globalThis.fetch = previousFetch
    if (previousKey === undefined) delete process.env.ADMIN_KEY; else process.env.ADMIN_KEY = previousKey
    if (previousBotUrl === undefined) delete process.env.BOT_ADMIN_URL; else process.env.BOT_ADMIN_URL = previousBotUrl
    route.cleanup()
  }
})

test('the largest single name-edit JSON request fits the unchanged 4 KiB limit', () => {
  const emoji = '😀'
  const maximum = {
    event_name: emoji.repeat(100),
    user_id: '1'.repeat(22),
    registration_timestamp: '2'.repeat(100),
    expected_revision: 'a'.repeat(64),
    display_name: emoji.repeat(32),
    guest_names: Object.fromEntries(Array.from({ length: 5 }, (_, index) => [String(index), emoji.repeat(160)])),
  }
  const raw = JSON.stringify(maximum)
  assert.ok(Buffer.byteLength(raw, 'utf8') <= 4096)
  assert.equal(Buffer.byteLength(raw, 'utf8'), 4064)
})

test('names route forwards a structured bot 413 and never retries an uncertain save', async () => {
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
      return Response.json({ error: 'request_too_large', saved: false }, { status: 413 })
    }
    const tooLarge = await route.POST(request())
    assert.equal(tooLarge.status, 413)
    assert.deepEqual(await tooLarge.json(), { error: 'request_too_large', saved: false })
    assert.equal(calls, 1)

    globalThis.fetch = async () => {
      calls++
      return Response.json({ error: 'registration_changed', saved: false }, { status: 409 })
    }
    const conflict = await route.POST(request())
    assert.equal(conflict.status, 409)
    assert.deepEqual(await conflict.json(), { error: 'registration_changed', saved: false })
    assert.equal(calls, 2)

    globalThis.fetch = async () => { calls++; throw new Error('Response lost after save') }
    const uncertain = await route.POST(request())
    assert.equal(uncertain.status, 502)
    assert.deepEqual(await uncertain.json(), { error: 'names_unconfirmed' })
    assert.equal(calls, 3)

    globalThis.fetch = async () => { calls++; return new Response('not json', { status: 200 }) }
    const malformed = await route.POST(request())
    assert.equal(malformed.status, 502)
    assert.deepEqual(await malformed.json(), { error: 'names_unconfirmed' })
    assert.equal(calls, 4)
  } finally {
    globalThis.fetch = previousFetch
    if (previousKey === undefined) delete process.env.ADMIN_KEY; else process.env.ADMIN_KEY = previousKey
    if (previousBotUrl === undefined) delete process.env.BOT_ADMIN_URL; else process.env.BOT_ADMIN_URL = previousBotUrl
    route.cleanup()
  }
})
