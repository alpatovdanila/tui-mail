"""Mail engine: config, parsing helpers, Demo and IMAP backends. Stdlib only.

Carries fixes for review-confirmed bugs of the old prototype:
- reply address is taken from the *raw/structured* From header, never from the
  RFC2047-decoded string (decoded "Doe, John" breaks strict parseaddr);
- FETCH response parsing does not assume item order — UID/FLAGS may legally
  arrive *after* the header literal, as trailing bytes fragments;
- fetching an expunged UID raises MailGone (clean message) instead of a blank
  StopIteration;
- In-Reply-To values are unfolded before being set as header values.
"""
import base64
import email
import mimetypes
import email.header
import email.policy
import email.utils
import imaplib
import json
import os
import re
import smtplib
import ssl
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path


class MailGone(Exception):
    """The message no longer exists on the server."""


# virtual folder backed by the local outgoing spool (tuimail/outbox.py); the
# prefix keeps it from ever colliding with a real IMAP folder called Outbox
OUTBOX = 'tuimail:Outbox'


class PartialDelivery(smtplib.SMTPRecipientsRefused):
    """Some recipients were refused after the others had accepted the mail."""

    def __init__(self, recipients):
        super().__init__(recipients)
        bad = ', '.join(f'{addr} ({code} {err_text(Exception(msg))})'
                        for addr, (code, msg) in recipients.items())
        # args carries the human text: err_text() and toasts read args
        self.args = (f'not delivered to {bad}; the other recipients did get it - '
                     'resend to the failed address only',)


# --- config ------------------------------------------------------------------
PROVIDERS = {
    'Gmail': {
        'imap': 'imap.gmail.com', 'smtp': 'smtp.gmail.com:465',
        'hint': 'Needs an app password: myaccount.google.com/apppasswords',
    },
    'Outlook / Office 365': {
        'imap': 'outlook.office365.com', 'smtp': 'smtp-mail.outlook.com:587',
        'hint': 'Needs an app password (account.microsoft.com → Security)',
    },
    'Yandex': {
        'imap': 'imap.yandex.com', 'smtp': 'smtp.yandex.com:465',
        'hint': 'Enable IMAP + app password: id.yandex.com → Security',
    },
    'iCloud': {
        'imap': 'imap.mail.me.com', 'smtp': 'smtp.mail.me.com:587',
        'hint': 'Needs an app-specific password: appleid.apple.com',
    },
    'Custom': {'imap': '', 'smtp': '', 'hint': 'Any IMAP/SSL + SMTP server; host or host:port'},
}


def _installed_location(exe_dir: Path, platform=None) -> bool:
    """True when the exe sits in a standard install destination — there the
    config (and saved passwords) belong in the home dir, exactly like a
    non-portable app. Portable mode is only for genuinely ad-hoc places:
    a USB stick, Downloads, an unpacked folder."""
    platform = platform or os.name
    # normalise separators so a Windows path is still recognisable when this
    # runs on a POSIX host (the test suite, and cross-platform tooling)
    p = exe_dir.as_posix().replace('\\', '/').rstrip('/').lower()
    real = Path(os.path.realpath(exe_dir)).as_posix().replace('\\', '/').lower()
    if '.app/contents/' in p or '/caskroom/' in real or '/cellar/' in real:
        return True
    if platform == 'nt':
        # match by path SHAPE, not just env vars: LOCALAPPDATA and ProgramFiles
        # can be unset (services, odd shells), and the installer's own target
        # is always one of these standard destinations
        substrings = ['/appdata/local/programs/',       # our install.ps1 target
                      '/appdata/local/microsoft/windowsapps',
                      '/program files/', '/program files (x86)/']
        if any(sub in p + '/' for sub in substrings):
            return True
        roots = [os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'),
                 os.environ.get('ProgramW6432')]
        if local := os.environ.get('LOCALAPPDATA'):
            roots += [str(Path(local) / 'Programs')]
        roots = [Path(r).as_posix().rstrip('/').lower() for r in roots if r]
    else:
        roots = ['/usr', '/opt', '/bin', '/sbin',
                 (Path.home() / '.local' / 'bin').as_posix().lower()]
    return any(p == r or p.startswith(r + '/') for r in roots)


def config_path() -> Path:
    if p := os.environ.get('TUIMAIL_CONFIG'):
        return Path(p)
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        if not _installed_location(exe_dir):
            here = exe_dir / 'tuimail.json'  # portable binary: settings travel next to it
            # non-writable dir -> fall through to home
            if here.exists() or os.access(exe_dir, os.W_OK):
                return here
    return Path.home() / '.tuimail.json'


DEFAULT_COLORS = ['#7aa2f7', '#9ece6a', '#e0af68', '#f7768e',
                  '#bb9af7', '#7dcfff', '#ff9e64', '#73daca']


def next_color(cfg) -> str:
    used = {a.get('color') for a in cfg.get('accounts', [])}
    return next((c for c in DEFAULT_COLORS if c not in used), DEFAULT_COLORS[0])


def load_config() -> dict:
    p = config_path()
    if not p.exists() and getattr(sys, 'frozen', False):
        # older builds kept the config next to the exe even for installed
        # copies (the .app bundle, LOCALAPPDATA\Programs, ~/.local/bin) — the
        # accounts migrate to $HOME; the password was never stored there
        legacy = Path(sys.executable).parent / 'tuimail.json'
        if legacy != p and legacy.exists():
            try:
                p.write_bytes(legacy.read_bytes())
                if os.name == 'posix':
                    p.chmod(0o600)
            except OSError:
                pass
    try:
        cfg = json.loads(p.read_text('utf-8'))
        if not isinstance(cfg, dict):
            return {}
    except (OSError, ValueError):
        return {}
    if 'accounts' not in cfg and cfg.get('address'):  # migrate pre-multi-account config
        acct = {'name': cfg['address'].split('@')[0], 'address': cfg['address'],
                'imap_host': cfg.get('imap_host', ''), 'smtp_host': cfg.get('smtp_host', ''),
                'color': DEFAULT_COLORS[0]}
        if cfg.get('password'):
            acct['password'] = cfg['password']
        cfg = {'accounts': [acct]}
    return cfg


def portable_mode() -> bool:
    """Config rides next to the exe (USB stick / shared dir) — hostile ground."""
    return (not os.environ.get('TUIMAIL_CONFIG')
            and getattr(sys, 'frozen', False)
            and config_path().parent == Path(sys.executable).parent)


def save_config(cfg: dict) -> bool:
    try:
        if portable_mode():
            # removable/shared media has no reliable ACLs — never persist passwords there
            cfg = dict(cfg)
            cfg['accounts'] = [{k: v for k, v in a.items() if k != 'password'}
                               for a in cfg.get('accounts', [])]
        # owner-only from the first byte: no chmod-after-write window
        fd = os.open(config_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(json.dumps(cfg, indent=2))
        return True
    except OSError:
        return False


def downloads_dir() -> Path:
    return Path(os.environ.get('TUIMAIL_DOWNLOADS', str(Path.home() / 'Downloads')))


# --- parsing helpers ---------------------------------------------------------
@dataclass
class Summary:
    uid: str
    sender: str
    subject: str
    date: datetime | None = None
    unread: bool = False
    flagged: bool = False
    account: str = ''  # set by Session when listing
    recipient: str = ''  # shown instead of sender in Sent-style folders


# A hostile message must not smuggle terminal escape sequences onto the
# screen: drop whole CSI/OSC sequences first, then every remaining C0 control
# (minus \t \n), DEL, and C1 control byte
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]'                 # CSI
                   r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?'     # OSC, even unterminated
                   r'|\x9b[0-?]*[ -/]*[@-~]')                 # C1 CSI
_CTRL = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')


def sanitize(s, keep_newlines=True) -> str:
    s = _CTRL.sub('', _ANSI.sub('', str(s)))
    return s if keep_newlines else s.replace('\n', ' ')


def dec(s) -> str:
    """Decode an RFC2047 header for *display*, collapsing folds."""
    if not s:
        return ''
    s = str(s)
    try:
        s = ' '.join(str(email.header.make_header(email.header.decode_header(s))).split())
    except Exception:
        pass
    return sanitize(s, keep_newlines=False)


def parse_date(d) -> datetime | None:
    try:
        dt = email.utils.parsedate_to_datetime(d)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except Exception:
        return None


def nice_date(dt: datetime | None) -> str:
    if not dt:
        return ''
    local = dt.astimezone()
    now = datetime.now()
    if local.date() == now.date():
        return local.strftime('%H:%M')
    if local.year == now.year:
        return local.strftime('%d %b')
    return local.strftime('%Y-%m-%d')


def tls_context() -> ssl.SSLContext:
    """Verifying TLS context that also works in frozen builds.

    A PyInstaller binary (esp. on macOS) can have no OS CA paths at all —
    verification would then fail on every connection; fall back to certifi's
    bundle, which ships inside the app.
    """
    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get('x509_ca'):
        try:
            import certifi
            ctx.load_verify_locations(certifi.where())
        except Exception:
            pass  # verification still on; connections fail closed without CAs
    return ctx


def decode_folder(name: str) -> str:
    """IMAP modified-UTF7 (RFC 3501) folder name -> readable text, display only.

    The raw name stays the key for every IMAP command; Gmail labels in
    non-Latin scripts arrive as '&BB8EQAQ4BDIENQRC-' style."""
    if name == OUTBOX:
        return 'Outbox'

    def _dec(m):
        chunk = m.group(1)
        if not chunk:
            return '&'
        try:
            pad = '=' * (-len(chunk) % 4)
            return base64.b64decode(chunk.replace(',', '/') + pad).decode('utf-16-be')
        except Exception:
            return m.group(0)
    # mUTF-7 is printable ASCII on the wire but can synthesize ESC/CR/LF —
    # it bypasses the raw-name filters, so sanitize the decoded result
    return sanitize(re.sub(r'&([A-Za-z0-9+,]*)-', _dec, name), keep_newlines=False)


def nice_from(raw) -> str:
    """Display name from a raw From header: parse first, decode after."""
    name, addr = email.utils.parseaddr(str(raw or ''))
    return dec(name) or sanitize(addr, keep_newlines=False) or dec(raw)


def body_of(msg) -> str:
    part = msg.get_body(preferencelist=('plain', 'html'))
    if part is None:
        return '(no readable text part)'
    try:
        text = part.get_content()
    except Exception:
        text = (part.get_payload(decode=True) or b'').decode('utf-8', 'replace')
    if part.get_content_type() == 'text/html':
        # (?:</tag>|\Z): an unclosed hostile tag matches to end-of-string instead
        # of forcing a quadratic rescan from every start position
        text = re.sub(r'(?is)<(script|style)\b.*?(?:</\1\s*>|\Z)', '', text)
        text = re.sub(r'(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>|</table>|</blockquote>',
                      '\n', text)
        import html as _html
        # block-ish tags separate content with a space; inline tags (b/i/a/span)
        # vanish so words spanning them don't split ('Casa<i>blanca</i>')
        text = re.sub(r'(?i)</?(?:td|th|p|div|table|tr|ul|ol|li|h[1-6]|blockquote)\b[^>]*>',
                      ' ', text)
        text = _html.unescape(re.sub(r'<[^>]+>', '', text))  # ponytail: naive HTML strip; html.parser if it matters
        text = re.sub(r'[ \t\xa0]+', ' ', text)
        # layout-table HTML leaves runs of whitespace-only lines; strip each
        # line so the blank-line collapse below actually collapses them
        text = '\n'.join(ln.strip() for ln in text.splitlines())
        text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return sanitize(text)


def body_markdown(msg) -> str | None:
    """HTML part -> Markdown for rich reader rendering; None when there's no
    HTML part or conversion is unavailable (caller falls back to body_of)."""
    part = msg.get_body(preferencelist=('html',))
    if part is None or part.get_content_type() != 'text/html':
        return None
    try:
        src = part.get_content()
    except Exception:
        src = (part.get_payload(decode=True) or b'').decode('utf-8', 'replace')
    src = re.sub(r'(?is)<(script|style)\b.*?(?:</\1\s*>|\Z)', '', src[:2_000_000])
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify
        soup = BeautifulSoup(src, 'html.parser')
        # email HTML is mostly LAYOUT tables (no <th>); as markdown tables they
        # render as empty grids — flatten them to plain blocks, keep data tables
        for t in soup.find_all('table'):
            if t.find('th') is None:
                own = [c for c in t.find_all(['thead', 'tbody', 'tfoot', 'tr', 'td'])
                       if c.find_parent('table') is t]
                for tag in own + [t]:
                    tag.name = 'div'
        md = markdownify(str(soup), heading_style='ATX',
                         strip=['script', 'style', 'img'])
    except Exception:
        return None
    md = re.sub(r'\n{3,}', '\n\n', md).strip()
    if not re.sub(r'[\s|\-:*_>#!\[\]()`.+]', '', md):
        return None  # only borders/punctuation survived — use the text path
    return sanitize(md)


URL_RE = re.compile(r'https?://[^\s<>"\')\]]+')


def extract_links(msg) -> list[tuple[str, str]]:
    """[(link text, url)] — anchors from HTML with their text (images and
    tracking pixels skipped), bare URLs from plain text; deduped by url."""
    seen: dict[str, str] = {}

    def add(text, url):
        url = sanitize(url, keep_newlines=False).rstrip('.,;:!?')
        if url.startswith(('http://', 'https://')) and url not in seen:
            seen[url] = sanitize(' '.join(text.split()), keep_newlines=False)[:80]

    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ('text/plain', 'text/html'):
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        if ctype == 'text/html':
            try:
                from bs4 import BeautifulSoup
                for a in BeautifulSoup(content, 'html.parser').find_all('a', href=True):
                    add(a.get_text(' ', strip=True), a['href'])
                continue
            except Exception:
                pass  # fall through to bare-URL scan
        for u in URL_RE.findall(content):
            add('', u)
    return [(text, url) for url, text in seen.items()]


def attachments_of(msg) -> list[tuple[str, bytes]]:
    out = []
    for part in msg.iter_attachments():
        name = os.path.basename(part.get_filename() or 'attachment.bin')
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f-\x9f]', '_', name) or 'attachment.bin'
        try:
            data = part.get_payload(decode=True) or b''
        except Exception:
            data = b''
        out.append((name, data))
    return out


def reply_seed(msg) -> dict:
    """Prefill for replying to a policy.default-parsed message."""
    frm = msg.get('From')
    addr = ''
    try:
        if frm is not None and frm.addresses:
            addr = frm.addresses[0].addr_spec
    except Exception:
        pass
    if not addr:  # malformed header: parse the raw-ish string, never the decoded one
        addr = email.utils.parseaddr(str(frm or ''))[1]
    subject = sanitize(str(msg.get('Subject') or ''), keep_newlines=False)
    if not subject.lower().startswith('re:'):
        subject = 'Re: ' + subject
    addr = sanitize(addr, keep_newlines=False)
    date = sanitize(str(msg.get('Date') or 'an earlier date'), keep_newlines=False)
    quoted = '\n'.join('> ' + ln for ln in body_of(msg).splitlines())
    body = f'\n\nOn {date}, {addr or "they"} wrote:\n{quoted}\n'
    mid = str(msg.get('Message-ID') or '')
    name = ''
    try:
        if frm is not None and frm.addresses:
            name = frm.addresses[0].display_name
    except Exception:
        pass
    return {'to': addr, 'subject': subject, 'body': body,
            'in_reply_to': ' '.join(mid.split()),
            'reply_to': sanitize(name or addr, keep_newlines=False)}


def markup_html(body) -> str | None:
    """Compose markup (**bold**, *italic*, [text](url)) -> an HTML alternative;
    None when the body carries no markup, keeping unformatted mail plain."""
    import html as _html

    def fmt(line):
        line = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', line)
        line = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', line)
        # label may not contain brackets/newlines: keeps the regex linear on
        # hostile input and unambiguous on nested markup
        return re.sub(r'\[([^\[\]]+)\]\((https?://[^)\s]+)\)', r'<a href="\2">\1</a>', line)

    changed = False
    out = []
    for raw in body.split('\n'):
        esc = _html.escape(raw)
        if raw.lstrip().startswith('>'):
            out.append(esc)  # quoted text is the other party's — never re-marked
            continue
        rich = fmt(esc)
        changed = changed or rich != esc
        out.append(rich)
    if not changed:
        return None
    return '<html><body>' + '<br>\n'.join(out) + '</body></html>'


def err_text(exc) -> str:
    """Exception -> readable text (imaplib raises with bytes payloads)."""
    parts = []
    for a in getattr(exc, 'args', ()) or ():
        parts.append(a.decode('utf-8', 'replace') if isinstance(a, bytes) else str(a))
    return sanitize(' '.join(p for p in parts if p) or str(exc), keep_newlines=False)


def build_message(sender, to, subject, body, in_reply_to=None,
                  attachments=None, markup=False, message_id=None) -> EmailMessage:
    m = EmailMessage()
    m['From'], m['To'], m['Subject'] = sender, to, subject
    m['Date'] = email.utils.formatdate(localtime=True)
    # pin the msgid domain to the sender's — the default embeds the local
    # machine's hostname in every outgoing mail
    m['Message-ID'] = message_id or email.utils.make_msgid(
        domain=sender.rsplit('@', 1)[-1] if '@' in sender else None)
    if in_reply_to:
        m['In-Reply-To'] = m['References'] = ' '.join(in_reply_to.split())
    m.set_content(body)
    # HTML alternative only when the composer's format keys were used — plain
    # text mail with incidental *asterisks* stays plain text
    rich = markup_html(body) if markup else None
    if rich:
        m.add_alternative(rich, subtype='html')
    for path in attachments or []:
        p = Path(path)
        ctype = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
        maintype, _, subtype = ctype.partition('/')
        m.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype,
                         filename=p.name)
    return m


def parse_fetch_headers(resp) -> list[Summary]:
    """Parse a batch UID FETCH (UID FLAGS BODY.PEEK[HEADER.FIELDS ...]) response.

    RFC 3501 fixes no item order: UID/FLAGS may trail the header literal as a
    bare bytes fragment after the tuple, so metadata is gathered from both.
    """
    out = []
    i = 0
    while i < len(resp):
        part = resp[i]
        i += 1
        if not isinstance(part, tuple):
            continue
        meta = part[0] or b''
        while i < len(resp) and isinstance(resp[i], bytes):
            meta += b' ' + resp[i]
            i += 1
        um = re.search(rb'UID (\d+)', meta)
        if not um:
            continue
        fm = re.search(rb'FLAGS \(([^)]*)\)', meta)
        flags = fm.group(1) if fm else b''
        h = email.message_from_bytes(part[1] or b'')
        out.append(Summary(
            uid=um.group(1).decode(),
            sender=nice_from(h['From']),
            subject=dec(h['Subject']) or '(no subject)',
            date=parse_date(h['Date']),
            unread=b'\\Seen' not in flags,
            flagged=b'\\Flagged' in flags,
            recipient=nice_from(h['To']),
        ))
    out.sort(key=lambda s: s.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


SMTP_TLS_PORTS = {465}  # implicit TLS from the first byte; other ports STARTTLS


def _smtp_send_once(host, port, address, password, msg) -> None:
    tls = tls_context()  # stdlib default skips verification
    # local_hostname: the default EHLO leaks the machine's hostname or LAN IP
    if port in SMTP_TLS_PORTS:
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=tls,
                                  local_hostname='localhost')
    else:
        server = smtplib.SMTP(host, port, timeout=20, local_hostname='localhost')
    with server as s:
        if port not in SMTP_TLS_PORTS:
            s.starttls(context=tls)
        s.login(address, password)
        refused = s.send_message(msg)  # smtplib raises only when EVERY recipient is refused
        if refused:
            raise PartialDelivery(refused)


# transport-level failures worth a second try on the submission port; a
# middlebox that filters the port shows up as any of these depending on how
# it kills the connection (reset, silent close, garbage into the handshake)
_SMTP_TRANSPORT_ERRORS = (ssl.SSLError, ConnectionError, TimeoutError,
                          smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError)
SMTP_FALLBACK_PORT = 587


def smtp_send(smtp_host, address, password, msg) -> None:
    host, _, port = smtp_host.partition(':')
    port = int(port or 465)
    try:
        _smtp_send_once(host, port, address, password, msg)
        return
    except _SMTP_TRANSPORT_ERRORS as exc:
        if port not in SMTP_TLS_PORTS:
            raise
        first = exc  # some networks break implicit-TLS :465 while 587 works
    try:
        _smtp_send_once(host, SMTP_FALLBACK_PORT, address, password, msg)
    except _SMTP_TRANSPORT_ERRORS as exc:
        # both ports dead: report BOTH errors — showing only the fallback's
        # turns a blocked network into a misleading mystery
        raise RuntimeError(
            f'port {port}: {err_text(first)} — retried on port {SMTP_FALLBACK_PORT}: '
            f'{err_text(exc)}. The network may be blocking outgoing mail '
            '(try another network or a VPN)') from exc


# --- IMAP backend ------------------------------------------------------------
class ImapBackend:
    def __init__(self, address, password, imap_host, smtp_host):
        self.address = address
        self._pw = password
        self._imap_host, self._smtp_host = imap_host, smtp_host
        self._lock = threading.Lock()
        self._conn = None
        self._selected = None
        with self._lock:
            self._connect()  # raises on bad host/credentials

    def _connect(self):
        host, _, port = self._imap_host.partition(':')
        # stdlib default context does NOT verify certificates — a MITM could
        # harvest the password; always verify cert + hostname
        self._conn = imaplib.IMAP4_SSL(host, int(port or 993), timeout=15,
                                       ssl_context=tls_context())
        self._conn.login(self.address, self._pw)
        self._selected = None

    def _retry(self, fn):
        with self._lock:
            try:
                return fn()
            except (imaplib.IMAP4.abort, OSError):
                self._connect()
                return fn()

    def _select(self, folder):
        if self._selected != folder:
            if re.search(r'["\r\n]', folder):
                raise RuntimeError('unsafe folder name')  # our quoting cannot round-trip it
            self._selected = None  # a failed SELECT leaves the connection unselected
            typ, _ = self._conn.select(f'"{folder}"')
            if typ != 'OK':
                raise RuntimeError(f'cannot open folder {folder}')
            self._selected = folder

    def folders(self):
        def go():
            typ, data = self._conn.list()
            names, special = [], {}
            for line in data or []:
                if not isinstance(line, bytes) or re.search(rb'(?i)\\Noselect', line):
                    continue
                m = re.match(rb'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)$', line)
                if not m:
                    continue
                attrs = m.group(1).lower()
                name = m.group(2).strip()
                if name.startswith(b'"') and name.endswith(b'"'):
                    name = name[1:-1]
                decoded = name.decode('ascii', 'replace')  # ponytail: modified-UTF7 folder names shown raw
                if re.search(r'["\r\n]', decoded):
                    continue  # server-controlled name that could break out of our IMAP quoting
                # RFC 6154 special-use attributes beat name guessing
                if b'\\trash' in attrs:
                    special.setdefault('trash', decoded)
                if b'\\archive' in attrs or b'\\all' in attrs:
                    special.setdefault('archive', decoded)
                names.append(decoded)
            names.sort(key=lambda n: (n.upper() != 'INBOX', n.upper()))
            # the STATUS round-trips below are capped, but special-folder lookups
            # must see every folder — a hidden Trash must never turn delete into expunge
            self.all_folder_names = list(names)
            self.special = special
            out = []
            for name in names[:30]:
                unseen = 0
                try:
                    typ, sdata = self._conn.status(f'"{name}"', '(UNSEEN)')
                    sm = re.search(rb'UNSEEN (\d+)', sdata[0] if sdata and isinstance(sdata[0], bytes) else b'')
                    if sm:
                        unseen = int(sm.group(1))
                except imaplib.IMAP4.error:
                    pass
                out.append((name, unseen))
            return out
        return self._retry(go)

    def list_messages(self, folder, limit=100, before_uid=None):
        """Newest `limit` messages; with before_uid, the `limit` older than it."""
        def go():
            self._select(folder)
            typ, d = self._conn.uid('search', None, 'ALL')
            uids = (d[0] or b'').split()
            if before_uid is not None:
                uids = [u for u in uids if u.isdigit() and int(u) < int(before_uid)]
            uids = uids[-limit:]
            if not uids:
                return []
            typ, resp = self._conn.uid(
                'fetch', b','.join(uids),
                '(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])')
            return parse_fetch_headers(resp or [])
        return self._retry(go)

    def fetch(self, folder, uid):
        def go():
            self._select(folder)
            typ, d = self._conn.uid('fetch', uid, '(BODY.PEEK[])')  # PEEK: fetching for preview must not mark \Seen
            raw = next((t[1] for t in d if isinstance(t, tuple)), None)
            if raw is None:
                raise MailGone('message no longer exists on the server')
            return email.message_from_bytes(raw, policy=email.policy.default)
        return self._retry(go)

    def mark(self, folder, uid, read=True):
        def go():
            self._select(folder)
            self._conn.uid('store', uid, '+FLAGS' if read else '-FLAGS', '\\Seen')
        self._retry(go)

    def flag(self, folder, uid, flagged=True):
        def go():
            self._select(folder)
            self._conn.uid('store', uid, '+FLAGS' if flagged else '-FLAGS', '\\Flagged')
        self._retry(go)

    def delete(self, folder, uid):
        def go():
            self._select(folder)
            self._conn.uid('store', uid, '+FLAGS', '\\Deleted')
            try:
                self._conn.uid('expunge', uid)  # UIDPLUS: expunge only this message
            except imaplib.IMAP4.error:
                self._conn.expunge()  # ponytail: non-UIDPLUS servers expunge every \Deleted
        self._retry(go)

    def move(self, folder, uid, dest):
        if re.search(r'["\r\n]', dest):
            raise RuntimeError('unsafe folder name')

        def go():
            self._select(folder)
            try:
                typ, _ = self._conn.uid('MOVE', uid, f'"{dest}"')  # RFC 6851
                if typ == 'OK':
                    return
            except imaplib.IMAP4.error:
                pass
            typ, _ = self._conn.uid('COPY', uid, f'"{dest}"')  # pre-MOVE servers
            if typ != 'OK':
                raise RuntimeError(f'could not copy to {dest}')
            self._conn.uid('store', uid, '+FLAGS', '\\Deleted')
            try:
                self._conn.uid('expunge', uid)
            except imaplib.IMAP4.error:
                self._conn.expunge()
        self._retry(go)

    def search(self, folder, query):
        """Server-side full-text search; None means 'filter locally'."""
        if not query.isascii():
            return None
        def go():
            self._select(folder)
            q = re.sub(r'[\\"\r\n]', ' ', query)  # no quoting or CRLF into the IMAP command
            typ, d = self._conn.uid('search', None, 'TEXT', f'"{q}"')
            return {u.decode() for u in (d[0] or b'').split()}
        try:
            return self._retry(go)
        except imaplib.IMAP4.error:
            return None

    def send(self, msg):
        # ponytail: no APPEND to Sent — Gmail/most providers save a copy themselves
        smtp_send(self._smtp_host, self.address, self._pw, msg)

    def close(self):
        try:
            with self._lock:
                self._conn.logout()
        except Exception:
            pass


# --- multi-account session ----------------------------------------------------
@dataclass
class Account:
    name: str
    color: str
    backend: object  # ImapBackend | DemoBackend


class Session:
    """One signed-in session over any number of account backends.

    `scope` is an account name to address one account, or None for the merged
    all-accounts view.
    """

    def __init__(self, accounts):
        self.accounts = list(accounts)
        seen = set()  # names are the routing key for every operation — force unique
        for a in self.accounts:
            base, n = a.name, 2
            while a.name in seen:
                a.name = f'{base}{n}'
                n += 1
            seen.add(a.name)

    def account(self, name) -> Account:
        return next(a for a in self.accounts if a.name == name)

    def color(self, name) -> str:
        try:
            return self.account(name).color
        except StopIteration:
            return 'white'

    def address(self, name) -> str:
        return self.account(name).backend.address

    def scoped(self, scope):
        return [a for a in self.accounts if scope in (None, a.name)]

    SPECIAL = {
        'trash': ('trash', '[gmail]/trash', '[gmail]/bin', 'bin', 'deleted items',
                  'deleted messages', 'junk/trash'),
        'archive': ('archive', 'archives', '[gmail]/all mail', 'all mail'),
    }
    outbox = None  # tuimail.outbox.Outbox, attached by the app; backs the OUTBOX folder

    def folders_detailed(self, scope=None):
        """-> (merged [(name, unread)], {account: {folder: unread}}); remembers
        each account's folder names for special-folder lookups."""
        accts = self.scoped(scope)
        order, merged, per_account, last_err = [], {}, {}, None
        for a in accts:
            try:
                account_folders = a.backend.folders()
            except Exception as exc:
                if len(accts) == 1:
                    raise
                last_err = exc  # merged view: one dead account must not brick the rest
                continue
            per_account[a.name] = dict(account_folders)
            self._folder_names = getattr(self, '_folder_names', {})
            self._folder_names[a.name] = list(
                getattr(a.backend, 'all_folder_names', None) or [n for n, _ in account_folders])
            for name, unread in account_folders:
                if name not in merged:
                    order.append(name)
                    merged[name] = 0
                merged[name] += unread
        if not merged and last_err is not None:
            raise last_err  # every account failed — that's an outage, not an empty list
        counts = [(n, merged[n]) for n in order]
        if self.outbox is not None:
            # a single-account session has no merged view: it sees everything
            names = None if scope is None or len(self.accounts) == 1 else {a.name for a in accts}
            queued = len(self.outbox.summaries(names))
            counts.insert(min(1, len(counts)), (OUTBOX, queued))  # right under INBOX
        return counts, per_account

    def folders(self, scope=None):
        return self.folders_detailed(scope)[0]

    def _refresh_names(self, account):
        """Fetch one account's folder list on demand (unknown != absent)."""
        self._folder_names = getattr(self, '_folder_names', {})
        try:
            backend = self.account(account).backend
            listed = backend.folders()
            self._folder_names[account] = list(
                getattr(backend, 'all_folder_names', None) or [n for n, _ in listed])
        except Exception:
            self._folder_names.pop(account, None)
        return self._folder_names.get(account)

    def special_folder(self, account, kind):
        """Name of the account's Trash/Archive folder, or None if it has none.
        Raises if the folder list is unavailable — the caller must not guess."""
        backend = self.account(account).backend
        declared = getattr(backend, 'special', {}).get(kind)
        if declared:
            return declared
        names = getattr(self, '_folder_names', {}).get(account)
        if names is None:
            names = self._refresh_names(account)
        if names is None:
            raise RuntimeError(f'{account}: folder list unavailable — refresh (R) first')
        wanted = self.SPECIAL[kind]
        for n in names:
            if n.lower() in wanted or decode_folder(n).lower() in wanted:
                return n
        return None

    def move(self, account, folder, uid, dest):
        if OUTBOX in (folder, dest):
            raise RuntimeError('Outbox messages can only be sent or deleted')
        self.account(account).backend.move(folder, uid, dest)

    def list_older(self, folder, scope, min_uids):
        """Next page per account: messages older than min_uids[account]."""
        if folder == OUTBOX:
            return []
        out = []
        for a in self.scoped(scope):
            before = min_uids.get(a.name)
            if before is None:
                continue
            try:
                msgs = a.backend.list_messages(folder, before_uid=before)
            except Exception:
                continue
            for s in msgs:
                s.account = a.name
            out.extend(msgs)
        out.sort(key=lambda s: s.date or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
        return out

    def list_messages(self, folder, scope=None):
        if folder == OUTBOX:
            if self.outbox is None:
                return []
            # the merged view also shows mail queued for an account that is
            # not signed in right now — otherwise it could never be deleted
            everything = scope is None or len(self.accounts) == 1
            return self.outbox.summaries(None if everything else {scope},
                                         signed_in={a.name for a in self.accounts})
        accts = self.scoped(scope)
        out, ok, last_err = [], 0, None
        for a in accts:
            try:
                msgs = a.backend.list_messages(folder)
            except Exception as exc:
                if len(accts) == 1:
                    raise
                last_err = exc  # merged view: an account without this folder is skipped
                continue
            ok += 1
            for s in msgs:
                s.account = a.name
            out.extend(msgs)
        if not ok and last_err is not None:
            raise last_err  # every account failed — surface it, don't fake an empty folder
        out.sort(key=lambda s: s.date or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
        return out

    def fetch(self, account, folder, uid):
        if folder == OUTBOX:
            rec = self.outbox.get(uid) if self.outbox is not None else None
            if rec is None:
                raise MailGone()
            return self.outbox.message(rec, for_preview=True)
        return self.account(account).backend.fetch(folder, uid)

    def mark(self, account, folder, uid, read=True):
        if folder == OUTBOX:
            return  # bold in the Outbox means "failed", not "unread"
        self.account(account).backend.mark(folder, uid, read=read)

    def flag(self, account, folder, uid, flagged=True):
        if folder == OUTBOX:
            return
        self.account(account).backend.flag(folder, uid, flagged=flagged)

    def delete(self, account, folder, uid):
        """Delete = move to the account's Trash when it has one (recoverable);
        deleting from Trash itself, or without a Trash folder, expunges."""
        if folder == OUTBOX:
            if self.outbox is not None:
                self.outbox.remove(uid)
            return
        trash = self.special_folder(account, 'trash')
        if trash and folder != trash:
            self.account(account).backend.move(folder, uid, trash)
        else:
            self.account(account).backend.delete(folder, uid)

    def search(self, folder, query, scope=None):
        """-> ({(account, uid)}, [account names that need a local fallback])"""
        if folder == OUTBOX:
            q = query.lower()
            return {(r['account'], r['id']) for r in (self.outbox.items() if self.outbox else [])
                    if scope in (None, r['account'])
                    and q in f'{r["to"]} {r["subject"]} {r["body"]}'.lower()}, []
        hits, fallback = set(), []
        for a in self.scoped(scope):
            try:
                h = a.backend.search(folder, query)
            except Exception:
                h = None
            if h is None:
                fallback.append(a.name)
            else:
                hits |= {(a.name, u) for u in h}
        return hits, fallback

    def send(self, account, msg):
        self.account(account).backend.send(msg)

    def close(self):
        for a in self.accounts:
            try:
                a.backend.close()
            except Exception:
                pass


def demo_session() -> Session:
    return Session([
        Account('personal', DEFAULT_COLORS[0], DemoBackend('you@tuimail.demo', 'home')),
        Account('work', DEFAULT_COLORS[1], DemoBackend('work@tuimail.demo', 'work')),
    ])


# --- demo backend ------------------------------------------------------------
def _demo_msg(sender, subject, body, *, to='you@tuimail.demo', html=False,
              hours=0, days=0, attach=None):
    m = EmailMessage()
    m['From'], m['To'], m['Subject'] = sender, to, subject
    m['Date'] = email.utils.format_datetime(
        datetime.now().astimezone() - timedelta(days=days, hours=hours))
    m['Message-ID'] = email.utils.make_msgid(domain='tuimail.demo')
    m.set_content(body, subtype='html' if html else 'plain')
    if attach:
        m.add_attachment(attach[1], maintype='text', subtype='plain', filename=attach[0])
    return m


def _demo_data(flavor='home', address='you@tuimail.demo'):
    if flavor == 'work':
        inbox = [
            dict(msg=_demo_msg('Rita Chen <rita@corp.example>', 'Standup notes + action items',
                               'Deploy freeze starts Thursday. Your two items:\n'
                               '- review the retry PR\n- rotate the staging certs\n',
                               to=address, hours=2), unread=True, flagged=False),
            dict(msg=_demo_msg('CI <ci@corp.example>', 'staging deploy #142 green',
                               'All 214 checks passed. https://ci.corp.example/142',
                               to=address, hours=7), unread=False, flagged=False),
            dict(msg=_demo_msg('Accounts <billing@corp.example>', 'Expense report approved',
                               'Your September expense report was approved.',
                               to=address, days=1), unread=False, flagged=False),
        ]
        sent = [dict(msg=_demo_msg(address, 'Re: Standup notes + action items',
                                   'On it — PR review today.', to='rita@corp.example',
                                   hours=1), unread=False, flagged=False)]
        data = {'INBOX': inbox, 'Sent': sent}
        for folder, items in data.items():
            for n, it in enumerate(items):
                it['uid'] = f'{folder[:2].lower()}{n + 1}'
        return data

    dt = email.utils.format_datetime(datetime.now().astimezone() - timedelta(hours=5))
    corp = email.message_from_bytes(
        (f'From: =?utf-8?q?Doe=2C_John?= <john.doe@corp.example>\r\n'
         f'To: {address}\r\nSubject: Q3 planning notes\r\nDate: {dt}\r\n'
         f'Message-ID: <q3-planning@corp.example>\r\n'
         f'Content-Type: text/plain; charset=utf-8\r\n\r\n'
         f'Hi,\r\n\r\nCould you look over the Q3 notes before the Friday sync?\r\n'
         f'The board deck depends on your numbers.\r\n\r\n-- John\r\n').encode(),
        policy=email.policy.default)
    inbox = [
        dict(msg=_demo_msg('Textual Weekly <news@textualize.io>', 'Beautiful terminals ship this week',
                           'The latest on terminal UIs:\n\n'
                           '- Docs: https://textual.textualize.io\n'
                           '- Source: https://github.com/Textualize/textual\n\n'
                           'Build something lovely.\n', hours=1), unread=True, flagged=False),
        dict(msg=corp, unread=True, flagged=False),
        dict(msg=_demo_msg('Mira <mira@example.com>', 'Flight itinerary + packing list',
                           'Landing Friday 18:40, gate B12. Packing list attached so we '
                           'do not forget the chargers again.\n\nx M',
                           hours=9, attach=('packing-list.txt', b'passport\nchargers\nadapter\nsunscreen\n')),
             unread=False, flagged=True),
        dict(msg=_demo_msg('GitHub <noreply@github.com>', '[tuimail] Your build passed',
                           '<html><body><h2>CI report</h2>'
                           '<p>Build <b>#42</b> passed on <i>main</i>.</p>'
                           '<ul><li>214 checks green</li><li>artifacts published</li></ul>'
                           '<p><a href="https://github.com">View run</a></p>'
                           '<style>p{color:red}</style></body></html>',
                           html=True, hours=14), unread=True, flagged=False),
        dict(msg=_demo_msg('Vera Marlow <vera@studio.example>', 'Logo drafts, round two',
                           'Three directions this time. The wordmark one is my favourite — '
                           'it survives a 16x16 favicon.\n\nVera', days=1), unread=False, flagged=False),
        dict(msg=_demo_msg('Library <notices@citylib.example>', 'Your reservation is ready',
                           'The Pragmatic Programmer is waiting at the front desk until Thursday.',
                           days=2), unread=False, flagged=False),
        dict(msg=_demo_msg('Sam Ortiz <sam@example.com>', 'Re: Saturday ride',
                           '> 60km, coffee halfway?\n\nDeal. 8am at the bridge.', days=3),
             unread=False, flagged=False),
        dict(msg=_demo_msg('statements@bank.example', 'August statement available',
                           'Your August statement is ready in the app.', days=6),
             unread=False, flagged=False),
    ]
    sent = [
        dict(msg=_demo_msg('you@tuimail.demo', 'Re: Logo drafts, round two',
                           'Wordmark it is. Invoice when ready!', days=1), unread=False, flagged=False),
        dict(msg=_demo_msg('you@tuimail.demo', 'Minutes from the retro',
                           'Attached in the doc. Main theme: fewer meetings.', days=4),
             unread=False, flagged=False),
    ]
    archive = [
        dict(msg=_demo_msg('Old Friend <ana@example.com>', 'Photos from the trip',
                           'Finally sorted them: 400 photos, 12 good ones. Classic.', days=40),
             unread=False, flagged=False),
    ]
    data = {'INBOX': inbox, 'Sent': sent, 'Archive': archive, 'Trash': []}
    for folder, items in data.items():
        for n, it in enumerate(items):
            it['uid'] = f'{folder[:2].lower()}{n + 1}'
    return data


def _nice_from_msg(msg):
    f = msg.get('From')
    try:
        if f is not None and f.addresses:
            a = f.addresses[0]
            return a.display_name or a.addr_spec
    except Exception:
        pass
    return nice_from(str(f or ''))


class DemoBackend:
    def __init__(self, address='you@tuimail.demo', flavor='home'):
        self.address = address
        self._data = _demo_data(flavor, address)
        self.outbox = []  # sent EmailMessage objects, for tests

    def _find(self, folder, uid):
        for it in self._data.get(folder, []):
            if it['uid'] == uid:
                return it
        raise MailGone('message no longer exists')

    def folders(self):
        return [(name, sum(1 for it in items if it['unread']))
                for name, items in self._data.items()]

    def list_messages(self, folder, limit=100, before_uid=None):
        if before_uid is not None:
            return []  # the demo mailbox has no older pages
        out = [Summary(uid=it['uid'], sender=_nice_from_msg(it['msg']),
                       subject=str(it['msg'].get('Subject') or '(no subject)'),
                       date=parse_date(it['msg'].get('Date')),
                       unread=it['unread'], flagged=it['flagged'],
                       recipient=nice_from(str(it['msg'].get('To') or '')))
               for it in self._data.get(folder, [])]
        out.sort(key=lambda s: s.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out

    def fetch(self, folder, uid):
        return self._find(folder, uid)['msg']

    def mark(self, folder, uid, read=True):
        self._find(folder, uid)['unread'] = not read

    def flag(self, folder, uid, flagged=True):
        self._find(folder, uid)['flagged'] = flagged

    def delete(self, folder, uid):
        self._data[folder] = [it for it in self._data.get(folder, []) if it['uid'] != uid]

    def move(self, folder, uid, dest):
        if dest not in self._data:
            raise RuntimeError(f'no folder {dest}')
        it = self._find(folder, uid)
        self._data[folder] = [x for x in self._data[folder] if x is not it]
        moved = dict(it, uid=f'{dest[:2].lower()}{len(self._data[dest]) + 1}-{uid}')
        self._data[dest].insert(0, moved)

    def search(self, folder, query):
        q = query.lower()
        hits = set()
        for it in self._data.get(folder, []):
            hay = ' '.join([str(it['msg'].get('From') or ''),
                            str(it['msg'].get('Subject') or ''),
                            body_of(it['msg'])]).lower()
            if q in hay:
                hits.add(it['uid'])
        return hits

    def send(self, msg):
        self.outbox.append(msg)
        self._data.setdefault('Sent', []).insert(
            0, dict(uid=f'sent-out{len(self.outbox)}', msg=msg, unread=False, flagged=False))

    def close(self):
        pass
