# Changelog

All notable changes to tuimail. The version in `pyproject.toml` is the source
of truth: bump it, add a section here, push — CI cuts the `vX.Y.Z` release
with these notes automatically.

## 1.13.0 — 2026-09-02

### Added
- **Outbox.** `Ctrl+S` no longer waits for the SMTP server: the message is
  written to a local outgoing spool (`~/.tuimail.outbox/`, next to the exe in
  portable mode; owner-only files) and the composer closes at once. A
  background sender delivers it and toasts "Sent to …". If sending fails the
  message stays in the new **Outbox** folder (right under INBOX in the
  sidebar, with a count) with the server's error shown in the preview.
  Transient failures (connection resets, timeouts, 4xx) retry automatically
  with backoff; refused recipients, bad credentials, 5xx replies and missing
  attachments wait for you. In the Outbox, `Enter` reopens the message in the
  composer (resending keeps its Message-ID), `R` retries everything now, `d`
  deletes (with the usual undo). Mail still queued when you quit goes out at
  the next start.
- `tuimail --version`.

### Removed
- The old behaviour where a failed send silently dropped you back into the
  composer with a short toast.

## 1.12.1 — 2026-09-02

An adversarial review of the 1.10/1.11 work; twelve confirmed findings, all fixed.

### Fixed
- **Delete could expunge when Trash existed but was not visible**: the
  special-folder lookup now uses the full IMAP `LIST` (not the 30-folder
  unread-count cap, which hid Gmail's `[Gmail]/Trash` behind 23+ labels) and
  honours RFC 6154 `\Trash` / `\Archive` attributes; an unknown folder list
  is fetched on demand rather than treated as "no Trash".
- A delete inside its 5 s undo window is committed on logout, `q` and
  `Ctrl+Q` instead of being silently dropped.
- A committed delete can no longer be resurrected by a poll that overlaps
  the server call; "load older" no longer hands a pending delete back, and
  undo after it no longer crashes the list with a duplicate key.
- Reader `d`, `u`, `s` and `m` act on the right message even when the list
  was refreshed underneath the reader.
- Reply title, folder picker, attachment list, palette entries and toasts no
  longer parse names such as `[Gmail]/Trash` or `[list] Bob` as markup
  (which crashed the compose screen).
- Editing an account no longer resets a customised SMTP/IMAP host (for
  example `smtp.gmail.com:587`) when the provider is inferred.
- Shrinking the terminal below 90 columns while the sidebar has focus no
  longer leaves the keyboard focus on a hidden widget.

## 1.12.0 — 2026-09-02

### Added
- **Homebrew cask**: `brew tap alpatovdanila/tui-mail
  https://github.com/alpatovdanila/tui-mail && brew install --cask tuimail`.
  The repository doubles as the tap; every versioned release publishes a
  `tuimail-macos-universal.tar.gz` and CI commits the new version and sha256
  into the cask, so `brew upgrade --cask tuimail` just works. The in-app
  updater recognises a brew install and points at brew instead of replacing
  the binary itself.

## 1.11.0 — 2026-09-02

The second batch from the usability study (P1) — the daily-driver verbs:

### Added
- **Move and archive**: `m` picks a folder (works on a selection too, and
  from the reader), `A` archives to the account's Archive / All Mail. IMAP
  `MOVE` with a `COPY` fallback for older servers.
- **Delete goes to Trash and can be undone**: a single `d` no longer asks —
  the message disappears and `z` brings it back within 5 seconds; when the
  window closes it moves to the account's Trash (expunged only from Trash
  itself or on servers without one). Bulk deletes still confirm.
- **Reader parity**: `n`/`p` next and previous message, `g`/`G`,
  `Ctrl+D`/`Ctrl+U`, `b`; `u`/`s`/`m`/`d` act on the open message.
- **Per-account unread counts** next to each account (and a total on "All
  accounts"); the current folder is kept when you switch scope if the new
  scope has it.
- **Links with their text**: `o` lists "Unsubscribe  https://…" instead of
  bare URLs; image sources and tracking pixels are no longer listed.
- **Attachment list**: `a` shows names and sizes; Enter saves one, `a` saves
  all — no accidental duplicates.
- **Load older messages** with `L` (or the palette) — the newest 100 are no
  longer a silent ceiling.
- Palette entries for move, archive, load older, and undo.

## 1.10.0 — 2026-09-02

The first batch from the six-persona usability study (P0):

### Changed
- **Keys respect focus**: j/k drive whichever list has focus (accounts,
  folders, messages); Space/d/u/s act only when the message list is focused —
  no more invisible-cursor actions from the sidebar. The preview pane left
  the Tab cycle; J/K scroll it from the list and p hides/shows it.
- **Responsive mailbox**: columns are computed from the terminal width (the
  list never scrolls sideways, the date stays visible, long text ellipsizes),
  the preview is proportional, and the sidebar collapses below 90 columns
  (accounts and folders stay reachable via Ctrl+P and the 0-9 keys).
- **Help screen** scrolls, shows the version, and opens with ? or F1 on
  every screen — including setup and compose.
- **Setup screens** take Esc (back), Ctrl+S / Enter (save), and Enter on an
  account to edit it; buttons fit the cards; the login card scrolls; the
  password-storage warning is a visible label; checkboxes show ☑/☐.
- **Compose** in the merged view defaults From to the highlighted message's
  account, the From row is labeled, replies are titled "Reply to <name>",
  the Sent toast names the account, changing From counts as a draft change,
  and Esc closes an open dropdown before it closes anything else.
- **Plain text stays plain**: the HTML alternative is generated only when the
  format keys were actually used, never from incidental *asterisks*.
- **q asks before quitting** (Ctrl+Q still quits at once) and first clears a
  selection or search filter instead — a stray q no longer kills the session.
- **Cursor and preview survive reloads**: the cursor follows the message
  (not the row number) when new mail arrives, and the preview keeps its
  scroll position.
- Update status is honest (off / not yet checked / failed / checked N min
  ago); a release without an integrity digest asks before installing; the
  running version shows in the header.
- Sent folders show the recipient; Ctrl+A selects everything shown; Enter
  moves between password fields at login; error toasts are decoded text.

## 1.9.2 — 2026-09-02

### Security
- Quoted text in a reply is never reinterpreted as your own markup: a
  hostile sender can no longer plant live links or formatting into the HTML
  part of your outgoing reply. The link pattern is also linear on hostile
  bracket floods.

### Fixed
- **Selection is cleared when you switch folder or account.** IMAP uids are
  only unique per folder, so a selection surviving the switch could bulk-
  delete unrelated mail in the next folder.
- Bulk delete/mark/star now reconcile with the server when they finish, so a
  background refresh mid-operation can no longer resurrect removed rows.
- Ctrl+B/E/K act only inside the body; in To/Subject they keep their normal
  editing meaning instead of dumping markers into the message.
- Selection tint follows the active theme (readable on light themes) and no
  longer hides the cursor row inside a selected block.

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
