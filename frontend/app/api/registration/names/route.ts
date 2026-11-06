import { NextRequest, NextResponse } from 'next/server'
import { parseEventParam, parseUserId } from '../../validation'
import { registrationTimestamp } from '../../payments'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

const MAX_DISPLAY_NAME_LENGTH = 32
const MAX_GUEST_NAME_LENGTH = 160
const MAX_EXTRA_PEOPLE = 5
// Mirrors the bot's names-only allowance. Other admin routes retain 4 KiB.
const MAX_NAME_EDIT_REQUEST_BYTES = 16 * 1024

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

type NamesBody = {
  event_name: string
  user_id: string
  registration_timestamp: string
  expected: { display_name: string | null; extra_people: number; extras_names: (string | null)[] }
  changes: { display_name?: string; extras_names?: { index: number; value: string }[] }
}

// Structural validation only; the bot validates trimmed/length policy against
// its current record immediately before the atomic commit.
function parseNamesBody(raw: unknown): NamesBody | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const body = raw as Record<string, unknown>
  if (Object.keys(body).sort().join(',') !== 'changes,event_name,expected,registration_timestamp,user_id') return null
  const eventName = parseEventParam(body.event_name)
  const userId = typeof body.user_id === 'string' ? parseUserId(body.user_id) : null
  if (!eventName || !userId || !/^[0-9]{16,22}$/.test(userId) || !/^[0-9]{16,22}$/.test(body.user_id as string) ||
      typeof body.registration_timestamp !== 'string' || !Number.isFinite(Date.parse(body.registration_timestamp))) return null
  const expected = body.expected
  if (!expected || typeof expected !== 'object' || Array.isArray(expected)) return null
  const snapshot = expected as Record<string, unknown>
  if (Object.keys(snapshot).sort().join(',') !== 'display_name,extra_people,extras_names') return null
  if (snapshot.display_name !== null && typeof snapshot.display_name !== 'string') return null
  if (typeof snapshot.extra_people !== 'number' || !Number.isInteger(snapshot.extra_people) ||
      snapshot.extra_people < 0 || snapshot.extra_people > MAX_EXTRA_PEOPLE) return null
  const extraPeople = snapshot.extra_people as number
  const extras = snapshot.extras_names
  if (!Array.isArray(extras) || extras.length < extraPeople) return null
  if (!extras.every((slot, index) => index < extraPeople
    ? slot === null || typeof slot === 'string' : typeof slot === 'string')) return null
  const changes = body.changes
  if (!changes || typeof changes !== 'object' || Array.isArray(changes)) return null
  const patch = changes as Record<string, unknown>
  const patchKeys = Object.keys(patch)
  if (!patchKeys.length || patchKeys.some(key => key !== 'display_name' && key !== 'extras_names')) return null
  if ('display_name' in patch &&
      (typeof patch.display_name !== 'string' || !patch.display_name.trim() ||
        patch.display_name.trim().length > MAX_DISPLAY_NAME_LENGTH)) return null
  const guestChanges = patch.extras_names
  if (guestChanges !== undefined) {
    if (!Array.isArray(guestChanges)) return null
    const indices = new Set<number>()
    for (const change of guestChanges) {
      if (!change || typeof change !== 'object' || Array.isArray(change)) return null
      const entry = change as Record<string, unknown>
      if (Object.keys(entry).sort().join(',') !== 'index,value') return null
      const index = entry.index
      if (typeof index !== 'number' || !Number.isInteger(index) || index < 0 || index >= snapshot.extra_people ||
          indices.has(index) || typeof entry.value !== 'string' || !entry.value.trim() ||
          entry.value.trim().length > MAX_GUEST_NAME_LENGTH) return null
      indices.add(index)
    }
  }
  if (patch.display_name === undefined && (!guestChanges || guestChanges.length === 0)) return null
  return {
    event_name: eventName,
    user_id: userId,
    registration_timestamp: registrationTimestamp(body.registration_timestamp),
    expected: {
      display_name: snapshot.display_name as string | null,
      extra_people: extraPeople,
      extras_names: extras as (string | null)[],
    },
    changes: {
      ...(patch.display_name !== undefined ? { display_name: patch.display_name as string } : {}),
      ...(guestChanges !== undefined ? { extras_names: guestChanges as { index: number; value: string }[] } : {}),
    },
  }
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
