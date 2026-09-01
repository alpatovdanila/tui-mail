import sys


def main():
    from .app import TuiMail
    if '--check' in sys.argv:  # headless boot smoke test, used by CI
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
