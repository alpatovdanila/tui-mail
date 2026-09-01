"""Acceptance loops for tuimail — run: python tests/acceptance.py

Drives the real app headless through Textual's Pilot against the demo backend.
Also asserts the pure parsing helpers, including regressions for the four
review-confirmed bugs of the old prototype.
"""
import asyncio
import email
import email.policy
import os
import sys
import tempfile
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP = tempfile.mkdtemp(prefix='tuimail-test-')
os.environ['TUIMAIL_CONFIG'] = str(Path(TMP) / 'config.json')
os.environ['TUIMAIL_DOWNLOADS'] = TMP

from textual.widgets import DataTable, Input, Select, TextArea  # noqa: E402

from tuimail import backend as be  # noqa: E402
from tuimail.app import (AccountScreen, ComposeScreen, HelpScreen,  # noqa: E402
                         LinksScreen, LoginScreen, MainScreen, OnboardingScreen,
                         ReaderScreen, TuiMail)


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
    be.config_path().unlink()

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
        assert isinstance(app.screen, AccountScreen)
        app.screen.query_one('#address', Input).value = 'user@gmail.com'
        app.screen.query_one('#provider', Select).value = 'Gmail'
        await pilot.pause()
        assert app.screen.query_one('#imap', Input).value == 'imap.gmail.com'
        assert app.screen.query_one('#smtp', Input).value == 'smtp.gmail.com:465'
        await pilot.click('#save')
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        cfg = be.load_config()
        assert cfg['address'] == 'user@gmail.com' and cfg['imap_host'] == 'imap.gmail.com'
        await demo_login(pilot)
        await pilot.press('ctrl+l')
        await pilot.pause()
        assert isinstance(app.screen, LoginScreen)
        assert app.backend is None
    print('phase 1 (onboarding/login/logout): ok')


# --- phase 2: mailbox ----------------------------------------------------------
async def phase2():
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await demo_login(pilot)
        t = table(app)
        n = t.row_count
        assert n >= 6, n
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
        was_unread = app.screen.view[0].unread
        assert was_unread
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
        sent = app.backend.outbox[-1]
        assert sent['To'] == 'friend@example.com'
        assert sent['Subject'] == 'Hello from tuimail'
        assert 'acceptance loop' in sent.get_content()

        # reply to the encoded "Doe, John" sender prefills the right address
        idx = next(i for i, s in enumerate(app.screen.view) if 'Q3 planning' in s.subject)
        table(app).move_cursor(row=idx)
        await pilot.press('r')
        await settle(pilot)
        assert isinstance(app.screen, ComposeScreen)
        assert app.screen.query_one('#to', Input).value == 'john.doe@corp.example'
        assert app.screen.query_one('#subject', Input).value == 'Re: Q3 planning notes'
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
        for ch in 'itinerary':
            await pilot.press(ch)
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


PHASES = {'1': phase1, '2': phase2, '3': phase3, '4': phase4}


async def main():
    wanted = [a for a in sys.argv[1:] if a in PHASES] or list(PHASES)
    helpers()
    for p in wanted:
        await PHASES[p]()
    print('all acceptance loops green')


if __name__ == '__main__':
    asyncio.run(main())
