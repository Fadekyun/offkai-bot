import { NextRequest, NextResponse } from 'next/server'
import { parseEventParam, parseUserId } from '../../validation'
import { registrationTimestamp } from '../../payments'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

const MAX_DISPLAY_NAME_LENGTH = 32
const MAX_GUEST_NAME_LENGTH = 160
// Name edits use bounded field parts plus one commit, so every request fits the
// same 4 KiB application limit as every other admin route.
const MAX_NAME_EDIT_REQUEST_BYTES = 4096
const MANIFEST_FIELD = /^(?:display_name|guest:[0-4])$/
const BATCH_ID = /^[A-Za-z0-9_-]{20,100}$/
const NAMES_REVISION = /^[0-9a-f]{64}$/

type BoundedJson = { kind: 'ok'; value: unknown } | { kind: 'too_large' } | { kind: 'invalid' }

function cancelBody(body: ReadableStream<Uint8Array> | null): void {
  if (!body) return
  try {
    body.cancel().catch(() => {})
  } catch {
    // A body may already be locked/closed; rejection is already decided.
  }
}

function cancelReader(reader: ReadableStreamDefaultReader<Uint8Array>): void {
  try {
    reader.cancel().catch(() => {})
  } catch {
    // Cancellation is best effort and must never delay the known rejection.
  }
}

// Read the raw UTF-8 body while bounding actual bytes consumed. Content-Length
// is only an early rejection hint; streamed/chunked bodies are counted too.
async function readBoundedJson(request: NextRequest): Promise<BoundedJson> {
  const declared = request.headers.get('content-length')
  if (declared !== null && /^[0-9]+$/.test(declared) && Number(declared) > MAX_NAME_EDIT_REQUEST_BYTES) {
    cancelBody(request.body)
    return { kind: 'too_large' }
  }
  if (!request.body) return { kind: 'invalid' }
  const reader = request.body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      if (!value) continue
      if (total + value.byteLength > MAX_NAME_EDIT_REQUEST_BYTES) {
        cancelReader(reader)
        return { kind: 'too_large' }
      }
      total += value.byteLength
      chunks.push(value)
    }
  } catch {
    cancelReader(reader)
    return { kind: 'invalid' }
  } finally {
    try {
      reader.releaseLock()
    } catch {
      // The reader may still be releasing a best-effort cancellation.
    }
  }
  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  try {
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
    return { kind: 'ok', value: JSON.parse(text) }
  } catch {
    return { kind: 'invalid' }
  }
}

type NamesIdentity = {
  event_name: string
  user_id: string
  registration_timestamp: string
  expected_revision: string
  manifest: string[]
}
type StageBody = NamesIdentity & { operation: 'stage'; field: string; value: string; batch_id?: string }
type CommitBody = NamesIdentity & { operation: 'commit'; batch_id: string }
type NamesBody = StageBody | CommitBody

function exactKeys(body: Record<string, unknown>, required: string[], optional: string[] = []): boolean {
  const keys = Object.keys(body)
  return required.every(key => keys.includes(key)) &&
    keys.every(key => required.includes(key) || optional.includes(key))
}

function parseManifest(raw: unknown): string[] | null {
  if (!Array.isArray(raw) || raw.length < 1 || raw.length > 6) return null
  if (!raw.every(value => typeof value === 'string' && MANIFEST_FIELD.test(value))) return null
  return new Set(raw).size === raw.length ? raw as string[] : null
}

function parseIdentity(body: Record<string, unknown>): NamesIdentity | null {
  const eventName = parseEventParam(body.event_name)
  const userId = typeof body.user_id === 'string' ? parseUserId(body.user_id) : null
  if (!eventName || !userId || !/^[0-9]{16,22}$/.test(userId) ||
      typeof body.registration_timestamp !== 'string' ||
      !Number.isFinite(Date.parse(body.registration_timestamp)) ||
      typeof body.expected_revision !== 'string' || !NAMES_REVISION.test(body.expected_revision)) return null
  const manifest = parseManifest(body.manifest)
  if (!manifest) return null
  return {
    event_name: eventName,
    user_id: userId,
    registration_timestamp: registrationTimestamp(body.registration_timestamp),
    expected_revision: body.expected_revision,
    manifest,
  }
}

// Structural validation only; the bot owns field-length policy against its
// current record and validates the revision immediately before the atomic commit.
function parseNamesBody(raw: unknown): NamesBody | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const body = raw as Record<string, unknown>
  if (body.operation === 'stage') {
    if (!exactKeys(body, ['operation', 'event_name', 'user_id', 'registration_timestamp',
      'expected_revision', 'manifest', 'field', 'value'], ['batch_id'])) return null
    const identity = parseIdentity(body)
    if (!identity) return null
    const field = body.field
    if (typeof field !== 'string' || !MANIFEST_FIELD.test(field) || !identity.manifest.includes(field)) return null
    const maximum = field === 'display_name' ? MAX_DISPLAY_NAME_LENGTH : MAX_GUEST_NAME_LENGTH
    if (typeof body.value !== 'string' || !body.value.trim() || body.value.length > maximum) return null
    if (body.batch_id !== undefined &&
        (typeof body.batch_id !== 'string' || !BATCH_ID.test(body.batch_id))) return null
    return {
      operation: 'stage',
      ...identity,
      field,
      value: body.value,
      ...(body.batch_id !== undefined ? { batch_id: body.batch_id as string } : {}),
    }
  }
  if (body.operation === 'commit') {
    if (!exactKeys(body, ['operation', 'event_name', 'user_id', 'registration_timestamp',
      'expected_revision', 'manifest', 'batch_id'])) return null
    const identity = parseIdentity(body)
    if (!identity || typeof body.batch_id !== 'string' || !BATCH_ID.test(body.batch_id)) return null
    return { operation: 'commit', ...identity, batch_id: body.batch_id }
  }
  return null
}

export async function POST(request: NextRequest) {
  const key = process.env.ADMIN_KEY
  if (!key || request.nextUrl.searchParams.get('key') !== key) {
    return NextResponse.json({ error: 'unauthorized', saved: false }, { status: 401 })
  }
  const bounded = await readBoundedJson(request)
  if (bounded.kind === 'too_large') {
    return NextResponse.json({ error: 'request_too_large', saved: false }, { status: 413 })
  }
  if (bounded.kind !== 'ok') {
    return NextResponse.json({ error: 'invalid_request', saved: false }, { status: 400 })
  }
  const body = parseNamesBody(bounded.value)
  if (!body) return NextResponse.json({ error: 'invalid_request', saved: false }, { status: 400 })
  const botUrl = process.env.BOT_ADMIN_URL
  if (!botUrl) return NextResponse.json({ error: 'bot_unavailable', saved: false }, { status: 503 })
  const forwardedBody = JSON.stringify(body)
  if (new TextEncoder().encode(forwardedBody).byteLength > MAX_NAME_EDIT_REQUEST_BYTES) {
    return NextResponse.json({ error: 'request_too_large', saved: false }, { status: 413 })
  }
  try {
    const response = await fetch(`${botUrl}/registrations/names`, {
      method: 'POST', headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
      body: forwardedBody,
      signal: AbortSignal.timeout(30_000),
    })
    if (response.status === 413) {
      // Known rejection: never wait for or read a possibly stalled/large error body.
      cancelBody(response.body)
      return NextResponse.json({ error: 'request_too_large', saved: false }, { status: 413 })
    }
    return NextResponse.json(JSON.parse(await response.text()), { status: response.status })
  } catch {
    // The bot may have committed before the response was lost. Do not claim no save happened,
    // and never retry blindly; the admin refreshes to confirm.
    return NextResponse.json({ error: 'names_unconfirmed' }, { status: 502 })
  }
}
