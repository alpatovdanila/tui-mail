"""tuimail — a keyboard-first TUI email client built on Textual."""
import os
import threading
import webbrowser
from functools import partial, wraps
from pathlib import Path

from rich.table import Table as RichTable
from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, Checkbox, DataTable, DirectoryTree,
                             Footer, Header, Input, Label, ListItem, ListView,
                             Markdown, OptionList, Select, Static, TextArea)

from . import __version__
from . import backend as be
from . import update as up
from .backend import nice_date

LOGO = (
    '▀█▀ █ █ █ █▀▄▀█ ▄▀█ █ █  \n'
    ' █  █▄█ █ █ ▀ █ █▀█ █ █▄▄'
)

WELCOME = (
    'A keyboard-first email client that lives in your terminal.\n\n'
    '  • reads over IMAP, sends over SMTP — Gmail, Outlook,\n'
    '    Yandex, iCloud or any custom server\n'
    '  • multiple accounts, each with its own color\n'
    '  • your credentials never leave this machine\n'
    '  • press ? or F1 anywhere for the keyboard reference\n'
)

HELP_ROWS = [
    ('Mailbox', ''),
    ('● / ○', 'unread / read — the circle color is the account'),
    ('j / k / ↑ ↓', 'move through messages'),
    ('J / K · p', 'scroll the preview · hide/show it'),
    ('g / G', 'first / last message'),
    ('Enter', 'open message'),
    ('Space', 'select · Ctrl+A selects everything shown · Esc cancels'),
    ('Tab', 'cycle panes (list · accounts · folders)'),
    ('0 1 2 …', 'all accounts · account 1, 2 …'),
    ('c / r', 'compose / reply'),
    ('u / s', 'toggle unread / star'),
    ('d', 'delete'),
    ('/', 'search this folder (Esc clears)'),
    ('R', 'refresh'),
    ('', ''),
    ('Compose', ''),
    ('Ctrl+S / Esc', 'send / cancel'),
    ('Ctrl+B  Ctrl+E  Ctrl+K', 'bold, italic, link'),
    ('Ctrl+O', 'attach a file (Tab completes paths)'),
    ('', ''),
    ('Reader', ''),
    ('j k Space b', 'scroll / page down / page up'),
    ('g G Ctrl+D Ctrl+U', 'top / bottom / half page'),
    ('o / a', 'links / save attachments'),
    ('q / Esc', 'back'),
    ('', ''),
    ('Anywhere', ''),
    ('Ctrl+P', 'palette — every command, account, folder, theme'),
    ('? / F1', 'this help'),
    ('Ctrl+L', 'logout'),
    ('q', 'quit from the mailbox (asks first)'),
    ('Ctrl+Q', 'quit immediately'),
]


def boxed(label, value):
    """Checkbox label with an unambiguous state glyph — Textual's toggle draws
    the same X in both states and only changes its color."""
    return ('☑ ' if value else '☐ ') + label


def rebox(checkbox, value):
    base = str(getattr(checkbox.label, 'plain', checkbox.label))
    if base[:2] in ('☑ ', '☐ '):
        base = base[2:]
    checkbox.label = boxed(base, value)


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


def complete_path(value):
    """Shell-style Tab completion for a filesystem path."""
    expanded = os.path.expanduser(value) or '.'
    base, prefix = os.path.split(expanded)
    try:
        entries = [e for e in os.listdir(base or '.')
                   if e.lower().startswith(prefix.lower())]
    except OSError:
        return value
    if not entries:
        return value
    if len(entries) == 1:
        full = os.path.join(base, entries[0])
        return full + os.sep if os.path.isdir(full) else full
    common = os.path.commonprefix(entries)
    return os.path.join(base, common) if len(common) > len(prefix) else value


def account_label(name, color, address=''):
    t = Text.assemble(('● ', color), name)
    if address:
        t.append(f'  <{address}>', 'dim')
    return t


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
    BINDINGS = [
        Binding('escape,q,question_mark,f1', 'close', 'Close'),
        Binding('j', 'scroll(2)', 'Down', show=False),
        Binding('k', 'scroll(-2)', 'Up', show=False),
        Binding('space', 'scroll(12)', 'Page', show=False),
    ]

    def compose(self):
        rt = RichTable(box=None, padding=(0, 2, 0, 0),
                       title=f'Keyboard reference — tuimail {__version__}',
                       title_style='bold cyan', title_justify='left')
        rt.add_column('Key', style='bold yellow')
        rt.add_column('Action')
        for key, action in HELP_ROWS:
            if key and not action:
                rt.add_row(Text(key, style='bold cyan underline'), '')
            else:
                rt.add_row(key, action)
        with VerticalScroll(id='help-card'):  # scrolls on short terminals
            yield Static(rt)

    def on_mount(self):
        self.query_one('#help-card', VerticalScroll).focus()

    def action_scroll(self, dy):
        self.query_one('#help-card', VerticalScroll).scroll_relative(y=dy, animate=False)

    def action_close(self):
        self.dismiss()


class FilePickScreen(ModalScreen):
    """Attach-file dialog: browse the tree, or type a path (Tab completes)."""
    BINDINGS = [Binding('escape', 'close', 'Cancel')]

    def __init__(self, start_dir):
        super().__init__()
        self.start_dir = start_dir

    def compose(self):
        with Vertical(id='filepick-card'):
            yield Label('Attach a file — Enter picks, Tab completes the path, Esc cancels',
                        classes='card-title')
            yield Input(value=str(self.start_dir) + os.sep, id='path',
                        placeholder='Type a path — Tab completes like your shell')
            yield DirectoryTree(str(self.start_dir), id='ftree')

    def on_mount(self):
        inp = self.query_one('#path', Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    def on_key(self, event):
        inp = self.query_one('#path', Input)
        if event.key == 'tab' and inp.has_focus:
            inp.value = complete_path(inp.value)
            inp.cursor_position = len(inp.value)
            event.stop()
            event.prevent_default()

    def on_input_submitted(self, event):
        p = Path(os.path.expanduser(event.value.strip().strip('"')))
        if p.is_file():
            self.dismiss(p)
        elif p.is_dir():
            self.query_one('#ftree', DirectoryTree).path = p
        else:
            self.app.notify('No such file', severity='warning')

    def on_directory_tree_file_selected(self, event):
        self.dismiss(Path(event.path))

    def action_close(self):
        self.dismiss(None)


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


# --- onboarding / accounts / login --------------------------------------------
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
            self.app.switch_screen(AccountFormScreen(None))
        else:
            self.app.session = be.demo_session()
            self.app.switch_screen(MainScreen())


class AccountFormScreen(Screen):
    """Add (index=None) or edit (index=i) one account."""
    BINDINGS = [
        Binding('escape', 'back', 'Back'),
        Binding('ctrl+s', 'save', 'Save', priority=True),
        Binding('question_mark,f1', 'help', 'Help', show=False),
    ]

    def __init__(self, index):
        super().__init__()
        self.index = index

    def compose(self):
        cfg = be.load_config()
        with VerticalScroll(classes='card form-card'):
            yield Label('Edit account' if self.index is not None else 'Add account',
                        classes='card-title')
            yield Input(placeholder='Account name (e.g. personal, work)', id='name')
            yield Select([(n, n) for n in be.PROVIDERS], prompt='Choose a provider', id='provider')
            yield Input(placeholder='Email address', id='address')
            yield Input(placeholder='IMAP host (host or host:port, SSL)', id='imap')
            yield Input(placeholder='SMTP host (host or host:port)', id='smtp')
            yield Select([(Text.assemble(('● ', c), c), c) for c in be.DEFAULT_COLORS],
                         value=be.next_color(cfg), allow_blank=False, id='color')
            yield Static('Password — optional. Blank: asked at sign-in. '
                         'Filled: stored on this machine as plain text.', classes='hint')
            yield Input(placeholder='Password', password=True, id='password')
            if (self.index is not None and self.index < len(cfg.get('accounts', []))
                    and cfg['accounts'][self.index].get('password')):
                yield Checkbox(boxed('Forget the saved password', False), id='forget')
            yield Static('Pick a provider to fill the servers in, or type your own.',
                         id='provider-hint', classes='hint')
            with Horizontal(classes='buttons'):
                yield Button('Save', variant='primary', id='save')
                yield Button('Back', id='back')

    def on_mount(self):
        self.query_one('.form-card').can_focus = False  # not a Tab stop
        if self.index is not None:
            accts = be.load_config().get('accounts', [])
            if 0 <= self.index < len(accts):
                a = accts[self.index]
                self.query_one('#name', Input).value = a.get('name', '')
                self.query_one('#address', Input).value = a.get('address', '')
                self.query_one('#imap', Input).value = a.get('imap_host', '')
                self.query_one('#smtp', Input).value = a.get('smtp_host', '')
                if a.get('color') in be.DEFAULT_COLORS:
                    self.query_one('#color', Select).value = a['color']
                # keep the provider context (and its app-password hint) on edit
                for pname, p in be.PROVIDERS.items():
                    if p['imap'] and p['imap'] == a.get('imap_host'):
                        self.query_one('#provider', Select).value = pname
                        break
        self.query_one('#name', Input).focus()

    def on_select_changed(self, event):
        if event.select.id != 'provider' or event.value in (None, Select.BLANK):
            return
        p = be.PROVIDERS[event.value]
        self.query_one('#imap', Input).value = p['imap']
        self.query_one('#smtp', Input).value = p['smtp']
        self.query_one('#provider-hint', Static).update(p['hint'])

    def on_checkbox_changed(self, event):
        rebox(event.checkbox, event.value)

    def on_input_submitted(self, event):
        self.action_save()

    def action_help(self):
        self.app.push_screen(HelpScreen())

    def action_back(self):
        self.app.switch_screen(
            AccountsScreen() if be.load_config().get('accounts') else OnboardingScreen())

    def on_button_pressed(self, event):
        if event.button.id == 'back':
            self.action_back()
        else:
            self.action_save()

    def action_save(self):
        address = self.query_one('#address', Input).value.strip()
        if '@' not in address:
            self.app.notify('Enter a valid email address', severity='warning')
            return
        domain = address.rsplit('@', 1)[-1]
        cfg = be.load_config()
        accts = cfg.setdefault('accounts', [])
        editing = self.index is not None and self.index < len(accts)
        old = accts[self.index] if editing else {}
        name = self.query_one('#name', Input).value.strip() or address.split('@')[0]
        others = {a.get('name') for i, a in enumerate(accts)
                  if not (editing and i == self.index)}
        base, n = name, 2
        while name in others:  # the name routes every operation — keep it unique
            name = f'{base}{n}'
            n += 1
        acct = {
            'name': name,
            'address': address,
            'imap_host': self.query_one('#imap', Input).value.strip() or f'imap.{domain}',
            'smtp_host': self.query_one('#smtp', Input).value.strip() or f'smtp.{domain}',
            'color': self.query_one('#color', Select).value,
        }
        pw = self.query_one('#password', Input).value
        forget = False
        try:
            forget = self.query_one('#forget', Checkbox).value
        except NoMatches:
            pass
        if pw:
            acct['password'] = pw
        elif old.get('password') and not forget:
            acct['password'] = old['password']  # blank keeps the saved one
        if editing:
            accts[self.index] = acct
        else:
            accts.append(acct)  # a stale edit index degrades to an add, never a crash
        if not be.save_config(cfg):
            self.app.notify('Could not write the settings file — check permissions',
                            severity='error')
            return
        self.app.notify(f'Account {acct["name"]} saved')
        self.app.switch_screen(LoginScreen() if len(accts) == 1 else AccountsScreen())


class AccountsScreen(Screen):
    BINDINGS = [
        Binding('escape', 'done', 'Done'),
        Binding('a', 'add', 'Add', show=False),
        Binding('e', 'edit', 'Edit', show=False),
        Binding('x', 'remove', 'Remove', show=False),
        Binding('question_mark,f1', 'help', 'Help', show=False),
    ]

    def compose(self):
        with VerticalScroll(classes='card'):
            yield Label('Accounts', classes='card-title')
            yield ListView(id='acctlist')
            with Horizontal(classes='buttons'):
                yield Button('Add (a)', variant='primary', id='add')
                yield Button('Edit (e)', id='edit')
                yield Button('Remove (x)', variant='error', id='remove')
                yield Button('Done (Esc)', id='done')
            yield Static('Each account gets a color — shown next to its mail everywhere. '
                         'Enter on an account edits it.', classes='hint')

    def on_mount(self):
        self.query_one('.card').can_focus = False
        self.refresh_list()
        self.query_one('#acctlist', ListView).focus()

    def on_list_view_selected(self, event):
        self.action_edit()

    def action_help(self):
        self.app.push_screen(HelpScreen())

    def action_add(self):
        self.app.switch_screen(AccountFormScreen(None))

    def action_edit(self):
        idx = self.query_one('#acctlist', ListView).index
        accts = be.load_config().get('accounts', [])
        if idx is not None and 0 <= idx < len(accts):
            self.app.switch_screen(AccountFormScreen(idx))

    def action_remove(self):
        accts = be.load_config().get('accounts', [])
        idx = self.query_one('#acctlist', ListView).index
        if idx is None or not (0 <= idx < len(accts)):
            return
        target = dict(accts[idx])  # remove by identity, not by (possibly stale) index

        def done(ok):
            if not ok:
                return
            cfg = be.load_config()
            lst = cfg.get('accounts', [])
            for i, a in enumerate(lst):
                if ((a.get('address'), a.get('name'))
                        == (target.get('address'), target.get('name'))):
                    lst.pop(i)
                    be.save_config(cfg)
                    self.app.notify(f'Removed {target.get("address", "?")}')
                    break
            else:
                self.app.notify('That account was already removed', severity='warning')
            self.refresh_list()
        self.app.push_screen(
            ConfirmScreen(f'Remove account {target.get("address", "?")}?'), done)

    def action_done(self):
        self.app.switch_screen(
            LoginScreen() if be.load_config().get('accounts') else OnboardingScreen())

    def refresh_list(self):
        lv = self.query_one('#acctlist', ListView)
        lv.clear()
        for a in be.load_config().get('accounts', []):
            t = account_label(a.get('name', '?'), a.get('color', 'white'), a.get('address', ''))
            t.append('  · password saved' if a.get('password') else '  · asks at sign-in', 'dim')
            lv.append(ListItem(Label(t)))
        lv.index = 0

    def on_button_pressed(self, event):
        {'add': self.action_add, 'edit': self.action_edit,
         'remove': self.action_remove, 'done': self.action_done}[event.button.id]()


class LoginScreen(Screen):
    BINDINGS = [Binding('question_mark,f1', 'help', 'Help', show=False)]

    def action_help(self):
        self.app.push_screen(HelpScreen())

    def on_checkbox_changed(self, event):
        rebox(event.checkbox, event.value)

    def on_mount(self):
        # every password saved -> no reason to make the user press Sign in;
        # the app-level latch keeps an explicit logout sticky across every
        # path back here (Manage accounts, account edits, ...)
        if (getattr(self.app, 'auto_login', True) and self._accounts
                and all(a.get('password') for a in self._accounts)):
            self._start_signin()

    def compose(self):
        self._accounts = be.load_config().get('accounts', [])
        with Vertical(classes='card'):
            yield Static(LOGO, id='logo')
            yield Static('mail, comfortably, in your terminal', classes='tagline')
            for i, a in enumerate(self._accounts):
                row = account_label(a.get('name', '?'), a.get('color', 'white'),
                                    a.get('address', ''))
                # always show where the password will be sent — makes a planted
                # config with a hostile imap_host visible before sign-in
                host = a.get('imap_host') or 'imap.' + a.get('address', '@?').rsplit('@', 1)[-1]
                row.append(f'  →  {host}', 'dim')
                yield Static(row, classes='acct-row')
                if not a.get('password'):
                    yield Input(placeholder=f'Password for {a.get("address", "")}',
                                password=True, id=f'pw-{i}')
            if not self._accounts:
                yield Static('No accounts configured yet.', classes='hint')
            if any(not a.get('password') for a in self._accounts):
                yield Checkbox(boxed('Remember typed passwords (stored as plain text)', False),
                               id='remember')
            with Horizontal(classes='buttons'):
                yield Button('Sign in', variant='primary', id='signin',
                             disabled=not self._accounts)
                yield Button('Demo mailbox', id='demo')
                yield Button('Manage accounts', id='manage')
            yield Static('', id='login-status')

    def on_button_pressed(self, event):
        if event.button.id == 'demo':
            self.app.session = be.demo_session()
            self.app.switch_screen(MainScreen())
        elif event.button.id == 'manage':
            self.app.switch_screen(
                AccountsScreen() if self._accounts else AccountFormScreen(None))
        elif event.button.id == 'signin':
            self._start_signin()

    def on_input_submitted(self, event):
        # several accounts without saved passwords: Enter moves to the next
        # empty password field, and only the last one submits
        for i in range(len(self._accounts)):
            try:
                inp = self.query_one(f'#pw-{i}', Input)
            except NoMatches:
                continue
            if not inp.value and inp is not event.input:
                inp.focus()
                return
        self._start_signin()

    def _start_signin(self):
        cfg = be.load_config()
        accts = cfg.get('accounts', [])
        if not accts:
            return
        creds = []
        for i, a in enumerate(accts):
            pw = a.get('password', '')
            if not pw:
                try:
                    pw = self.query_one(f'#pw-{i}', Input).value
                except NoMatches:
                    pw = ''
            if not pw:
                self.app.notify(f'Password needed for {a.get("address", "?")}',
                                severity='warning')
                return
            creds.append((a, pw))
        self.query_one('#signin', Button).disabled = True
        try:
            remember = self.query_one('#remember', Checkbox).value
        except NoMatches:
            remember = False
        self._connect(creds, cfg, remember)

    @work(thread=True, exclusive=True, group='login')
    def _connect(self, creds, cfg, remember):
        app = self.app
        good, bad = [], []
        for a, pw in creds:
            address = a.get('address', '')
            domain = address.rsplit('@', 1)[-1]
            app.call_from_thread(self._status, f'Connecting {address} …')
            try:
                backend = be.ImapBackend(address, pw,
                                         a.get('imap_host') or f'imap.{domain}',
                                         a.get('smtp_host') or f'smtp.{domain}')
            except Exception as exc:
                bad.append(f'{address}: {be.err_text(exc)}')
                continue
            good.append((a, pw, backend))
        app.call_from_thread(self._done, good, bad, cfg, remember)

    @ui_callback
    def _status(self, text):
        self.query_one('#login-status', Static).update(text)

    @ui_callback
    def _done(self, good, bad, cfg, remember):
        self.query_one('#signin', Button).disabled = False
        for msg in bad:
            self.app.notify(msg, severity='error', timeout=8)
        if not good:
            self._status(Text('✗ no account could connect', style='bold red'))
            return
        if remember:
            if be.portable_mode():
                self.app.notify('Portable mode: passwords are never stored next to the exe',
                                severity='warning')
            for a, pw, _ in good:
                a['password'] = pw  # `a` is the dict inside cfg['accounts']; portable save strips it
            if not be.save_config(cfg):
                self.app.notify('Could not write the settings file — passwords not saved',
                                severity='warning')
        self.app.session = be.Session([
            be.Account(a.get('name') or a.get('address', '?'),
                       a.get('color', be.DEFAULT_COLORS[0]), bk)
            for a, _, bk in good])
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
        Binding('space', 'toggle_select', 'Select', show=False),
        Binding('ctrl+a', 'select_all', 'Select all', show=False),
        Binding('J', 'scroll_preview(3)', 'Preview down', show=False),
        Binding('K', 'scroll_preview(-3)', 'Preview up', show=False),
        Binding('p', 'toggle_preview', 'Preview', show=False),
        Binding('slash', 'search', 'Search', key_display='/'),
        Binding('escape', 'cancel_mode', 'Cancel', show=False),
        Binding('R', 'refresh', 'Refresh'),
        Binding('question_mark', 'help', 'Help', key_display='?'),
        Binding('q', 'quit_app', 'Quit'),
    ] + [Binding(str(n), f'scope_key({n})', 'Account', show=False) for n in range(10)]

    def compose(self):
        yield Header(show_clock=True)
        with Horizontal(id='main-split'):
            with Vertical(id='sidebar'):
                yield Label('ACCOUNTS', classes='sidebar-title')
                yield ListView(id='accounts')
                yield Label('MAILBOXES', classes='sidebar-title')
                yield ListView(id='folders')
            with Vertical(id='content'):
                yield Input(placeholder='Search this folder — Enter filters, Esc clears',
                            id='search')
                yield DataTable(id='msgtable')
                with VerticalScroll(id='preview-scroll'):
                    yield Static(id='preview')
        yield Static(id='selbar')
        yield Footer()

    def on_mount(self):
        self.scope = None  # account name, or None = all accounts merged
        self.folder = 'INBOX'
        self.all_msgs, self.view = [], []
        self.filter_uids = None  # set of (account, uid)
        self.selected = set()  # selection mode: set of (account, uid)
        self.folder_counts = []
        self._cache = {}
        self._pv_timer = None
        self._preview_key = None  # (account, uid) currently shown in the preview
        self._seq = 0  # bumped on every optimistic local change; stale loads are dropped
        table = self.query_one('#msgtable', DataTable)
        table.cursor_type = 'row'
        table.cursor_background_priority = 'css'  # cursor stays visible over selection tint
        table.loading = True
        # the preview is scrolled from the list (J/K) — not a Tab stop
        self.query_one('#preview-scroll', VerticalScroll).can_focus = False
        accts = self.app.session.accounts
        self.app.sub_title = (accts[0].backend.address if len(accts) == 1
                              else f'{len(accts)} accounts')
        self.update_accounts()
        self._apply_layout()
        self.set_interval(60, partial(self._load_all, False))  # quiet background poll
        self._load_all(True)
        self.app.maybe_offer_cli()

    # -- responsive layout --
    def on_resize(self, event):
        self._apply_layout()

    def _apply_layout(self):
        width = self.size.width
        self.query_one('#sidebar').display = width >= 90  # palette still switches
        if self.view:
            self.rebuild_table(keep_cursor=True)

    def _column_widths(self):
        width = self.size.width
        avail = width - (28 if width >= 90 else 0) - 4  # sidebar + borders
        when = 10 if avail >= 70 else 6
        frm = max(12, min(26, avail // 4))
        subject = max(12, avail - 2 - frm - when - 8)  # 8 = cell padding x4 columns
        return frm, subject, when

    # -- data loading --
    @work(thread=True, exclusive=True, group='load')
    def _load_all(self, focus=False):
        app = self.app
        seq, scope, folder = self._seq, self.scope, self.folder
        try:
            counts = app.session.folders(scope)
            msgs = app.session.list_messages(folder, scope)
        except Exception as exc:
            app.call_from_thread(self._load_failed, exc)
            return
        app.call_from_thread(self._loaded, counts, msgs, seq, scope, folder, focus)

    @work(thread=True, exclusive=True, group='load')
    def _load_folder(self, folder):
        app = self.app
        seq, scope = self._seq, self.scope
        try:
            msgs = app.session.list_messages(folder, scope)
        except Exception as exc:
            app.call_from_thread(self._load_failed, exc)
            return
        app.call_from_thread(self._folder_loaded, folder, msgs, seq, scope)

    @ui_callback
    def _load_failed(self, exc):
        self.query_one('#msgtable', DataTable).loading = False
        self.app.notify(str(exc), severity='error', title='Mail')

    @ui_callback
    def _loaded(self, counts, msgs, seq, scope, folder, focus):
        table = self.query_one('#msgtable', DataTable)
        if seq != self._seq or scope != self.scope or folder != self.folder:
            return  # stale snapshot; leave the spinner to the load that superseded us
        table.loading = False
        self.folder_counts = counts
        self._cache.clear()
        self.all_msgs = msgs
        self.update_sidebar()
        self.apply_filter(keep_cursor=True)
        if focus:
            table.focus()

    @ui_callback
    def _folder_loaded(self, folder, msgs, seq, scope):
        if folder != self.folder or seq != self._seq or scope != self.scope:
            return
        self._cache.clear()
        self.all_msgs = msgs
        self.apply_filter()
        table = self.query_one('#msgtable', DataTable)
        table.loading = False
        table.focus()

    def _fetch_cached(self, s, folder):
        # folder is captured when the action fires — self.folder may change
        # while the worker thread is still in flight
        key = (s.account, folder, s.uid)
        if key not in self._cache:
            self._cache[key] = self.app.session.fetch(s.account, folder, s.uid)
        return self._cache[key]

    # -- rendering --
    def update_accounts(self):
        lv = self.query_one('#accounts', ListView)
        lv.clear()
        session = self.app.session
        rows = []
        if len(session.accounts) > 1:
            rows.append((None, Text.assemble(('● ', 'dim'), 'All accounts')))
        for a in session.accounts:
            rows.append((a.name, account_label(a.name, a.color)))
        idx = 0
        for i, (value, label) in enumerate(rows):
            marker = Text('▸ ' if value == self.scope else '  ')
            marker.append(label)
            item = ListItem(Label(marker))
            item.scope_value = value
            lv.append(item)
            if value == self.scope:
                idx = i
        lv.index = idx

    def update_sidebar(self):
        lv = self.query_one('#folders', ListView)
        lv.clear()
        idx = 0
        for i, (name, unread) in enumerate(self.folder_counts):
            label = Text(f'{"▸" if name == self.folder else " "} {be.decode_folder(name)}')
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
                     if self.filter_uids is None or (s.account, s.uid) in self.filter_uids]
        # selection only ever covers visible rows — a hidden selected message
        # must not be silently acted on
        self.selected &= {(s.account, s.uid) for s in self.view}
        self.update_selection_ui()
        self.rebuild_table(keep_cursor)
        if not self.view:
            self._set_preview(Text('Nothing here — the folder is empty or no matches.',
                                   style='dim'))

    def _sel_bg(self):
        """Selection tint derived from the active theme, so light themes stay
        readable; falls back to a fixed dark blue."""
        try:
            from textual.color import Color
            theme = self.app.current_theme
            base = Color.parse(theme.surface or theme.background or '#1e2030')
            return ' on ' + base.blend(Color.parse(theme.accent), 0.35).hex
        except Exception:
            return ' on #2d3f76'

    def rebuild_table(self, keep_cursor=False):
        table = self.query_one('#msgtable', DataTable)
        cur_row = table.cursor_row if keep_cursor else 0
        cur_key = None
        if keep_cursor and 0 <= table.cursor_row < table.row_count:
            # read the identity from the table itself — self.view may already
            # hold the new ordering by the time we get here
            try:
                from textual.coordinate import Coordinate
                cur_key = table.coordinate_to_cell_key(
                    Coordinate(table.cursor_row, 0)).row_key.value
            except Exception:
                cur_key = None
        table.clear(columns=True)
        frm_w, subj_w, when_w = self._column_widths()
        sent_like = 'sent' in be.decode_folder(self.folder).lower()
        table.add_column(' ', width=2)
        table.add_column('To' if sent_like else 'From', width=frm_w)
        table.add_column('Subject', width=subj_w)
        table.add_column('When', width=when_w)
        session = self.app.session
        sel_bg = self._sel_bg()
        for s in self.view:
            sel = (s.account, s.uid) in self.selected
            bg = sel_bg if sel else ''
            style = ('bold' if s.unread else '') + bg
            # one circle, colored by account: filled = unread, hollow = read
            icons = Text.assemble(
                ('●' if s.unread else '○', session.color(s.account) + bg),
                ('★' if s.flagged else ' ', 'yellow' + bg),
            )
            who = Text((s.recipient if sent_like else s.sender) or s.sender, style=style)
            who.truncate(frm_w, overflow='ellipsis')
            subj = Text(s.subject, style=style)
            subj.truncate(subj_w, overflow='ellipsis')
            when = nice_date(s.date)
            if len(when) > when_w:
                when = when[-when_w:]  # '2026-09-02' -> '09-02' in narrow panes
            table.add_row(icons, who, subj, Text(when, style='dim' + bg),
                          key=f'{s.account}/{s.uid}')
        if self.view:
            # restore by message identity: a poll that inserts new mail must not
            # leave the cursor on a different message at the same row number
            idx = next((i for i, s in enumerate(self.view)
                        if f'{s.account}/{s.uid}' == cur_key), None)
            if idx is None:
                idx = max(0, min(cur_row, len(self.view) - 1))
            table.move_cursor(row=idx)

    # -- selection mode --
    def _selected_msgs(self):
        return [s for s in self.view if (s.account, s.uid) in self.selected]

    def update_selection_ui(self):
        n = len(self.selected)
        bar = self.query_one('#selbar', Static)
        footer = self.query_one(Footer)
        if n:
            bar.update(Text.assemble(
                (f' {n} selected ', 'bold'),
                ('  Space toggle · ', ''),
                (f'd delete ({n})', 'bold'),
                (' · u mark read/unread · s star · Esc cancel', ''),
            ))
        bar.display = bool(n)
        footer.display = not n

    def _table_focused(self):
        """Message-list actions only fire when the list itself has focus — a
        key pressed in the sidebar must never act on an invisible cursor."""
        return self.query_one('#msgtable', DataTable).has_focus

    def action_toggle_select(self):
        if not self._table_focused():
            return
        s = self.current()
        if not s:
            return
        key = (s.account, s.uid)
        table = self.query_one('#msgtable', DataTable)
        if key in self.selected and table.cursor_row == len(self.view) - 1:
            pass  # last row on key-repeat: stay selected instead of flapping
        else:
            self.selected.symmetric_difference_update({key})
        self.update_selection_ui()
        self.rebuild_table(keep_cursor=True)
        self.action_move(1)  # advance like a file manager

    def action_select_all(self):
        if not self._table_focused() or not self.view:
            return
        keys = {(s.account, s.uid) for s in self.view}
        self.selected = set() if self.selected == keys else keys
        self.update_selection_ui()
        self.rebuild_table(keep_cursor=True)

    def action_scroll_preview(self, dy):
        self.query_one('#preview-scroll', VerticalScroll).scroll_relative(y=dy, animate=False)

    def action_toggle_preview(self):
        pane = self.query_one('#preview-scroll', VerticalScroll)
        pane.display = not pane.display

    def action_scope_key(self, n):
        accts = self.app.session.accounts
        if n == 0:
            self.set_scope(None if len(accts) > 1 else accts[0].name)
        elif n <= len(accts):
            self.set_scope(accts[n - 1].name)

    def action_cancel_mode(self):
        if self.selected:
            self.selected.clear()
            self.update_selection_ui()
            self.rebuild_table(keep_cursor=True)
            return
        self.action_clear_search()

    @ui_callback
    def _set_preview(self, content, key=None):
        self.preview_text = getattr(content, 'plain', str(content))
        self._preview_key = key
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
            s, folder = self.view[event.cursor_row], self.folder
            if (s.account, s.uid) == self._preview_key:
                return  # same message (table was rebuilt) — keep the preview and its scroll
            self._pv_timer = self.set_timer(0.25, lambda: self._load_preview(s, folder))

    @work(thread=True, exclusive=True, group='preview')
    def _load_preview(self, s, folder):
        app = self.app
        try:
            msg = self._fetch_cached(s, folder)
        except be.MailGone:
            app.call_from_thread(self._set_preview,
                                 Text('Message no longer exists on the server.', style='dim'))
            return
        except Exception as exc:
            app.call_from_thread(self._set_preview, Text(f'Preview failed: {exc}', style='dim'))
            return
        t = Text()
        t.append(f'{s.subject}\n', style='bold')
        t.append(f'{s.sender} · {nice_date(s.date)}  ', style='dim')
        t.append('● ', style=app.session.color(s.account))
        t.append(f'{s.account}\n\n', style='dim')
        t.append(be.body_of(msg)[:4000])
        app.call_from_thread(self._set_preview, t, (s.account, s.uid))

    # -- accounts & folders --
    def on_list_view_selected(self, event):
        if event.list_view.id == 'accounts':
            if hasattr(event.item, 'scope_value') and event.item.scope_value != self.scope:
                self.set_scope(event.item.scope_value)
        elif event.list_view.id == 'folders':
            name = getattr(event.item, 'folder_name', None)
            if name and name != self.folder:
                self.goto_folder(name)

    def _leave_view(self):
        """Folder/scope is changing: drop search and selection. Selection keys are
        (account, uid) and IMAP uids are only unique per folder — a surviving
        selection could target unrelated mail in the next folder."""
        self.filter_uids = None
        self.selected.clear()
        self.update_selection_ui()
        inp = self.query_one('#search', Input)
        inp.value = ''
        inp.remove_class('visible')
        self.query_one('#msgtable', DataTable).loading = True

    def set_scope(self, scope):
        if scope == self.scope:
            return
        self.scope = scope
        self.folder = 'INBOX'
        self._leave_view()
        self.update_accounts()
        self._load_all(True)

    def goto_folder(self, name):
        self.folder = name
        self._leave_view()
        self.update_sidebar()
        self._load_folder(name)

    def _adjust_unread(self, folder, delta):
        self.folder_counts = [(n, max(0, c + delta) if n == folder else c)
                              for n, c in self.folder_counts]
        self.update_sidebar()

    # -- open / reply --
    def on_data_table_row_selected(self, event):
        if self.selected:  # selection mode: Enter/click toggles, never opens
            self.action_toggle_select()
            return
        s = self.current()
        if s:
            self._open(s, self.folder)

    @work(thread=True, exclusive=True, group='open')
    def _open(self, s, folder):
        app = self.app
        try:
            msg = self._fetch_cached(s, folder)
        except be.MailGone:
            app.call_from_thread(self._gone, s, folder)
            return
        except Exception as exc:
            app.call_from_thread(app.notify, str(exc), severity='error')
            return
        if s.unread:
            try:
                app.session.mark(s.account, folder, s.uid, read=True)
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
    def _gone(self, s, folder):
        self.app.notify('That message no longer exists on the server', severity='warning')
        self._seq += 1
        if s in self.all_msgs:
            self.all_msgs.remove(s)
        self._cache.pop((s.account, folder, s.uid), None)
        if s.unread and folder == self.folder:
            self._adjust_unread(folder, -1)
        self.apply_filter(keep_cursor=True)

    def action_reply(self):
        if self.selected:
            self.app.notify('Replying needs a single message — Esc to leave selection')
            return
        s = self.current()
        if s:
            self._reply(s, self.folder)

    @work(thread=True, exclusive=True, group='open')
    def _reply(self, s, folder):
        app = self.app
        try:
            msg = self._fetch_cached(s, folder)
        except be.MailGone:
            app.call_from_thread(self._gone, s, folder)
            return
        except Exception as exc:
            app.call_from_thread(app.notify, str(exc), severity='error')
            return
        seed = be.reply_seed(msg)
        seed['account'] = s.account
        app.call_from_thread(self.app.push_screen, ComposeScreen(seed))

    # -- generic fire-and-forget backend IO with optimistic UI --
    @work(thread=True, group='io')
    def _io(self, fn, reload=False):
        try:
            fn()
        except Exception as exc:
            self.app.call_from_thread(self.app.notify, be.err_text(exc),
                                      severity='error', title='Mail')
        if reload:
            self.app.call_from_thread(self._after_bulk)

    def _after_bulk(self):
        # a poll that read the mailbox mid-way through N server commands may
        # have passed the seq guard with a half-applied snapshot — invalidate
        # it and reconcile with the server now rather than at the next poll
        self._seq += 1
        self._load_all(False)

    # -- actions --
    def action_move(self, delta):
        focused = self.app.focused
        if isinstance(focused, ListView):  # j/k drive whichever list has focus
            (focused.action_cursor_down if delta > 0 else focused.action_cursor_up)()
            return
        table = self.query_one('#msgtable', DataTable)
        if table.row_count:
            table.move_cursor(row=max(0, min(table.cursor_row + delta, table.row_count - 1)))

    def action_top(self):
        self.action_move(-len(self.view))

    def action_bottom(self):
        self.action_move(len(self.view))

    def action_compose(self):
        # merged view: the highlighted message's account is the least surprising From
        s = self.current()
        account = self.scope or (s.account if s else None)
        self.app.push_screen(ComposeScreen({'account': account}))

    def action_toggle_read(self):
        if not self._table_focused():
            return
        targets = self._selected_msgs() or ([self.current()] if self.current() else [])
        if not targets:
            return
        make_unread = not any(s.unread for s in targets)  # any unread -> read all
        session, folder = self.app.session, self.folder
        changed = [s for s in targets if s.unread != make_unread]
        if not changed:
            return
        self._seq += 1
        for s in changed:
            s.unread = make_unread
        ops = [(s.account, s.uid) for s in changed]

        def store():
            for account, uid in ops:
                session.mark(account, folder, uid, read=not make_unread)
        self._io(store, reload=len(ops) > 1)
        self._adjust_unread(folder, len(changed) if make_unread else -len(changed))
        self.rebuild_table(keep_cursor=True)

    def action_toggle_flag(self):
        if not self._table_focused():
            return
        targets = self._selected_msgs() or ([self.current()] if self.current() else [])
        if not targets:
            return
        make_flagged = not all(s.flagged for s in targets)  # any unstarred -> star all
        session, folder = self.app.session, self.folder
        changed = [s for s in targets if s.flagged != make_flagged]
        if not changed:
            return
        self._seq += 1
        for s in changed:
            s.flagged = make_flagged
        ops = [(s.account, s.uid) for s in changed]

        def store():
            for account, uid in ops:
                session.flag(account, folder, uid, flagged=make_flagged)
        self._io(store, reload=len(ops) > 1)
        self.rebuild_table(keep_cursor=True)

    def action_delete(self):
        if not self._table_focused():
            return
        targets = self._selected_msgs()
        if targets:
            self.app.push_screen(
                ConfirmScreen(f'Delete {len(targets)} selected message(s)?'),
                lambda ok: self.do_delete_many(targets) if ok else None)
            return
        s = self.current()
        if not s:
            return
        self.app.push_screen(ConfirmScreen(f'Delete "{s.subject[:48]}"?'),
                             lambda ok: self.do_delete(s) if ok else None)

    def do_delete_many(self, targets):
        self._seq += 1
        session, folder = self.app.session, self.folder
        ops = [(s.account, s.uid) for s in targets]

        def rm():
            failed = 0
            for account, uid in ops:
                try:
                    session.delete(account, folder, uid)
                except Exception:
                    failed += 1
            if failed:
                raise RuntimeError(f'{failed} of {len(ops)} deletions failed — refresh (R)')
        self._io(rm, reload=True)
        unread_gone = 0
        for s in targets:
            if s in self.all_msgs:
                self.all_msgs.remove(s)
            self._cache.pop((s.account, folder, s.uid), None)
            unread_gone += s.unread
        if unread_gone:
            self._adjust_unread(folder, -unread_gone)
        self.selected.clear()
        self.apply_filter(keep_cursor=True)
        self.app.notify(f'Deleted {len(targets)}')

    def do_delete(self, s):
        self._seq += 1
        self._io(partial(self.app.session.delete, s.account, self.folder, s.uid))
        if s in self.all_msgs:
            self.all_msgs.remove(s)
        self._cache.pop((s.account, self.folder, s.uid), None)
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
        # q is "back" one screen deeper — never let a stray press kill the session
        if self.selected or self.filter_uids is not None:
            self.action_cancel_mode()
            return
        self.app.push_screen(ConfirmScreen('Quit tuimail? (Ctrl+Q quits without asking)'),
                             lambda ok: self.app.exit() if ok else None)

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
        scope = self.scope
        try:
            hits, fallback = app.session.search(folder, q, scope)
        except Exception:
            hits, fallback = set(), [a.name for a in app.session.scoped(scope)]
        if fallback:  # accounts whose server can't search — filter what we have
            ql = q.lower()
            hits |= {(s.account, s.uid) for s in self.all_msgs
                     if s.account in fallback
                     and (ql in s.sender.lower() or ql in s.subject.lower())}
        app.call_from_thread(self._search_done, folder, q, hits, scope)

    @ui_callback
    def _search_done(self, folder, q, hits, scope):
        if folder != self.folder or scope != self.scope:
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
        self.markdown_source = be.body_markdown(msg)
        self.account_line = ''

    def compose(self):
        yield Header()
        with VerticalScroll(id='reader-scroll'):
            yield Static(id='reader-body')
            yield Markdown(id='reader-md')
        yield Footer()

    def on_mount(self):
        session = self.app.session
        t = Text()
        t.append(f'{be.sanitize(self.msg.get("Subject") or "(no subject)", keep_newlines=False)}\n\n',
                 style='bold')
        for label in ('From', 'To', 'Cc', 'Date'):
            v = self.msg.get(label)
            if v:
                t.append(f'{label:>8}: {be.sanitize(v, keep_newlines=False)}\n', style='dim')
        try:
            self.account_line = f'{self.summary.account} <{session.address(self.summary.account)}>'
            t.append(f'{"Account":>8}: ', style='dim')
            t.append('● ', style=session.color(self.summary.account))
            t.append(self.account_line + '\n', style='dim')
        except StopIteration:
            pass
        atts = be.attachments_of(self.msg)
        links = be.extract_links(self.msg)
        extras = []
        if atts:
            extras.append(f'📎 {len(atts)} attachment(s) — press a to save')
        if links:
            extras.append(f'🔗 {len(links)} link(s) — press o to open')
        if extras:
            t.append('\n' + '\n'.join(extras) + '\n', style='italic cyan')
        t.append('\n' + '─' * 72 + '\n', style='dim')
        md = self.query_one('#reader-md', Markdown)
        if self.markdown_source:
            md.update(self.markdown_source)
        else:
            md.display = False
            t.append('\n')
            t.append(self.body_text)
        self.query_one('#reader-body', Static).update(t)
        self.query_one('#reader-scroll', VerticalScroll).focus()

    def on_markdown_link_clicked(self, event):
        href = str(getattr(event, 'href', '') or '')
        event.prevent_default()
        if href.startswith(('http://', 'https://')):
            webbrowser.open(href)
            self.app.notify(f'Opened {href[:60]}')
        else:
            self.app.notify('Only web links can be opened', severity='warning')

    def action_back(self):
        self.app.pop_screen()

    def action_reply(self):
        seed = be.reply_seed(self.msg)
        seed['account'] = self.summary.account
        self.app.push_screen(ComposeScreen(seed))

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
        Binding('ctrl+o', 'attach', 'Attach', priority=True),
        Binding('ctrl+b', 'fmt_bold', 'Bold', priority=True),
        Binding('ctrl+e', 'fmt_italic', 'Italic', priority=True),
        Binding('ctrl+k', 'fmt_link', 'Link', priority=True),
        Binding('f1', 'help', 'Help', show=False),
    ]

    def __init__(self, seed=None):
        super().__init__()
        self.seed = seed or {}
        self._sending = False
        self._used_markup = False
        self.attachments = []

    def compose(self):
        session = self.app.session
        options = [(account_label(a.name, a.color, a.backend.address), a.name)
                   for a in session.accounts]
        self._initial_from = self.seed.get('account') or session.accounts[0].name
        title = (f'✉  Reply to {self.seed["reply_to"]}' if self.seed.get('reply_to')
                 else '✉  New message')
        with Vertical():
            yield Label(title, classes='card-title')
            with Horizontal(id='from-row'):
                yield Label('From')
                yield Select(options, value=self._initial_from, allow_blank=False, id='from')
            yield Input(placeholder='To', id='to')
            yield Input(placeholder='Subject', id='subject')
            yield Static(id='attach-line')
            yield TextArea(id='body')
        yield Footer()

    def on_mount(self):
        self.query_one('#to', Input).value = self.seed.get('to', '')
        self.query_one('#subject', Input).value = self.seed.get('subject', '')
        self.query_one('#body', TextArea).text = self.seed.get('body', '')
        self.query_one('#body' if self.seed.get('to') else '#to').focus()

    def action_help(self):
        self.app.push_screen(HelpScreen())

    # -- formatting & attachments --
    def _body_focused(self):
        # formatting keys are body-only; in To/Subject, Ctrl+E/K keep their
        # stock readline meaning (end of line / delete to end)
        return self.query_one('#body', TextArea).has_focus

    def _wrap(self, left, right):
        if not self._body_focused():
            return
        ta = self.query_one('#body', TextArea)
        sel = ta.selected_text
        if sel:
            start, end = sorted((ta.selection.start, ta.selection.end))
            ta.replace(f'{left}{sel}{right}', start, end)
        else:
            ta.insert(left + right)
            ta.move_cursor_relative(columns=-len(right))
        self._used_markup = True
        ta.focus()

    def action_fmt_bold(self):
        self._wrap('**', '**')

    def action_fmt_italic(self):
        self._wrap('*', '*')

    def action_fmt_link(self):
        if not self._body_focused():
            return
        ta = self.query_one('#body', TextArea)
        label = ta.selected_text or 'link text'
        start, end = sorted((ta.selection.start, ta.selection.end))
        ta.replace(f'[{label}](https://)', start, end)
        ta.move_cursor_relative(columns=-1)  # cursor inside the (), after https://
        self._used_markup = True
        ta.focus()

    def action_attach(self):
        start = Path(be.load_config().get('last_attach_dir', str(Path.home())))
        if not start.is_dir():
            start = Path.home()

        def done(picked):
            if not picked:
                return
            self.attachments.append(Path(picked))
            cfg = be.load_config()
            cfg['last_attach_dir'] = str(Path(picked).parent)
            be.save_config(cfg)
            self._update_attach_line()
        self.app.push_screen(FilePickScreen(start), done)

    def _update_attach_line(self):
        line = self.query_one('#attach-line', Static)
        line.update(Text('📎 ' + ', '.join(p.name for p in self.attachments),
                         style='cyan'))
        line.display = bool(self.attachments)

    def action_send(self):
        if self._sending:
            return  # SMTP thread can't be aborted; a second Ctrl+S would send twice
        account = self.query_one('#from', Select).value
        to = self.query_one('#to', Input).value.strip()
        subject = self.query_one('#subject', Input).value.strip()
        body = self.query_one('#body', TextArea).text
        if not to:
            self.app.notify('Add a recipient first', severity='warning')
            return
        self._sending = True
        self.app.notify(f'Sending from {account} to {to} …', timeout=3)
        self._send(account, to, subject, body)

    @work(thread=True, exclusive=True, group='send')
    def _send(self, account, to, subject, body):
        app = self.app
        try:
            msg = be.build_message(app.session.address(account), to, subject, body,
                                   self.seed.get('in_reply_to'),
                                   attachments=self.attachments,
                                   markup=self._used_markup)
            app.session.send(account, msg)
        except Exception as exc:
            app.call_from_thread(self._send_failed, exc)
            return
        app.call_from_thread(self._sent, account, to)

    def _send_failed(self, exc):
        # the draft stays open — nothing typed is lost on a failed send
        self._sending = False
        self.app.notify(f'Send failed, draft kept: {be.err_text(exc)}',
                        severity='error', timeout=8)

    def _sent(self, account, to):
        self._sending = False
        self.app.notify(f'Sent from {account} to {to}')
        if self.app.screen is self:
            self.dismiss(True)  # dismiss pops the CURRENT screen — only pop ourselves

    def action_cancel(self):
        sel = self.query_one('#from', Select)
        if sel.expanded:
            sel.expanded = False  # Esc closes the open dropdown, nothing more
            return
        to = self.query_one('#to', Input).value
        subject = self.query_one('#subject', Input).value
        body = self.query_one('#body', TextArea).text
        dirty = (to != self.seed.get('to', '') or subject != self.seed.get('subject', '')
                 or body != self.seed.get('body', '') or bool(self.attachments)
                 or sel.value != self._initial_from)
        if not dirty:
            self.dismiss(False)
            return
        self.app.push_screen(ConfirmScreen('Discard this draft?'),
                             lambda ok: self.dismiss(False) if ok else None)


# --- command palette ----------------------------------------------------------
class MailCommands(Provider):
    async def discover(self):
        # empty query: show the app's own commands, not just Textual's stock ones
        screen = self.screen
        if not isinstance(screen, MainScreen):
            return
        for label, fn in self._commands(screen):
            yield DiscoveryHit(label, fn)

    async def search(self, query):
        screen = self.screen  # app.screen is the palette itself while it's open
        if not isinstance(screen, MainScreen):
            return
        matcher = self.matcher(query)
        for label, fn in self._commands(screen):
            score = matcher.match(label)
            if score > 0:
                yield Hit(score, matcher.highlight(label), fn)

    def _commands(self, screen):
        session = self.app.session
        commands = [
            ('Compose new message', screen.action_compose),
            ('Refresh mailbox', screen.action_refresh),
            ('Search messages', screen.action_search),
            ('Keyboard reference', screen.action_help),
            ('Logout', self.app.action_logout),
        ]
        if self.app.update_info:
            commands.append((f'Install update {self.app.update_info["version"]}',
                             self.app.action_update_app))
        else:
            commands.append(('Check for updates now', self.app.action_check_updates_now))
        if len(session.accounts) > 1:
            commands.append(('Show all accounts', partial(screen.set_scope, None)))
            commands += [(f'Switch to account: {a.name}', partial(screen.set_scope, a.name))
                         for a in session.accounts]
        commands += [(f'Open folder: {be.decode_folder(name)}', partial(screen.goto_folder, name))
                     for name, _ in screen.folder_counts]
        if up.install_kind() == 'macos' and not up.cli_installed():
            commands.append(('Install the tuimail terminal command',
                             self.app._install_cli))
        return commands


# --- app ----------------------------------------------------------------------
class TuiMail(App):
    CSS_PATH = 'app.tcss'
    TITLE = f'tuimail {__version__}'
    COMMANDS = App.COMMANDS | {MailCommands}
    BINDINGS = [
        Binding('ctrl+l', 'logout', 'Logout', show=False, priority=True),
        Binding('ctrl+u', 'update_app', 'Update', show=False),
        Binding('f1', 'help', 'Help', show=False, priority=True),
    ]

    session = None
    auto_login = True
    update_info = None
    restart_after_exit = False
    _notified_update = ''
    _update_state = 'never'  # never | checking | ok | failed | off
    _update_checked_at = None

    def on_mount(self):
        try:
            self.theme = 'tokyo-night'
        except Exception:
            pass  # older/newer theme sets — default theme is fine
        if (be.load_config().get('update_check', True)
                and not os.environ.get('TUIMAIL_NO_UPDATE_CHECK')):
            self.set_interval(up.CHECK_EVERY, self._check_updates)
            self._check_updates()
        else:
            self._update_state = 'off'
        self.push_screen(
            LoginScreen() if be.load_config().get('accounts') else OnboardingScreen())

    def action_help(self):
        if isinstance(self.screen, HelpScreen):
            self.pop_screen()
        else:
            self.push_screen(HelpScreen())

    # -- updates --
    @work(thread=True, exclusive=True, group='update-check')
    def _check_updates(self):
        import time
        self._update_state = 'checking'
        try:
            info = up.check_latest()
        except Exception:
            self._update_state = 'failed'  # offline / rate-limited — next interval retries
            return
        self._update_state = 'ok'
        self._update_checked_at = time.time()
        if info and up.is_newer(info['version']):
            self.call_from_thread(self._update_available, info)

    def _update_status_text(self):
        import time
        state = self._update_state
        if state == 'off':
            return 'Update checks are off ("update_check": false in the config)'
        if state in ('never', 'checking'):
            return 'The update check has not completed yet'
        if state == 'failed':
            return 'The last update check failed (offline?) — Ctrl+P → Check for updates now'
        mins = int((time.time() - (self._update_checked_at or time.time())) // 60)
        return f'You are on the newest version ({__version__}, checked {mins} min ago)'

    def _update_available(self, info):
        self.update_info = info
        if self._notified_update != info['version']:
            self._notified_update = info['version']
            self.notify(f'tuimail {info["version"]} is available — Ctrl+U to update',
                        timeout=10)

    def action_check_updates_now(self):
        self.notify('Checking for updates …', timeout=3)
        self._check_updates()
        self.set_timer(6, self._report_check)

    def _report_check(self):
        if not self.update_info:
            self.notify(self._update_status_text())

    def action_update_app(self):
        info = self.update_info
        if not info:
            self.notify(self._update_status_text())
            return
        kind = up.install_kind()
        if kind == 'pip':
            self.notify('Installed via pip — update with: pipx upgrade tuimail  (or '
                        'pip install -U git+https://github.com/alpatovdanila/tui-mail.git)',
                        timeout=12)
            return
        if kind == 'unsupported':
            self.notify('Self-update only works for the released binaries',
                        severity='warning')
            return
        asset = info['assets'].get(up.asset_name()) or {}
        if asset.get('sha256'):
            prompt = (f'Update tuimail to {info["version"]}? Downloads {up.asset_name()} '
                      f'from github.com — sha256 verified before install. Restarts after.')
        else:
            prompt = (f'Update to {info["version"]}? WARNING: this release publishes no '
                      f'integrity digest, so the download cannot be verified. Install anyway?')
        self.push_screen(ConfirmScreen(prompt),
                         lambda ok: self._apply_update(info) if ok else None)

    @work(thread=True, exclusive=True, group='update')
    def _apply_update(self, info):
        try:
            asset = info['assets'].get(up.asset_name())
            if not asset or not asset.get('url'):
                raise ValueError('this release has no binary for your platform')
            self.call_from_thread(self.notify, f'Downloading {info["version"]} …')
            staged = up.download(asset['url'], asset.get('sha256', ''),
                                 up.target_path().parent)
            mode = up.apply_update(staged)
        except Exception as exc:
            self.call_from_thread(self.notify, f'Update failed: {exc}',
                                  severity='error', timeout=8)
            return
        self.call_from_thread(self._update_done, mode)

    def _update_done(self, mode):
        if mode == 'restart':
            self.restart_after_exit = True  # __main__ re-execs the new binary
        self.exit()

    # -- terminal command (macOS) --
    def maybe_offer_cli(self):
        """First-launch offer to link `tuimail` into PATH — the way VS Code
        and iTerm install their CLIs; asked once, redoable via the palette."""
        if up.install_kind() != 'macos' or up.cli_installed():
            return
        cfg = be.load_config()
        if cfg.get('cli_offered'):
            return
        cfg['cli_offered'] = True
        be.save_config(cfg)
        self.push_screen(
            ConfirmScreen('Install the tuimail terminal command? '
                          '(your admin password may be asked)'),
            lambda ok: self._install_cli() if ok else None)

    @work(thread=True, exclusive=True, group='cli')
    def _install_cli(self):
        try:
            up.install_cli()
        except Exception as exc:
            self.call_from_thread(self.notify, f'Could not install the command: {exc}',
                                  severity='error', timeout=8)
            return
        self.call_from_thread(self.notify, 'Installed — run tuimail from any terminal')

    def action_logout(self):
        if self.session is None:
            return
        old, self.session = self.session, None
        self.auto_login = False  # signing out must stick for the whole session
        threading.Thread(target=old.close, daemon=True).start()
        self.sub_title = ''
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(
            LoginScreen() if be.load_config().get('accounts') else OnboardingScreen())
        self.notify('Signed out')
