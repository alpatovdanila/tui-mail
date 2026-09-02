import sys


def main():
    from .app import TuiMail
    if '--check' in sys.argv:  # headless boot smoke test, used by CI
        import asyncio
        import os
        os.environ['TUIMAIL_NO_UPDATE_CHECK'] = '1'
        import email
        import email.policy

        from .backend import body_markdown
        probe = email.message_from_bytes(
            b'Content-Type: text/html\r\n\r\n<h2>ok</h2>',
            policy=email.policy.default)
        assert (body_markdown(probe) or '').startswith('## ok'), \
            'html-to-markdown converter missing from this build'

        async def go():
            async with TuiMail().run_test():
                pass

        asyncio.run(go())
        print('ok')
        return
    app = TuiMail()
    app.run()
    if getattr(app, 'restart_after_exit', False):
        import os
        os.execv(sys.executable, [sys.executable] + sys.argv[1:])


if __name__ == '__main__':
    main()
