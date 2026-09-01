# tuimail

Mail, comfortably, in your terminal. A keyboard-first email client built on
[Textual](https://textual.textualize.io) — IMAP for reading, SMTP for sending,
Python stdlib for everything mail.

![mailbox](docs/mailbox.svg)

## Features

- **Onboarding wizard** — provider presets (Gmail, Outlook, Yandex, iCloud,
  custom) fill the servers in; app-password hints per provider.
- **Login / logout** (`Ctrl+L`), optional remember-password (explicit opt-in,
  stored plain in the config file and labelled as such).
- **Three-pane mailbox** — folders with unread counts, message list with
  unread ● and star ★ markers, live preview pane.
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

**Installing on macOS** so it runs as the `tuimail` command: download the
binary (arm64 for Apple Silicon, intel otherwise) from the Actions artifacts,
then put it on PATH and clear the Gatekeeper quarantine once:

```bash
sudo install -m 755 tuimail /usr/local/bin/tuimail && sudo xattr -d com.apple.quarantine /usr/local/bin/tuimail
```

After that, `tuimail` from any terminal starts the app. When the binary sits
in a non-writable directory like `/usr/local/bin`, settings go to
`~/.tuimail.json`; in a writable directory (USB stick), they stay next to the
binary — portable mode.

**Or install with Python** (any OS, no binary needed) — this also creates the
`tuimail` command:

```bash
pipx install git+https://github.com/alpatovdanila/tui-mail.git
```

**All platforms at once**: push the repo to GitHub —
[.github/workflows/build.yml](.github/workflows/build.yml) runs the acceptance
suite, then builds and smoke-tests Windows, macOS arm64 (Apple Silicon), and
macOS Intel binaries as downloadable artifacts on every push.

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

More screens: [onboarding](docs/onboarding.svg) · [login](docs/login.svg) ·
[reader](docs/reader.svg) · [compose](docs/compose.svg) · [help](docs/help.svg)
