"""Self-update from GitHub releases.

The check is a single anonymous API call over verified TLS; applying an
update downloads the platform asset, verifies its sha256 digest when the API
provides one, and swaps the running binary:

- macOS/POSIX: os.replace over the running binary (allowed), restart on exit
- Windows: a detached helper waits for the process to exit, swaps, relaunches
- pip/pipx installs are never self-updated — the user gets the right command
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import __version__
from . import backend as be

REPO = 'alpatovdanila/tui-mail'
API_LATEST = f'https://api.github.com/repos/{REPO}/releases/latest'
CHECK_EVERY = 900  # seconds


def parse_version(tag) -> tuple:
    nums = re.findall(r'\d+', tag or '')
    return tuple(int(n) for n in nums[:3])


def is_newer(tag, current=__version__) -> bool:
    latest = parse_version(tag)
    return bool(latest) and latest > parse_version(current)


def check_latest(timeout=10):
    """-> {'version': 'vX.Y.Z', 'assets': {name: {'url', 'sha256'}}}; raises on failure."""
    req = urllib.request.Request(API_LATEST, headers={
        'User-Agent': f'tuimail/{__version__}',
        'Accept': 'application/vnd.github+json',
    })
    with urllib.request.urlopen(req, timeout=timeout, context=be.tls_context()) as r:
        data = json.load(r)
    assets = {}
    for a in data.get('assets', []):
        digest = a.get('digest') or ''
        assets[a.get('name', '')] = {
            'url': a.get('browser_download_url', ''),
            'sha256': digest[7:] if digest.startswith('sha256:') else '',
        }
    return {'version': data.get('tag_name', ''), 'assets': assets}


def install_kind() -> str:
    if not getattr(sys, 'frozen', False):
        return 'pip'
    if sys.platform == 'darwin':
        return 'macos'
    if os.name == 'nt':
        return 'windows'
    return 'unsupported'


def asset_name() -> str:
    return 'tuimail-windows.exe' if os.name == 'nt' else 'tuimail-macos-universal'


def target_path() -> Path:
    return Path(os.path.realpath(sys.executable))


def download(url, sha256, dest_dir, timeout=120) -> Path:
    """Download to a temp file in dest_dir (same filesystem as the target, so
    the final swap is atomic) and verify the digest before returning it."""
    req = urllib.request.Request(url, headers={'User-Agent': f'tuimail/{__version__}'})
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix='.tuimail-update-')
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, 'wb') as out, \
                urllib.request.urlopen(req, timeout=timeout, context=be.tls_context()) as r:
            while chunk := r.read(65536):
                digest.update(chunk)
                out.write(chunk)
        if sha256 and digest.hexdigest() != sha256:
            raise ValueError('downloaded update failed its integrity check')
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return Path(tmp)


CLI_LINK = '/usr/local/bin/tuimail'


def cli_installed() -> bool:
    """True when `tuimail` on PATH (or the /usr/local/bin link) is this binary —
    curl-installed copies in ~/.local/bin already count."""
    import shutil
    try:
        found = shutil.which('tuimail')
        if found and os.path.realpath(found) == str(target_path()):
            return True
        return os.path.realpath(CLI_LINK) == str(target_path())
    except OSError:
        return False


def install_cli() -> None:
    """Link /usr/local/bin/tuimail -> this binary. Tries a plain symlink and
    falls back to the native admin-password dialog — the VS Code/iTerm way,
    no Terminal sudo and no separate installer script."""
    target = str(target_path())
    try:
        os.makedirs('/usr/local/bin', exist_ok=True)
        if os.path.islink(CLI_LINK) or os.path.exists(CLI_LINK):
            os.unlink(CLI_LINK)
        os.symlink(target, CLI_LINK)
        return
    except OSError:
        pass  # root-owned dir — ask properly
    script = (f'do shell script "mkdir -p /usr/local/bin && '
              f'ln -sf \\"{target}\\" \\"{CLI_LINK}\\"" with administrator privileges')
    r = subprocess.run(['osascript', '-e', script],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or 'authorization declined')


def apply_update(staged: Path) -> str:
    """Swap the binary; returns 'restart' (re-exec on exit) or 'exit' (helper
    relaunches after the process ends)."""
    target = target_path()
    if install_kind() == 'macos':
        os.chmod(staged, 0o755)
        os.replace(staged, target)  # POSIX happily replaces a running binary
        return 'restart'
    # Windows locks the running exe — a detached helper swaps it after exit
    bat = target.parent / 'tuimail-update.bat'
    bat.write_text(
        '@echo off\n'
        ':try\n'
        f'move /y "{staged}" "{target}" >nul 2>&1 || (timeout /t 1 /nobreak >nul & goto try)\n'
        f'start "" "{target}"\n'
        '(goto) 2>nul & del "%~f0"\n',
        encoding='ascii',
    )
    subprocess.Popen(
        ['cmd', '/c', str(bat)],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=True)
    return 'exit'
