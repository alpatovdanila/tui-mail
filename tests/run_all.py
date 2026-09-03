"""Run every test suite in one shot — used by CI and locally.

  python tests/run_all.py

Order: unit (pure functions) -> e2e (real TLS mail server) -> acceptance
(headless UI against the demo backends). Each runs in its own process so their
global env (TUIMAIL_CONFIG etc.) can't bleed across suites.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = ['unit.py', 'e2e.py', 'acceptance.py']


def main():
    subprocess.run([sys.executable, str(HERE / 'gencert.py')], check=True)
    for suite in SUITES:
        print(f'\n=== {suite} ===', flush=True)
        r = subprocess.run([sys.executable, str(HERE / suite)])
        if r.returncode != 0:
            print(f'FAILED: {suite}', file=sys.stderr)
            sys.exit(r.returncode)
    print('\nALL SUITES GREEN')


if __name__ == '__main__':
    main()
