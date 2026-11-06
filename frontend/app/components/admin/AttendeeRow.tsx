'use client'
import { EditNames } from './EditNames'
import { PaymentControl } from './PaymentControl'
import { RemoveRegistration } from './RemoveRegistration'
import { drinkDot } from '../../lib/format'
import type { Attendee, CheckinRecord } from '../../lib/types'
import type { NamesChanges, NamesSaveOutcome } from '../../hooks/useAdminData'

export function AttendeeRow({
  attendee: a,
  checkin,
  onCheckin,
  onCheckout,
  adminKey, eventName, onPayment, onRemove, onSaveNames,
}: {
  adminKey: string
  eventName: string
  onPayment: (userId: string, paid: boolean, timestamp: string) => Promise<boolean>
  onRemove: (userId: string, timestamp: string) => Promise<boolean>
  onSaveNames: (eventName: string, userId: string, timestamp: string, expectedRevision: string,
    changes: NamesChanges) => Promise<NamesSaveOutcome>
  attendee: Attendee
  checkin?: CheckinRecord
  onCheckin: (userId: string, timestamp: string) => void
  onCheckout: (userId: string, timestamp: string) => void
}) {
  const name = a.display_name || a.username
  const isIn = !!checkin
  const checkinTime = isIn ? new Date(checkin!.checked_in_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : null
  return (
    <div className={`rounded-2xl border-2 border-[#17120F] overflow-hidden shadow-[4px_4px_0_rgba(23,18,15,0.25)] ${isIn ? 'bg-green-50' : 'bg-white'}`}>
      <div className="p-4 flex items-center gap-3">
        <div className={`w-10 h-10 rounded-full border-2 border-[#17120F] flex items-center justify-center font-black text-lg shrink-0 ${isIn ? 'bg-green-100 text-green-800' : 'bg-[#FFD51B] text-[#17120F]'}`}>
          {isIn ? '✓' : name[0].toUpperCase()}
        </div>
        <div className="flex-1 min-w-0">
          <p className="font-black text-[#17120F] break-words">
            {a.attendee_number != null && (
              <span className="mr-1.5 inline-block rounded-md border-2 border-[#17120F] bg-[#FFD51B] px-1.5 text-[11px] tabular-nums align-middle">#{a.attendee_number}</span>
            )}
            {name}
          </p>
          <div className="flex flex-wrap gap-1 mt-1">
            {a.drinks.map((d, i) => (
              <span key={i} className="flex items-center gap-1 text-[9px] font-bold text-[#5B3428] uppercase tracking-wide">
                <span className={`w-2 h-2 rounded-full shrink-0 ${drinkDot(d)}`} />
                {d}
              </span>
            ))}
          </div>
          {a.extra_people > 0 && (
            <p className="text-[9px] text-[#8B2D1F] mt-0.5">+{a.extra_people} guest{a.extra_people > 1 ? 's' : ''}{a.extras_names.length > 0 ? `: ${a.extras_names.join(', ')}` : ''}</p>
          )}
          <p className="mt-1 text-[9px] font-bold uppercase tracking-widest text-[#8B2D1F]/60">@{a.username}</p>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <div className="text-right min-w-[34px]">
            {isIn ? (
              <div>
                <span className="text-[9px] font-black text-green-700 uppercase tracking-widest">In</span>
                <p className="text-[9px] text-[#8B2D1F]">{checkinTime}</p>
              </div>
            ) : (
              <span className="text-[9px] font-black text-[#8B2D1F]/60 uppercase tracking-widest">Pending</span>
            )}
          </div>
          {/* Missing registration identities cannot safely mutate arrival. */}
          <button
            onClick={() => a.registration_timestamp && onCheckin(a.user_id, a.registration_timestamp)}
            disabled={!a.registration_timestamp}
            aria-label={`Check in ${name}`}
            title="Check in"
            className={`w-11 h-11 rounded-xl border-2 border-[#17120F] flex items-center justify-center shrink-0 active:translate-x-[1px] active:translate-y-[1px] transition ${isIn ? 'bg-green-600 text-white' : 'bg-[#FFD51B] text-[#17120F]'}`}
          >
            <svg viewBox="0 0 24 24" className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M20 6 9 17l-5-5" />
            </svg>
          </button>
          {/* Checkout targets the same displayed registration as check-in. */}
          <button
            onClick={() => a.registration_timestamp && onCheckout(a.user_id, a.registration_timestamp)}
            disabled={!a.registration_timestamp}
            aria-label={`Check out ${name}`}
            title="Check out"
            className="w-11 h-11 rounded-xl border-2 border-[#17120F] flex items-center justify-center shrink-0 bg-white text-[#E51F1F] active:translate-x-[1px] active:translate-y-[1px] transition"
          >
            <svg viewBox="0 0 24 24" className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
          <EditNames attendee={a} eventName={eventName} onSave={onSaveNames} />
          <RemoveRegistration attendee={a} onRemove={onRemove} />
        </div>
      </div>
      <PaymentControl attendee={a} adminKey={adminKey} eventName={eventName} onPayment={onPayment} />
    </div>
  )
}
