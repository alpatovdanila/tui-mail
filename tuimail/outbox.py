"""Outbox: a local spool of outgoing mail.

Sending never blocks the composer: Ctrl+S drops a JSON record into the spool
directory, the screen closes, and a background sender delivers it. A message
that could not be sent stays in the Outbox with its error until it is edited,
retried or deleted — nothing typed is ever lost to a flaky SMTP server.

Files, one message each (owner-only, written atomically):
  <id>.json     queued (or failed) record
  <id>.sending  the same record while an SMTP transaction is running — the
                rename is the cross-process claim, so two tuimail windows on
                one spool cannot both deliver it
  <id>.sent     marker left when a delivered record's file could not be
                unlinked yet (Windows sharing violation): never re-sent
  <id>.files/   snapshot of the attachments taken at queue time
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
STALE_CLAIM = 600  # s: a .sending older than this belongs to a process that died
FIELDS = {  # name -> accepted types; a record failing this is ignored, not fatal
    'id': str, 'account': str, 'address': str, 'to': str, 'subject': str,
    'body': str, 'in_reply_to': str, 'attachments': list, 'markup': bool,
    'message_id': str, 'created': (int, float), 'tries': int,
    'last_try': (int, float), 'error': str, 'permanent': bool,
}


def outbox_dir() -> Path:
    if p := os.environ.get('TUIMAIL_OUTBOX'):
        return Path(p)
    cfg = be.config_path()  # ~/.tuimail.outbox/, or <exe dir>/tuimail.outbox/ when portable
    return cfg.with_name(cfg.stem + '.outbox')


PartialDelivery = be.PartialDelivery  # raised by the SMTP layer, classified here


def permanent_error(exc) -> bool:
    """Errors that will not fix themselves get no automatic retry: 5xx replies
    (refused recipients/sender, bad credentials), a partial delivery (a retry
    would duplicate the mail for those who got it), a missing attachment."""
    if isinstance(exc, (PartialDelivery, FileNotFoundError, PermissionError)):
        return True
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        codes = [c for c, _ in exc.recipients.values()]
        return bool(codes) and all(500 <= c < 600 for c in codes)  # 4xx = greylisting
    code = getattr(exc, 'smtp_code', None)
    return isinstance(code, int) and 500 <= code < 600


def _retry_io(fn, tries=40):
    """Windows refuses to delete, rename or replace a file another thread has
    open for a moment, and the UI polls the spool — give the reader time."""
    for i in range(tries):
        try:
            return fn()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.025)


def _clean(value) -> str:
    return be.sanitize(str(value), keep_newlines=False)


def _chmod(path, mode):
    try:
        os.chmod(path, mode)
    except OSError:
        pass  # FAT/exFAT media has no permission bits — best effort


def _valid(rec) -> bool:
    return (isinstance(rec, dict)
            and all(isinstance(rec.get(k), t) for k, t in FIELDS.items())
            and all(isinstance(a, str) for a in rec['attachments']))


class Outbox:
    MARK = {'failed': '⚠ ', 'sending': '⋯ ', 'retrying': '↻ ', 'waiting': '⏸ '}

    def __init__(self, path=None):
        self.path = Path(path) if path else outbox_dir()
        self._lock = threading.Lock()
        self.sending = set()  # ids this process is delivering right now
        self.held = set()  # ids the UI owns for the moment (undo window, composer)

    # -- storage --
    def _file(self, id):
        return self.path / f'{id}.json'

    def _claim(self, id):
        return self.path / f'{id}.sending'

    def _marker(self, id):
        return self.path / f'{id}.sent'

    def _files(self, id):
        return self.path / f'{id}.files'

    def _mkdir(self):
        self.path.mkdir(parents=True, exist_ok=True)
        _chmod(self.path, 0o700)

    def _write(self, rec, target=None):
        self._mkdir()
        target = target or self._file(rec['id'])
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
        _chmod(fdir, 0o700)
        out, used = [], set()
        try:
            for n, src in enumerate(paths):
                src = Path(src)
                name = src.name if src.name not in used else f'{n}-{src.name}'
                used.add(name)
                dest = fdir / name
                shutil.copyfile(src, dest)
                _chmod(dest, 0o600)
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

    def _read(self, p):
        """-> (record or None, still-live) — an unreadable file is live (its
        snapshot must survive), a malformed one is not."""
        try:
            rec = json.loads(p.read_text('utf-8'))
        except OSError:
            return None, True
        except ValueError:
            return None, False
        if _valid(rec) and rec['id'] == p.stem:
            return rec, True
        return None, False  # one damaged record must not take every folder load down

    def items(self):
        out, live = [], set()
        for p in list(self.path.glob('*.json')) + list(self.path.glob('*.sending')):
            if self._marker(p.stem).exists():
                self._finish(p.stem)  # delivered earlier; the file was stuck then
                continue
            rec, alive = self._read(p)
            if alive:
                live.add(p.stem)
            if rec is not None:
                rec['_sending'] = p.suffix == '.sending'
                out.append(rec)
        self._sweep(live)
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
                elif p.suffix == '.sent' and not (self._file(p.stem).exists()
                                                  or self._claim(p.stem).exists()):
                    p.unlink()
            except OSError:
                pass

    def get(self, id):
        if self._marker(id).exists():
            return None
        for p in (self._file(id), self._claim(id)):
            if p.exists():
                rec, _ = self._read(p)
                if rec is not None:
                    rec['_sending'] = p.suffix == '.sending'
                return rec
        return None

    def is_sending(self, id) -> bool:
        return id in self.sending or self._claim(id).exists()

    def _finish(self, id):
        """Delete every trace of a delivered record; if a file is stuck open,
        leave a .sent marker so it is never re-sent, and try again later."""
        shutil.rmtree(self._files(id), ignore_errors=True)
        stuck = False
        for p in (self._file(id), self._claim(id)):
            try:
                _retry_io(p.unlink)
            except FileNotFoundError:
                pass
            except PermissionError:
                stuck = True
        if stuck:
            try:
                self._marker(id).touch()
            except OSError:
                pass
        else:
            try:
                self._marker(id).unlink()
            except OSError:
                pass

    def remove(self, id) -> bool:
        """Delete a queued record (not one being sent). -> True if it existed."""
        existed = self._file(id).exists()
        self._finish(id)
        return existed

    def hold(self, id):
        self.held.add(id)

    def release(self, id):
        self.held.discard(id)

    def recover(self):
        """At start-up: a .sending left by a process that died mid-transaction
        goes back to the queue (the server may or may not have taken it — the
        retry is the lesser evil, and the error says so)."""
        now = time.time()
        for p in self.path.glob('*.sending'):
            try:
                if now - p.stat().st_mtime < STALE_CLAIM:
                    continue  # another live tuimail is sending it right now
                rec, _ = self._read(p)
                if rec is None:
                    continue
                rec['tries'] += 1
                rec['last_try'] = now
                rec['error'] = 'interrupted while sending (the app stopped)'
                self._write(rec)
                p.unlink()
            except OSError:
                pass

    # -- presentation --
    def status(self, rec, signed_in=None) -> str:
        if rec['id'] in self.sending or rec.get('_sending'):
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

    def _attachments(self, rec, for_preview=False):
        """Only the snapshot inside the spool is ever attached: a record edited
        by hand (or by something else running as the user) cannot turn the
        Outbox into a tool for mailing out arbitrary local files."""
        fdir = self._files(rec['id']).resolve()
        out = []
        for p in rec['attachments']:
            path = Path(p)
            try:
                inside = path.resolve().parent == fdir
            except OSError:
                inside = False
            if not inside:
                if for_preview:
                    continue
                raise PermissionError(f'attachment outside the spool: {path.name}')
            if for_preview and not path.is_file():
                continue  # the send reports what is missing
            out.append(path)
        return out

    def message(self, rec, for_preview=False):
        return be.build_message(rec['address'], rec['to'], rec['subject'], rec['body'],
                                rec['in_reply_to'] or None,
                                attachments=self._attachments(rec, for_preview),
                                markup=rec['markup'], message_id=rec['message_id'])

    # -- delivery --
    def due(self, signed_in, force=False):
        """Records the sender should try now, oldest first. force retries
        everything, including messages that failed for good."""
        now = time.time()
        out = []
        for rec in reversed(self.items()):
            if rec.get('_sending') or rec['id'] in self.sending or rec['id'] in self.held:
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
        """Deliver one record through its account. Returns False when it is no
        longer ours to send (held meanwhile, deleted, or claimed by another
        process); on failure the record keeps the error and the exception is
        re-raised."""
        id = rec['id']
        src, claim = self._file(id), self._claim(id)
        with self._lock:
            # re-checked here, not just in due(): the user may have deleted or
            # opened the message while an earlier record was on the wire
            if id in self.sending or id in self.held:
                return False
            try:
                _retry_io(lambda: os.rename(src, claim))  # atomic: one winner
            except OSError:
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
                    if claim.exists():  # unless the user deleted it meanwhile
                        self._write({k: v for k, v in rec.items() if not k.startswith('_')})
                        _retry_io(claim.unlink)
                except OSError:
                    pass  # the record stays as it is; recover()/the next drain get it
            raise
        finally:
            self.sending.discard(id)
        self._finish(id)  # never raises: a delivered message must never be re-sent
        return True
