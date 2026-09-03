"""Function-level tests — run: python tests/unit.py

Covers the pure helpers of backend/outbox/update one by one, plus regressions
for the field-reported bugs: portable mode wrongly claiming installed copies
(passwords refused), and the SMTP 465 fallback hiding the original error.
"""
import email
import email.policy
import json
import os
import smtplib
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP = tempfile.mkdtemp(prefix='tuimail-unit-')
os.environ['TUIMAIL_CONFIG'] = str(Path(TMP) / 'config.json')
os.environ['TUIMAIL_OUTBOX'] = str(Path(TMP) / 'outbox')
os.environ['TUIMAIL_DOWNLOADS'] = TMP
os.environ['TUIMAIL_NO_UPDATE_CHECK'] = '1'

from tuimail import backend as be  # noqa: E402
from tuimail import outbox as ob  # noqa: E402
from tuimail import update as up  # noqa: E402


def test_installed_location():
    f = be._installed_location
    # Windows: the installer target and Program Files are installs
    local = os.environ.get('LOCALAPPDATA', r'C:\Users\u\AppData\Local')
    assert f(Path(local) / 'Programs' / 'tuimail', platform='nt')
    assert f(Path(local) / 'Programs', platform='nt')
    pf = os.environ.get('ProgramFiles', r'C:\Program Files')
    assert f(Path(pf) / 'tuimail', platform='nt')
    assert not f(Path(r'D:\USB\tuimail'), platform='nt')
    assert not f(Path(local) / 'Temp' / 'tuimail', platform='nt')  # not Programs
    # POSIX: system prefixes and ~/.local/bin are installs
    assert f(Path('/usr/local/bin'), platform='posix')
    assert f(Path('/opt/tuimail'), platform='posix')
    assert f(Path.home() / '.local' / 'bin', platform='posix')
    assert not f(Path('/media/usb/tuimail'), platform='posix')
    assert not f(Path.home() / 'Downloads', platform='posix')
    # bundles and Homebrew regardless of platform arg
    assert f(Path('/Applications/tuimail.app/Contents/MacOS'), platform='posix')


def test_config_path_installed_vs_portable():
    """The field bug: an installed exe must keep config (and passwords) in
    $HOME; only an ad-hoc location is portable."""
    saved = os.environ.pop('TUIMAIL_CONFIG')
    orig_exe = sys.executable
    local = os.environ.get('LOCALAPPDATA', r'C:\Users\u\AppData\Local')
    installed = Path(local) / 'Programs' / 'tuimail' / 'tuimail.exe'
    portable_dir = Path(tempfile.mkdtemp(prefix='tuimail-usb-'))
    try:
        sys.frozen = True
        sys.executable = str(installed)
        assert be.config_path() == Path.home() / '.tuimail.json'
        assert not be.portable_mode()
        sys.executable = str(portable_dir / 'tuimail.exe')
        assert be.config_path() == portable_dir / 'tuimail.json'
        assert be.portable_mode()
    finally:
        del sys.frozen
        sys.executable = orig_exe
        os.environ['TUIMAIL_CONFIG'] = saved
    # non-portable saves keep the password
    assert be.save_config({'accounts': [{'name': 'a', 'password': 'pw1'}]})
    assert be.load_config()['accounts'][0]['password'] == 'pw1'


def test_smtp_send_fallback():
    calls = []
    orig, orig_fb = be._smtp_send_once, be.SMTP_FALLBACK_PORT
    be.SMTP_TLS_PORTS.add(4650)
    be.SMTP_FALLBACK_PORT = 5870

    def fake(host, port, address, password, msg):
        calls.append(port)
        exc = plan.get(port)
        if exc:
            raise exc
    be._smtp_send_once = fake
    try:
        # SMTPServerDisconnected on the TLS port now triggers the fallback
        plan = {4650: smtplib.SMTPServerDisconnected('closed')}
        calls.clear()
        be.smtp_send('h:4650', 'a@b', 'pw', None)
        assert calls == [4650, 5870], calls
        # both ports dead -> one error naming BOTH attempts
        plan = {4650: smtplib.SMTPServerDisconnected('closed'),
                5870: ConnectionResetError('reset by peer')}
        calls.clear()
        try:
            be.smtp_send('h:4650', 'a@b', 'pw', None)
            raise AssertionError('must raise')
        except RuntimeError as exc:
            s = str(exc)
            assert 'port 4650' in s and 'port 5870' in s and 'closed' in s \
                and 'reset by peer' in s and 'blocking outgoing mail' in s, s
        # an auth error is not a transport problem: no fallback, no rewrite
        plan = {4650: smtplib.SMTPAuthenticationError(535, b'bad creds')}
        calls.clear()
        try:
            be.smtp_send('h:4650', 'a@b', 'pw', None)
            raise AssertionError('must raise')
        except smtplib.SMTPAuthenticationError:
            assert calls == [4650]
        # a non-TLS port never falls back
        plan = {5870: TimeoutError('t')}
        calls.clear()
        try:
            be.smtp_send('h:5870', 'a@b', 'pw', None)
            raise AssertionError('must raise')
        except TimeoutError:
            assert calls == [5870]
    finally:
        be._smtp_send_once = orig
        be.SMTP_FALLBACK_PORT = orig_fb
        be.SMTP_TLS_PORTS.discard(4650)


def test_text_helpers():
    assert be.sanitize('a\x1b[31mb\x9cc') == 'abc'
    assert be.sanitize('a\nb', keep_newlines=False) == 'a b'
    assert be.dec('=?utf-8?b?0J/RgNC40LLQtdGC?=') == 'Привет'
    assert be.decode_folder('&BB8EQAQ4BDIENQRC-') == 'Привет'
    assert be.decode_folder(be.OUTBOX) == 'Outbox'
    assert be.parse_date('Mon, 01 Sep 2025 10:00:00 +0300').hour == 10
    assert be.parse_date('garbage') is None
    now = datetime.now().astimezone()
    assert ':' in be.nice_date(now)
    assert be.nice_date(now - timedelta(days=400)) == (
        (now - timedelta(days=400)).strftime('%Y-%m-%d'))
    assert be.nice_date(None) == ''
    assert be.err_text(Exception(b'a \x1b[2Jerr', 'b')) == 'a err b'
    cfg = {'accounts': [{'color': be.DEFAULT_COLORS[0]}]}
    assert be.next_color(cfg) == be.DEFAULT_COLORS[1]


def test_markup_and_links():
    html = be.markup_html('plain **bold** and *it* [x](https://e.com)\n> quoted **kept**')
    assert '<b>bold</b>' in html and '<i>it</i>' in html
    assert 'href="https://e.com"' in html
    assert '&gt; quoted **kept**' in html  # quoted lines are never re-marked
    m = EmailMessage()
    m.set_content('<p>see <a href="https://x.y/page">the page</a>'
                  '<img src="https://t.rk/pixel.gif"></p>', subtype='html')
    parsed = email.message_from_bytes(m.as_bytes(), policy=email.policy.default)
    links = be.extract_links(parsed)
    assert ('the page', 'https://x.y/page') in [tuple(t) for t in links]
    assert all('pixel' not in u for _, u in links)


def test_message_roundtrip():
    f = Path(TMP) / 'att.bin'
    f.write_bytes(b'\x00\x01payload')
    msg = be.build_message('me@ex.org', 'you@ex.org', 'Тема', 'тело **b**',
                           in_reply_to='<x@y>', attachments=[f], markup=True,
                           message_id='<fixed@ex.org>')
    assert msg['Message-ID'] == '<fixed@ex.org>'
    assert msg['In-Reply-To'] == '<x@y>'
    parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
    assert be.body_of(parsed).strip().startswith('тело')
    assert parsed.get_body(preferencelist=('html',)) is not None
    atts = be.attachments_of(parsed)
    assert atts == [('att.bin', b'\x00\x01payload')]
    seed = be.reply_seed(parsed)
    assert seed['to'] == 'me@ex.org' and seed['subject'] == 'Re: Тема'
    assert seed['in_reply_to'] == '<fixed@ex.org>'


def test_partial_delivery_text():
    exc = be.PartialDelivery({'bob@x.y': (550, b'5.1.1 no such user')})
    s = str(exc)
    assert 'bob@x.y' in s and '550' in s and 'did get it' in s
    assert ob.permanent_error(exc)


def test_outbox_unit():
    spool = Path(tempfile.mkdtemp(prefix='tuimail-spool-'))
    box = ob.Outbox(spool)
    src = Path(TMP) / 'doc.txt'
    src.write_text('v1', encoding='utf-8')
    rec = box.enqueue('acc', 'a@b.c', 'to@b.c', 'Subj', 'Body', attachments=[src])
    assert (spool / f'{rec["id"]}.json').exists()
    snap = Path(rec['attachments'][0])
    assert snap.parent == spool / f'{rec["id"]}.files' and snap.read_text() == 'v1'
    # status/describe lifecycle
    assert box.status(rec) == 'queued'
    rec2 = dict(rec, tries=1, last_try=time.time(), error='boom')
    assert box.status(rec2) == 'retrying' and 'boom' in box.describe(rec2)
    rec3 = dict(rec, tries=1, permanent=True, error='no')
    assert box.status(rec3) == 'failed'
    assert box.status(rec, signed_in=set()) == 'waiting'
    # due(): backoff window and holds respected
    assert [r['id'] for r in box.due({'acc'})] == [rec['id']]
    box.hold(rec['id'])
    assert box.due({'acc'}, force=True) == []
    box.release(rec['id'])
    box._write(dict(rec, tries=2, last_try=time.time()))
    assert box.due({'acc'}) == []  # inside the backoff window
    assert [r['id'] for r in box.due({'acc'}, force=True)] == [rec['id']]
    box._write(dict(rec, tries=2, last_try=time.time() - 1000))
    assert [r['id'] for r in box.due({'acc'})] == [rec['id']]
    # summaries sanitize spool-derived text
    box._write(dict(rec, subject='S\x1b[31mub'))
    summ = box.summaries()[0]
    assert '\x1b' not in summ.subject and summ.recipient == 'to@b.c'
    # a crashed send's claim recovers with a note
    (spool / f'{rec["id"]}.json').rename(spool / f'{rec["id"]}.sending')
    os.utime(spool / f'{rec["id"]}.sending', (time.time() - 3600,) * 2)
    box.recover()
    got = box.get(rec['id'])
    assert got and 'interrupted' in got['error'] and not got['_sending']
    # remove() clears the snapshot too
    assert box.remove(rec['id'])
    assert not (spool / f'{rec["id"]}.files').exists()
    assert box.items() == []
    # validation: correct keys, wrong types -> ignored, not fatal
    bad = dict(rec, tries='3')
    (spool / f'{rec["id"]}.json').write_text(json.dumps(bad), encoding='utf-8')
    assert box.items() == [] and box.get(rec['id']) is None
    (spool / f'{rec["id"]}.json').unlink()


def test_outbox_send_guards():
    spool = Path(tempfile.mkdtemp(prefix='tuimail-spool2-'))
    box = ob.Outbox(spool)
    sent = []

    class Sess:
        def send(self, account, msg):
            sent.append(str(msg['Subject']))
    rec = box.enqueue('acc', 'a@b.c', 'to@b.c', 'S1', 'B')
    # a hold placed after due() but before send() still wins
    box.hold(rec['id'])
    assert box.send(rec, Sess()) is False and sent == []
    box.release(rec['id'])
    # a record deleted after due() is not sent
    box.remove(rec['id'])
    assert box.send(rec, Sess()) is False and sent == []
    # normal delivery removes every trace
    rec = box.enqueue('acc', 'a@b.c', 'to@b.c', 'S2', 'B')
    assert box.send(rec, Sess()) is True and sent == ['S2']
    assert box.items() == [] and not (spool / f'{rec["id"]}.sending').exists()

    class Boom:
        def send(self, account, msg):
            raise ConnectionError('nope')
    rec = box.enqueue('acc', 'a@b.c', 'to@b.c', 'S3', 'B')
    try:
        box.send(rec, Boom())
        raise AssertionError('must raise')
    except ConnectionError:
        pass
    got = box.get(rec['id'])
    assert got['tries'] == 1 and 'nope' in got['error'] and not got['permanent']
    box.remove(rec['id'])


def test_update_helpers():
    assert up.parse_version('v1.13.0') == (1, 13, 0)
    assert up.is_newer('v9.9.9') and not up.is_newer('v0.0.1')
    assert not up.is_newer('')
    assert up.asset_name() in ('tuimail-windows.exe', 'tuimail-macos-universal')
    assert up.install_kind() == 'pip'  # this process is not frozen


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for t in tests:
        t()
        print(f'{t.__name__}: ok')
    print(f'all {len(tests)} unit tests green')


if __name__ == '__main__':
    main()
