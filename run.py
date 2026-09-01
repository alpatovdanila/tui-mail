"""Entry point — also the PyInstaller build target.

`tuimail --check` boots the whole app headless once and prints ok; used to
smoke-test the frozen exe.
"""
import sys


def main():
    from tuimail.app import TuiMail
    if '--check' in sys.argv:
        import asyncio

        async def go():
            async with TuiMail().run_test():
                pass

        asyncio.run(go())
        print('ok')
        return
    TuiMail().run()


if __name__ == '__main__':
    main()
