# Changelog

All notable changes to tuimail. The version in `pyproject.toml` is the source
of truth: bump it, add a section here, push — CI cuts the `vX.Y.Z` release
with these notes automatically.

## 1.5.0 — 2026-09-02

### Added
- **Real HTML mail rendering**: messages with an HTML part now render with
  headings, bold/italic, lists, tables, blockquotes, and clickable links
  (opened in your browser; only http/https links are ever opened). Powered by
  markdownify (MIT) + Textual's Markdown widget; plain-text messages and the
  preview pane keep the fast text path, and anything unconvertible falls back
  to it.

## 1.4.0 — 2026-09-02

From first real-world macOS field testing — thanks for the feedback:

### Added
- **Auto sign-in**: when every account has a saved password, the app connects
  on launch instead of asking you to press Sign in. Logging out sticks.
- Gmail labels (and any folder) in non-Latin scripts now display correctly
  (IMAP modified-UTF7 decoding).
- The dmg regained **Install command line tool.command** — one double-click
  links `tuimail` into /usr/local/bin for use from your own terminal.
- The app bundle opens iTerm when installed, falling back to Terminal.

### Fixed
- Sending on networks that break SMTPS (`[SSL: UNEXPECTED_EOF_WHILE_READING]`
  on port 465) now automatically retries over 587 STARTTLS.
- Frozen builds without OS CA paths (typical on macOS) now verify TLS against
  the bundled certifi store instead of failing every connection.
- HTML mail no longer renders as one line of text between ten blank ones —
  layout-table whitespace is collapsed properly.

## 1.3.0 — 2026-09-02

Security release — a full adversarial audit of the client, every finding
verified by reproduction and fixed:

### Security
- **TLS certificates and hostnames are now verified** on IMAP, SMTPS, and
  STARTTLS connections. Python's stdlib defaults verify nothing, so previous
  builds could be intercepted by a network man-in-the-middle; update.
- Hostile messages can no longer inject terminal escape sequences (ANSI/OSC)
  through any displayed field — subjects, senders, bodies, reader headers,
  links, or reply prefills are sanitized at ingestion.
- A crafted HTML mail full of unclosed script tags no longer freezes the UI
  (quadratic scrub made linear).
- Server-controlled folder names that could break out of IMAP quoting are
  rejected instead of replayed into commands.
- Outgoing mail no longer leaks the machine's hostname or LAN IP (Message-ID
  domain pinned to the account; EHLO sends `localhost`).
- Portable mode (settings next to the exe) never stores passwords — removable
  and shared media have no reliable file protection; the login screen now
  shows each account's IMAP host so a tampered portable config is visible.
- The config file is created owner-only from the first byte on POSIX (no
  chmod-after-write window).
- CI release builds pin exact dependency versions.

### Fixed
- macOS Terminal: input/select/button borders no longer render as broken
  dashes (block-glyph borders replaced with box-drawing ones).

## 1.2.1 — 2026-09-01

### Changed
- The macOS dmg is now a proper drag-and-drop installer: a `tuimail.app`
  bundle (with its own icon) plus an Applications shortcut. Double-clicking
  the installed app opens Terminal running tuimail; the launcher clears the
  quarantine flag after the first approved run so Gatekeeper only asks once.
- The `tuimail` CLI on macOS can simply be a symlink to the app's binary.

## 1.2.0 — 2026-09-01

### Added
- Versioned releases with changelog notes, cut automatically on version bump.
- macOS `.dmg` installer with a one-click `Install tuimail.command` script.
- Universal macOS binary — one file for Apple Silicon and Intel.
- Rolling `latest` prerelease refreshed on every push to `main`.

### Fixed
- CI no longer waits forever on GitHub's retired Intel mac runners.

## 1.1.0 — 2026-09-01

### Added
- Multi-account support: add, edit, and remove accounts, each with its own
  color shown as a dot next to its mail everywhere.
- All-in-one mode merging every account into one mailbox; switch scope from
  the sidebar or the Ctrl+P palette.
- Account identity in the reader header and a colored From-account picker in
  compose.
- `tuimail` console command via pip/pipx; PATH-safe portable settings.

### Fixed
- Duplicate account names could route delete/send to the wrong mailbox.
- One unreachable account no longer breaks the merged view.
- Command palette mail commands (never matched before), stale worker and
  search/scope races, teardown crashes.

## 1.0.0 — 2026-09-01

### Added
- Initial release: onboarding wizard with provider presets, login/logout,
  three-pane mailbox with unread counts and stars, reader, compose and reply
  with threading, folder search, link opener, attachment saving, help overlay,
  command palette, demo mailbox, portable Windows exe.
