"""Export SVG screenshots of the app's screens into docs/."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix='tuimail-shots-')
os.environ['TUIMAIL_CONFIG'] = str(Path(TMP) / 'config.json')
os.environ.pop('NO_COLOR', None)  # sandbox shells set it; it grayscales the export

from tuimail import backend as be  # noqa: E402
from tuimail.app import TuiMail  # noqa: E402

DOCS = ROOT / 'docs'
DOCS.mkdir(exist_ok=True)
SIZE = (120, 36)


async def settle(pilot, delay=0.0):
    if delay:
        await pilot.pause(delay)
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


async def main():
    app = TuiMail()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.save_screenshot('onboarding.svg', path=str(DOCS))
        await pilot.click('#demo')
        await settle(pilot, 0.5)
        await settle(pilot)
        app.save_screenshot('mailbox.svg', path=str(DOCS))
        await pilot.press('enter')
        await settle(pilot)
        app.save_screenshot('reader.svg', path=str(DOCS))
        await pilot.press('r')
        await settle(pilot)
        app.save_screenshot('compose.svg', path=str(DOCS))
        await pilot.press('escape')
        await pilot.pause()
        await pilot.press('question_mark')
        await pilot.pause()
        app.save_screenshot('help.svg', path=str(DOCS))

    be.save_config({'address': 'you@example.com', 'imap_host': 'imap.example.com',
                    'smtp_host': 'smtp.example.com'})
    app = TuiMail()
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.save_screenshot('login.svg', path=str(DOCS))
    print('screenshots saved to', DOCS)


if __name__ == '__main__':
    asyncio.run(main())
