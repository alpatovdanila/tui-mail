"""tuimail — a keyboard-first TUI email client built on Textual."""
import threading
import webbrowser
from functools import partial, wraps
from pathlib import Path

from rich.table import Table as RichTable
from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.command import Hit, Provider
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header,
                             Input, Label, ListItem, ListView, OptionList,
                             Select, Static, TextArea)

from . import backend as be
from .backend import nice_date

LOGO = (
    '▀█▀ █ █ █ █▀▄▀█ ▄▀█ █ █  \n'
    ' █  █▄█ █ █ ▀ █ █▀█ █ █▄▄'
)

WELCOME = (
    'A keyboard-first email client that lives in your terminal.\n\n'
    '  • reads over IMAP, sends over SMTP — Gmail, Outlook,\n'
    '    Yandex, iCloud or any custom server\n'
    '  • your credentials never leave this machine\n'
    '  • press ? anywhere for the keyboard reference\n'
)

HELP_ROWS = [
    ('Mailbox', ''),
    ('j / k / ↑ ↓', 'move through messages'),
    ('g / G', 'first / last message'),
    ('Enter', 'open message'),
    ('Tab', 'cycle panes'),
    ('c / r', 'compose / reply'),
    ('u / s', 'toggle unread / star'),
    ('d', 'delete (asks first)'),
    ('/', 'search this folder (Esc clears)'),
    ('R', 'refresh'),
    ('', ''),
    ('Reader', ''),
    ('j k Space', 'scroll / page'),
    ('o', 'open a link from the message'),
    ('a', 'save attachments to Downloads'),
    ('q / Esc', 'back'),
    ('', ''),
    ('Anywhere', ''),
    ('Ctrl+P', 'command palette'),
    ('Ctrl+L', 'logout'),
    ('?', 'this help'),
    ('q', 'quit (from the mailbox)'),
]


def ui_callback(fn):
    """Worker results can land while the screen is being torn down (logout/quit);
    the widgets are already gone then — drop the update instead of crashing."""
    @wraps(fn)
    def inner(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except NoMatches:
            pass
    return inner


# --- small modals -------------------------------------------------------------
class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding('y', 'yes', 'Yes'), Binding('n,escape', 'no', 'No')]

    def __init__(self, prompt):
        super().__init__()
        self.prompt = prompt

    def compose(self):
        with Vertical(id='confirm-card'):
            yield Static(Text(self.prompt))
            with Horizontal(classes='buttons'):
                yield Button('Yes (y)', variant='error', id='yes')
                yield Button('No (n)', id='no')

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == 'yes')

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class HelpScreen(ModalScreen):
    BINDINGS = [Binding('escape,q,question_mark', 'close', 'Close')]

    def compose(self):
        rt = RichTable(box=None, padding=(0, 2, 0, 0), title='Keyboard reference',
                       title_style='bold cyan', title_justify='left')
        rt.add_column('Key', style='bold yellow')
        rt.add_column('Action')
        for key, action in HELP_ROWS:
            if key and not action:
                rt.add_row(Text(key, style='bold cyan underline'), '')
            else:
                rt.add_row(key, action)
        with Vertical(id='help-card'):
            yield Static(rt)

    def action_close(self):
        self.dismiss()


class LinksScreen(ModalScreen):
    BINDINGS = [Binding('escape,q', 'close', 'Close')]

    def __init__(self, links):
        super().__init__()
        self.links = links

    def compose(self):
        with Vertical(id='links-card'):
            yield Label('Links — Enter opens in your browser, Esc closes', classes='card-title')
            yield OptionList(*self.links, id='linklist')

    def on_mount(self):
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event):
        url = self.links[event.option_index]
        webbrowser.open(url)
        self.app.notify(f'Opened {url[:60]}')

    def action_close(self):
        self.dismiss()


# --- onboarding / account / login --------------------------------------------
class OnboardingScreen(Screen):
    def compose(self):
        with Vertical(classes='card'):
            yield Static(LOGO, id='logo')
            yield Static('mail, comfortably, in your terminal', classes='tagline')
            yield Static(WELCOME, id='welcome')
            with Horizontal(classes='buttons'):
                yield Button('Set up my account', variant='primary', id='setup')
                yield Button('Try the demo first', id='demo')

    def on_button_pressed(self, event):
        if event.button.id == 'setup':
            self.app.switch_screen(AccountScreen())
        else:
            self.app.backend = be.DemoBackend()
            self.app.switch_screen(MainScreen())


class AccountScreen(Screen):
    def compose(self):
        with Vertical(classes='card'):
            yield Label('Account setup', classes='card-title')
            yield Select([(n, n) for n in be.PROVIDERS], prompt='Choose a provider', id='provider')
            yield Input(placeholder='Email address', id='address')
            yield Input(placeholder='IMAP host (host or host:port, SSL)', id='imap')
            yield Input(placeholder='SMTP host (host or host:port)', id='smtp')
            yield Static('Pick a provider to fill the servers in, or type your own.',
                         id='provider-hint', classes='hint')
            with Horizontal(classes='buttons'):
                yield Button('Save', variant='primary', id='save')
                yield Button('Back', id='back')

    def on_mount(self):
        cfg = be.load_config()
        for fid, key in (('address', 'address'), ('imap', 'imap_host'), ('smtp', 'smtp_host')):
            self.query_one(f'#{fid}', Input).value = cfg.get(key, '')
        self.query_one('#address', Input).focus()

    def on_select_changed(self, event):
        if event.value in (None, Select.BLANK):
            return
        p = be.PROVIDERS[event.value]
        self.query_one('#imap', Input).value = p['imap']
        self.query_one('#smtp', Input).value = p['smtp']
        self.query_one('#provider-hint', Static).update(p['hint'])

    def on_button_pressed(self, event):
        if event.button.id == 'back':
            self.app.switch_screen(
                LoginScreen() if be.load_config().get('address') else OnboardingScreen())
            return
        address = self.query_one('#address', Input).value.strip()
        if '@' not in address:
            self.app.notify('Enter a valid email address', severity='warning')
            return
        domain = address.rsplit('@', 1)[-1]
        cfg = be.load_config()
        cfg.update(
            address=address,
            imap_host=self.query_one('#imap', Input).value.strip() or f'imap.{domain}',
            smtp_host=self.query_one('#smtp', Input).value.strip() or f'smtp.{domain}',
        )
        if not be.save_config(cfg):
            self.app.notify('Could not write the settings file — check permissions',
                            severity='error')
            return
        self.app.notify('Account saved')
        self.app.switch_screen(LoginScreen())


class LoginScreen(Screen):
    def compose(self):
        with Vertical(classes='card'):
            yield Static(LOGO, id='logo')
            yield Static('mail, comfortably, in your terminal', classes='tagline')
            yield Input(placeholder='Email address', id='address')
            yield Input(placeholder='Password / app password', password=True, id='password')
            yield Checkbox('Remember password (stored as plain text on this machine)',
                           id='remember')
            with Horizontal(classes='buttons'):
                yield Button('Sign in', variant='primary', id='signin')
                yield Button('Demo mailbox', id='demo')
                yield Button('Account settings', id='settings')
            yield Static('', id='login-status')

    def on_mount(self):
        cfg = be.load_config()
        self.query_one('#address', Input).value = cfg.get('address', '')
        if cfg.get('password'):
            self.query_one('#password', Input).value = cfg['password']
            self.query_one('#remember', Checkbox).value = True
        self.query_one('#password' if cfg.get('address') else '#address', Input).focus()

    def on_button_pressed(self, event):
        if event.button.id == 'demo':
            self.app.backend = be.DemoBackend()
            self.app.switch_screen(MainScreen())
        elif event.button.id == 'settings':
            self.app.switch_screen(AccountScreen())
        elif event.button.id == 'signin':
            self._start_signin()

    def on_input_submitted(self, event):
        self._start_signin()

    def _start_signin(self):
        address = self.query_one('#address', Input).value.strip()
        password = self.query_one('#password', Input).value
        if '@' not in address or not password:
            self.app.notify('Address and password are both required', severity='warning')
            return
        cfg = be.load_config()
        domain = address.rsplit('@', 1)[-1]
        cfg.setdefault('imap_host', f'imap.{domain}')
        cfg.setdefault('smtp_host', f'smtp.{domain}')
        cfg['address'] = address
        self.query_one('#signin', Button).disabled = True
        self.query_one('#login-status', Static).update(f'Connecting to {cfg["imap_host"]} …')
        self._connect(address, password, cfg)

    @work(thread=True, exclusive=True, group='login')
    def _connect(self, address, password, cfg):
        app = self.app
        try:
            backend = be.ImapBackend(address, password, cfg['imap_host'], cfg['smtp_host'])
        except Exception as exc:
            app.call_from_thread(self._fail, exc)
            return
        app.call_from_thread(self._ok, backend, cfg, password)

    @ui_callback
    def _fail(self, exc):
        self.query_one('#signin', Button).disabled = False
        self.query_one('#login-status', Static).update(Text(f'✗ {exc}', style='bold red'))

    @ui_callback
    def _ok(self, backend, cfg, password):
        if self.query_one('#remember', Checkbox).value:
            cfg['password'] = password  # explicit opt-in; the checkbox label says it's plain text
        else:
            cfg.pop('password', None)
        if not be.save_config(cfg):
            self.app.notify('Could not write the settings file — settings not saved',
                            severity='warning')
        self.app.backend = backend
        self.app.switch_screen(MainScreen())


# --- mailbox ------------------------------------------------------------------
class MainScreen(Screen):
    BINDINGS = [
        Binding('j', 'move(1)', 'Down', show=False),
        Binding('k', 'move(-1)', 'Up', show=False),
        Binding('g', 'top', 'Top', show=False),
        Binding('G', 'bottom', 'Bottom', show=False),
        Binding('c', 'compose', 'Compose'),
        Binding('r', 'reply', 'Reply'),
        Binding('d', 'delete', 'Delete'),
        Binding('u', 'toggle_read', 'Read/unread', show=False),
        Binding('s', 'toggle_flag', 'Star', show=False),
        Binding('slash', 'search', 'Search', key_display='/'),
        Binding('escape', 'clear_search', 'Clear search', show=False),
        Binding('R', 'refresh', 'Refresh'),
        Binding('question_mark', 'help', 'Help', key_display='?'),
        Binding('q', 'quit_app', 'Quit'),
    ]

    def compose(self):
        yield Header(show_clock=True)
        with Horizontal(id='main-split'):
            with Vertical(id='sidebar'):
                yield Label('MAILBOXES', id='sidebar-title')
                yield ListView(id='folders')
            with Vertical(id='content'):
                yield Input(placeholder='Search this folder — Enter filters, Esc clears',
                            id='search')
                yield DataTable(id='msgtable')
                with VerticalScroll(id='preview-scroll'):
                    yield Static(id='preview')
        yield Footer()

    def on_mount(self):
        self.folder = 'INBOX'
        self.all_msgs, self.view = [], []
        self.filter_uids = None
        self.folder_counts = []
        self._cache = {}
        self._pv_timer = None
        self._seq = 0  # bumped on every optimistic local change; stale loads are dropped
        table = self.query_one('#msgtable', DataTable)
        table.cursor_type = 'row'
        table.add_column(' ', width=2)
        table.add_column('From', width=26)
        table.add_column('Subject')
        table.add_column('When', width=10)
        table.loading = True
        self.app.sub_title = self.app.backend.address
        self.set_interval(60, partial(self._load_all, False))  # quiet background poll
        self._load_all(True)

    # -- data loading --
    @work(thread=True, exclusive=True, group='load')
    def _load_all(self, focus=False):
        app = self.app
        seq = self._seq
        try:
            counts = app.backend.folders()
            msgs = app.backend.list_messages(self.folder)
        except Exception as exc:
            app.call_from_thread(self._load_failed, exc)
            return
        app.call_from_thread(self._loaded, counts, msgs, seq, focus)

    @work(thread=True, exclusive=True, group='load')
    def _load_folder(self, folder):
        app = self.app
        seq = self._seq
        try:
            msgs = app.backend.list_messages(folder)
        except Exception as exc:
            app.call_from_thread(self._load_failed, exc)
            return
        app.call_from_thread(self._folder_loaded, folder, msgs, seq)

    @ui_callback
    def _load_failed(self, exc):
        self.query_one('#msgtable', DataTable).loading = False
        self.app.notify(str(exc), severity='error', title='Mail')

    @ui_callback
    def _loaded(self, counts, msgs, seq, focus):
        table = self.query_one('#msgtable', DataTable)
        table.loading = False
        if seq != self._seq:
            return  # snapshot predates a local change; the next poll reconciles
        self.folder_counts = counts
        self._cache.clear()
        self.all_msgs = msgs
        self.update_sidebar()
        self.apply_filter(keep_cursor=True)
        if focus:
            table.focus()

    @ui_callback
    def _folder_loaded(self, folder, msgs, seq):
        if folder != self.folder or seq != self._seq:
            return
        self._cache.clear()
        self.all_msgs = msgs
        self.apply_filter()
        table = self.query_one('#msgtable', DataTable)
        table.loading = False
        table.focus()

    def _fetch_cached(self, folder, uid):
        key = (folder, uid)
        if key not in self._cache:
            self._cache[key] = self.app.backend.fetch(folder, uid)
        return self._cache[key]

    # -- rendering --
    def update_sidebar(self):
        lv = self.query_one('#folders', ListView)
        lv.clear()
        idx = 0
        for i, (name, unread) in enumerate(self.folder_counts):
            label = Text(f'{"▸" if name == self.folder else " "} {name}')
            if unread:
                label.append(f'  {unread}', style='bold cyan')
            item = ListItem(Label(label))
            item.folder_name = name
            lv.append(item)
            if name == self.folder:
                idx = i
        lv.index = idx

    def apply_filter(self, keep_cursor=False):
        self.view = [s for s in self.all_msgs
                     if self.filter_uids is None or s.uid in self.filter_uids]
        self.rebuild_table(keep_cursor)
        if not self.view:
            self._set_preview(Text('Nothing here — the folder is empty or no matches.',
                                   style='dim'))

    def rebuild_table(self, keep_cursor=False):
        table = self.query_one('#msgtable', DataTable)
        cur = table.cursor_row if keep_cursor else 0
        table.clear()
        for s in self.view:
            icons = ('●' if s.unread else ' ') + ('★' if s.flagged else ' ')
            style = 'bold' if s.unread else ''
            table.add_row(
                Text(icons, style='yellow' if s.flagged else 'cyan'),
                Text(s.sender[:26], style=style),
                Text(s.subject[:80], style=style),
                Text(nice_date(s.date), style='dim'),
                key=s.uid,
            )
        if self.view:
            table.move_cursor(row=max(0, min(cur, len(self.view) - 1)))

    @ui_callback
    def _set_preview(self, content):
        self.preview_text = getattr(content, 'plain', str(content))
        self.query_one('#preview', Static).update(content)
        self.query_one('#preview-scroll', VerticalScroll).scroll_home(animate=False)

    def current(self):
        table = self.query_one('#msgtable', DataTable)
        if 0 <= table.cursor_row < len(self.view):
            return self.view[table.cursor_row]
        return None

    # -- preview --
    def on_data_table_row_highlighted(self, event):
        if self._pv_timer is not None:
            self._pv_timer.stop()
        if 0 <= event.cursor_row < len(self.view):
            s = self.view[event.cursor_row]
            self._pv_timer = self.set_timer(0.25, lambda: self._load_preview(s))

    @work(thread=True, exclusive=True, group='preview')
    def _load_preview(self, s):
        app = self.app
        try:
            msg = self._fetch_cached(self.folder, s.uid)
        except be.MailGone:
            app.call_from_thread(self._set_preview,
                                 Text('Message no longer exists on the server.', style='dim'))
            return
        except Exception as exc:
            app.call_from_thread(self._set_preview, Text(f'Preview failed: {exc}', style='dim'))
            return
        t = Text()
        t.append(f'{s.subject}\n', style='bold')
        t.append(f'{s.sender} · {nice_date(s.date)}\n\n', style='dim')
        t.append(be.body_of(msg)[:4000])
        app.call_from_thread(self._set_preview, t)

    # -- folders --
    def on_list_view_selected(self, event):
        name = getattr(event.item, 'folder_name', None)
        if name and name != self.folder:
            self.goto_folder(name)

    def goto_folder(self, name):
        self.folder = name
        self.filter_uids = None
        inp = self.query_one('#search', Input)
        inp.value = ''
        inp.remove_class('visible')
        self.query_one('#msgtable', DataTable).loading = True
        self.update_sidebar()
        self._load_folder(name)

    def _adjust_unread(self, folder, delta):
        self.folder_counts = [(n, max(0, c + delta) if n == folder else c)
                              for n, c in self.folder_counts]
        self.update_sidebar()

    # -- open / reply --
    def on_data_table_row_selected(self, event):
        s = self.current()
        if s:
            self._open(s)

    @work(thread=True, exclusive=True, group='open')
    def _open(self, s):
        app = self.app
        try:
            msg = self._fetch_cached(self.folder, s.uid)
        except be.MailGone:
            app.call_from_thread(self._gone, s)
            return
        except Exception as exc:
            app.call_from_thread(app.notify, str(exc), severity='error')
            return
        if s.unread:
            try:
                app.backend.mark(self.folder, s.uid, read=True)
            except Exception:
                pass
        app.call_from_thread(self._show_reader, s, msg)

    @ui_callback
    def _show_reader(self, s, msg):
        if s.unread:
            s.unread = False
            self._seq += 1
            self._adjust_unread(self.folder, -1)
            self.rebuild_table(keep_cursor=True)
        self.app.push_screen(ReaderScreen(s, msg, self))

    @ui_callback
    def _gone(self, s):
        self.app.notify('That message no longer exists on the server', severity='warning')
        self._seq += 1
        if s in self.all_msgs:
            self.all_msgs.remove(s)
        self._cache.pop((self.folder, s.uid), None)
        if s.unread:
            self._adjust_unread(self.folder, -1)
        self.apply_filter(keep_cursor=True)

    def action_reply(self):
        s = self.current()
        if s:
            self._reply(s)

    @work(thread=True, exclusive=True, group='open')
    def _reply(self, s):
        app = self.app
        try:
            msg = self._fetch_cached(self.folder, s.uid)
        except be.MailGone:
            app.call_from_thread(self._gone, s)
            return
        except Exception as exc:
            app.call_from_thread(app.notify, str(exc), severity='error')
            return
        app.call_from_thread(self.app.push_screen, ComposeScreen(be.reply_seed(msg)))

    # -- generic fire-and-forget backend IO with optimistic UI --
    @work(thread=True, group='io')
    def _io(self, fn):
        try:
            fn()
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, str(exc),
                                      severity='error', title='Mail')

    # -- actions --
    def action_move(self, delta):
        table = self.query_one('#msgtable', DataTable)
        if table.row_count:
            table.move_cursor(row=max(0, min(table.cursor_row + delta, table.row_count - 1)))

    def action_top(self):
        self.action_move(-len(self.view))

    def action_bottom(self):
        self.action_move(len(self.view))

    def action_compose(self):
        self.app.push_screen(ComposeScreen())

    def action_toggle_read(self):
        s = self.current()
        if not s:
            return
        s.unread = not s.unread
        self._seq += 1
        self._io(partial(self.app.backend.mark, self.folder, s.uid, read=not s.unread))
        self._adjust_unread(self.folder, 1 if s.unread else -1)
        self.rebuild_table(keep_cursor=True)

    def action_toggle_flag(self):
        s = self.current()
        if not s:
            return
        s.flagged = not s.flagged
        self._seq += 1
        self._io(partial(self.app.backend.flag, self.folder, s.uid, flagged=s.flagged))
        self.rebuild_table(keep_cursor=True)

    def action_delete(self):
        s = self.current()
        if not s:
            return
        self.app.push_screen(ConfirmScreen(f'Delete "{s.subject[:48]}"?'),
                             lambda ok: self.do_delete(s) if ok else None)

    def do_delete(self, s):
        self._seq += 1
        self._io(partial(self.app.backend.delete, self.folder, s.uid))
        if s in self.all_msgs:
            self.all_msgs.remove(s)
        self._cache.pop((self.folder, s.uid), None)
        if s.unread:
            self._adjust_unread(self.folder, -1)
        self.apply_filter(keep_cursor=True)
        self.app.notify('Deleted')

    def action_refresh(self):
        self.query_one('#msgtable', DataTable).loading = True
        self._load_all(True)

    def action_help(self):
        self.app.push_screen(HelpScreen())

    def action_quit_app(self):
        self.app.exit()

    # -- search --
    def action_search(self):
        inp = self.query_one('#search', Input)
        inp.add_class('visible')
        inp.focus()

    def on_input_submitted(self, event):
        if event.input.id != 'search':
            return
        q = event.value.strip()
        if not q:
            self.action_clear_search()
            return
        self._search(self.folder, q)

    @work(thread=True, exclusive=True, group='search')
    def _search(self, folder, q):
        app = self.app
        try:
            hits = app.backend.search(folder, q)
        except Exception:
            hits = None
        if hits is None:  # server can't do it — filter what we have locally
            ql = q.lower()
            hits = {s.uid for s in self.all_msgs
                    if ql in s.sender.lower() or ql in s.subject.lower()}
        app.call_from_thread(self._search_done, folder, q, hits)

    @ui_callback
    def _search_done(self, folder, q, hits):
        if folder != self.folder:
            return
        self.filter_uids = hits
        self.apply_filter()
        self.app.notify(f'{len(self.view)} match(es) for "{q}" — Esc clears')
        self.query_one('#msgtable', DataTable).focus()

    def action_clear_search(self):
        inp = self.query_one('#search', Input)
        if self.filter_uids is None and not inp.has_class('visible'):
            return
        inp.value = ''
        inp.remove_class('visible')
        self.filter_uids = None
        self.apply_filter()
        self.query_one('#msgtable', DataTable).focus()


# --- reader -------------------------------------------------------------------
class ReaderScreen(Screen):
    BINDINGS = [
        Binding('q,escape', 'back', 'Back'),
        Binding('r', 'reply', 'Reply'),
        Binding('o', 'links', 'Links'),
        Binding('a', 'attachments', 'Save attachments'),
        Binding('d', 'delete', 'Delete'),
        Binding('question_mark', 'help', 'Help', key_display='?'),
        Binding('j', 'scroll(3)', 'Down', show=False),
        Binding('k', 'scroll(-3)', 'Up', show=False),
        Binding('space', 'page', 'Page down', show=False),
    ]

    def __init__(self, summary, msg, main):
        super().__init__()
        self.summary, self.msg, self.main = summary, msg, main
        self.body_text = be.body_of(msg)

    def compose(self):
        yield Header()
        with VerticalScroll(id='reader-scroll'):
            yield Static(id='reader-body')
        yield Footer()

    def on_mount(self):
        t = Text()
        t.append(f'{self.msg.get("Subject") or "(no subject)"}\n\n', style='bold')
        for label in ('From', 'To', 'Cc', 'Date'):
            v = self.msg.get(label)
            if v:
                t.append(f'{label:>8}: {v}\n', style='dim')
        atts = be.attachments_of(self.msg)
        links = be.extract_links(self.msg)
        extras = []
        if atts:
            extras.append(f'📎 {len(atts)} attachment(s) — press a to save')
        if links:
            extras.append(f'🔗 {len(links)} link(s) — press o to open')
        if extras:
            t.append('\n' + '\n'.join(extras) + '\n', style='italic cyan')
        t.append('\n' + '─' * 72 + '\n\n', style='dim')
        t.append(self.body_text)
        self.query_one('#reader-body', Static).update(t)
        self.query_one('#reader-scroll', VerticalScroll).focus()

    def action_back(self):
        self.app.pop_screen()

    def action_reply(self):
        self.app.push_screen(ComposeScreen(be.reply_seed(self.msg)))

    def action_links(self):
        links = be.extract_links(self.msg)
        if not links:
            self.app.notify('No links in this message')
            return
        self.app.push_screen(LinksScreen(links))

    def action_attachments(self):
        atts = be.attachments_of(self.msg)
        if not atts:
            self.app.notify('No attachments in this message')
            return
        dest = be.downloads_dir()
        dest.mkdir(parents=True, exist_ok=True)
        saved = []
        for name, data in atts:
            p = dest / name
            i = 1
            while p.exists():
                p = dest / f'{Path(name).stem} ({i}){Path(name).suffix}'
                i += 1
            p.write_bytes(data)
            saved.append(p.name)
        self.app.notify(f'Saved to {dest}: {", ".join(saved)}')

    def action_delete(self):
        def done(ok):
            if ok:
                self.main.do_delete(self.summary)
                self.app.pop_screen()
        self.app.push_screen(
            ConfirmScreen(f'Delete "{self.summary.subject[:48]}"?'), done)

    def action_help(self):
        self.app.push_screen(HelpScreen())

    def action_scroll(self, dy):
        self.query_one('#reader-scroll', VerticalScroll).scroll_relative(y=dy, animate=False)

    def action_page(self):
        self.query_one('#reader-scroll', VerticalScroll).scroll_page_down(animate=False)


# --- compose ------------------------------------------------------------------
class ComposeScreen(Screen[bool]):
    BINDINGS = [
        Binding('ctrl+s', 'send', 'Send', priority=True),
        Binding('escape', 'cancel', 'Cancel', priority=True),
    ]

    def __init__(self, seed=None):
        super().__init__()
        self.seed = seed or {}
        self._sending = False

    def compose(self):
        with Vertical():
            yield Label('✉  New message — Ctrl+S sends, Esc cancels', classes='card-title')
            yield Input(placeholder='To', id='to')
            yield Input(placeholder='Subject', id='subject')
            yield TextArea(id='body')
        yield Footer()

    def on_mount(self):
        self.query_one('#to', Input).value = self.seed.get('to', '')
        self.query_one('#subject', Input).value = self.seed.get('subject', '')
        self.query_one('#body', TextArea).text = self.seed.get('body', '')
        self.query_one('#body' if self.seed.get('to') else '#to').focus()

    def action_send(self):
        if self._sending:
            return  # SMTP thread can't be aborted; a second Ctrl+S would send twice
        to = self.query_one('#to', Input).value.strip()
        subject = self.query_one('#subject', Input).value.strip()
        body = self.query_one('#body', TextArea).text
        if not to:
            self.app.notify('Add a recipient first', severity='warning')
            return
        self._sending = True
        self.app.notify(f'Sending to {to} …', timeout=3)
        self._send(to, subject, body)

    @work(thread=True, exclusive=True, group='send')
    def _send(self, to, subject, body):
        app = self.app
        try:
            msg = be.build_message(app.backend.address, to, subject, body,
                                   self.seed.get('in_reply_to'))
            app.backend.send(msg)
        except Exception as exc:
            app.call_from_thread(self._send_failed, exc)
            return
        app.call_from_thread(self._sent, to)

    def _send_failed(self, exc):
        # the draft stays open — nothing typed is lost on a failed send
        self._sending = False
        self.app.notify(f'Send failed, draft kept: {exc}', severity='error', timeout=8)

    def _sent(self, to):
        self._sending = False
        self.app.notify(f'Sent to {to}')
        if self.app.screen is self:
            self.dismiss(True)  # dismiss pops the CURRENT screen — only pop ourselves

    def action_cancel(self):
        to = self.query_one('#to', Input).value
        subject = self.query_one('#subject', Input).value
        body = self.query_one('#body', TextArea).text
        dirty = (to != self.seed.get('to', '') or subject != self.seed.get('subject', '')
                 or body != self.seed.get('body', ''))
        if not dirty:
            self.dismiss(False)
            return
        self.app.push_screen(ConfirmScreen('Discard this draft?'),
                             lambda ok: self.dismiss(False) if ok else None)


# --- command palette ----------------------------------------------------------
class MailCommands(Provider):
    async def search(self, query):
        screen = self.app.screen
        if not isinstance(screen, MainScreen):
            return
        matcher = self.matcher(query)
        commands = [
            ('Compose new message', screen.action_compose),
            ('Refresh mailbox', screen.action_refresh),
            ('Search messages', screen.action_search),
            ('Keyboard reference', screen.action_help),
            ('Logout', self.app.action_logout),
        ] + [(f'Open folder: {name}', partial(screen.goto_folder, name))
             for name, _ in screen.folder_counts]
        for label, fn in commands:
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), fn)


# --- app ----------------------------------------------------------------------
class TuiMail(App):
    CSS_PATH = 'app.tcss'
    TITLE = 'tuimail'
    COMMANDS = App.COMMANDS | {MailCommands}
    BINDINGS = [Binding('ctrl+l', 'logout', 'Logout', show=False, priority=True)]

    backend = None

    def on_mount(self):
        try:
            self.theme = 'tokyo-night'
        except Exception:
            pass  # older/newer theme sets — default theme is fine
        self.push_screen(
            LoginScreen() if be.load_config().get('address') else OnboardingScreen())

    def action_logout(self):
        if self.backend is None:
            return
        old, self.backend = self.backend, None
        threading.Thread(target=old.close, daemon=True).start()
        self.sub_title = ''
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(LoginScreen())
        self.notify('Signed out')
