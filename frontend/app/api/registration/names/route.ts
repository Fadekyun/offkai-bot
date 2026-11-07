import { NextRequest, NextResponse } from 'next/server'

export const runtime = 'nodejs'
export const dynamic = 'force-dynamic'

export async function POST(request: NextRequest) {
  const key = process.env.ADMIN_KEY
  if (!key || request.nextUrl.searchParams.get('key') !== key) {
    return NextResponse.json({ error: 'unauthorized', saved: false }, { status: 401 })
  }
  let body: unknown
  try {
    body = await request.json()
  } catch {
    return NextResponse.json({ error: 'invalid_request', saved: false }, { status: 400 })
  }
  const botUrl = process.env.BOT_ADMIN_URL
  if (!botUrl) return NextResponse.json({ error: 'bot_unavailable', saved: false }, { status: 503 })
  try {
    const response = await fetch(`${botUrl}/registrations/names`, {
      method: 'POST', headers: { Authorization: `Bearer ${key}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(30_000),
    })
    return NextResponse.json(JSON.parse(await response.text()), { status: response.status })
  } catch {
    // The bot may have committed before the response was lost. Do not claim no save happened,
    // and never retry blindly; the admin refreshes to confirm.
    return NextResponse.json({ error: 'names_unconfirmed' }, { status: 502 })
  }
}
