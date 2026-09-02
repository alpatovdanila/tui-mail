# Changelog

All notable changes to tuimail. The version in `pyproject.toml` is the source
of truth: bump it, add a section here, push — CI cuts the `vX.Y.Z` release
with these notes automatically.

## 1.9.1 — 2026-09-02

### Added
- Updates in the Ctrl+P palette: "Install update vX.Y.Z" when one is
  pending, "Check for updates now" otherwise.

## 1.9.0 — 2026-09-02

### Added
- **Compose formatting**: Ctrl+B bold, Ctrl+E italic (Ctrl+I is Tab in
  terminals), Ctrl+K link — markers wrap the selection; formatted mail is
  sent with an HTML alternative part so recipients see real bold/italic/
  links, while the plain part stays readable.
- **File attachments**: Ctrl+O opens a picker — browse the directory tree or
  type a path with shell-style Tab completion; the last-used directory is
  remembered. Attached files are listed under the subject line.

## 1.8.0 — 2026-09-02

### Added
- **Selection mode**: Space selects the message under the cursor (and
  advances); selected rows highlight. While anything is selected, the footer
  becomes a selection bar with live counts — "d delete (3)", bulk
  read/unread (u) and star (s) — Enter toggles instead of opening, reply is
  blocked, and Esc clears the selection. Search filters prune the selection
  so hidden messages are never acted on.

## 1.7.2 — 2026-09-02

### Fixed
- Marketing HTML (nested layout tables) no longer renders as empty grids in
  the reader: header-less tables flatten into plain blocks, image tags are
  dropped, and a conversion that still comes out as junk falls back to the
  plain-text renderer.
- The message list shows **one** circle per message, colored by account:
  filled = unread, hollow = read; ★ only when starred. (Previously unread
  mail grew a second dot.)

## 1.7.1 — 2026-09-02

### Fixed
- The terminal is no longer silent while the app boots: "tuimail is
  starting..." (or "Preparing the first start..." on a fresh install) prints
  before the UI loads. The one remaining silent moment is macOS scanning a
  freshly installed binary — one-time, before any code runs.
- install.sh works under macOS's ancient /bin/sh (bash 3.2): the script is
  ASCII-only with braced variable expansions.

## 1.7.0 — 2026-09-02

### Added
- **Terminal-first install**: `curl -fsSL …/install.sh | sh` on macOS and
  `irm …/install.ps1 | iex` on Windows — sha256-verified, no sudo, no
  Gatekeeper prompts, installs to a user directory where the in-app updater
  can replace it. CI smoke-tests both installers on real runners.

### Removed
- The macOS dmg. A terminal app is installed from the terminal; the dmg's
  app-bundle wrapper, icon, and Gatekeeper dance are gone with it. Existing
  .app installs keep working and their settings were already migrated to
  `~/.tuimail.json`.

## 1.6.1 — 2026-09-02

### Changed
- The dmg no longer ships a separate "Install command line tool" script (an
  extra Gatekeeper-blocked click). Instead, on first run the app itself
  offers to install the `tuimail` terminal command through the native macOS
  admin dialog — the VS Code/iTerm2 pattern; also available any time from
  the Ctrl+P palette.

## 1.6.0 — 2026-09-02

### Added
- **Auto-update**: the app checks GitHub for a newer release every 15 minutes
  (and at start), shows a notification, and Ctrl+U installs it after a
  confirmation — download over verified TLS, sha256-checked against the
  release digest, then the binary swaps itself and restarts (macOS replaces
  in place; Windows swaps via a helper after exit). pip/pipx installs are
  pointed at the right upgrade command instead. Disable the check with
  `"update_check": false` in the config; nothing but the version request
  ever goes to GitHub.

## 1.5.1 — 2026-09-02

### Fixed
- **macOS .app installs now keep settings in `~/.tuimail.json`** instead of
  inside the app bundle (where updates wiped them and portable mode refused
  to save passwords, silently breaking auto sign-in). Existing in-bundle
  configs migrate automatically on first run.
- Signing out now sticks across the Manage accounts round-trip too.
- Decoded folder names are sanitized — a hostile server can't smuggle
  terminal escapes through modified-UTF7 labels.
- Inline HTML tags no longer split words ("Casa blanca") or detach
  punctuation in text-mode rendering.

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
