"""Entry point: start the local server, open the window, exit when it closes.

Run with:
    pythonw -m app        (silent, how the launcher starts it)
    python  -m app --url  (print the URL and stay headless, for debugging)
"""

from __future__ import annotations

import sys
import time

from . import APP_NAME, __version__, server, window


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    headless = "--url" in argv or "--headless" in argv

    httpd, url = server.serve()

    if headless:
        print(f"{APP_NAME} {__version__}")
        print(url, flush=True)
        try:
            while not server.STATE.should_quit.is_set():
                time.sleep(0.3)
        except KeyboardInterrupt:
            pass
        httpd.shutdown()
        httpd.server_close()
        return 0

    proc = window.open_window(url)

    try:
        if proc is not None:
            # closing the window exits the app; polling lets /api/quit end it too
            while proc.poll() is None and not server.STATE.should_quit.is_set():
                time.sleep(0.3)
            if server.STATE.should_quit.is_set() and proc.poll() is None:
                proc.terminate()
        else:
            # default browser: no process to watch, wait for /api/quit
            while not server.STATE.should_quit.is_set():
                time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
