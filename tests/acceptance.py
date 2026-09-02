"""Acceptance loops for tuimail — run: python tests/acceptance.py

Drives the real app headless through Textual's Pilot against the demo backends.
Also asserts the pure parsing helpers, including regressions for
review-confirmed bugs of earlier iterations.
"""
import asyncio
import email
import email.policy
import json
import os
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP = tempfile.mkdtemp(prefix='tuimail-test-')
os.environ['TUIMAIL_CONFIG'] = str(Path(TMP) / 'config.json')
os.environ['TUIMAIL_DOWNLOADS'] = TMP
os.environ['TUIMAIL_NO_UPDATE_CHECK'] = '1'  # never hit the GitHub API from tests
os.environ['TUIMAIL_OUTBOX'] = str(Path(TMP) / 'outbox')

from textual.widgets import DataTable, Input, ListView, Select, TextArea  # noqa: E402

from tuimail import backend as be  # noqa: E402
from tuimail import update as up  # noqa: E402
from tuimail.app import (AccountFormScreen, AccountsScreen, ComposeScreen,  # noqa: E402
                         HelpScreen, LinksScreen, LoginScreen, MainScreen,
                         OnboardingScreen, ReaderScreen, TuiMail)


# --- phase 0: pure helpers ----------------------------------------------------
def helpers():
    # body extraction prefers plain, strips html/script from html-only mail
    m = EmailMessage()
    m.set_content('plain text')
    m.add_alternative('<p>html <b>bold</b></p>', subtype='html')
    parsed = email.message_from_bytes(m.as_bytes(), policy=email.policy.default)
    assert be.body_of(parsed).strip() == 'plain text'
    h = EmailMessage()
    h.set_content('<p>x &amp; y</p><script>bad()</script>', subtype='html')
    parsed = email.message_from_bytes(h.as_bytes(), policy=email.policy.default)
    assert 'x & y' in be.body_of(parsed) and 'bad()' not in be.body_of(parsed)
    assert be.dec('=?utf-8?q?J=C3=BCrgen?=') == 'Jürgen'

    # regression: reply address from an RFC2047 "Last, First" sender
    raw = (b'From: =?utf-8?q?Doe=2C_John?= <john.doe@corp.example>\r\n'
           b'Subject: hi\r\nMessage-ID: <a@b>\r\n\r\nbody\r\n')
    seed = be.reply_seed(email.message_from_bytes(raw, policy=email.policy.default))
    assert seed['to'] == 'john.doe@corp.example', seed
    assert seed['subject'] == 'Re: hi'
    assert '> body' in seed['body']

    # regression: folded Message-ID must not blow up header assignment
    msg = be.build_message('a@b', 'c@d', 's', 'body', in_reply_to='<x\r\n @y>')
    assert msg['In-Reply-To'] == '<x @y>'

    # regression: UID/FLAGS trailing the header literal (RFC-legal order)
    hdr = b'From: x@y\r\nSubject: post-literal\r\nDate: Mon, 01 Sep 2025 10:00:00 +0000\r\n\r\n'
    resp = [(b'1 (BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {%d}' % len(hdr), hdr),
            b' UID 100 FLAGS (\\Flagged))']
    out = be.parse_fetch_headers(resp)
    assert len(out) == 1 and out[0].uid == '100' and out[0].flagged and out[0].unread

    # normal order still parses
    resp = [(b'2 (UID 7 FLAGS (\\Seen) BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {%d}' % len(hdr),
             hdr), b')']
    out = be.parse_fetch_headers(resp)
    assert out[0].uid == '7' and not out[0].unread

    # regression: config file with valid-but-not-object JSON must not crash startup
    be.config_path().write_text('"oops"', encoding='utf-8')
    assert be.load_config() == {}

    # migration: pre-multi-account flat config becomes accounts[0]
    be.config_path().write_text(json.dumps(
        {'address': 'old@x.com', 'imap_host': 'i.x.com', 'password': 'pw'}), encoding='utf-8')
    cfg = be.load_config()
    a = cfg['accounts'][0]
    assert a['address'] == 'old@x.com' and a['imap_host'] == 'i.x.com'
    assert a['password'] == 'pw' and a['color'] == be.DEFAULT_COLORS[0]
    be.config_path().unlink()

    # security: hostile control chars (ESC/CSI/C1) never reach the terminal
    evil = 'x\x1b]0;pwn\x07\x1b[31m\x9cy'
    assert be.sanitize(evil) == 'xy'
    assert be.dec('inv\x1boice') == 'invoice'
    assert be.nice_from('e\x1bvil <a\x1b@b.c>') in ('evil', 'a@b.c', 'evil <a@b.c>')
    hostile = email.message_from_bytes(
        b'Content-Type: text/plain\r\n\r\nhi \x1b]52;c;evil\x07 https://x.y/\x1b[2Jz\r\n',
        policy=email.policy.default)
    assert '\x1b' not in be.body_of(hostile)
    assert all('\x1b' not in u for u in be.extract_links(hostile))

    # security: reply prefill can't smuggle escapes via Date or the sender addr
    raw = (b'From: att\x1backer <x@y.z>\r\n'
           b'Date: Mon\x1b]0;pwn\x07, 01 Sep 2025 10:00:00 +0000\r\n'
           b'Subject: s\r\nMessage-ID: <m@x>\r\n\r\nbody\r\n')
    seed = be.reply_seed(email.message_from_bytes(raw, policy=email.policy.default))
    assert '\x1b' not in seed['body'] and '\x1b' not in seed['to'] and 'pwn' not in seed['body']

    # security: a bomb of unclosed <script tags must not freeze the HTML scrub
    import time
    bomb = email.message_from_bytes(
        b'Content-Type: text/html\r\n\r\n' + b'<script ' * 20000,
        policy=email.policy.default)
    t0 = time.time()
    be.body_of(bomb)
    assert time.time() - t0 < 3, 'HTML scrub is quadratic again'

    # security: portable mode (config next to the exe) never persists passwords
    import sys as _sys
    import tempfile as _tf
    saved_env = os.environ.pop('TUIMAIL_CONFIG')
    pdir = Path(_tf.mkdtemp(prefix='tuimail-portable-'))
    (pdir / 'tuimail.json').write_text('{}', encoding='utf-8')
    orig_exe = _sys.executable
    _sys.frozen = True
    _sys.executable = str(pdir / 'tuimail.exe')
    try:
        assert be.portable_mode()
        assert be.save_config({'accounts': [
            {'name': 'a', 'address': 'a@b.c', 'password': 'sekret'}]})
        data = (pdir / 'tuimail.json').read_text(encoding='utf-8')
        assert 'sekret' not in data and 'a@b.c' in data
    finally:
        del _sys.frozen
        _sys.executable = orig_exe
        os.environ['TUIMAIL_CONFIG'] = saved_env

    # security: outgoing Message-ID must not leak the local hostname
    msg = be.build_message('me@example.org', 'you@x.y', 's', 'b')
    assert str(msg['Message-ID']).endswith('@example.org>')

    # security: IMAP connections must verify certificates and hostnames
    import ssl as _ssl
    captured = {}

    class _Boom(Exception):
        pass

    class _FakeImap:
        def __init__(self, host, port, timeout=None, ssl_context=None):
            captured['ctx'] = ssl_context
            raise _Boom

    orig = be.imaplib.IMAP4_SSL
    be.imaplib.IMAP4_SSL = _FakeImap
    try:
        try:
            be.ImapBackend('a@b.c', 'pw', 'imap.b.c', 'smtp.b.c')
        except _Boom:
            pass
    finally:
        be.imaplib.IMAP4_SSL = orig
    ctx = captured['ctx']
    assert ctx is not None and ctx.verify_mode == _ssl.CERT_REQUIRED and ctx.check_hostname

    # marketing layout tables (no <th>) flatten instead of empty markdown grids
    layout_html = ('<table><tr><td><h2>Hello</h2></td><td>'
                   '<table><tr><td>inner text</td></tr></table></td></tr></table>')
    m3 = email.message_from_bytes(
        ('Content-Type: text/html\r\n\r\n' + layout_html).encode(),
        policy=email.policy.default)
    md3 = be.body_markdown(m3)
    assert md3 and 'Hello' in md3 and 'inner text' in md3, repr(md3)
    assert '|' not in md3  # no grid borders for layout tables
    # real data tables (with <th>) keep their grid
    m4 = email.message_from_bytes(
        b'Content-Type: text/html\r\n\r\n'
        b'<table><tr><th>H</th></tr><tr><td>v</td></tr></table>',
        policy=email.policy.default)
    md4 = be.body_markdown(m4)
    assert md4 and '|' in md4 and 'v' in md4, repr(md4)
    # a junk-only conversion falls back to the plain-text path
    m5 = email.message_from_bytes(
        b'Content-Type: text/html\r\n\r\n<table><tr><td></td><td></td></tr></table>',
        policy=email.policy.default)
    assert be.body_markdown(m5) is None

    # compose markup: bold/italic/link become an HTML alternative, escaped
    rich = be.build_message('a@b.c', 'x@y.z', 's',
                            '**bold** and *it* [go](https://x.y) <script>', markup=True)
    # without the composer's format keys, incidental asterisks stay plain text
    assert be.build_message('a@b.c', 'x@y.z', 's', '2*3*4 and *emphasis*'
                            ).get_body(preferencelist=('html',)) is None
    html_part = rich.get_body(preferencelist=('html',))
    assert html_part is not None
    html_src = html_part.get_content()
    assert '<b>bold</b>' in html_src and '<i>it</i>' in html_src
    assert '<a href="https://x.y">go</a>' in html_src
    assert '<script>' not in html_src and '&lt;script&gt;' in html_src
    assert be.body_of(rich).strip().startswith('**bold**')  # plain part keeps markup text
    plain = be.build_message('a@b.c', 'x@y.z', 's', 'no markup here')
    assert plain.get_body(preferencelist=('html',)) is None
    # quoted text belongs to the other party: never re-marked into OUR html
    reply_body = '**mine**\n\n> *theirs* [phish](https://evil.example)\n> more'
    h = be.markup_html(reply_body)
    assert h and '<b>mine</b>' in h and '<i>theirs</i>' not in h
    assert 'href' not in h and 'evil.example' in h  # url shown as text, not a link
    # hostile bracket runs must not make the link regex quadratic
    import time as _t
    t0 = _t.time()
    be.markup_html('[' * 50000 + '(https://x')
    assert _t.time() - t0 < 1.5, 'markup_html link regex is backtracking'

    # attachments ride along with the right filename
    att = Path(TMP) / 'notes.txt'
    att.write_text('hi', encoding='utf-8')
    with_att = be.build_message('a@b.c', 'x@y.z', 's', 'b', attachments=[att])
    got = be.attachments_of(with_att)
    assert got and got[0][0] == 'notes.txt' and got[0][1] == b'hi'

    # shell-style path completion
    from tuimail.app import complete_path
    cdir = Path(TMP) / 'comp'
    cdir.mkdir()
    (cdir / 'alpha.txt').write_text('a')
    (cdir / 'alphabet.txt').write_text('b')
    (cdir / 'beta').mkdir()
    assert complete_path(str(cdir / 'alph')).endswith('alpha')  # common prefix
    assert complete_path(str(cdir / 'alphabet')).endswith('alphabet.txt')
    assert complete_path(str(cdir / 'be')).endswith('beta' + os.sep)  # dirs get a sep
    assert complete_path(str(cdir / 'zzz')) == str(cdir / 'zzz')  # no match: unchanged

    # updater basics: version compare and platform asset selection
    assert up.parse_version('v1.2.3') == (1, 2, 3)
    assert up.is_newer('v99.0.0') and not up.is_newer('v0.0.1') and not up.is_newer('')
    assert up.asset_name() in ('tuimail-windows.exe', 'tuimail-macos-universal')
    assert up.cli_installed() is False  # never true off-macOS / unfrozen, never raises
    # a Homebrew-owned binary is never self-replaced
    import sys as _sysb
    orig_exeb = _sysb.executable
    _sysb.frozen = True
    _sysb.executable = '/opt/homebrew/Caskroom/tuimail/1.12.0/tuimail'
    try:
        assert up.install_kind() == 'brew'
    finally:
        del _sysb.frozen
        _sysb.executable = orig_exeb

    # HTML mail converts to markdown for the reader (headings, bold, links kept)
    rich_html = email.message_from_bytes(
        b'Content-Type: text/html\r\n\r\n<h1>T</h1><p><b>bold</b> '
        b'<a href="https://x.y">link</a></p><script>bad()</script>',
        policy=email.policy.default)
    md_out = be.body_markdown(rich_html)
    assert md_out and '# T' in md_out and '**bold**' in md_out
    assert '[link](https://x.y)' in md_out and 'bad()' not in md_out
    plain_only = email.message_from_bytes(
        b'Content-Type: text/plain\r\n\r\njust text\r\n', policy=email.policy.default)
    assert be.body_markdown(plain_only) is None

    # gmail labels in non-Latin scripts (IMAP modified UTF-7) decode for display
    assert be.decode_folder('&BB8EQAQ4BDIENQRC-') == 'Привет'
    assert be.decode_folder('INBOX') == 'INBOX'
    assert be.decode_folder('&-x') == '&x'
    # ...but mUTF-7 can synthesize ESC/newlines from printable wire names
    assert '\x1b' not in be.decode_folder('&ABs-[31mEVIL&ABs-[0m')
    assert '\n' not in be.decode_folder('A&AAo-B')

    # inline tags must not split words or detach punctuation
    inline = email.message_from_bytes(
        b'Content-Type: text/html\r\n\r\n<p>Casa<i>blanca</i> on <b>main</b>.</p>',
        policy=email.policy.default)
    out = be.body_of(inline)
    assert 'Casablanca' in out and 'main.' in out, repr(out)

    # a macOS .app install is NOT portable mode: config lives in $HOME
    import sys as _sysx
    saved_envx = os.environ.pop('TUIMAIL_CONFIG')
    orig_exex = _sysx.executable
    _sysx.frozen = True
    _sysx.executable = '/Applications/tuimail.app/Contents/MacOS/tuimail-bin'
    try:
        assert be.config_path() == Path.home() / '.tuimail.json'
        assert not be.portable_mode()
    finally:
        del _sysx.frozen
        _sysx.executable = orig_exex
        os.environ['TUIMAIL_CONFIG'] = saved_envx

    # layout-table HTML must not render as runs of blank lines
    layout = ('<table>' + '<tr><td>&nbsp;</td></tr>' * 10
              + '<tr><td>hello</td></tr>' + '<tr><td>&nbsp;</td></tr>' * 10
              + '<tr><td>world</td></tr></table>')
    m2 = email.message_from_bytes(
        ('Content-Type: text/html\r\n\r\n' + layout).encode(),
        policy=email.policy.default)
    out = be.body_of(m2)
    assert 'hello' in out and 'world' in out and '\n\n\n' not in out, repr(out)

    # a network that resets :465 mid-handshake gets an automatic 587 retry
    import ssl as _ssl465
    calls = []
    orig_once = be._smtp_send_once

    def fake_once(host, port, address, password, msg):
        calls.append(port)
        if port == 465:
            raise _ssl465.SSLEOFError('EOF occurred in violation of protocol')
    be._smtp_send_once = fake_once
    try:
        be.smtp_send('smtp.x.y:465', 'a@x.y', 'pw', None)
    finally:
        be._smtp_send_once = orig_once
    assert calls == [465, 587], calls

    # special folders: RFC 6154 attributes and the full LIST (not the 30-folder
    # STATUS cap) decide where deletes go — a hidden Trash must never mean expunge
    class _FakeConn:
        def __init__(self):
            self.log = []

        def list(self):
            lines = [b'(\\HasNoChildren) "/" "INBOX"']
            lines += [b'(\\HasNoChildren) "/" "Label%02d"' % i for i in range(40)]
            lines.append(b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"')
            return 'OK', lines

        def status(self, name, what):
            return 'OK', [b'%s (UNSEEN 0)' % name.encode()]

        def select(self, folder):
            return 'OK', [b'1']

        def uid(self, *args):
            self.log.append(args)
            return 'OK', [b'']

        def expunge(self):
            self.log.append(('expunge',))
            return 'OK', [b'']

    ib = be.ImapBackend.__new__(be.ImapBackend)
    ib.address, ib._pw, ib._imap_host, ib._smtp_host = 'a@b.c', '', 'i', 's'
    ib._conn, ib._selected = _FakeConn(), None
    import threading as _th
    ib._lock = _th.Lock()
    counted = ib.folders()
    assert len(counted) == 30 and '[Gmail]/Trash' in ib.all_folder_names
    assert ib.special['trash'] == '[Gmail]/Trash'
    sess = be.Session([be.Account('g', 'red', ib)])
    sess.folders_detailed()
    sess.delete('g', 'INBOX', '7')
    assert ('MOVE', '7', '"[Gmail]/Trash"') in ib._conn.log, ib._conn.log
    assert ('expunge',) not in ib._conn.log
    # unknown folder list -> refreshed on demand, never guessed as 'no Trash'
    sess2 = be.Session([be.Account('g', 'red', ib)])
    ib._conn.log.clear()
    sess2.delete('g', 'INBOX', '8')
    assert ('MOVE', '8', '"[Gmail]/Trash"') in ib._conn.log

    # regression: duplicate account names are uniquified (the name routes every op)
    s = be.Session([be.Account('john', 'red', be.DemoBackend('a@x', 'home')),
                    be.Account('john', 'blue', be.DemoBackend('b@x', 'work'))])
    assert [a.name for a in s.accounts] == ['john', 'john2']

    # regression: one dead account must not brick the merged view; all-dead raises
    class Dead:
        address = 'dead@x'

        def folders(self):
            raise OSError('down')

        def list_messages(self, folder, limit=100):
            raise OSError('down')

    s = be.Session([be.Account('ok', 'red', be.DemoBackend()),
                    be.Account('dead', 'blue', Dead())])
    assert dict(s.folders()).get('INBOX', 0) >= 1
    assert s.list_messages('INBOX')
    dead2 = be.Session([be.Account('d1', 'red', Dead()), be.Account('d2', 'blue', Dead())])
    try:
        dead2.list_messages('INBOX')
        raise AssertionError('all-accounts failure must raise, not fake an empty folder')
    except OSError:
        pass

    assert be.extract_links(email.message_from_bytes(
        b'Content-Type: text/plain\r\n\r\nsee https://example.com/x. and http://a.b\r\n',
        policy=email.policy.default)) == [('', 'https://example.com/x'), ('', 'http://a.b')]
    # html: anchors carry their text; image sources and tracking pixels are not links
    assert be.extract_links(email.message_from_bytes(
        b'Content-Type: text/html\r\n\r\n<a href="https://u.x/unsub">Unsubscribe</a>'
        b'<img src="https://px.example/1x1.gif"><a href="https://u.x/unsub">dup</a>',
        policy=email.policy.default)) == [('Unsubscribe', 'https://u.x/unsub')]
    print('phase 0 (helpers): ok')


# --- pilot plumbing -----------------------------------------------------------
async def settle(pilot, delay=0.0):
    if delay:
        await pilot.pause(delay)
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def wait_until(pilot, cond, timeout=6.0):
    """Sending runs on a plain thread, not a worker — poll for its effects."""
    import time as _t
    t0 = _t.time()
    while not cond():
        assert _t.time() - t0 < timeout, 'timed out waiting for the background sender'
        await pilot.pause(0.05)
    await pilot.pause()


async def demo_login(pilot):
    assert isinstance(pilot.app.screen, LoginScreen), pilot.app.screen
    await pilot.click('#demo')
    await settle(pilot)
    assert isinstance(pilot.app.screen, MainScreen), pilot.app.screen
    await settle(pilot)


def table(app):
    return app.screen.query_one('#msgtable', DataTable)


def unread_total(app):
    main = next(s for s in app.screen_stack if isinstance(s, MainScreen))
    return sum(c for _, c in main.folder_counts)


# --- phase 1: onboarding, login/logout ----------------------------------------
async def phase1():
    assert not be.config_path().exists()
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        assert isinstance(app.screen, OnboardingScreen)
        await pilot.click('#setup')
        await pilot.pause()
        assert isinstance(app.screen, AccountFormScreen)
        app.screen.query_one('#address', Input).value = 'user@gmail.com'
        app.screen.query_one('#provider', Select).value = 'Gmail'
        await pilot.pause()
        assert app.screen.query_one('#imap', Input).value == 'imap.gmail.com'
        assert app.screen.query_one('#smtp', Input).value == 'smtp.gmail.com:465'
        await pilot.click('#save')
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)  # first account -> straight to login
        a = be.load_config()['accounts'][0]
        assert a['address'] == 'user@gmail.com' and a['imap_host'] == 'imap.gmail.com'
        assert a['name'] == 'user' and a['color'] in be.DEFAULT_COLORS
        await demo_login(pilot)
        await pilot.press('ctrl+l')
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert app.session is None
    print('phase 1 (onboarding/login/logout): ok')


# --- phase 2: mailbox ----------------------------------------------------------
async def phase2():
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        t = table(app)
        n = t.row_count
        assert n >= 8, n  # merged all-accounts view: home + work inboxes
        assert [name for name, _ in app.screen.folder_counts][0] == 'INBOX'

        await pilot.press('j', 'j')
        assert t.cursor_row == 2
        await pilot.press('k')
        assert t.cursor_row == 1
        await pilot.press('G')
        assert t.cursor_row == n - 1
        await pilot.press('g')
        assert t.cursor_row == 0

        await settle(pilot, 0.4)  # preview debounce
        preview = app.screen.preview_text
        assert 'Beautiful terminals' in preview, preview

        before = unread_total(app)
        await pilot.press('u')  # newest demo message starts unread -> read
        await settle(pilot)
        assert unread_total(app) == before - 1
        await pilot.press('u')
        await settle(pilot)
        assert unread_total(app) == before

        assert not app.screen.view[0].flagged
        await pilot.press('s')
        await settle(pilot)
        assert app.screen.view[0].flagged

        await pilot.press('d')  # single delete: immediate, with an undo window
        await pilot.pause()
        assert table(app).row_count == n - 1
        await pilot.press('z')  # undo restores it
        await pilot.pause()
        assert table(app).row_count == n
        await pilot.press('d')
        await settle(pilot)
        assert table(app).row_count == n - 1

        await pilot.press('enter')
        await settle(pilot)
        assert isinstance(app.screen, ReaderScreen)
        await pilot.press('q')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    print('phase 2 (mailbox): ok')


# --- phase 3: read, compose, reply ---------------------------------------------
async def phase3():
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)

        # open the HTML-only build notification; reader shows stripped text
        await pilot.press('slash')
        await pilot.pause()
        app.screen.query_one('#search', Input).value = 'build passed'
        await pilot.press('enter')
        await settle(pilot)
        assert table(app).row_count == 1
        assert app.screen.view[0].unread
        before = unread_total(app)
        await pilot.press('enter')
        await settle(pilot)
        reader = app.screen
        assert isinstance(reader, ReaderScreen)
        assert 'Build #42 passed' in reader.body_text.replace('\n', ' ')
        assert '<' not in reader.body_text
        # HTML mail renders through the Markdown widget
        assert reader.markdown_source and '## CI report' in reader.markdown_source
        assert reader.query_one('#reader-md').display
        assert unread_total(app) == before - 1  # opening marks read
        await pilot.press('escape')
        await pilot.pause()
        await pilot.press('escape')  # clear search
        await pilot.pause()

        # compose and send in demo mode
        await pilot.press('c')
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        app.screen.query_one('#to', Input).value = 'friend@example.com'
        app.screen.query_one('#subject', Input).value = 'Hello from tuimail'
        app.screen.query_one('#body', TextArea).text = 'Sent from the acceptance loop.'
        await pilot.press('ctrl+s')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)  # the composer closes at once
        await wait_until(pilot, lambda: app.session.account('personal').backend.outbox)
        await settle(pilot)
        sent = app.session.account('personal').backend.outbox[-1]
        assert sent['To'] == 'friend@example.com'
        assert sent['Subject'] == 'Hello from tuimail'
        assert 'acceptance loop' in sent.get_content()

        # reply to the encoded "Doe, John" sender prefills address AND account
        idx = next(i for i, s in enumerate(app.screen.view) if 'Q3 planning' in s.subject)
        table(app).move_cursor(row=idx)
        await pilot.press('r')
        await settle(pilot)
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one('#to', Input).value == 'john.doe@corp.example'
        assert app.screen.query_one('#subject', Input).value == 'Re: Q3 planning notes'
        assert app.screen.query_one('#from', Select).value == 'personal'
        assert '> ' in app.screen.query_one('#body', TextArea).text
        # esc on an untouched reply seed closes without a confirm
        await pilot.press('escape')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    print('phase 3 (read/compose/reply): ok')


# --- phase 4: clever features ---------------------------------------------------
async def phase4():
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        n = table(app).row_count

        # search narrows, esc restores
        await pilot.press('slash')
        await pilot.pause()
        app.screen.query_one('#search', Input).value = 'itinerary'
        await pilot.press('enter')
        await settle(pilot)
        assert table(app).row_count == 1
        assert 'itinerary' in app.screen.view[0].subject.lower()
        await pilot.press('escape')
        await pilot.pause()
        assert table(app).row_count == n

        # links modal on the newsletter
        idx = next(i for i, s in enumerate(app.screen.view) if 'terminals' in s.subject.lower())
        table(app).move_cursor(row=idx)
        await pilot.press('enter')
        await settle(pilot)
        await pilot.press('o')
        await pilot.pause()
        links_screen = app.screen
        assert isinstance(links_screen, LinksScreen)
        urls = [u for _, u in links_screen.links]
        assert 'https://textual.textualize.io' in urls
        assert 'https://github.com/Textualize/textual' in urls
        await pilot.press('escape')
        await pilot.pause()

        # attachment saving from the itinerary message
        await pilot.press('escape')  # back to mailbox
        await pilot.pause()
        idx = next(i for i, s in enumerate(app.screen.view) if 'itinerary' in s.subject.lower())
        table(app).move_cursor(row=idx)
        await pilot.press('enter')
        await settle(pilot)
        await pilot.press('a')
        await pilot.pause()
        from tuimail.app import AttachmentsScreen
        assert isinstance(app.screen, AttachmentsScreen)
        await pilot.press('enter')  # save the highlighted attachment
        await pilot.pause()
        saved = Path(TMP) / 'packing-list.txt'
        assert saved.exists() and b'passport' in saved.read_bytes()
        await pilot.press('escape')
        await pilot.pause()
        await pilot.press('q')
        await pilot.pause()

        # help overlay
        await pilot.press('question_mark')
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press('escape')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
    print('phase 4 (search/links/attachments/help): ok')


# --- phase 5: multi-account ------------------------------------------------------
async def phase5():
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        main = app.screen

        # all-in-one mode merges both demo accounts, dots identify them
        assert {s.account for s in main.view} == {'personal', 'work'}
        accounts_list = main.query_one('#accounts', ListView)
        assert len(accounts_list.children) == 3  # All + personal + work
        assert app.session.color('personal') != app.session.color('work')

        # switch scope to a single account
        main.set_scope('work')
        await settle(pilot)
        assert main.view and all(s.account == 'work' for s in main.view)
        assert any('Standup notes' in s.subject for s in main.view)

        # reader announces the owning account
        await pilot.press('enter')
        await settle(pilot)
        reader = app.screen
        assert isinstance(reader, ReaderScreen)
        assert 'work' in reader.account_line and 'work@tuimail.demo' in reader.account_line
        await pilot.press('q')
        await pilot.pause()

        # compose defaults its From to the current scope; send goes via that account
        await pilot.press('c')
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one('#from', Select).value == 'work'
        app.screen.query_one('#to', Input).value = 'x@y.z'
        app.screen.query_one('#subject', Input).value = 'from work'
        await pilot.press('ctrl+s')
        wb = app.session.account('work').backend
        await wait_until(pilot, lambda: wb.outbox)
        await settle(pilot)
        assert wb.outbox[-1]['From'] == 'work@tuimail.demo'
        assert not app.session.account('personal').backend.outbox

        # back to the merged view
        app.screen.set_scope(None)
        await settle(pilot)
        assert {s.account for s in app.screen.view} == {'personal', 'work'}
    print('phase 5 (multi-account/all-in-one): ok')


# --- phase 6: account management -------------------------------------------------
async def phase6():
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        assert isinstance(app.screen, LoginScreen)
        await pilot.click('#manage')
        await pilot.pause()
        assert isinstance(app.screen, AccountsScreen)

        # add a second account
        await pilot.click('#add')
        await pilot.pause()
        assert isinstance(app.screen, AccountFormScreen)
        app.screen.query_one('#address', Input).value = 'user@yandex.ru'
        app.screen.query_one('#provider', Select).value = 'Yandex'
        await pilot.pause()
        await pilot.click('#save')
        await pilot.pause()
        assert isinstance(app.screen, AccountsScreen)
        accts = be.load_config()['accounts']
        assert len(accts) == 2 and accts[1]['address'] == 'user@yandex.ru'
        assert accts[1]['name'] == 'user2'  # same local part -> auto-uniquified name
        assert accts[0]['color'] != accts[1]['color']  # auto-assigned distinct colors

        # edit the second account's name
        app.screen.query_one('#acctlist', ListView).index = 1
        await pilot.click('#edit')
        await pilot.pause()
        assert isinstance(app.screen, AccountFormScreen)
        app.screen.query_one('#name', Input).value = 'backup'
        await pilot.click('#save')
        await pilot.pause()
        assert be.load_config()['accounts'][1]['name'] == 'backup'

        # remove it again
        assert isinstance(app.screen, AccountsScreen)
        app.screen.query_one('#acctlist', ListView).index = 1
        await pilot.click('#remove')
        await pilot.pause()
        await pilot.press('y')
        await pilot.pause()
        assert len(be.load_config()['accounts']) == 1
        await pilot.click('#done')
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
    print('phase 6 (account management): ok')


# --- phase 7: auto sign-in -------------------------------------------------------
async def phase7():
    cfg = be.load_config()
    cfg['accounts'][0]['password'] = 'pw'
    be.save_config(cfg)
    orig = be.ImapBackend
    be.ImapBackend = lambda addr, pw, ih, sh: be.DemoBackend(addr)
    try:
        app = TuiMail()
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await settle(pilot)
            assert isinstance(app.screen, MainScreen), app.screen  # signed in by itself
            await pilot.press('ctrl+l')
            await settle(pilot)
            assert isinstance(app.screen, LoginScreen)
            await pilot.pause(0.3)  # logging out must stick — no auto re-login loop
            assert isinstance(app.screen, LoginScreen)
            # ...including across the Manage accounts round-trip
            await pilot.click('#manage')
            await pilot.pause()
            await pilot.click('#done')
            await settle(pilot)
            await pilot.pause(0.3)
            assert isinstance(app.screen, LoginScreen), app.screen
    finally:
        be.ImapBackend = orig
        cfg = be.load_config()
        cfg['accounts'][0].pop('password', None)
        be.save_config(cfg)
    print('phase 7 (auto sign-in): ok')


# --- phase 8: update notifications -----------------------------------------------
async def phase8():
    app = TuiMail()
    orig = up.check_latest
    up.check_latest = lambda timeout=10: {'version': 'v99.0.0', 'assets': {}}
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await demo_login(pilot)
            app._check_updates()
            await settle(pilot)
            assert app.update_info and app.update_info['version'] == 'v99.0.0'
            # not frozen -> Ctrl+U explains the pip path instead of self-updating
            await pilot.press('ctrl+u')
            await pilot.pause()
            from tuimail.app import ConfirmScreen, MailCommands
            assert not isinstance(app.screen, ConfirmScreen)
            # the pending update surfaces in the command palette
            provider = MailCommands(app.screen)
            labels = [str(h.match_display) async for h in provider.search('update')]
            assert any('v99.0.0' in lab for lab in labels), labels
    finally:
        up.check_latest = orig
    print('phase 8 (update check): ok')


# --- phase 9: selection mode -----------------------------------------------------
async def phase9():
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        main = app.screen
        t = table(app)
        n = t.row_count

        # space selects and advances; the selection bar replaces the footer
        await pilot.press('space', 'space')
        assert len(main.selected) == 2 and t.cursor_row == 2
        bar = main.query_one('#selbar')
        assert bar.display and 'd delete (2)' in main.query_one('#selbar').render().plain
        from textual.widgets import Footer
        assert not main.query_one(Footer).display

        # enter toggles instead of opening; reply is blocked
        await pilot.press('enter')
        await settle(pilot)
        assert isinstance(app.screen, MainScreen) and len(main.selected) == 3
        await pilot.press('r')
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)

        # bulk star: any unstarred -> all three starred
        await pilot.press('s')
        await settle(pilot)
        assert all(s.flagged for s in main.view[:3])

        # escape leaves selection mode and restores the footer
        await pilot.press('escape')
        assert not main.selected and main.query_one(Footer).display
        assert not main.query_one('#selbar').display

        # bulk delete with count in the confirm
        await pilot.press('space', 'space')
        await pilot.press('d')
        await pilot.pause()
        await pilot.press('y')
        await settle(pilot)
        assert table(app).row_count == n - 2
        assert not main.selected

        # a selection never survives a folder switch (uids are folder-scoped)
        await pilot.press('space')
        assert main.selected
        main.goto_folder('Sent')
        await settle(pilot)
        assert not main.selected and not main.query_one('#selbar').display
    print('phase 9 (selection mode): ok')


# --- phase 10: compose formatting + attachments ----------------------------------
async def phase10():
    from tuimail.app import ComposeScreen, FilePickScreen
    attach_src = Path(TMP) / 'report.txt'
    attach_src.write_text('quarterly numbers', encoding='utf-8')
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        await pilot.press('c')
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        compose = app.screen
        compose.query_one('#to', Input).value = 'x@y.z'
        compose.query_one('#subject', Input).value = 'formatted'

        # formatting keys only act inside the body: in To they leave it alone
        body = compose.query_one('#body', TextArea)
        compose.query_one('#to', Input).focus()
        await pilot.pause()
        await pilot.press('ctrl+b', 'ctrl+k')
        assert body.text == ''
        body.focus()
        await pilot.pause()
        await pilot.press('ctrl+b')
        assert body.text == '****'
        body.insert('bold')
        await pilot.press('ctrl+k')
        assert '[link text](https://)' in body.text

        # attach via the picker's path input (Tab completion + Enter)
        await pilot.press('ctrl+o')
        await pilot.pause()
        assert isinstance(app.screen, FilePickScreen)
        path_input = app.screen.query_one('#path', Input)
        path_input.value = str(Path(TMP) / 'repo')
        await pilot.press('tab')  # completes to report.txt
        assert path_input.value.endswith('report.txt')
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        assert compose.attachments and compose.attachments[0].name == 'report.txt'
        assert be.load_config().get('last_attach_dir') == str(attach_src.parent)

        await pilot.press('ctrl+s')
        await wait_until(pilot, lambda: app.session.account('personal').backend.outbox)
        await settle(pilot)
        sent = app.session.account('personal').backend.outbox[-1]
        atts = be.attachments_of(sent)
        assert atts and atts[0][0] == 'report.txt' and atts[0][1] == b'quarterly numbers'
        assert sent.get_body(preferencelist=('html',)) is not None  # markup -> html part
    print('phase 10 (compose formatting/attachments): ok')


# --- phase 11: UX-study P0 fixes --------------------------------------------------
async def phase11():
    from textual.containers import VerticalScroll
    from tuimail.app import ConfirmScreen, HelpScreen, MailCommands
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        main = app.screen
        t = table(app)

        # help card scrolls, names the version, closes on F1
        await pilot.press('question_mark')
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        assert isinstance(app.screen.query_one('#help-card'), VerticalScroll)
        await pilot.press('f1')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

        # j/k drive the focused sidebar list; Space/d never act on the hidden cursor
        folders = main.query_one('#folders', ListView)
        folders.focus()
        await pilot.pause()
        row_before = t.cursor_row
        await pilot.press('j')
        assert t.cursor_row == row_before and folders.index == 1, (t.cursor_row, folders.index)
        await pilot.press('space', 'd')
        await pilot.pause()
        assert not main.selected and isinstance(app.screen, MainScreen)
        t.focus()
        await pilot.pause()

        # the preview pane is not a Tab stop
        assert not main.query_one('#preview-scroll').can_focus

        # compose in the merged view seeds From from the highlighted message
        idx = next(i for i, s in enumerate(main.view) if s.account == 'work')
        t.move_cursor(row=idx)
        await pilot.pause()
        await pilot.press('c')
        await pilot.pause()
        assert app.screen.query_one('#from', Select).value == 'work'
        # ...and changing only From makes the draft dirty
        app.screen.query_one('#from', Select).value = 'personal'
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press('y')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)

        # palette lists the app's commands on an empty query
        labels = [str(h.display) async for h in MailCommands(app.screen).discover()]
        assert any('Compose' in lab for lab in labels), labels

        # cursor survives a reload by identity, not by row number
        key = (main.view[2].account, main.view[2].uid)
        t.move_cursor(row=2)
        await pilot.pause()
        main.all_msgs.insert(0, main.all_msgs.pop())  # simulate new mail on top
        main.apply_filter(keep_cursor=True)
        assert (main.current().account, main.current().uid) == key

        # q asks before quitting; a stray q never kills the session
        await pilot.press('q')
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press('n')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen) and app.is_running

        # preview toggles
        await pilot.press('p')
        assert not main.query_one('#preview-scroll').display
        await pilot.press('p')
        assert main.query_one('#preview-scroll').display

    # narrow pane: sidebar collapses and the table never scrolls sideways
    app = TuiMail()
    async with app.run_test(size=(80, 24)) as pilot:
        await demo_login(pilot)
        main = app.screen
        t = table(app)
        assert not main.query_one('#sidebar').display
        assert t.virtual_size.width <= t.size.width, (t.virtual_size, t.size)
        assert t.row_count >= 6  # the list keeps most of the screen
    print('phase 11 (P0: help/focus/layout/quit/compose): ok')


# --- phase 12: UX-study P1 features ---------------------------------------------
async def phase12():
    from textual.widgets import OptionList
    from tuimail.app import FolderPickScreen
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        main = app.screen
        t = table(app)
        n = t.row_count
        personal = app.session.account('personal').backend

        # per-account unread counts
        assert main.per_account['personal']['INBOX'] == 3
        assert main.per_account['work']['INBOX'] == 1

        # reader: n/p walk the list, u toggles unread from inside
        await pilot.press('enter')
        await settle(pilot)
        first = app.screen.summary
        await pilot.press('n')
        await settle(pilot)
        assert isinstance(app.screen, ReaderScreen) and app.screen.summary is main.view[1]
        await pilot.press('p')
        await settle(pilot)
        assert app.screen.summary is first
        before = unread_total(app)
        await pilot.press('u')
        await settle(pilot)
        assert unread_total(app) == before + 1  # opened (read) message -> unread again
        await pilot.press('q')
        await pilot.pause()

        # delete has an undo window and lands in Trash when committed
        t.move_cursor(row=0)
        await pilot.pause()
        victim = main.current()
        await pilot.press('d')
        await pilot.pause()
        assert t.row_count == n - 1 and main._pending
        await pilot.press('z')
        await pilot.pause()
        assert t.row_count == n and not main._pending and main.current() is victim
        await pilot.press('d')
        await pilot.pause()
        main.goto_folder('Sent')  # leaving the view commits the pending delete
        await settle(pilot)
        assert [it['msg']['Subject'] for it in personal._data['Trash']] == [victim.subject]
        main.goto_folder('INBOX')
        await settle(pilot)

        # move to a folder via the picker
        idx = next(i for i, s in enumerate(main.view) if s.account == 'personal')
        t.move_cursor(row=idx)
        await pilot.pause()
        moved = main.current()
        await pilot.press('m')
        await pilot.pause()
        assert isinstance(app.screen, FolderPickScreen)
        pick = app.screen.query_one(OptionList)
        pick.highlighted = app.screen.folders.index('Archive')
        await pilot.press('enter')
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)
        assert any(it['msg']['Subject'] == moved.subject for it in personal._data['Archive'])
        assert all(s is not moved for s in main.view)

        # archive: works for an account with an Archive folder, refuses otherwise
        idx = next(i for i, s in enumerate(main.view) if s.account == 'personal')
        t.move_cursor(row=idx)
        await pilot.pause()
        archived = main.current()
        await pilot.press('A')
        await settle(pilot)
        assert any(it['msg']['Subject'] == archived.subject for it in personal._data['Archive'])
        idx = next(i for i, s in enumerate(main.view) if s.account == 'work')
        t.move_cursor(row=idx)
        await pilot.pause()
        rows = t.row_count
        await pilot.press('A')  # work has no Archive folder -> nothing happens
        await settle(pilot)
        assert t.row_count == rows

        # the folder survives a scope switch when the target scope has it
        main.goto_folder('Sent')
        await settle(pilot)
        main.set_scope('work')
        await settle(pilot)
        assert main.folder == 'Sent'
        main.set_scope(None)
        await settle(pilot)

        # load older: the demo has none, and that is reported, not crashed
        await pilot.press('L')
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)

        # a page of older mail must not resurrect a pending delete, and undo
        # after it must not crash the table with a duplicate key
        t.move_cursor(row=0)
        await pilot.pause()
        pending_victim = main.current()
        await pilot.press('d')
        await pilot.pause()
        orig_older = app.session.list_older
        app.session.list_older = lambda folder, scope, mins: [be.Summary(
            uid=pending_victim.uid, sender='x', subject='resurrected',
            account=pending_victim.account)]
        try:
            main._older_loaded(main.folder, main.scope, app.session.list_older('', None, {}))
        finally:
            app.session.list_older = orig_older
        assert all(s.uid != pending_victim.uid or s.account != pending_victim.account
                   for s in main.all_msgs)
        await pilot.press('z')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen) and app.is_running
        assert sum(1 for s in main.all_msgs
                   if (s.account, s.uid) == (pending_victim.account, pending_victim.uid)) == 1

        # bracketed sender names and Gmail-style folder names are never markup
        from tuimail.app import ComposeScreen
        app.push_screen(ComposeScreen({'reply_to': '[/list] Bob', 'account': 'personal'}))
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        await pilot.press('escape')
        await pilot.pause()
        app.push_screen(FolderPickScreen(['[Gmail]/Trash']))
        await pilot.pause()
        opt = app.screen.query_one(OptionList).get_option_at_index(0)
        assert '[Gmail]/Trash' in str(getattr(opt.prompt, 'plain', opt.prompt))
        await pilot.press('escape')
        await pilot.pause()

        # logging out inside the undo window still commits the delete
        t.focus()
        await pilot.pause()
        t.move_cursor(row=0)
        await pilot.pause()
        gone = main.current()
        trash_before = len(personal._data['Trash'])
        await pilot.press('d')
        await pilot.pause()
        await pilot.press('ctrl+l')
        await settle(pilot)
        assert len(personal._data['Trash']) == trash_before + (gone.account == 'personal')
    print('phase 12 (P1: move/archive/undo/reader/counts/older): ok')


# --- phase 13: review fixes for P0/P1 ------------------------------------------
async def phase13():
    from textual.widgets import ListView as _LV
    # editing an account keeps a customised SMTP host when the provider is inferred
    cfg = be.load_config()
    cfg['accounts'][0]['smtp_host'] = 'smtp.gmail.com:587'
    be.save_config(cfg)
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        from tuimail.app import AccountFormScreen
        app.switch_screen(AccountFormScreen(0))
        await pilot.pause()
        await pilot.pause()
        assert app.screen.query_one('#smtp', Input).value == 'smtp.gmail.com:587'
        assert app.screen.query_one('#provider', Select).value == 'Gmail'
    cfg = be.load_config()
    cfg['accounts'][0]['smtp_host'] = 'smtp.gmail.com:465'
    be.save_config(cfg)

    # shrinking below 90 columns never strands focus on the hidden sidebar
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        main = app.screen
        main.query_one('#folders', _LV).focus()
        await pilot.pause()
        await pilot.resize_terminal(80, 24)
        await pilot.pause()
        assert app.focused is table(app), app.focused
    print('phase 13 (review fixes: account edit, layout focus): ok')


# --- phase 14: outbox ---------------------------------------------------------
async def phase14():
    import smtplib
    import stat
    import time as _t
    from tuimail import outbox as ob
    spool = Path(os.environ['TUIMAIL_OUTBOX'])
    for p in spool.glob('*.json'):
        p.unlink()

    # a record queued in an earlier run (owner-only files) goes out right after sign-in
    store = ob.Outbox()
    rec0 = store.enqueue('personal', 'you@tuimail.demo', 'later@example.com',
                         'from last run', 'body')
    assert (spool / f'{rec0["id"]}.json').exists()
    if os.name == 'posix':
        assert stat.S_IMODE(spool.stat().st_mode) == 0o700
        assert stat.S_IMODE((spool / f'{rec0["id"]}.json').stat().st_mode) == 0o600
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        main = app.screen
        personal = app.session.account('personal').backend
        await wait_until(pilot, lambda: personal.outbox)
        assert personal.outbox[-1]['Subject'] == 'from last run'
        await wait_until(pilot, lambda: not app.outbox.items())
        await settle(pilot)
        assert dict(main.folder_counts).get(be.OUTBOX) == 0
        assert [n for n, _ in main.folder_counts][:2] == ['INBOX', be.OUTBOX]

        # Ctrl+S closes the composer at once, even while the SMTP call is slow
        real_send = personal.send

        def slow_send(msg):
            _t.sleep(0.8)
            real_send(msg)
        personal.send = slow_send
        await pilot.press('c')
        await pilot.pause()
        app.screen.query_one('#to', Input).value = 'slow@example.com'
        app.screen.query_one('#subject', Input).value = 'slow one'
        await pilot.press('ctrl+s')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert any(r['subject'] == 'slow one' for r in app.outbox.items())
        assert dict(main.folder_counts).get(be.OUTBOX) == 1
        await wait_until(pilot, lambda: personal.outbox[-1]['Subject'] == 'slow one')
        await wait_until(pilot, lambda: dict(main.folder_counts).get(be.OUTBOX) == 0)

        # a permanent failure stays in the Outbox with its error, no auto-retry
        def refuse(msg):
            raise smtplib.SMTPRecipientsRefused({'nobody@example.com': (550, b'no such user')})
        personal.send = refuse
        await pilot.press('c')
        await pilot.pause()
        app.screen.query_one('#to', Input).value = 'nobody@example.com'
        app.screen.query_one('#subject', Input).value = 'bounces'
        app.screen.query_one('#body', TextArea).text = 'will fail'
        await pilot.press('ctrl+s')
        await wait_until(pilot, lambda: any(r['tries'] for r in app.outbox.items()))
        rec = app.outbox.items()[0]
        assert rec['permanent'] and 'no such user' in rec['error'], rec
        await wait_until(pilot, lambda: dict(main.folder_counts).get(be.OUTBOX) == 1)
        app.drain_outbox()
        await pilot.pause(0.3)
        assert app.outbox.get(rec['id'])['tries'] == 1

        # the Outbox folder shows it bold and marked, with the error in the preview
        main.goto_folder(be.OUTBOX)
        await settle(pilot)
        assert main.folder == be.OUTBOX and len(main.view) == 1, main.view
        s = main.view[0]
        assert s.subject.startswith('⚠ ') and s.unread and s.recipient == 'nobody@example.com'
        await pilot.pause(0.5)
        await settle(pilot)
        assert 'Not sent' in main.preview_text and 'no such user' in main.preview_text
        assert any(be.decode_folder(n) == 'Outbox' for n, _ in main.folder_counts)
        table(app).focus()
        await pilot.pause()
        for key in ('u', 's', 'A', 'm', 'L'):  # mailbox verbs are refused, not applied
            await pilot.press(key)
            await pilot.pause()
            assert isinstance(app.screen, MainScreen) and len(main.view) == 1, key

        # Enter edits it; the sender leaves a held record alone
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        assert rec['id'] in app.outbox.held
        assert app.screen.query_one('#to', Input).value == 'nobody@example.com'
        assert app.screen.query_one('#body', TextArea).text == 'will fail'
        assert app.outbox.due({'personal'}, force=True) == []
        await pilot.press('escape')  # unchanged: closes without asking
        await pilot.pause()
        assert isinstance(app.screen, MainScreen) and rec['id'] not in app.outbox.held

        # fix the address and resend: old record replaced, Message-ID kept, delivered
        personal.send = real_send
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        app.screen.query_one('#to', Input).value = 'somebody@example.com'
        await pilot.press('ctrl+s')
        await wait_until(pilot, lambda: personal.outbox[-1]['Subject'] == 'bounces')
        assert app.outbox.get(rec['id']) is None
        await wait_until(pilot, lambda: not app.outbox.items())
        assert personal.outbox[-1]['To'] == 'somebody@example.com'
        assert str(personal.outbox[-1]['Message-ID']) == rec['message_id']
        await wait_until(pilot, lambda: main.view == [])
        assert dict(main.folder_counts).get(be.OUTBOX) == 0

        # transient failures back off; R in the Outbox retries right now
        calls = []

        def flaky(msg):
            calls.append(1)
            if len(calls) < 2:
                raise ConnectionError('reset by peer')
            real_send(msg)
        personal.send = flaky
        await pilot.press('c')
        await pilot.pause()
        app.screen.query_one('#to', Input).value = 'retry@example.com'
        app.screen.query_one('#subject', Input).value = 'flaky'
        await pilot.press('ctrl+s')
        await wait_until(pilot, lambda: any(r['tries'] for r in app.outbox.items()))
        rec = app.outbox.items()[0]
        assert not rec['permanent'] and rec['tries'] == 1 and 'reset by peer' in rec['error']
        app.drain_outbox()  # inside the backoff window: not tried again
        await pilot.pause(0.3)
        assert len(calls) == 1
        assert app.outbox.status(app.outbox.get(rec['id'])) == 'retrying'
        table(app).focus()
        await pilot.pause()
        await pilot.press('R')
        await wait_until(pilot, lambda: not app.outbox.items())
        assert len(calls) == 2 and personal.outbox[-1]['Subject'] == 'flaky'

        # d deletes with undo; the commit removes the file
        personal.send = refuse
        await pilot.press('c')
        await pilot.pause()
        app.screen.query_one('#to', Input).value = 'gone@example.com'
        app.screen.query_one('#subject', Input).value = 'to delete'
        await pilot.press('ctrl+s')
        await wait_until(pilot, lambda: any(r['tries'] for r in app.outbox.items()))
        await wait_until(pilot, lambda: len(main.view) == 1)
        table(app).focus()
        await pilot.pause()
        await pilot.press('d')
        await pilot.pause()
        assert main.view == [] and app.outbox.items()  # optimistic: the file waits for undo
        await pilot.press('z')
        await pilot.pause()
        assert len(main.view) == 1
        await pilot.press('d')
        await pilot.pause()
        main._flush_pending(sync=True)
        await settle(pilot)
        assert not app.outbox.items()
        personal.send = real_send

        # mail queued for an account that is not signed in waits, visible in the merged view
        waiting = app.outbox.enqueue('nobody', 'x@y.z', 'a@b.c', 'orphan', 'body')
        app.drain_outbox(force=True)
        await pilot.pause(0.3)
        assert app.outbox.get(waiting['id'])['tries'] == 0
        main._outbox_changed()
        await pilot.pause()
        assert main.scope is None and len(main.view) == 1
        assert main.view[0].subject.startswith('⏸ ')
        main.set_scope('personal')
        await settle(pilot)
        assert main.folder == be.OUTBOX and main.view == []
        main.set_scope(None)
        await settle(pilot)
        assert app.outbox.remove(waiting['id'])
    print('phase 14 (outbox: queue, retry, edit, delete, persistence): ok')


PHASES_EXTRA = {'13': phase13, '14': phase14}


PHASES = {'1': phase1, '2': phase2, '3': phase3, '4': phase4, '5': phase5,
          '6': phase6, '7': phase7, '8': phase8, '9': phase9, '10': phase10,
          '11': phase11, '12': phase12, **PHASES_EXTRA}


async def main():
    wanted = [a for a in sys.argv[1:] if a in PHASES] or list(PHASES)
    helpers()
    for p in wanted:
        await PHASES[p]()
    print('all acceptance loops green')


if __name__ == '__main__':
    asyncio.run(main())
