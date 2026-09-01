# tuimail — full-fledged TUI email client

Stack: Python 3.14 + Textual (only dependency). IMAP/SMTP/parsing via stdlib.
Every phase ends with an **acceptance loop**: scripted Textual Pilot tests in
`tests/acceptance.py` (driven against a built-in demo account, no real server
needed) + a visual check via SVG screenshot export. A phase isn't done until
its loop passes.

## Architecture

- `tuimail/backend.py` — config load/save, mail helpers (header decode, body
  extraction, link/attachment extraction), `DemoBackend` (in-memory sample
  mailbox — powers onboarding "try demo", and all acceptance tests),
  `ImapBackend` (imaplib/smtplib, thread-safe, reconnect-once).
- `tuimail/app.py` — Textual app + screens: Onboarding, Login, Main
  (folders ▏message table ▏preview), Reader, Compose, Help, Confirm.
- `tuimail/app.tcss` — theme (custom palette, borders, focus states).
- `tests/acceptance.py` — plain asyncio + assert, one function per phase.

## Phases & acceptance criteria

### Phase 1 — skeleton, onboarding, login/logout
- First run (no config): Onboarding — welcome art → account setup with
  provider presets (Gmail/Outlook/Yandex/iCloud/custom auto-fill hosts,
  app-password hint) → saved config → Login.
- Login screen: prefilled address, password field, "remember password
  (stored plain on disk)" opt-in, **Demo mode** button.
- Logout (ctrl+l) returns to Login, drops the session.
- **Accept:** pilot: fresh config dir → onboarding shown → complete wizard →
  config file exists → login via demo → MainScreen; ctrl+l → back to Login.

### Phase 2 — mailbox
- Main: folder sidebar with unread counts, message DataTable
  (flag/unread dot, from, subject, date), preview pane for selection.
- Keys: j/k/arrows move, tab cycles panes, Enter read, u toggle read,
  s star, d delete (confirm modal), R refresh, g/G top/bottom.
- Auto-poll refresh every 60 s (IMAP mode).
- **Accept:** pilot on demo data: rows rendered, j/k moves + preview updates,
  u flips unread count, s flags, d+y removes row, Enter opens Reader.

### Phase 3 — read, compose, reply
- Reader: decoded headers, plain-text body (HTML stripped), scroll keys.
- Compose (c): To/Subject/TextArea body, ctrl+s send, esc cancel.
- Reply (r): prefills To/Re:/quoted body, sets In-Reply-To/References.
- **Accept:** pilot: Reader shows demo HTML message as text; compose+send in
  demo lands in demo outbox with right headers; reply prefills quote.

### Phase 4 — clever features
- `/` search (IMAP SEARCH / demo substring) filtering the table, esc clears.
- Reader `o`: extract links from message → modal list → open in browser.
- Reader `a`: save attachments to ~/Downloads.
- `?` help overlay; ctrl+p command palette (compose/refresh/logout/folders).
- **Accept:** pilot: search narrows rows and esc restores; link modal lists
  demo message URLs; attachment saved to tmp dir; help opens/closes.

### Phase 5 — polish & release loop
- Theme pass (palette, focus rings, loading states, empty states).
- README with SVG screenshots (exported from the app), run instructions.
- Final loop: full acceptance suite green + review workflow findings fixed.
