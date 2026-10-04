# PR 138 browser screenshots

Captured from the real production frontend at commit `79fd3d95463587ffff5abab2d799346dcb3d1bb0`, built with Webpack and tested in Chromium 149. All events, attendees, receipts, IDs and payment details are synthetic. No real payment or live Discord interaction occurred.

- `01-confirmed-proof.png`: confirmed attendee, uploaded private proof, still unpaid.
- `02-waitlist-proof.png`: waitlisted attendee with uploaded private proof.
- `03-admin-payments.png`: admin preview and separate paid/unpaid controls for attendees and waitlist.

Browser actions passed for uploads, previews, paid/unpaid changes, saved status after reload and delayed navigation between attendee tokens. Payment actions did not change attendance or check-ins.
