'use client'
import { useEffect, useRef, useState } from 'react'
import { useT } from '../../lib/i18n'
import type { Attendee } from '../../lib/types'
import type { NamesChanges, NamesSaveOutcome } from '../../hooks/useAdminData'

const MAX_DISPLAY_NAME_LENGTH = 32
const MAX_GUEST_NAME_LENGTH = 160

type Draft = {
  identity: string
  eventName: string
  userId: string
  timestamp: string
  namesRevision: string
  originalDisplayName: string | null
  originalExtrasNames: (string | null)[]
  displayNameInput: string
  guestInputs: string[]
}

type ErrorKind = 'invalid' | 'conflict' | 'too_large' | 'unconfirmed' | 'busy' | 'expired' | 'error' | 'unchanged'

function buildChanges(draft: Draft): { changes: NamesChanges | null; invalid: boolean } {
  const changes: NamesChanges = {}
  let invalid = false
  const trimmedDisplayName = draft.displayNameInput.trim()
  if (draft.displayNameInput !== (draft.originalDisplayName ?? '')) {
    if (!trimmedDisplayName || trimmedDisplayName.length > MAX_DISPLAY_NAME_LENGTH) invalid = true
    else changes.display_name = trimmedDisplayName
  }
  const guestChanges: { index: number; value: string }[] = []
  draft.guestInputs.forEach((input, index) => {
    if (input === (draft.originalExtrasNames[index] ?? '')) return
    const trimmed = input.trim()
    if (!trimmed || trimmed.length > MAX_GUEST_NAME_LENGTH) invalid = true
    else guestChanges.push({ index, value: trimmed })
  })
  if (invalid) return { changes: null, invalid: true }
  if (changes.display_name === undefined && guestChanges.length === 0) return { changes: null, invalid: false }
  if (guestChanges.length > 0) changes.extras_names = guestChanges
  return { changes, invalid: false }
}

export function EditNames({ attendee, eventName, onSave }: {
  attendee: Attendee
  eventName: string
  onSave: (eventName: string, userId: string, timestamp: string, expectedRevision: string,
    changes: NamesChanges) => Promise<NamesSaveOutcome>
}) {
  const { t } = useT()
  const identity = `${eventName}:${attendee.user_id}:${attendee.registration_timestamp ?? ''}:${attendee.status}`
  const [draft, setDraft] = useState<Draft | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ErrorKind | null>(null)
  const [saved, setSaved] = useState(false)
  const inFlight = useRef(false)
  const requestToken = useRef(0)
  const currentIdentity = useRef(identity)
  const previousIdentity = useRef(identity)
  const activeDraft = draft?.identity === identity ? draft : null
  const name = attendee.display_name || attendee.username

  // A rejoin, event switch or demotion invalidates the editor and its snapshot.
  useEffect(() => {
    currentIdentity.current = identity
    if (previousIdentity.current === identity) return
    previousIdentity.current = identity
    requestToken.current += 1
    inFlight.current = false
    setDraft(null)
    setBusy(false)
    setError(null)
    setSaved(false)
  }, [identity])

  useEffect(() => {
    if (!activeDraft) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !inFlight.current) setDraft(null)
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [activeDraft])

  function open() {
    if (attendee.status !== 'attending' || !attendee.registration_timestamp ||
        typeof attendee.names_revision !== 'string') return
    requestToken.current += 1
    inFlight.current = false
    setBusy(false)
    const originalExtrasNames = Array.from(
      { length: Math.max(attendee.extra_people, attendee.extras_names.length) },
      (_, index) => index < attendee.extra_people
        ? (index < attendee.extras_names.length ? attendee.extras_names[index] : null)
        : attendee.extras_names[index],
    )
    setDraft({
      identity,
      eventName,
      userId: attendee.user_id,
      timestamp: attendee.registration_timestamp,
      namesRevision: attendee.names_revision,
      originalDisplayName: attendee.display_name ?? null,
      originalExtrasNames,
      displayNameInput: attendee.display_name ?? '',
      guestInputs: Array.from({ length: attendee.extra_people },
        (_, index) => index < attendee.extras_names.length ? attendee.extras_names[index] : ''),
    })
    setError(null)
    setSaved(false)
  }

  function cancel() {
    if (inFlight.current) return
    setDraft(null)
    setError(null)
  }

  async function submit(submitEvent: React.FormEvent) {
    submitEvent.preventDefault()
    if (!activeDraft || inFlight.current) return
    const result = buildChanges(activeDraft)
    if (result.invalid) { setError('invalid'); return }
    if (!result.changes) { setError('unchanged'); return }
    const requestIdentity = activeDraft.identity
    const token = ++requestToken.current
    inFlight.current = true
    setBusy(true)
    setError(null)
    let outcome: NamesSaveOutcome
    try {
      outcome = await onSave(
        activeDraft.eventName,
        activeDraft.userId,
        activeDraft.timestamp,
        activeDraft.namesRevision,
        result.changes,
      )
    } catch {
      outcome = 'unconfirmed'
    } finally {
      if (requestToken.current === token && currentIdentity.current === requestIdentity) {
        inFlight.current = false
        setBusy(false)
      }
    }
    // A rejoin/demotion, event switch or newer editing request owns the state now.
    if (requestToken.current !== token || currentIdentity.current !== requestIdentity) return
    if (outcome === 'saved') { setDraft(null); setSaved(true); return }
    // A stale event/identity response must not surface in the new view.
    if (outcome === 'stale') return
    if (outcome === 'conflict' || outcome === 'unconfirmed') { setDraft(null); setError(outcome); return }
    if (outcome === 'invalid') { setError('invalid'); return }
    if (outcome === 'too_large') { setError('too_large'); return }
    if (outcome === 'busy') { setError('busy'); return }
    if (outcome === 'expired') { setError('expired'); return }
    setError('error')
  }

  if (attendee.status !== 'attending') return null
  const errorMessage = error === 'invalid' ? t.namesInvalid
    : error === 'conflict' ? t.namesConflict
      : error === 'too_large' ? t.namesTooLarge
        : error === 'unconfirmed' ? t.namesUnconfirmed
          : error === 'busy' ? t.namesBusy
            : error === 'expired' ? t.namesExpired
              : error === 'unchanged' ? t.namesNoChanges
                : t.namesSaveError

  return (
    <div className="relative text-xs">
      <button
        type="button"
        className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border-2 border-[#17120F] bg-blue-50 text-blue-800 transition active:translate-x-[1px] active:translate-y-[1px]"
        disabled={!attendee.registration_timestamp || typeof attendee.names_revision !== 'string'}
        onClick={open}
        aria-label={t.editNames}
        title={t.editNames}
      >
        <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z" />
        </svg>
      </button>
      {saved && !activeDraft && <p role="status" className="mt-1 w-40 font-bold text-green-800">{t.namesSaved}</p>}
      {error && !activeDraft && <p role="alert" className="mt-1 w-40 leading-snug text-red-700">{errorMessage}</p>}
      {activeDraft && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#17120F]/40 p-4">
          <form role="dialog" aria-modal="true" aria-labelledby={`names-title-${attendee.user_id}`}
            onSubmit={submit} className="brand-card max-h-[90dvh] w-full max-w-lg space-y-3 overflow-y-auto rounded-2xl p-4">
            <h2 id={`names-title-${attendee.user_id}`} className="text-base font-black text-[#17120F]">
              {t.editNames}: {name}
            </h2>
            <div>
              <label htmlFor={`names-display-${attendee.user_id}`} className="text-[10px] font-black uppercase tracking-widest text-[#8B2D1F]">
                {t.attendeeName}
              </label>
              <input id={`names-display-${attendee.user_id}`} autoFocus maxLength={MAX_DISPLAY_NAME_LENGTH}
                value={activeDraft.displayNameInput}
                onChange={change => setDraft(current => current ? { ...current, displayNameInput: change.target.value } : current)}
                className="mt-1 min-h-[44px] w-full rounded-xl border-2 border-[#17120F] bg-white px-3 text-sm text-[#17120F]" />
            </div>
            {activeDraft.guestInputs.map((value, index) => (
              <div key={index}>
                <label htmlFor={`names-guest-${attendee.user_id}-${index}`} className="text-[10px] font-black uppercase tracking-widest text-[#8B2D1F]">
                  {t.guestName(index + 1)}
                </label>
                <input id={`names-guest-${attendee.user_id}-${index}`} maxLength={MAX_GUEST_NAME_LENGTH}
                  value={value} placeholder={t.unnamedGuest}
                  onChange={change => setDraft(current => current ? {
                    ...current,
                    guestInputs: current.guestInputs.map((guest, slot) => slot === index ? change.target.value : guest),
                  } : current)}
                  className="mt-1 min-h-[44px] w-full rounded-xl border-2 border-[#17120F] bg-white px-3 text-sm text-[#17120F]" />
              </div>
            ))}
            {error && <p role="alert" className="text-sm font-bold text-red-700">{errorMessage}</p>}
            <div className="flex flex-wrap gap-2 pt-1">
              <button type="submit" disabled={busy} className="brand-action min-h-[44px] rounded-xl px-4 py-2">
                {busy ? '…' : t.saveNames}
              </button>
              <button type="button" disabled={busy} onClick={cancel}
                className="min-h-[44px] rounded-xl border-2 border-[#17120F] bg-white px-4 py-2 font-black text-[#17120F]">
                {t.cancel}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  )
}
