# tuimail

Mail, comfortably, in your terminal. A keyboard-first email client built on
[Textual](https://textual.textualize.io) — IMAP for reading, SMTP for sending,
Python stdlib for everything mail.

![mailbox](docs/mailbox.svg)

## Features

- **Multiple accounts** — add, edit, and remove accounts (each with its own
  provider settings and credentials); every account gets a **color**, shown as
  a dot next to its mail everywhere.
- **All-in-one mode** — the default view merges every account's mailbox into
  one list (sorted by date, colored dots tell them apart); switch to a single
  account from the sidebar or the `Ctrl+P` palette.
- **Account always visible** — the reader shows which account received the
  message, and compose has a colored From-account picker.
- **Onboarding wizard** — provider presets (Gmail, Outlook, Yandex, iCloud,
  custom) fill the servers in; app-password hints per provider.
- **Login / logout** (`Ctrl+L`), optional remember-password (explicit opt-in,
  stored plain in the config file and labelled as such).
- **Three-pane mailbox** — accounts and folders with unread counts, message
  list with unread ● and star ★ markers, live preview pane.
- **Full keyboard control** — vim-style `j/k/g/G`, `?` shows the complete
  reference, `Ctrl+P` opens a command palette (jump to any folder, compose,
  refresh, logout, switch theme).
- **Search** (`/`) — server-side IMAP full-text when possible, local fallback.
- **Link opener** (`o` in the reader) — every URL in the message in a list,
  Enter opens it in your browser.
- **Attachment saving** (`a`) — straight to `~/Downloads`, collision-safe.
- **Reply** with quoted body and proper `In-Reply-To`/`References` threading.
- Background refresh every 60 s; failed sends keep your draft open.
- **Demo mailbox** — try the whole UI without an account.

## Run

```bash
pip install textual
python -m tuimail
```

First run shows the onboarding wizard. Settings live in `~/.tuimail.json`
(next to the exe for the portable build). Password is only stored if you tick
"remember password".

## Portable builds

The binary is self-contained and reads/writes `tuimail.json` next to itself,
so it runs from a USB stick. `tuimail --check` boots the app headless once and
prints `ok`. PyInstaller cannot cross-compile — each OS builds its own binary.

**Windows** (note the `;` in --add-data):

```bash
pip install pyinstaller
pyinstaller --onefile --console --name tuimail --collect-all textual --add-data "tuimail/app.tcss;tuimail" run.py
```

**macOS / Linux** (same command, `:` instead of `;`):

```bash
pyinstaller --onefile --console --name tuimail --collect-all textual --add-data "tuimail/app.tcss:tuimail" run.py
```

**Installing on macOS**: grab `tuimail-macos.dmg` from the
[latest release](https://github.com/alpatovdanila/tui-mail/releases/latest),
open it and **drag tuimail into Applications** — the usual two-icon window.
The app is not code-signed, so the first launch needs a right-click > Open
(or System Settings → Privacy & Security → Open Anyway); after that it opens
normally. Double-clicking the app opens Terminal running tuimail.

Want the `tuimail` command in your own terminal too? Either link the app's
binary:

```bash
sudo ln -sf /Applications/tuimail.app/Contents/MacOS/tuimail-bin /usr/local/bin/tuimail
```

or install the bare `tuimail-macos-universal` binary from the same release:

```bash
sudo install -m 755 tuimail-macos-universal /usr/local/bin/tuimail && sudo xattr -d com.apple.quarantine /usr/local/bin/tuimail
```

When the binary sits in a non-writable directory like `/usr/local/bin`,
settings go to `~/.tuimail.json`; in a writable directory (USB stick), they
stay next to the binary — portable mode.

**Or install with Python** (any OS, no binary needed) — this also creates the
`tuimail` command:

```bash
pipx install git+https://github.com/alpatovdanila/tui-mail.git
```

**Download instead of building**: grab the
[latest versioned release](https://github.com/alpatovdanila/tui-mail/releases/latest)
— Windows exe, macOS dmg installer, and a universal macOS binary (one file for
Apple Silicon and Intel), built and smoke-tested by
[CI](.github/workflows/build.yml). A new `vX.Y.Z` release (with notes from
[CHANGELOG.md](CHANGELOG.md)) is cut automatically whenever the version in
`pyproject.toml` bumps; a rolling `latest` prerelease tracks every push to
`main` for the impatient.

## Keys

| Where   | Key | Action |
|---------|-----|--------|
| Mailbox | `j` `k` `↑` `↓` / `g` `G` | move / first / last |
| Mailbox | `Enter` | read message |
| Mailbox | `c` / `r` | compose / reply |
| Mailbox | `u` / `s` / `d` | unread / star / delete |
| Mailbox | `/` then `Esc` | search, clear |
| Mailbox | `R` | refresh |
| Reader  | `j` `k` `Space` | scroll / page |
| Reader  | `o` / `a` | open links / save attachments |
| Compose | `Ctrl+S` / `Esc` | send / cancel |
| Anywhere| `Ctrl+P` / `Ctrl+L` / `?` | palette / logout / help |

## Development

```bash
python tests/acceptance.py        # full acceptance suite (headless, demo backend)
python scripts/screenshots.py     # regenerate docs/*.svg
```

Releasing: bump `version` in [pyproject.toml](pyproject.toml), add a matching
section to [CHANGELOG.md](CHANGELOG.md), push to `main` — CI builds, tags
`vX.Y.Z`, and publishes the release with those notes.

More screens: [onboarding](docs/onboarding.svg) · [login](docs/login.svg) ·
[reader](docs/reader.svg) · [compose](docs/compose.svg) · [help](docs/help.svg)
