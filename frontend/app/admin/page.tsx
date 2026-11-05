'use client'
import { useCallback } from 'react'
import { useAdminData } from '../hooks/useAdminData'
import { useScanner } from '../hooks/useScanner'
import { AdminLogin } from '../components/admin/AdminLogin'
import { StatHeader } from '../components/admin/StatHeader'
import { FilterBar } from '../components/admin/FilterBar'
import { AttendeeRow } from '../components/admin/AttendeeRow'
import { WaitlistList } from '../components/admin/WaitlistList'
import { ScanPopup } from '../components/admin/ScanPopup'

export default function AdminPage() {
  const data = useAdminData()
  const scanner = useScanner({
    adminKey: data.key,
    selectedEvent: data.selectedEvent,
    eventName: data.eventName,
    attendees: data.attendees,
    onCheckedIn: data.applyScanCheckin,
  })

  // Switching events clears both the sticky rows (data) and any open scan popup.
  const changeEvent = useCallback((next: string) => {
    data.changeEvent(next)
    scanner.clearResult()
  }, [data, scanner])

  if (!data.authed) {
    return (
      <AdminLogin
        keyInput={data.keyInput}
        setKeyInput={data.setKeyInput}
        loginError={data.loginError}
        onLogin={data.handleLogin}
      />
    )
  }

  return (
    <main className="brand-bg min-h-dvh w-full max-w-6xl mx-auto text-[#23110D] pb-12">
      <StatHeader
        eventName={data.eventName}
        events={data.events}
        selectedEvent={data.selectedEvent}
        onChangeEvent={changeEvent}
        pendingCount={data.pendingCount}
        checkedInCount={data.checkedInCount}
        attendingCount={data.attendingCount}
        waitlistCount={data.waitlistCount}
        scanning={scanner.scanning}
        onToggleScan={scanner.scanning ? scanner.stopScanner : scanner.startScanner}
      />

      <FilterBar
        filter={data.filter}
        onChangeFilter={data.changeFilter}
        search={data.search}
        onSearch={data.setSearch}
      />

      <div className="p-4 space-y-4 lg:p-6">
        {data.actionWarnings.map((warning, index) => <p key={index} role="status" className="brand-card rounded-xl p-3 text-sm">{warning}</p>)}
        {/* Scanner — div is always mounted so html5-qrcode can attach to it. */}
        <div className={`brand-card rounded-2xl overflow-hidden ${scanner.scanning ? '' : 'hidden'}`}>
          <div id={scanner.scannerDivId} className="w-full" />
        </div>

        <div className="space-y-2">
          {data.filtered.map(a => (
            <AttendeeRow
              key={`${data.eventName}:${a.user_id}:${a.registration_timestamp ?? 'none'}`}
              attendee={a}
              adminKey={data.key} eventName={data.eventName} onPayment={data.updatePayment}
              onRemove={data.removeRegistration}
              onSaveNames={data.saveAttendeeNames}
              checkin={data.checkins[a.user_id]}
              onCheckin={data.manualCheckin}
              onCheckout={data.manualCheckout}
            />
          ))}
          {data.filtered.length === 0 && (
            <p className="text-center text-sm font-black text-[#8B2D1F] py-8">No attendees found</p>
          )}
        </div>

        {data.filter === 'all' && !data.search && <WaitlistList waitlist={data.waitlist} adminKey={data.key} eventName={data.eventName} onPayment={data.updatePayment} onRemove={data.removeRegistration} onPromote={data.promoteRegistration} />}
      </div>

      {scanner.scanResult && (
        <ScanPopup result={scanner.scanResult} popupMsLeft={scanner.popupMsLeft} onDismiss={scanner.dismissPopup} />
      )}
    </main>
  )
}
