"""End-to-end: the real app against a real mail server — run: python tests/e2e.py

Starts genuine SMTP/IMAP servers on localhost (tests/mailserver.py) with real
TLS handshakes, signs the app in through its own LoginScreen with two saved
accounts, and drives it with keys. Everything between the keyboard and the
mailbox is the production code path: imaplib/smtplib over verified TLS,
LoginScreen -> ImapBackend, Ctrl+S -> Outbox spool -> sender thread -> SMTP.
No traffic leaves the machine.
"""
import asyncio
import email
import email.policy
import json
import os
import sys
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[0]))
sys.path.insert(0, str(HERE))
TMP = tempfile.mkdtemp(prefix='tuimail-e2e-')
os.environ['TUIMAIL_CONFIG'] = str(Path(TMP) / 'config.json')
os.environ['TUIMAIL_OUTBOX'] = str(Path(TMP) / 'outbox')
os.environ['TUIMAIL_DOWNLOADS'] = TMP
os.environ['TUIMAIL_NO_UPDATE_CHECK'] = '1'
# trust the test server's self-signed localhost cert — tls_context() still
# verifies certificate and hostname, exactly as against a real provider
os.environ['SSL_CERT_FILE'] = str(HERE / 'testcert.pem')

from gencert import ensure_cert  # noqa: E402
ensure_cert()  # mint the throwaway localhost cert if it is not there yet

from textual.widgets import DataTable, Input, TextArea  # noqa: E402

from mailserver import ImapServer, MailStore, SmtpServer  # noqa: E402
from tuimail import backend as be  # noqa: E402
from tuimail.app import ComposeScreen, LoginScreen, MainScreen, TuiMail  # noqa: E402

ANNA, BORIS = 'anna@test.dev', 'boris@test.dev'


def seed_message(frm, to, subject, body, *, hours_ago=0, html=False, attach=None):
    m = EmailMessage()
    m['From'], m['To'], m['Subject'] = frm, to, subject
    dt = time.time() - hours_ago * 3600
    m['Date'] = email.utils.formatdate(dt)
    m['Message-ID'] = email.utils.make_msgid(domain='test.dev')
    m.set_content(body, subtype='html' if html else 'plain')
    if attach:
        m.add_attachment(attach[1], maintype='text', subtype='plain', filename=attach[0])
    return m.as_bytes()


def build_world():
    store = MailStore()
    store.add_user(ANNA, 'secret123')
    store.add_user(BORIS, 'pass456')
    store.deposit(ANNA, 'INBOX', seed_message(
        '=?utf-8?b?0J/RkdGC0YAg0JjQstCw0L3QvtCy?= <petya@corp.ru>', ANNA,
        '=?utf-8?b?0J7RgtGH0ZHRgiDQt9CwINC60LLQsNGA0YLQsNC7?=',
        'Полный отчёт во вложении не влез, поэтому просто цифры.', hours_ago=3))
    store.deposit(ANNA, 'INBOX', seed_message(
        'release-bot@test.dev', ANNA, 'Release notes',
        '<h2>Release notes</h2><p>Now with <a href="https://test.dev/ch">a changelog</a>.</p>',
        hours_ago=2, html=True))
    store.deposit(ANNA, 'INBOX', seed_message(
        'files@test.dev', ANNA, 'The numbers file', 'attached as promised',
        hours_ago=1, attach=('numbers.csv', b'q1,42\r\nq2,17\r\n')))
    store.deposit(ANNA, 'Sent', seed_message(ANNA, 'petya@corp.ru', 'Re: планы', 'ok'))
    smtp = SmtpServer(store, 'ssl')
    imap = ImapServer(store)
    be.SMTP_TLS_PORTS.add(smtp.port)
    return store, smtp, imap


def write_config(store, smtp, imap, anna_pw='secret123'):
    cfg = {'accounts': [
        {'name': 'anna', 'address': ANNA, 'password': anna_pw,
         'imap_host': f'localhost:{imap.port}', 'smtp_host': f'localhost:{smtp.port}',
         'color': be.DEFAULT_COLORS[0]},
        {'name': 'boris', 'address': BORIS, 'password': 'pass456',
         'imap_host': f'localhost:{imap.port}', 'smtp_host': f'localhost:{smtp.port}',
         'color': be.DEFAULT_COLORS[1]},
    ]}
    Path(os.environ['TUIMAIL_CONFIG']).write_text(json.dumps(cfg), encoding='utf-8')


async def settle(pilot):
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def wait_until(pilot, cond, timeout=15.0, what='condition'):
    t0 = time.time()
    while not cond():
        assert time.time() - t0 < timeout, f'timed out waiting for {what}'
        await pilot.pause(0.05)
    await pilot.pause()


def table(app):
    return app.screen.query_one('#msgtable', DataTable)


async def main_flow():
    store, smtp, imap = build_world()
    write_config(store, smtp, imap)
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        # saved passwords -> auto sign-in straight into the mailbox
        await wait_until(pilot, lambda: isinstance(app.screen, MainScreen),
                         what='auto sign-in')
        await settle(pilot)
        main = app.screen
        await wait_until(pilot, lambda: len(main.view) == 3, what='INBOX load')
        rows = [(s.sender, s.subject, s.unread) for s in main.view]
        assert rows[0][1] == 'The numbers file' and rows[0][2], rows  # newest first
        assert any('Пётр Иванов' in s and 'Отчёт за квартал' in t for s, t, _ in rows), rows
        assert dict(main.folder_counts)['INBOX'] == 3

        # open the HTML mail: rendered body, and \Seen lands on the SERVER
        idx = next(i for i, s in enumerate(main.view) if s.subject == 'Release notes')
        uid = int(main.view[idx].uid)
        table(app).move_cursor(row=idx)
        await pilot.press('enter')
        await settle(pilot)
        reader = app.screen
        assert 'changelog' in reader.body_text
        assert reader.markdown_source and 'Release notes' in reader.markdown_source
        await wait_until(pilot, lambda: '\\Seen' in store.flags_of(ANNA, 'INBOX', uid),
                         what='server \\Seen')
        await pilot.press('escape')
        await settle(pilot)

        # star -> \Flagged on the server
        table(app).focus()
        await pilot.pause()
        star_uid = int(main.current().uid)
        await pilot.press('s')
        await wait_until(pilot, lambda: '\\Flagged' in store.flags_of(ANNA, 'INBOX', star_uid),
                         what='server \\Flagged')

        # server-side TEXT search
        await pilot.press('slash')
        await pilot.pause()
        app.screen.query_one('#search', Input).value = 'promised'
        await pilot.press('enter')
        await settle(pilot)
        assert len(main.view) == 1 and main.view[0].subject == 'The numbers file'
        await pilot.press('escape')
        await pilot.pause()
        await wait_until(pilot, lambda: len(main.view) == 3, what='filter cleared')

        # attachments arrive byte-for-byte
        idx = next(i for i, s in enumerate(main.view) if s.subject == 'The numbers file')
        msg = app.session.fetch('anna', 'INBOX', main.view[idx].uid)
        assert be.attachments_of(msg) == [('numbers.csv', b'q1,42\r\nq2,17\r\n')]

        # compose -> Ctrl+S -> Outbox -> real SMTP -> boris's INBOX on the server
        await pilot.press('c')
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        app.screen.query_one('#to', Input).value = BORIS
        app.screen.query_one('#subject', Input).value = 'Привет от Анны'
        app.screen.query_one('#body', TextArea).text = 'Тело письма **жирным**.'
        app.screen._used_markup = True
        att = Path(TMP) / 'report.txt'
        att.write_text('quarterly numbers', encoding='utf-8')
        app.screen.attachments.append(att)
        await pilot.press('ctrl+s')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)  # closed at once
        await wait_until(pilot, lambda: store.delivered, what='SMTP delivery')
        await wait_until(pilot, lambda: not app.outbox.items(), what='spool empty')
        frm, rcpts, data = store.delivered[-1]
        assert (frm, rcpts) == (ANNA, [BORIS])
        sent = email.message_from_bytes(data, policy=email.policy.default)
        assert be.dec(sent['Subject']) == 'Привет от Анны'
        assert str(sent['Message-ID']).endswith('@test.dev>')
        assert sent.get_body(preferencelist=('html',)) is not None
        assert be.attachments_of(sent) == [('report.txt', b'quarterly numbers')]
        assert set(store.ehlo_names) == {'localhost'}  # no hostname leak
        # and boris sees it in the app, via his own real IMAP account
        main.set_scope('boris')
        await settle(pilot)
        await wait_until(pilot, lambda: any('Привет от Анны' in s.subject for s in main.view),
                         what="boris's copy")
        main.set_scope(None)
        await settle(pilot)

        # a 550-refused recipient waits in the Outbox with the server's words,
        # then Enter-edit + resend delivers it
        store.refuse_rcpt['nobody@test.dev'] = (550, '5.1.1 no such user here')
        await pilot.press('c')
        await pilot.pause()
        app.screen.query_one('#to', Input).value = 'nobody@test.dev'
        app.screen.query_one('#subject', Input).value = 'bounces once'
        await pilot.press('ctrl+s')
        await wait_until(pilot, lambda: any(r['tries'] for r in app.outbox.items()),
                         what='failed record')
        rec = app.outbox.items()[0]
        assert rec['permanent'] and '5.1.1 no such user here' in rec['error'], rec
        main.goto_folder(be.OUTBOX)
        await settle(pilot)
        assert len(main.view) == 1 and main.view[0].subject.startswith('⚠ ')
        table(app).focus()
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert isinstance(app.screen, ComposeScreen)
        app.screen.query_one('#to', Input).value = BORIS
        await pilot.press('ctrl+s')
        await wait_until(pilot, lambda: not app.outbox.items(), what='resend delivered')
        assert store.delivered[-1][1] == [BORIS]
        resent = email.message_from_bytes(store.delivered[-1][2], policy=email.policy.default)
        assert str(resent['Message-ID']) == rec['message_id']  # identity survives the edit

        # greylisting (450) retries by itself and succeeds
        store.greylist_once.add(BORIS)
        await pilot.press('c')
        await pilot.pause()
        app.screen.query_one('#to', Input).value = BORIS
        app.screen.query_one('#subject', Input).value = 'greylisted'
        await pilot.press('ctrl+s')
        await wait_until(pilot, lambda: any(r['tries'] for r in app.outbox.items()),
                         what='greylist failure')
        rec = app.outbox.items()[0]
        assert not rec['permanent'] and '4.7.1' in rec['error'], rec
        app.drain_outbox(force=True)
        await wait_until(pilot, lambda: not app.outbox.items(), what='greylist retry')
        assert be.dec(email.message_from_bytes(store.delivered[-1][2])['Subject']) == 'greylisted'

        # partial refusal: one accepted, one refused after the fact — kept as a
        # permanent failure naming the refused address, delivered exactly once
        before = len([d for d in store.delivered if BORIS in d[1]])
        await pilot.press('c')
        await pilot.pause()
        app.screen.query_one('#to', Input).value = f'{BORIS}, nobody@test.dev'
        app.screen.query_one('#subject', Input).value = 'partial'
        await pilot.press('ctrl+s')
        await wait_until(pilot, lambda: any(r['tries'] for r in app.outbox.items()),
                         what='partial failure')
        rec = app.outbox.items()[0]
        assert rec['permanent'] and 'nobody@test.dev' in rec['error'] \
            and 'did get it' in rec['error'], rec
        assert len([d for d in store.delivered if BORIS in d[1]]) == before + 1
        assert app.outbox.remove(rec['id'])

        # delete -> the server's Trash (MOVE), archive -> Archive. Scope to anna
        # so the acted-on rows are unambiguously hers.
        main.goto_folder('INBOX')
        await settle(pilot)
        main.set_scope('anna')
        await settle(pilot)
        await wait_until(pilot, lambda: main.folder == 'INBOX' and len(main.view) == 3,
                         what='anna INBOX')
        assert len(store.msgs(ANNA, 'INBOX')) == 3
        table(app).focus()
        await pilot.pause()
        table(app).move_cursor(row=0)
        await pilot.pause()
        await pilot.press('d')
        await pilot.pause()
        main._flush_pending(sync=True)
        await wait_until(pilot, lambda: len(store.msgs(ANNA, 'Trash')) == 1,
                         what='server Trash')
        await wait_until(pilot, lambda: len(main.view) == 2, what='row gone')
        table(app).move_cursor(row=0)
        await pilot.pause()
        await pilot.press('A')
        await wait_until(pilot, lambda: len(store.msgs(ANNA, 'Archive')) == 1,
                         what='server Archive')
        await wait_until(pilot, lambda: len(store.msgs(ANNA, 'INBOX')) == 1,
                         what='INBOX down to one')

        # the server drops every IMAP connection; the next refresh reconnects
        store.drop_imap()
        await pilot.press('R')
        await settle(pilot)
        await wait_until(pilot, lambda: main.folder == 'INBOX' and len(main.view) == 1,
                         what='reload after drop')
    smtp.close()
    imap.close()
    print('e2e main flow: ok')
    return store


async def transport_failures():
    """The field bug: 465 'accepts' but dies -> the fallback must run; when
    the fallback dies too, the error names BOTH ports."""
    store, smtp, imap = build_world()
    write_config(store, smtp, imap)
    greet_close = SmtpServer(store, 'greet_close')  # TLS ok, 220, then close
    dead = SmtpServer(store, 'dead')
    starttls = SmtpServer(store, 'starttls')
    be.SMTP_TLS_PORTS.update({greet_close.port, dead.port})
    orig_fb = be.SMTP_FALLBACK_PORT
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_until(pilot, lambda: isinstance(app.screen, MainScreen),
                         what='auto sign-in')
        await settle(pilot)
        main = app.screen
        anna = app.session.account('anna').backend
        try:
            # greet-then-close on the TLS port (SMTPServerDisconnected) now
            # falls back to STARTTLS and the mail goes out
            anna._smtp_host = f'localhost:{greet_close.port}'
            be.SMTP_FALLBACK_PORT = starttls.port
            await pilot.press('c')
            await pilot.pause()
            app.screen.query_one('#to', Input).value = BORIS
            app.screen.query_one('#subject', Input).value = 'via fallback'
            await pilot.press('ctrl+s')
            await wait_until(pilot, lambda: store.delivered, what='fallback delivery')
            assert be.dec(email.message_from_bytes(
                store.delivered[-1][2])['Subject']) == 'via fallback'
            await wait_until(pilot, lambda: not app.outbox.items(), what='spool empty')

            # both ports dead: the record keeps ONE error naming both attempts
            anna._smtp_host = f'localhost:{dead.port}'
            be.SMTP_FALLBACK_PORT = greet_close.port
            await pilot.press('c')
            await pilot.pause()
            app.screen.query_one('#to', Input).value = BORIS
            app.screen.query_one('#subject', Input).value = 'no way out'
            await pilot.press('ctrl+s')
            await wait_until(pilot, lambda: any(r['tries'] for r in app.outbox.items()),
                             what='both-ports failure')
            rec = app.outbox.items()[0]
            assert f'port {dead.port}' in rec['error'] \
                and f'port {greet_close.port}' in rec['error'] \
                and 'blocking outgoing mail' in rec['error'], rec['error']
            assert not rec['permanent']  # a broken network deserves retries
            assert app.outbox.remove(rec['id'])
        finally:
            be.SMTP_FALLBACK_PORT = orig_fb
    for s in (smtp, imap, greet_close, dead, starttls):
        s.close()
    print('e2e transport failures: ok')


async def bad_password():
    store, smtp, imap = build_world()
    write_config(store, smtp, imap, anna_pw='wrong')
    cfg = json.loads(Path(os.environ['TUIMAIL_CONFIG']).read_text('utf-8'))
    cfg['accounts'][1]['password'] = 'also-wrong'
    Path(os.environ['TUIMAIL_CONFIG']).write_text(json.dumps(cfg), encoding='utf-8')
    app = TuiMail()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.5)
        await settle(pilot)
        # the server said NO [AUTHENTICATIONFAILED] to both: still on the login
        # screen with the session unset — no crash, no half-login
        assert isinstance(app.screen, LoginScreen), app.screen
        assert app.session is None
    smtp.close()
    imap.close()
    print('e2e bad password: ok')


async def main():
    await main_flow()
    await transport_failures()
    await bad_password()
    print('all e2e tests green')


if __name__ == '__main__':
    asyncio.run(main())
