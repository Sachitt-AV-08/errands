"""A local shop served over HTTP, for the three-project end-to-end test.

A `file://` fixture cannot be walked by clicking: Chromium refuses relative
navigation between file URLs, so `location.href = 'checkout.html'` lands on
`chrome-error://chromewebdata/` and the page under test is gone. Real shops are
served over HTTP, so this serves over HTTP too - the point is to exercise the
real navigation path, not to work around a browser restriction inside a fixture.
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D102 - silence the test output
        pass


class ShopServer:
    """Serve `tests/fixtures/` on 127.0.0.1 for the duration of a test.

    Bound to port 0 so a parallel run cannot collide, and shut down on exit so a
    failed test does not leave a listener behind.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or FIXTURES
        handler = functools.partial(_Quiet, directory=str(self._dir))
        self._httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def url(self, page: str) -> str:
        return f"http://127.0.0.1:{self.port}/{page}"

    def __enter__(self) -> "ShopServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)
