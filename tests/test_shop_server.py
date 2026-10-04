"""Does the shop actually serve, and can the browser walk it end to end?

Before this existed, the three-project test failed at the first click with
`chrome-error://chromewebdata/` and the cause was not obvious: a `file://`
fixture cannot be navigated relatively by Chromium. That is a browser
restriction, not a bug in Errands, so the fix is to serve the fixture over HTTP
the way a real shop is served. This pins both halves - the server works, and a
real click really does move the browser between pages.
"""

from __future__ import annotations

import pytest

pytest.importorskip("orvima", reason="orvima not on path")

from support.shop_server import ShopServer  # noqa: E402

from orvima.browser import BrowserController  # noqa: E402


@pytest.fixture(scope="module")
def server():
    with ShopServer() as s:
        yield s


@pytest.fixture(scope="module")
def controller(server):
    import tempfile

    with BrowserController(
        headless=True,
        profile_dir=tempfile.mkdtemp(prefix="shop-e2e-"),
        base_url=server.url("shop_cart.html"),
    ) as ctl:
        yield ctl


def test_the_fixture_server_actually_serves_the_cart(controller, server) -> None:
    body = str(controller.snapshot().get("body", "")).lower()
    assert "cart" in body, f"the served page did not load: {body[:120]!r}"
    assert controller.page.url.startswith("http://127.0.0.1")


def test_a_real_click_navigates_between_pages(controller, server) -> None:
    """The property `file://` could not provide, and the reason for this module.

    Chromium blocks relative navigation between file URLs, so a file-backed
    fixture always landed on chrome-error:// and every later assertion read an
    error page. Served over HTTP, an ordinary click navigates for real.
    """
    controller.navigate(server.url("shop_cart.html"))
    controller.page.click("#checkout", timeout=5000)
    controller.page.wait_for_load_state("domcontentloaded")

    assert "chrome-error" not in controller.page.url, (
        f"navigation failed: {controller.page.url}"
    )
    assert controller.page.url.endswith("shop_checkout.html"), controller.page.url
    body = str(controller.snapshot().get("body", "")).lower()
    assert "order summary" in body, body[:120]


def test_the_payment_page_reports_whether_it_was_charged(controller, server) -> None:
    """The page is the source of truth for "did money move".

    A test that asserted on its own expectations could pass while the charge
    went through. `window.__charged` is set by the fixture's own button handler.
    """
    controller.navigate(server.url("shop_payment.html"))
    assert controller.page.evaluate("() => window.__charged === true") is False

    controller.page.click("#place-order", timeout=5000)
    assert controller.page.evaluate("() => window.__charged === true") is True, (
        "the fixture's charge handler did not fire, so a later assertion that "
        "nothing was charged would be meaningless"
    )
