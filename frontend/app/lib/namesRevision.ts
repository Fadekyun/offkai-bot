import { createHash } from 'node:crypto'

// The canonical form matches the bot's compact ASCII JSON array. Escape every
// UTF-16 code unit >= 0x7f so astral characters, unpaired surrogates, control
// characters and non-ASCII names hash to identical bytes in both languages.
function canonicalNamesJson(value: unknown): string {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g,
    character => '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0'))
}

export function namesRevision(
  displayName: string | null,
  extraPeople: number,
  extrasNames: readonly (string | null)[],
): string {
  const canonical = canonicalNamesJson([displayName, extraPeople, [...extrasNames]])
  return createHash('sha256').update(canonical, 'utf8').digest('hex')
}
