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


def config_path() -> Path:
    if p := os.environ.get('TUIMAIL_CONFIG'):
        return Path(p)
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).parent
        # a macOS .app install is not portable media: its Contents/MacOS dir is
        # user-writable, but settings belong in $HOME (and survive app updates)
        if '.app/Contents/' not in exe_dir.as_posix():
            here = exe_dir / 'tuimail.json'  # portable binary: settings travel next to it
            # non-writable dir (e.g. /usr/local/bin) -> fall through to home
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
        # pre-1.5.1 mac builds kept the config inside the .app bundle — migrate
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


def extract_links(msg) -> list[str]:
    texts = []
    for part in msg.walk():
        if part.get_content_type() in ('text/plain', 'text/html'):
            try:
                texts.append(part.get_content())
            except Exception:
                pass
    seen: dict[str, None] = {}
    for t in texts:
        for u in URL_RE.findall(t):
            seen.setdefault(sanitize(u, keep_newlines=False).rstrip('.,;:!?'))
    return list(seen)


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
    return {'to': addr, 'subject': subject, 'body': body,
            'in_reply_to': ' '.join(mid.split())}


def build_message(sender, to, subject, body, in_reply_to=None) -> EmailMessage:
    m = EmailMessage()
    m['From'], m['To'], m['Subject'] = sender, to, subject
    m['Date'] = email.utils.formatdate(localtime=True)
    # pin the msgid domain to the sender's — the default embeds the local
    # machine's hostname in every outgoing mail
    m['Message-ID'] = email.utils.make_msgid(
        domain=sender.rsplit('@', 1)[-1] if '@' in sender else None)
    if in_reply_to:
        m['In-Reply-To'] = m['References'] = ' '.join(in_reply_to.split())
    m.set_content(body)
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
        ))
    out.sort(key=lambda s: s.date or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out


def _smtp_send_once(host, port, address, password, msg) -> None:
    tls = tls_context()  # stdlib default skips verification
    # local_hostname: the default EHLO leaks the machine's hostname or LAN IP
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=20, context=tls,
                                  local_hostname='localhost')
    else:
        server = smtplib.SMTP(host, port, timeout=20, local_hostname='localhost')
    with server as s:
        if port != 465:
            s.starttls(context=tls)
        s.login(address, password)
        s.send_message(msg)


def smtp_send(smtp_host, address, password, msg) -> None:
    host, _, port = smtp_host.partition(':')
    port = int(port or 465)
    try:
        _smtp_send_once(host, port, address, password, msg)
    except (ssl.SSLError, ConnectionError, TimeoutError):
        if port != 465:
            raise
        # some networks break implicit-TLS :465 (resets mid-handshake) while
        # the submission port still works — retry over 587 STARTTLS
        _smtp_send_once(host, 587, address, password, msg)


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
            names = []
            for line in data or []:
                if not isinstance(line, bytes) or re.search(rb'(?i)\\Noselect', line):
                    continue
                m = re.match(rb'\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(.+)$', line)
                if not m:
                    continue
                name = m.group(1).strip()
                if name.startswith(b'"') and name.endswith(b'"'):
                    name = name[1:-1]
                decoded = name.decode('ascii', 'replace')  # ponytail: modified-UTF7 folder names shown raw
                if re.search(r'["\r\n]', decoded):
                    continue  # server-controlled name that could break out of our IMAP quoting
                names.append(decoded)
            names.sort(key=lambda n: (n.upper() != 'INBOX', n.upper()))
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

    def list_messages(self, folder, limit=100):
        def go():
            self._select(folder)
            typ, d = self._conn.uid('search', None, 'ALL')
            uids = (d[0] or b'').split()[-limit:]
            if not uids:
                return []
            typ, resp = self._conn.uid(
                'fetch', b','.join(uids),
                '(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
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

    def folders(self, scope=None):
        accts = self.scoped(scope)
        order, merged, last_err = [], {}, None
        for a in accts:
            try:
                account_folders = a.backend.folders()
            except Exception as exc:
                if len(accts) == 1:
                    raise
                last_err = exc  # merged view: one dead account must not brick the rest
                continue
            for name, unread in account_folders:
                if name not in merged:
                    order.append(name)
                    merged[name] = 0
                merged[name] += unread
        if not merged and last_err is not None:
            raise last_err  # every account failed — that's an outage, not an empty list
        return [(n, merged[n]) for n in order]

    def list_messages(self, folder, scope=None):
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
        return self.account(account).backend.fetch(folder, uid)

    def mark(self, account, folder, uid, read=True):
        self.account(account).backend.mark(folder, uid, read=read)

    def flag(self, account, folder, uid, flagged=True):
        self.account(account).backend.flag(folder, uid, flagged=flagged)

    def delete(self, account, folder, uid):
        self.account(account).backend.delete(folder, uid)

    def search(self, folder, query, scope=None):
        """-> ({(account, uid)}, [account names that need a local fallback])"""
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
    data = {'INBOX': inbox, 'Sent': sent, 'Archive': archive}
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

    def list_messages(self, folder, limit=100):
        out = [Summary(uid=it['uid'], sender=_nice_from_msg(it['msg']),
                       subject=str(it['msg'].get('Subject') or '(no subject)'),
                       date=parse_date(it['msg'].get('Date')),
                       unread=it['unread'], flagged=it['flagged'])
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
