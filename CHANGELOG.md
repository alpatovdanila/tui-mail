# Changelog

All notable changes to tuimail. The version in `pyproject.toml` is the source
of truth: bump it, add a section here, push — CI cuts the `vX.Y.Z` release
with these notes automatically.

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
