"""Outbox: a local spool of outgoing mail.

Sending never blocks the composer: Ctrl+S drops a JSON record into the spool
directory, the screen closes, and a background sender delivers it. A message
that could not be sent stays in the Outbox with its error until it is edited,
retried or deleted — nothing typed is ever lost to a flaky SMTP server.

One file per message (`<id>.json`, owner-only, written atomically), so the
sender thread and the UI never fight over a shared index.
"""
import email.utils
import json
import os
import shutil
import smtplib
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import backend as be

OUTBOX = be.OUTBOX
MAX_TRIES = 5
FIELDS = ('id', 'account', 'address', 'to', 'subject', 'body', 'in_reply_to',
          'attachments', 'markup', 'message_id', 'created', 'tries', 'last_try',
          'error', 'permanent')


def outbox_dir() -> Path:
    if p := os.environ.get('TUIMAIL_OUTBOX'):
        return Path(p)
    cfg = be.config_path()  # ~/.tuimail.outbox/, or <exe dir>/tuimail.outbox/ when portable
    return cfg.with_name(cfg.stem + '.outbox')


def permanent_error(exc) -> bool:
    """5xx replies, refused recipients/sender, bad credentials and a missing
    attachment will not fix themselves — no automatic retry for those."""
    if isinstance(exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                        smtplib.SMTPAuthenticationError, FileNotFoundError,
                        PermissionError)):
        return True
    code = getattr(exc, 'smtp_code', None)
    return isinstance(code, int) and 500 <= code < 600


def _retry_io(fn, tries=40):
    """Windows refuses to delete or replace a file another thread has open for
    a moment, and the UI polls the spool — give the reader time to finish."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.025)


def _clean(value) -> str:
    return be.sanitize(str(value), keep_newlines=False)


class Outbox:
    MARK = {'failed': '⚠ ', 'sending': '⋯ ', 'retrying': '↻ ', 'waiting': '⏸ '}

    def __init__(self, path=None):
        self.path = Path(path) if path else outbox_dir()
        self._lock = threading.Lock()
        self.sending = set()  # ids with an SMTP transaction in flight
        self.held = set()  # ids open in the composer — the sender leaves them alone
        self.done = set()  # delivered but the file could not be unlinked yet: never re-sent

    # -- storage --
    def _file(self, id):
        return self.path / f'{id}.json'

    def _files(self, id):
        return self.path / f'{id}.files'

    def _mkdir(self):
        self.path.mkdir(parents=True, exist_ok=True)
        if os.name == 'posix':
            os.chmod(self.path, 0o700)

    def _write(self, rec):
        self._mkdir()
        target = self._file(rec['id'])
        tmp = target.with_suffix('.tmp')
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(rec, f)
            _retry_io(lambda: os.replace(tmp, target))
        except BaseException:
            try:
                tmp.unlink()  # never leave a half-written copy of the mail behind
            except OSError:
                pass
            raise

    def _copy_attachments(self, id, paths):
        """Snapshot the attachments into the spool: what was attached is what
        gets sent, however the originals change (or vanish) before a retry."""
        if not paths:
            return []
        fdir = self._files(id)
        fdir.mkdir(parents=True, exist_ok=True)
        if os.name == 'posix':
            os.chmod(fdir, 0o700)
        out, used = [], set()
        try:
            for n, src in enumerate(paths):
                src = Path(src)
                name = src.name if src.name not in used else f'{n}-{src.name}'
                used.add(name)
                dest = fdir / name
                shutil.copyfile(src, dest)
                if os.name == 'posix':
                    os.chmod(dest, 0o600)
                out.append(str(dest))
        except BaseException:
            shutil.rmtree(fdir, ignore_errors=True)
            raise
        return out

    def enqueue(self, account, address, to, subject, body, *, in_reply_to='',
                attachments=(), markup=False, message_id=''):
        id = uuid.uuid4().hex[:12]
        self._mkdir()
        self.held.add(id)  # invisible to the sender and the sweeper until fully written
        try:
            rec = dict(
                id=id, account=account, address=address, to=to,
                subject=subject, body=body, in_reply_to=in_reply_to or '',
                attachments=self._copy_attachments(id, attachments), markup=bool(markup),
                # fixed at enqueue time: a retry after a half-completed transaction
                # re-sends the same message, not a new one
                message_id=message_id or email.utils.make_msgid(
                    domain=address.rsplit('@', 1)[-1] if '@' in address else None),
                created=time.time(), tries=0, last_try=0.0, error='', permanent=False)
            with self._lock:
                self._write(rec)
        except BaseException:
            shutil.rmtree(self._files(id), ignore_errors=True)
            raise
        finally:
            self.held.discard(id)
        return rec

    def items(self):
        out, ids = [], set()
        for p in self.path.glob('*.json'):
            if p.stem in self.done:
                try:
                    p.unlink()
                    self.done.discard(p.stem)
                except OSError:
                    pass
                continue
            try:
                rec = json.loads(p.read_text('utf-8'))
            except (OSError, ValueError):
                continue
            # one damaged record must not take every folder load down with it
            if not (isinstance(rec, dict) and rec.get('id') == p.stem
                    and all(k in rec for k in FIELDS)
                    and isinstance(rec['attachments'], list)):
                continue
            ids.add(p.stem)
            out.append(rec)
        self._sweep(ids)
        out.sort(key=lambda r: r.get('created', 0), reverse=True)  # newest first
        return out

    def _sweep(self, live_ids):
        """Leftovers of crashed writes (old .tmp) and attachment snapshots
        whose record is gone carry message content — remove them."""
        now = time.time()
        try:
            entries = list(self.path.iterdir())
        except OSError:
            return
        for p in entries:
            try:
                if p.suffix == '.tmp' and now - p.stat().st_mtime > 60:
                    p.unlink()
                elif (p.suffix == '.files' and p.is_dir() and p.stem not in live_ids
                      and p.stem not in self.sending and p.stem not in self.held):
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

    def get(self, id):
        if id in self.done:
            return None
        try:
            rec = json.loads(self._file(id).read_text('utf-8'))
        except (OSError, ValueError):
            return None
        return rec if isinstance(rec, dict) else None

    def remove(self, id) -> bool:
        shutil.rmtree(self._files(id), ignore_errors=True)
        try:
            _retry_io(self._file(id).unlink)
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            self.done.add(id)  # hidden from now on; unlinked by the next listing
            return True

    def hold(self, id):
        self.held.add(id)

    def release(self, id):
        self.held.discard(id)

    # -- presentation --
    def status(self, rec, signed_in=None) -> str:
        if rec['id'] in self.sending:
            return 'sending'
        if signed_in is not None and rec['account'] not in signed_in:
            return 'waiting'
        if rec['permanent'] or rec['tries'] >= MAX_TRIES:
            return 'failed'
        return 'retrying' if rec['tries'] else 'queued'

    def describe(self, rec, signed_in=None) -> str:
        st = self.status(rec, signed_in)
        err = _clean(rec['error'])  # spool files are data, not trusted text
        if st == 'sending':
            return 'Sending now …'
        if st == 'waiting':
            return f'Waiting — account {_clean(rec["account"])} is not signed in'
        if st == 'failed':
            n = rec['tries']
            return (f'Not sent after {n} attempt{"s" if n != 1 else ""}: {err}'
                    '  —  Enter edits, R retries, d deletes')
        if st == 'retrying':
            wait = max(0, int(rec['last_try'] + 60 * rec['tries'] - time.time()))
            return f'Retrying in {wait} s — attempt {rec["tries"]} failed: {err}'
        return 'Queued — sending shortly'

    def summaries(self, accounts=None, signed_in=None):
        out = []
        for rec in self.items():
            if accounts is not None and rec['account'] not in accounts:
                continue
            st = self.status(rec, signed_in)
            out.append(be.Summary(
                uid=rec['id'], sender=_clean(rec['address']),
                subject=self.MARK.get(st, '') + (_clean(rec['subject']) or '(no subject)'),
                date=datetime.fromtimestamp(rec['created'], timezone.utc),
                unread=st == 'failed',  # bold = needs attention
                account=rec['account'], recipient=_clean(rec['to'])))
        return out

    def message(self, rec, for_preview=False):
        atts = rec['attachments']
        if for_preview:
            atts = [p for p in atts if Path(p).is_file()]  # the send reports what's missing
        return be.build_message(rec['address'], rec['to'], rec['subject'], rec['body'],
                                rec['in_reply_to'] or None, attachments=atts,
                                markup=rec['markup'], message_id=rec['message_id'])

    # -- delivery --
    def due(self, signed_in, force=False):
        """Records the sender should try now, oldest first. force retries
        everything, including messages that failed for good."""
        now = time.time()
        out = []
        for rec in reversed(self.items()):
            if rec['id'] in self.sending or rec['id'] in self.held:
                continue
            if rec['account'] not in signed_in:
                continue
            if force:
                rec.update(tries=0, permanent=False)
            elif rec['permanent'] or rec['tries'] >= MAX_TRIES:
                continue
            elif rec['tries'] and now - rec['last_try'] < 60 * rec['tries']:
                continue  # linear backoff: 1, 2, 3, 4 minutes
            out.append(rec)
        return out

    def send(self, rec, session) -> bool:
        """Deliver one record through its account. Returns False if it was
        already being sent; on failure the record keeps the error and the
        exception is re-raised."""
        id = rec['id']
        with self._lock:
            if id in self.sending:
                return False
            self.sending.add(id)
        try:
            session.send(rec['account'], self.message(rec))
        except Exception as exc:
            rec['tries'] += 1
            rec['last_try'] = time.time()
            rec['error'] = be.err_text(exc) or exc.__class__.__name__
            rec['permanent'] = permanent_error(exc)
            with self._lock:
                try:
                    if self._file(id).exists():  # unless the user deleted it meanwhile
                        self._write(rec)
                except OSError:
                    pass  # the record stays queued; the next drain tries again
            raise
        finally:
            self.sending.discard(id)
        self.remove(id)  # never raises: a delivered message must never be re-sent
        return True
