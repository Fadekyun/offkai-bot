import assert from 'node:assert/strict'
import test from 'node:test'
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'
import { NextRequest } from 'next/server.js'

test('actual paid route rejects pending new choices before writes and preserves selected/legacy cash payments', async () => {
  const dir = fs.mkdtempSync(path.join(process.cwd(), 'node_modules', '.method-guard-'))
  const previousFetch = globalThis.fetch
  const oldEnv = { ...process.env }
  try {
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
    compile('app/lib', path.join(dir, 'lib'))
    const data = path.join(dir, 'data')
    fs.mkdirSync(data)
    process.env.BOT_DATA_DIR = data
    process.env.ADMIN_KEY = 'synthetic-admin'
    process.env.BOT_ADMIN_URL = 'http://synthetic.invalid'
    process.env.MOCK_MODE = 'false'
    const stamp = new Date(Date.now() - 1000).toISOString()
    const uid = '191524132624531458'
    const name = 'Synthetic payment guard'
    fs.writeFileSync(path.join(data, 'events.json'), JSON.stringify([{
      event_name: name, event_datetime: new Date(Date.now() + 86400000).toISOString(),
      open: true, archived: false, signup_form: { fields: [], payment_methods: { Bank: 'Synthetic instructions' } },
    }]))
    const writeRegistration = (method: string | null, demotes: boolean) => fs.writeFileSync(path.join(data, 'responses.json'), JSON.stringify({
      [name]: { attendees: [{ user_id: uid, username: 'Synthetic', timestamp: stamp, payment_method: method,
        payment_selection_demotes: demotes, payment_selection_expires_at: new Date(Date.now() + 86400000).toISOString() }], waitlist: [] },
    }))
    writeRegistration(null, true)
    const { POST } = await import(pathToFileURL(path.join(dir, 'api/payment/route.js')).href)
    const { GET } = await import(pathToFileURL(path.join(dir, 'api/attendees/route.js')).href)
    const roster = await GET(new NextRequest(`http://localhost/api/attendees?key=synthetic-admin&event=${encodeURIComponent(name)}`))
    assert.equal((await roster.json()).attendees[0].payment_method_required, true)
    const request = (paid: boolean) => new NextRequest('http://localhost/api/payment?key=synthetic-admin', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event_name: name, user_id: uid, registration_timestamp: stamp, paid }),
    })
    let callbacks = 0
    globalThis.fetch = async () => { callbacks++; throw new Error('Synthetic callback unavailable') }
    const rejected = await POST(request(true))
    assert.equal(rejected.status, 409)
    assert.deepEqual(await rejected.json(), { error: 'payment_method_required' })
    assert.equal(fs.existsSync(path.join(data, 'payments.json')), false)
    assert.equal(callbacks, 0)
    writeRegistration('Removed method', true)
    assert.equal((await POST(request(true))).status, 409)
    assert.equal((await POST(request(false))).status, 200, 'Unmarking paid remains available')
    writeRegistration('Bank', false)
    const selected = await POST(request(true))
    assert.equal(selected.status, 200)
    assert.equal((await selected.json()).payment.paid, true, 'Callback failure cannot undo a paid record after selection')
    writeRegistration(null, false)
    assert.equal((await POST(request(false))).status, 200)
    assert.equal((await POST(request(true))).status, 200, 'Legacy non-demoting records retain organizer correction access')
    assert.equal(callbacks, 2)
  } finally {
    globalThis.fetch = previousFetch
    for (const key of ['BOT_DATA_DIR', 'ADMIN_KEY', 'BOT_ADMIN_URL', 'MOCK_MODE']) {
      if (oldEnv[key] === undefined) delete process.env[key]; else process.env[key] = oldEnv[key]
    }
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
