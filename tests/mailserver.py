"""A real mail server for the e2e suite — actual sockets, actual TLS.

Runs an SMTP submission server (implicit TLS or STARTTLS) and an IMAP4rev1
server (implicit TLS) on localhost, sharing one in-memory MailStore. The
client side is the app's own smtplib/imaplib code over a verified TLS
handshake (tests/testcert.pem, a self-signed localhost cert trusted via
SSL_CERT_FILE) — the same code paths as against a real provider, with none of
the test traffic leaving the machine. Stdlib only.

Failure modes for the SMTP server (`mode=`):
  'ssl'         normal implicit-TLS submission (a :465)
  'starttls'    plain greeting + STARTTLS upgrade (a :587)
  'dead'        accepts the TCP connection and closes it at once (a filtered
                port; the client sees a handshake error / reset)
  'greet_close' completes TLS, sends the 220 greeting, then closes (the
                client's EHLO raises SMTPServerDisconnected)
"""
import base64
import email
import re
import socket
import ssl
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
CERT, KEY = HERE / 'testcert.pem', HERE / 'testkey.pem'


def server_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    return ctx


class MailStore:
    """Accounts, folders, messages — shared by the SMTP and IMAP servers."""

    def __init__(self):
        self.lock = threading.RLock()
        self.users = {}  # address -> password
        self.folders = {}  # address -> {name: {'attrs': str, 'msgs': [dict]}}
        self._uid = {}  # (address, folder) -> last uid
        self.delivered = []  # every accepted SMTP message: (from, [rcpts], bytes)
        self.refuse_rcpt = {}  # address -> (code, text) for RCPT TO
        self.greylist_once = set()  # addresses whose FIRST RCPT gets a 450
        self.ehlo_names = []  # what clients said in EHLO/HELO
        self.imap_conns = []  # live IMAP sockets, for drop_imap()

    def add_user(self, address, password, folders=('INBOX', 'Sent', 'Archive', 'Trash')):
        with self.lock:
            self.users[address] = password
            self.folders[address] = {}
            for name in folders:
                attrs = {'Trash': r'\HasNoChildren \Trash',
                         'Archive': r'\HasNoChildren \Archive'}.get(name, r'\HasNoChildren')
                self.folders[address][name] = {'attrs': attrs, 'msgs': []}

    def deposit(self, address, folder, data: bytes, flags=()):
        with self.lock:
            uid = self._uid.get((address, folder), 0) + 1
            self._uid[(address, folder)] = uid
            box = self.folders[address].setdefault(
                folder, {'attrs': r'\HasNoChildren', 'msgs': []})
            box['msgs'].append({'uid': uid, 'flags': set(flags), 'data': data})
            return uid

    def msgs(self, address, folder):
        with self.lock:
            return list(self.folders[address][folder]['msgs'])

    def flags_of(self, address, folder, uid):
        with self.lock:
            for m in self.folders[address][folder]['msgs']:
                if m['uid'] == uid:
                    return set(m['flags'])
        return None

    def drop_imap(self):
        """Kill every live IMAP connection — simulates a server-side drop."""
        with self.lock:
            conns, self.imap_conns = self.imap_conns, []
        for c in conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass


class _Base:
    """One accept-loop thread; a handler thread per connection."""

    def __init__(self, store, mode='ssl'):
        self.store, self.mode = store, mode
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(8)
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._safe_handle, args=(conn,), daemon=True).start()

    def _safe_handle(self, conn):
        try:
            self.handle(conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def _line_reader(conn):
    state = {'buf': b''}

    def read_line():
        while b'\r\n' not in state['buf']:
            chunk = conn.recv(4096)
            if not chunk:
                raise ConnectionError('client gone')
            state['buf'] += chunk
        line, state['buf'] = state['buf'].split(b'\r\n', 1)
        return line

    def read_exact(n):
        while len(state['buf']) < n:
            chunk = conn.recv(4096)
            if not chunk:
                raise ConnectionError('client gone')
            state['buf'] += chunk
        out, state['buf'] = state['buf'][:n], state['buf'][n:]
        return out

    return read_line, read_exact


# --- SMTP ---------------------------------------------------------------------
class SmtpServer(_Base):
    def handle(self, conn):
        if self.mode == 'dead':
            return  # close instantly: the TLS handshake dies
        tls = self.mode in ('ssl', 'greet_close')
        if tls:
            conn = server_ctx().wrap_socket(conn, server_side=True)

        def send(s):
            conn.sendall(s.encode() + b'\r\n')
        send('220 localhost tuimail test ESMTP')
        if self.mode == 'greet_close':
            time.sleep(0.05)  # let the client read the greeting first
            return
        read_line, read_exact = _line_reader(conn)
        authed = None
        mail_from, rcpts = None, []
        while True:
            line = read_line().decode('utf-8', 'replace')
            verb = line.split(' ', 1)[0].upper()
            if verb in ('EHLO', 'HELO'):
                self.store.ehlo_names.append(line.split(' ', 1)[1] if ' ' in line else '')
                exts = ['localhost greets you', 'AUTH PLAIN LOGIN', '8BITMIME']
                if self.mode == 'starttls' and not tls:
                    exts.insert(1, 'STARTTLS')
                for e in exts[:-1]:
                    send(f'250-{e}')
                send(f'250 {exts[-1]}')
            elif verb == 'STARTTLS' and self.mode == 'starttls' and not tls:
                send('220 2.0.0 ready')
                conn = server_ctx().wrap_socket(conn, server_side=True)

                def send(s, _c=conn):
                    _c.sendall(s.encode() + b'\r\n')
                read_line, read_exact = _line_reader(conn)
                tls = True
            elif verb == 'AUTH':
                if not tls:
                    send('538 5.7.11 encryption required')
                    continue
                parts = line.split(' ')
                kind = parts[1].upper() if len(parts) > 1 else ''
                if kind == 'PLAIN':
                    if len(parts) > 2:
                        blob = parts[2]
                    else:
                        send('334 ')
                        blob = read_line().decode()
                    try:
                        _, user, pw = base64.b64decode(blob).decode().split('\0')
                    except ValueError:
                        user = pw = ''
                elif kind == 'LOGIN':
                    send('334 VXNlcm5hbWU6')
                    user = base64.b64decode(read_line()).decode()
                    send('334 UGFzc3dvcmQ6')
                    pw = base64.b64decode(read_line()).decode()
                else:
                    send('504 5.5.4 mechanism not supported')
                    continue
                if self.store.users.get(user) == pw:
                    authed = user
                    send('235 2.7.0 accepted')
                else:
                    send('535 5.7.8 authentication credentials invalid')
            elif verb == 'MAIL':
                if not authed:
                    send('530 5.7.0 authentication required')
                    continue
                m = re.search(r'<([^>]*)>', line)
                mail_from, rcpts = m.group(1) if m else '', []
                send('250 2.1.0 ok')
            elif verb == 'RCPT':
                m = re.search(r'<([^>]*)>', line)
                addr = m.group(1) if m else ''
                if addr in self.store.greylist_once:
                    self.store.greylist_once.discard(addr)
                    send('450 4.7.1 greylisted, try again later')
                elif addr in self.store.refuse_rcpt:
                    code, text = self.store.refuse_rcpt[addr]
                    send(f'{code} {text}')
                else:
                    rcpts.append(addr)
                    send('250 2.1.5 ok')
            elif verb == 'DATA':
                if not rcpts:
                    send('503 5.5.1 no valid recipients')
                    continue
                send('354 go ahead')
                chunks = []
                while True:
                    raw_line = read_line()
                    if raw_line == b'.':
                        break
                    chunks.append(raw_line[1:] if raw_line.startswith(b'..') else raw_line)
                data = b'\r\n'.join(chunks) + b'\r\n'
                with self.store.lock:
                    self.store.delivered.append((mail_from, list(rcpts), data))
                for r in rcpts:
                    if r in self.store.users:
                        self.store.deposit(r, 'INBOX', data)
                send('250 2.0.0 ok queued')
                mail_from, rcpts = None, []
            elif verb == 'RSET':
                mail_from, rcpts = None, []
                send('250 2.0.0 ok')
            elif verb == 'NOOP':
                send('250 2.0.0 ok')
            elif verb == 'QUIT':
                send('221 2.0.0 bye')
                return
            else:
                send('502 5.5.2 command not implemented')


# --- IMAP ---------------------------------------------------------------------
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')


def _tokens(s: str):
    return [m.group(1) if m.group(1) is not None else m.group(2)
            for m in _QUOTED.finditer(s)]


class ImapServer(_Base):
    def handle(self, conn):
        conn = server_ctx().wrap_socket(conn, server_side=True)
        with self.store.lock:
            self.store.imap_conns.append(conn)

        def send(s):
            conn.sendall(s.encode() + b'\r\n')

        def raw(b):
            conn.sendall(b)
        send('* OK tuimail test IMAP4rev1 ready')
        read_line, read_exact = _line_reader(conn)
        user, selected = None, None
        while True:
            line = read_line()
            # literal continuation: "... {n}" -> "+", n raw bytes, rest of line
            while (m := re.search(rb'\{(\d+)\}$', line)):
                send('+ go ahead')
                blob = read_exact(int(m.group(1)))
                quoted = b'"' + blob.replace(b'\\', b'\\\\').replace(b'"', b'\\"') + b'"'
                line = line[:m.start()] + quoted + read_line()
            text = line.decode('utf-8', 'replace')
            parts = text.split(' ', 2)
            if len(parts) < 2:
                continue
            tag, cmd = parts[0], parts[1].upper()
            args = parts[2] if len(parts) > 2 else ''
            if cmd == 'CAPABILITY':
                send('* CAPABILITY IMAP4rev1 UIDPLUS MOVE SPECIAL-USE')
                send(f'{tag} OK done')
                continue
            if cmd == 'LOGIN':
                toks = _tokens(args)
                u = toks[0]
                pw = toks[1].replace('\\"', '"').replace('\\\\', '\\') if len(toks) > 1 else ''
                if self.store.users.get(u) == pw:
                    user = u
                    send(f'{tag} OK [CAPABILITY IMAP4rev1 UIDPLUS MOVE] logged in')
                else:
                    send(f'{tag} NO [AUTHENTICATIONFAILED] invalid credentials')
                continue
            if cmd == 'LOGOUT':
                send('* BYE see you')
                send(f'{tag} OK logged out')
                return
            if cmd == 'NOOP':
                send(f'{tag} OK noop')
                continue
            if user is None:
                send(f'{tag} NO login first')
                continue
            boxes = self.store.folders[user]
            if cmd == 'LIST':
                for name, box in boxes.items():
                    send(f'* LIST ({box["attrs"]}) "/" "{name}"')
                send(f'{tag} OK list done')
            elif cmd == 'STATUS':
                name = _tokens(args)[0]
                if name not in boxes:
                    send(f'{tag} NO no such mailbox')
                    continue
                unseen = sum(1 for x in boxes[name]['msgs'] if '\\Seen' not in x['flags'])
                send(f'* STATUS "{name}" (UNSEEN {unseen})')
                send(f'{tag} OK status done')
            elif cmd in ('SELECT', 'EXAMINE'):
                name = _tokens(args)[0]
                if name not in boxes:
                    send(f'{tag} NO no such mailbox')
                    continue
                selected = name
                send(f'* {len(boxes[name]["msgs"])} EXISTS')
                send('* 0 RECENT')
                send(r'* FLAGS (\Seen \Flagged \Deleted)')
                send('* OK [UIDVALIDITY 1] ok')
                send(f'{tag} OK [READ-WRITE] selected')
            elif cmd == 'UID' and selected:
                sub = args.split(' ', 1)
                subcmd = sub[0].upper()
                rest = sub[1] if len(sub) > 1 else ''
                msgs = boxes[selected]['msgs']
                if subcmd == 'SEARCH':
                    if rest.upper().startswith('TEXT'):
                        q = _tokens(rest)[1].lower().encode()
                        hits = [x for x in msgs if q in x['data'].lower()]
                    else:  # ALL
                        hits = list(msgs)
                    send(('* SEARCH ' + ' '.join(str(x['uid']) for x in hits)).rstrip())
                    send(f'{tag} OK search done')
                elif subcmd == 'FETCH':
                    uidset, spec = rest.split(' ', 1)
                    wanted = {int(u) for u in uidset.split(',') if u.isdigit()}
                    fm = re.search(r'HEADER.FIELDS \(([^)]*)\)', spec, re.I)
                    for seq, x in enumerate(msgs, 1):
                        if x['uid'] not in wanted:
                            continue
                        if fm:
                            fields = fm.group(1).split()
                            head = email.message_from_bytes(x['data'])
                            payload = b''.join(
                                f'{f}: {head[f]}\r\n'.encode('utf-8', 'replace')
                                for f in fields if head.get(f)) + b'\r\n'
                            item = f'BODY[HEADER.FIELDS ({fm.group(1)})]'
                        else:
                            payload = x['data']
                            item = 'BODY[]'
                        flags = ' '.join(sorted(x['flags']))
                        raw(f'* {seq} FETCH (UID {x["uid"]} FLAGS ({flags}) '
                            f'{item} {{{len(payload)}}}\r\n'.encode())
                        raw(payload)
                        raw(b')\r\n')
                    send(f'{tag} OK fetch done')
                elif subcmd == 'STORE':
                    uidset, op, flagspec = rest.split(' ', 2)
                    wanted = {int(u) for u in uidset.split(',') if u.isdigit()}
                    flagset = set(re.findall(r'\\\w+', flagspec))
                    with self.store.lock:
                        for x in msgs:
                            if x['uid'] in wanted:
                                (x['flags'].update if op.startswith('+')
                                 else x['flags'].difference_update)(flagset)
                    send(f'{tag} OK store done')
                elif subcmd == 'EXPUNGE':
                    wanted = {int(u) for u in rest.split(',') if u.isdigit()}
                    self._expunge(send, msgs, lambda x: x['uid'] in wanted
                                  and '\\Deleted' in x['flags'])
                    send(f'{tag} OK expunged')
                elif subcmd in ('MOVE', 'COPY'):
                    uidstr, dest = rest.split(' ', 1)
                    dest = _tokens(dest)[0]
                    if dest not in boxes:
                        send(f'{tag} NO [TRYCREATE] no such mailbox')
                        continue
                    wanted = {int(u) for u in uidstr.split(',') if u.isdigit()}
                    with self.store.lock:
                        for x in [y for y in msgs if y['uid'] in wanted]:
                            self.store.deposit(user, dest, x['data'], x['flags'])
                    if subcmd == 'MOVE':
                        self._expunge(send, msgs, lambda x: x['uid'] in wanted)
                    send(f'{tag} OK {subcmd.lower()} done')
                else:
                    send(f'{tag} BAD unknown uid command')
            elif cmd == 'EXPUNGE' and selected:
                self._expunge(send, boxes[selected]['msgs'],
                              lambda x: '\\Deleted' in x['flags'])
                send(f'{tag} OK expunged')
            else:
                send(f'{tag} BAD unknown command')

    def _expunge(self, send, msgs, pred):
        # walk backwards so the untagged EXPUNGE sequence numbers stay valid
        for i in range(len(msgs) - 1, -1, -1):
            if pred(msgs[i]):
                with self.store.lock:
                    del msgs[i]
                send(f'* {i + 1} EXPUNGE')
