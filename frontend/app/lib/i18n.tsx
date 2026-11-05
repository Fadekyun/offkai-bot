'use client'
import { createContext, useCallback, useContext, useSyncExternalStore } from 'react'

export type Lang = 'en' | 'ja'

export const STRINGS = {
  en: {
    paymentUnavailable: "Payment data unavailable. Please contact the organizer.",
    payment: "Payment",
    paid: "Paid",
    unpaid: "Unpaid",
    paymentPending: "Pending",
    waitingForProof: "Pending payment proof",
    waitingForPaymentMethod: "Waiting for payment method",
    removeUser: "Remove user",
    promoteUser: "Promote to confirmed",
    confirmPromote: (name: string) => `Promote ${name} and their guests to confirmed attendance, even if the event is full? Events with payment methods will send a fresh 72-hour payment-choice window.`,
    promotionRegistrationChanged: "This registration changed. The list has been refreshed; check it before promoting again.",
    promotionNotStarted: "Promotion could not start. Check the registration and event before trying again.",
    promotionError: "Promotion could not be confirmed. Check the refreshed list before trying again.",
    confirmRemove: (name: string) => `Remove ${name} and their guests from this event and Discord thread? Freed places will be offered to the waitlist.`,
    removalError: "Could not confirm removal. Refresh the list before trying again.",
    removalRegistrationChanged: "This registration changed. The list has been refreshed; check it before removing again.",
    removalNotStarted: "Removal did not start. Check the bot's access to this user and event, then try again.",
    editNames: "Edit names",
    attendeeName: "Attendee name",
    guestName: (n: number) => `Guest ${n} name`,
    unnamedGuest: "Unnamed guest",
    saveNames: "Save names",
    cancel: "Cancel",
    namesSaved: "Names saved.",
    namesNoChanges: "No changes to save.",
    namesInvalid: "Enter a non-blank name within the allowed length in each edited field.",
    namesConflict: "This registration changed. Refresh the list and check it before editing again.",
    namesUnconfirmed: "Could not confirm whether the names were saved. Refresh the list and check it before trying again.",
    namesSaveError: "Could not save names. Try again.",
    markPaid: "Mark paid",
    confirmPaidWithoutProof: "Mark this attendee paid without an uploaded proof? Confirm you have received their payment.",
    paymentPreviewError: "Could not load payment proof. Try again.",
    markUnpaid: "Mark unpaid",
    previewProof: "Preview proof",
    uploadProof: "Upload payment proof",
    chooseFile: "Choose file",
    noFileChosen: "No file chosen",
    replaceProof: "Replace payment proof",
    paymentProof: "Payment proof",
    proofDoesNotMarkPaid: "Uploading proof does not mark payment as paid. The organizer confirms payment.",
    proofExpired: "The payment proof retention period has ended.",
    paymentImageLimit: "JPEG, PNG, WebP or GIF; up to 10 MiB.",
    paymentUploadError: "Could not upload proof. Check the image or try again.",
    paymentSaveError: "Could not save payment status. Try again.",
    paymentMethodRequired: "The attendee must choose a payment method before you can mark them paid.",
    locale: 'en-GB',
    rsvpPass: 'RSVP Pass',
    loading: 'Loading...',
    checkDms: 'Check Your DMs',
    checkDmsBody: 'Your personal event link was sent to you via Discord DM. Open that message and tap the link to view your RSVP details.',
    linkInvalid: 'Link Invalid',
    rsvpNotFound: 'RSVP Not Found',
    rsvpUnavailable: 'RSVP Unavailable',
    notFoundBody: "We couldn't find your RSVP for this event. Check with the event host if you believe this is an error.",
    unavailableBody: "We couldn't load your RSVP right now. Check your connection and try again.",
    invalidBody: 'This link has expired or is invalid. Check your Discord DMs for an updated link.',
    retry: 'Retry',
    offkaiPass: 'Offkai Pass',
    today: 'Today',
    arrival: 'Arrival',
    entryPass: 'Entry Pass',
    waitlist: 'Waitlist',
    confirmed: 'Confirmed',
    name: 'Name',
    entryNo: 'Entry No.',
    guestNos: 'Guest no.',
    showAtCheckin: 'Show at check-in',
    live: 'Live',
    standbyMode: 'Standby Mode',
    standbyBody: "You're on the waitlist — we'll DM you on Discord if a spot opens up.",
    venue: 'Venue',
    maps: 'Maps',
    route: 'Route',
    drinkTickets: 'Drink Tickets',
    firstDrinkTicket: 'First Drink Ticket',
    readyCheck: 'Ready Check',
    chkEntry: 'Entry pass',
    chkRules: 'Rules',
    chkArrival: 'Arrival',
    offkaiDetails: 'Offkai Details',
    capacity: 'Capacity',
    rsvp: 'RSVP',
    deadline: 'Deadline',
    personalLink: "This link is personal — please don't share it.",
    open: 'Open',
    closed: 'Closed',
    tbd: 'TBD',
    tba: 'TBA',
    solo: 'Solo',
    partyOf: (n: number) => `Party of ${n}`,
    guests: (n: number) => `+${n} guest${n > 1 ? 's' : ''}`,
    datePending: 'Date pending',
    startsOn: (d: string) => `Starts ${d}`,
    startsInDays: (n: number) => `In ${n} day${n === 1 ? '' : 's'}`,
    startsInHours: (h: number, m: number) => `In ${h} hr${h === 1 ? '' : 's'}${m > 0 ? ` ${m} min` : ''}`,
    startsIn: (m: number) => `Starts in ${m} min`,
    happeningNow: 'Happening now',
    eventEnded: 'Event ended',
  },
  ja: {
    paymentUnavailable: "支払い情報を取得できません。主催者にお問い合わせください。",
    payment: "お支払い",
    paid: "支払い済み",
    unpaid: "未払い",
    paymentPending: "確認待ち",
    waitingForProof: "支払い証明待ち",
    waitingForPaymentMethod: "支払い方法の選択待ち",
    removeUser: "参加者を削除",
    promoteUser: "参加確定に昇格",
    confirmPromote: (name: string) => `定員に達していても、${name}さんと同伴者を参加確定に昇格しますか？支払い方法が設定されたイベントでは、新たに72時間の選択期限が案内されます。`,
    promotionRegistrationChanged: "登録情報が変更されました。一覧を更新したので、再度昇格する前に確認してください。",
    promotionNotStarted: "昇格を開始できませんでした。登録情報とイベントを確認してください。",
    promotionError: "昇格結果を確認できませんでした。再試行する前に更新された一覧を確認してください。",
    confirmRemove: (name: string) => `${name}さんと同伴者をこのイベントとDiscordスレッドから削除しますか？空いた枠はウェイトリストに割り当てられます。`,
    removalError: "削除を確認できませんでした。再試行する前に一覧を更新してください。",
    removalRegistrationChanged: "登録情報が変更されました。一覧を更新したので、再度削除する前に確認してください。",
    removalNotStarted: "削除は実行されていません。ボットがユーザーとイベントにアクセスできるか確認し、再試行してください。",
    editNames: "名前を編集",
    attendeeName: "参加者名",
    guestName: (n: number) => `同伴者${n}の名前`,
    unnamedGuest: "未入力の同伴者",
    saveNames: "名前を保存",
    cancel: "キャンセル",
    namesSaved: "名前を保存しました。",
    namesNoChanges: "保存する変更がありません。",
    namesInvalid: "編集した各項目に、空白以外の名前を文字数以内で入力してください。",
    namesConflict: "登録情報が変更されました。一覧を更新して確認してから、再度編集してください。",
    namesUnconfirmed: "名前を保存できたか確認できませんでした。一覧を更新して確認してから、再試行してください。",
    namesSaveError: "名前を保存できませんでした。再試行してください。",
    markPaid: "支払い済みにする",
    confirmPaidWithoutProof: "支払い証明が未提出ですが、支払い済みにしますか？入金を確認してください。",
    paymentPreviewError: "支払い証明を読み込めませんでした。再試行してください。",
    markUnpaid: "未払いにする",
    previewProof: "証明画像を確認",
    uploadProof: "支払い証明をアップロード",
    chooseFile: "ファイルを選択",
    noFileChosen: "ファイルが選択されていません",
    replaceProof: "支払い証明を差し替え",
    paymentProof: "支払い証明",
    proofDoesNotMarkPaid: "画像のアップロードだけでは支払い済みになりません。主催者が確認します。",
    proofExpired: "支払い証明の保存期間が終了しました。",
    paymentImageLimit: "JPEG・PNG・WebP・GIF、最大10 MiB。",
    paymentUploadError: "アップロードできませんでした。画像を確認して再試行してください。",
    paymentSaveError: "支払い状態を保存できませんでした。再試行してください。",
    paymentMethodRequired: "支払い済みにする前に、参加者が支払い方法を選択する必要があります。",
    locale: 'ja-JP',
    rsvpPass: '参加パス',
    loading: '読み込み中...',
    checkDms: 'DMをご確認ください',
    checkDmsBody: '個別の参加リンクをDiscordのDMでお送りしました。そのメッセージを開き、リンクをタップして参加情報をご確認ください。',
    linkInvalid: 'リンクが無効です',
    rsvpNotFound: '参加情報が見つかりません',
    rsvpUnavailable: '参加情報を取得できません',
    notFoundBody: 'このイベントの参加情報が見つかりませんでした。お心当たりがない場合は主催者にお問い合わせください。',
    unavailableBody: '現在、参加情報を読み込めませんでした。接続を確認して再度お試しください。',
    invalidBody: 'このリンクは期限切れか無効です。最新のリンクはDiscordのDMをご確認ください。',
    retry: '再試行',
    offkaiPass: 'オフ会パス',
    today: '本日',
    arrival: '到着',
    entryPass: '入場パス',
    waitlist: 'キャンセル待ち',
    confirmed: '確定',
    name: 'お名前',
    entryNo: '受付番号',
    guestNos: '同伴番号',
    showAtCheckin: '受付でご提示ください',
    live: 'ライブ',
    standbyMode: 'キャンセル待ち',
    standbyBody: 'キャンセル待ちです。空きが出たらDiscordのDMでお知らせします。',
    venue: '会場',
    maps: '地図',
    route: '経路',
    drinkTickets: 'ドリンクチケット',
    firstDrinkTicket: '1杯目ドリンクチケット',
    readyCheck: '準備確認',
    chkEntry: '入場',
    chkRules: 'ルール',
    chkArrival: '到着',
    offkaiDetails: 'オフ会詳細',
    capacity: '定員',
    rsvp: '受付',
    deadline: '締切',
    personalLink: 'このリンクは個人用です。共有しないでください。',
    open: '受付中',
    closed: '締切',
    tbd: '未定',
    tba: '未定',
    solo: '1名',
    partyOf: (n: number) => `${n}名`,
    guests: (n: number) => `同伴${n}名`,
    datePending: '日程未定',
    startsOn: (d: string) => `${d} 開始`,
    startsInDays: (n: number) => `あと${n}日`,
    startsInHours: (h: number, m: number) => `あと${h}時間${m > 0 ? `${m}分` : ''}`,
    startsIn: (m: number) => `あと${m}分`,
    happeningNow: '開催中',
    eventEnded: '終了',
  },
}

export type Strings = typeof STRINGS['en']

const LangContext = createContext<{ lang: Lang; t: Strings; setLang: (l: Lang) => void }>({
  lang: 'en', t: STRINGS.en, setLang: () => {},
})

export const useT = () => useContext(LangContext)

const LANG_EVENT = 'offkai-lang-change'

function subscribeLang(cb: () => void) {
  window.addEventListener(LANG_EVENT, cb)
  window.addEventListener('storage', cb)
  return () => {
    window.removeEventListener(LANG_EVENT, cb)
    window.removeEventListener('storage', cb)
  }
}

// Reads the saved choice, else falls back to the browser locale. Used as the
// useSyncExternalStore client snapshot so language resolves on the client
// without a hydration mismatch (server always snapshots 'en').
function readLang(): Lang {
  const saved = localStorage.getItem('lang')
  if (saved === 'en' || saved === 'ja') return saved
  return navigator.language?.toLowerCase().startsWith('ja') ? 'ja' : 'en'
}

function useLang(): [Lang, (l: Lang) => void] {
  const lang = useSyncExternalStore(subscribeLang, readLang, () => 'en' as Lang)
  const setLang = useCallback((l: Lang) => {
    localStorage.setItem('lang', l)
    window.dispatchEvent(new Event(LANG_EVENT))
  }, [])
  return [lang, setLang]
}

export function LangProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLang] = useLang()
  return (
    <LangContext.Provider value={{ lang, t: STRINGS[lang], setLang }}>
      {children}
    </LangContext.Provider>
  )
}

export function LangToggle({ className = '' }: { className?: string }) {
  const { lang, setLang } = useT()
  return (
    <div className={`inline-flex shrink-0 overflow-hidden rounded-lg border-2 border-[#17120F] text-[10px] font-black uppercase tracking-widest shadow-[2px_2px_0_#17120F] ${className}`}>
      {(['en', 'ja'] as const).map(l => (
        <button
          key={l}
          onClick={() => setLang(l)}
          aria-pressed={lang === l}
          className={`inline-flex min-h-[44px] items-center justify-center px-4 ${lang === l ? 'bg-[#17120F] text-white' : 'bg-white text-[#17120F]'}`}
        >
          {l === 'en' ? 'EN' : '日本語'}
        </button>
      ))}
    </div>
  )
}
