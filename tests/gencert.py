"""Generate the throwaway localhost TLS cert the e2e mail server uses.

Self-signed, 100-year, CN=localhost with localhost/127.0.0.1 SANs. Regenerated
on demand (it is git-ignored — a private key never belongs in the repo) via the
openssl binary, which is present on every CI runner and dev box. Idempotent.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CERT, KEY = HERE / 'testcert.pem', HERE / 'testkey.pem'


def ensure_cert() -> None:
    if CERT.exists() and KEY.exists():
        return
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048',
        '-keyout', str(KEY), '-out', str(CERT), '-days', '36500', '-nodes',
        '-subj', '/CN=localhost/O=tuimail test suite - NOT A REAL CA',
        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
    ], check=True, capture_output=True)


if __name__ == '__main__':
    try:
        ensure_cert()
        print(f'cert ready: {CERT}')
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f'could not generate the test cert: {exc}', file=sys.stderr)
        sys.exit(1)
