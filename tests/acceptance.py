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

from textual.widgets import DataTable, Input, ListView, Select, TextArea  # noqa: E402

from tuimail import backend as be  # noqa: E402
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
        policy=email.policy.default)) == ['https://example.com/x', 'http://a.b']
    print('phase 0 (helpers): ok')


# --- pilot plumbing -----------------------------------------------------------
async def settle(pilot, delay=0.0):
    if delay:
        await pilot.pause(delay)
    await pilot.app.workers.wait_for_complete()
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

        await pilot.press('d')
        await pilot.pause()
        await pilot.press('n')  # decline -> nothing deleted
        await pilot.pause()
        assert table(app).row_count == n
        await pilot.press('d')
        await pilot.pause()
        await pilot.press('y')
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
        await settle(pilot)
        assert isinstance(app.screen, MainScreen)
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
        assert 'https://textual.textualize.io' in links_screen.links
        assert 'https://github.com/Textualize/textual' in links_screen.links
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
        saved = Path(TMP) / 'packing-list.txt'
        assert saved.exists() and b'passport' in saved.read_bytes()
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
        await settle(pilot)
        wb = app.session.account('work').backend
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


PHASES = {'1': phase1, '2': phase2, '3': phase3, '4': phase4, '5': phase5, '6': phase6}


async def main():
    wanted = [a for a in sys.argv[1:] if a in PHASES] or list(PHASES)
    helpers()
    for p in wanted:
        await PHASES[p]()
    print('all acceptance loops green')


if __name__ == '__main__':
    asyncio.run(main())
