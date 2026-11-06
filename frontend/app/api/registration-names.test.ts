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


test('names route bounds incoming raw UTF-8 bytes and classifies backend plain 413 explicitly', async () => {
  const route = await loadRoute()
  const previousFetch = globalThis.fetch
  const previousKey = process.env.ADMIN_KEY
  const previousBotUrl = process.env.BOT_ADMIN_URL
  try {
    process.env.ADMIN_KEY = 'synthetic-admin'
    process.env.BOT_ADMIN_URL = 'http://synthetic-bot.invalid'
    const url = 'http://localhost/api/registration/names?key=synthetic-admin'
    const posts: { init: RequestInit }[] = []
    globalThis.fetch = async (_url, init) => {
      posts.push({ init: init! })
      return Response.json({ saved: true, registration: { ...identity, ...changes } })
    }
    const raw = JSON.stringify(body)
    const exact = raw + ' '.repeat(16384 - Buffer.byteLength(raw, 'utf8'))
    const exactResponse = await route.POST(new NextRequest(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: exact,
    }))
    assert.equal(exactResponse.status, 200)
    assert.equal(posts.length, 1)
    const forwarded = String(posts[0].init.body)
    assert.ok(Buffer.byteLength(forwarded, 'utf8') <= 16384)
    assert.deepEqual(JSON.parse(forwarded), {
      ...identity,
      registration_timestamp: '2026-10-01T00:00:00.123Z',
      expected,
      changes,
    })

    const over = exact + ' '
    const overResponse = await route.POST(new NextRequest(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: over,
    }))
    assert.equal(overResponse.status, 413)
    assert.deepEqual(await overResponse.json(), { error: 'request_too_large', saved: false })
    assert.equal(posts.length, 1)

    // A non-closing stream that crosses the limit must be cancelled without waiting
    // for EOF and without any upstream request.
    let cancelled = false
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('x'.repeat(17000)))
      },
      cancel() { cancelled = true },
    })
    const streamResponse = await Promise.race([
      route.POST(new NextRequest(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: stream, duplex: 'half',
      } as unknown as ConstructorParameters<typeof NextRequest>[1])),
      new Promise<never>((_, reject) => setTimeout(() => reject(new Error('streamed overflow read did not terminate')), 2000)),
    ])
    assert.equal(streamResponse.status, 413)
    assert.deepEqual(await streamResponse.json(), { error: 'request_too_large', saved: false })
    assert.equal(cancelled, true)
    assert.equal(posts.length, 1)

    globalThis.fetch = async (_url, init) => {
      posts.push({ init: init! })
      return new Response('Maximum request body size 16384 exceeded.', {
        status: 413, headers: { 'Content-Type': 'text/plain; charset=utf-8' },
      })
    }
    const plain413 = await route.POST(new NextRequest(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: raw,
    }))
    assert.equal(plain413.status, 413)
    assert.deepEqual(await plain413.json(), { error: 'request_too_large', saved: false })
    assert.equal(posts.length, 2)
  } finally {
    globalThis.fetch = previousFetch
    if (previousKey === undefined) delete process.env.ADMIN_KEY; else process.env.ADMIN_KEY = previousKey
    if (previousBotUrl === undefined) delete process.env.BOT_ADMIN_URL; else process.env.BOT_ADMIN_URL = previousBotUrl
    route.cleanup()
  }
})



const promptly = async <T>(promise: Promise<T>): Promise<T> => {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new Error('known 413 waited on a stalled stream')), 150)
      }),
    ])
  } finally {
    if (timer) clearTimeout(timer)
  }
}

test('names route rejects stalled overflow and stalled backend 413 without awaiting cancellation', async () => {
  const route = await loadRoute()
  const previousFetch = globalThis.fetch
  const previousKey = process.env.ADMIN_KEY
  const previousBotUrl = process.env.BOT_ADMIN_URL
  try {
    process.env.ADMIN_KEY = 'synthetic-admin'
    process.env.BOT_ADMIN_URL = 'http://synthetic-bot.invalid'
    const url = 'http://localhost/api/registration/names?key=synthetic-admin'

    let upstreamCalls = 0
    globalThis.fetch = async () => { upstreamCalls++; throw new Error('Overflow must not be forwarded') }
    for (const declared of [true, false]) {
      let cancels = 0
      const stream = new ReadableStream<Uint8Array>({
        start(controller) { controller.enqueue(new Uint8Array(16385)) },
        cancel() { cancels++; return new Promise(() => {}) },
      })
      const request = new NextRequest(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...(declared ? { 'Content-Length': '16385' } : {}) },
        body: stream,
        duplex: 'half',
      } as unknown as ConstructorParameters<typeof NextRequest>[1])
      const response = await promptly<Response>(route.POST(request))
      assert.equal(response.status, 413)
      assert.deepEqual(await response.json(), { error: 'request_too_large', saved: false })
      assert.equal(cancels, 1)
      assert.equal(upstreamCalls, 0)
    }

    let responseCancels = 0
    globalThis.fetch = async () => new Response(new ReadableStream({
      cancel() { responseCancels++; return new Promise(() => {}) },
    }), { status: 413 })
    const stalled413 = await promptly<Response>(route.POST(new NextRequest(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })))
    assert.equal(stalled413.status, 413)
    assert.deepEqual(await stalled413.json(), { error: 'request_too_large', saved: false })
    assert.equal(responseCancels, 1)
    assert.equal(upstreamCalls, 0)
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
