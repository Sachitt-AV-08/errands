"""Contract tests against the real orvima `BrowserController`.

`test_orvima_integration.py` drives `FakeController`, which is shaped like the
controller by hand. That shape rots silently: rename a verb in orvima, or change
its signature, and Errands' suite stays green while the bridge is broken in
production. Sentinel already binds to the real `orvima.tools`; this file is the
same idea for the browser, and it is the only test in this project that opens a
real Chromium.

Two kinds of test live here, and the split matters:

* `test_every_verb_the_bridge_forwards_exists_with_a_compatible_signature`
  needs no browser and is the drift guard. It is pure introspection, so it
  cannot be satisfied by a fake.
* the rest drive a real controller against a local fixture, proving the verbs
  work rather than merely exist.

Skipped, not failed, when orvima is not importable, so Errands' own suite still
runs standalone.
"""

from __future__ import annotations

import inspect
import re
import time
from pathlib import Path
from urllib.request import pathname2url

import pytest

pytest.importorskip("orvima", reason="orvima not on path")
pytest.importorskip("sentinel", reason="sentinel not on path")

from orvima.browser import BrowserController  # noqa: E402

from errands.orvima_bridge import OrvimaBrowser, ToolError  # noqa: E402
from errands.sentinel_bridge import make_classifier  # noqa: E402
from errands.core import PlanContext  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "form_page.html"
FIXTURE_URL = FIXTURE.as_uri()


# ------------------------------------------------------- the drift guard ----


def _forwarded_calls() -> dict[str, dict[str, str]]:
    """Every `_call("verb", kw=...)` the bridge makes, read out of the source.

    Read from the source rather than hardcoded, so adding a verb to the bridge
    without a matching test here is what fails - not a silent gap. The keyword
    names are parsed too, because that is what makes the signature check real:
    binding one shared union of kwargs to every verb would just assert that each
    verb accepts arguments that are not its own.
    """
    source = Path(inspect.getfile(OrvimaBrowser)).read_text(encoding="utf-8")
    calls: dict[str, dict[str, str]] = {}
    for verb, kwargs in re.findall(r"_call\(\s*[\"']([a-z_]+)[\"']([^)]*)\)", source):
        names = re.findall(r"([a-z_]+)\s*=", kwargs)
        calls[verb] = {n: "x" for n in names}
    return calls


_SAMPLE = {
    "selector": "s", "text": "t", "url": "u", "direction": "down", "timeout_ms": 1,
}

def test_the_bridge_forwards_at_least_one_verb() -> None:
    """Guards the guard: if the regex ever stops matching, fail loudly.

    Without this, a rename of `_call` would make `test_every_verb...` vacuously
    pass with an empty dict, which is the same failure mode as a mutation that
    does not apply.
    """
    calls = _forwarded_calls()
    assert calls, (
        "no verbs found in OrvimaBrowser - if _call was renamed, fix "
        "_forwarded_calls() rather than letting the drift guard pass empty"
    )
    assert "navigate" in calls and "click" in calls
    assert sorted(calls["navigate"]) == ["url"], (
        f"navigate is called with {sorted(calls['navigate'])}, expected url only"
    )


def test_every_verb_the_bridge_forwards_exists_with_a_compatible_signature() -> None:
    missing = []
    incompatible = []
    for verb, kwargs in _forwarded_calls().items():
        fn = getattr(BrowserController, verb, None)
        if fn is None:
            missing.append(verb)
            continue
        probe = {k: _SAMPLE.get(k, "x") for k in kwargs}
        try:
            inspect.signature(fn).bind(object.__new__(BrowserController), **probe)
        except TypeError as exc:
            incompatible.append(f"{verb}(**{sorted(probe)}): {exc}")

    assert not missing, f"OrvimaBrowser forwards verbs orvima no longer has: {missing}"
    assert not incompatible, f"signature drift against BrowserController: {incompatible}"


def test_the_verbs_the_bridge_needs_are_all_real_browser_methods() -> None:
    """The reverse direction: nothing the bridge needs is a private accident."""
    needed = [
        "navigate", "click", "type", "fill", "extract",
        "wait_for", "go_back", "scroll", "snapshot",
    ]
    absent = [n for n in needed if not callable(getattr(BrowserController, n, None))]
    assert not absent, f"BrowserController is missing verbs Errands relies on: {absent}"


# ------------------------------------------------- against a real browser ----


@pytest.fixture(scope="module")
def controller(tmp_path_factory):
    # `profile_dir` must be explicit, and this is not optional tidiness.
    #
    # `BrowserController` falls back to the shared `~/.orvima/profile` when it is
    # not given one, and Chromium refuses to open a profile another process holds
    # (exit code 21, surfaced as a bare "could not start a browser"). That makes
    # this file pass alone and error inside a full run, or beside orvima's own
    # browser tests - which is exactly the shared-profile defect the controller
    # already documents a `profile_dir` parameter for.
    profile = tmp_path_factory.mktemp("orvima-contract-profile")
    # `base_url` is pinned to the local fixture for the same class of reason:
    # `start()` navigates there, and the default is `https://example.com`. A
    # network dependency in a test that only needs a local file turns an offline
    # machine into nine errors that look like bridge failures.
    with BrowserController(
        headless=True, profile_dir=str(profile), base_url=FIXTURE_URL
    ) as ctl:
        yield ctl


@pytest.fixture
def browser(controller) -> OrvimaBrowser:
    controller.navigate(FIXTURE_URL)
    return OrvimaBrowser(controller)


def _context(**kw) -> PlanContext:
    base = {
        "errand": "sign_in", "step_id": "fill_email", "step_index": 1,
        "step_count": 2, "goal": "sign in", "intent": "fill the email field",
    }
    base.update(kw)
    return PlanContext(**base)


class _StubPolicy:
    """Records what the classifier was handed, so wiring is observable."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def evaluate(self, tool, args, **kw):
        self.calls.append({"tool": tool, "args": args, **kw})
        return kw


def test_the_url_reaches_the_policy_from_a_real_browser(browser) -> None:
    policy = _StubPolicy()
    classify = make_classifier(policy, page_url=lambda: browser.url)
    classify("browse_extract", {"selector": "#detail"}, _context())

    assert policy.calls, "the classifier was never called"
    got = policy.calls[0]["page_url"]
    assert got.startswith("file://"), f"expected the real page URL, got {got!r}"
    assert "form_page.html" in got, f"wrong fixture: {got!r}"


def test_a_string_page_url_works_too(browser) -> None:
    """The shape the README's first draft used, and a natural mistake.

    `OrvimaBrowser.url` is a property, so `page_url=browser.url` is what someone
    writes. Before `_resolve_page_url` this raised `TypeError: 'str' object is
    not callable` from inside the classifier - at decision time, on the first
    gated action, with the traceback nowhere near the wiring.
    """
    policy = _StubPolicy()
    classify = make_classifier(policy, page_url=browser.url)
    classify("browse_click", {"selector": "#go"}, _context())

    assert policy.calls[0]["page_url"].startswith("file://")


def test_no_page_url_is_an_empty_string_not_a_crash(browser) -> None:
    policy = _StubPolicy()
    classify = make_classifier(policy)
    classify("browse_click", {"selector": "#go"}, _context())
    assert policy.calls[0]["page_url"] == ""


def test_a_real_navigation_and_extract_through_the_bridge(browser) -> None:
    assert browser.url.startswith("file://")
    out = browser.extract("#detail")
    assert "no orders" in str(out), f"extract did not read the page: {out!r}"


def test_a_real_fill_and_click_through_the_bridge(browser) -> None:
    filled = browser.fill("#email", "someone@example.invalid")
    assert filled.get("verified") is True, f"fill did not verify: {filled!r}"

    before = str(browser.extract("#status"))
    assert "awaiting input" in before, f"fixture is in an unexpected state: {before!r}"

    clicked = browser.click("#go")
    assert clicked.get("verified") is True, f"click did not verify: {clicked!r}"

    after = str(browser.extract("#status"))
    # The success phrase must not be a substring of the initial one. "signed in"
    # is a substring of "not signed in", so a click that did nothing at all would
    # have satisfied a naive check - the guard would be decorative.
    assert "awaiting input" not in after, (
        f"the click reported success but the page never changed: {after!r}"
    )
    assert "accepted the click" in after, (
        "the click reported success but the page did not change - this is the "
        f"exact failure the bridge exists to prevent: {after!r}"
    )


def test_a_click_that_does_nothing_is_detected(browser) -> None:
    """The control for the test above, run against a known no-op.

    If the page-change assertion in `test_a_real_fill_and_click_through_the_bridge`
    could not tell a real click from no click at all, this is where it would show.
    """
    before = str(browser.extract("#status"))
    assert "awaiting input" in before
    assert "accepted the click" not in before, (
        "fixture already shows the post-click state, so the click test proves nothing"
    )


def test_an_element_under_a_sticky_header_still_reaches_the_right_target(
    controller,
) -> None:
    """Occlusion handling survives the bridge.

    orvima uncovers elements hidden under a sticky or fixed header before
    clicking. Errands forwards `click` to exactly that method, so this pins the
    two together: a covered target must be uncovered and clicked, not timed out
    after 10 seconds and not clicked on the header by mistake.

    Uses orvima's own `sticky_page.html` rather than a local copy, because the
    occluder and the probe are orvima's to maintain; if that fixture moves, this
    test skips instead of quietly testing nothing.
    """
    sticky = Path(r"C:\Users\sachi\orvima\tests\fixtures\sticky_page.html")
    if not sticky.exists():
        pytest.skip(f"orvima's sticky fixture not found at {sticky}")

    controller.navigate(sticky.as_uri())
    adapter = OrvimaBrowser(controller)

    # Default scrollIntoView parks the target at the very top, directly under the
    # sticky bar - the case the probe exists for.
    controller.page.evaluate("() => document.querySelector('#target').scrollIntoView()")
    time.sleep(0.2)

    geometry = controller.page.evaluate(
        "() => { const t = document.querySelector('#target').getBoundingClientRect();"
        " const b = document.querySelector('#bar').getBoundingClientRect();"
        " return { overlapped: t.top < b.bottom }; }"
    )
    assert geometry["overlapped"], (
        "fixture did not put the target under the bar, so this proves nothing"
    )

    started = time.time()
    adapter.click("#target")
    elapsed = time.time() - started

    landed = controller.page.evaluate("() => window.__clicks")
    assert landed, "no click reached the page at all"
    assert landed[0]["where"] == "target", (
        f"the click landed on {landed[0]['where']}, not the target: {landed!r}"
    )
    assert elapsed < 5.0, (
        f"clicking a covered element took {elapsed:.1f}s - the occlusion probe "
        "should have uncovered it rather than waiting out a timeout"
    )


def test_a_click_lands_on_the_element_it_was_asked_for(browser) -> None:
    """Catches a bridge that ignores the selector it was handed.

    There are two buttons, each producing different page text, so clicking the
    wrong one is observable. A bridge that always clicked `#go` would pass
    `test_a_real_fill_and_click_through_the_bridge` forever and be wrong here.
    """
    browser.click("#cancel")
    after = str(browser.extract("#status"))
    assert "cancelled" in after, f"the click did not reach #cancel: {after!r}"
    assert "accepted the click" not in after, (
        f"the click reached #go instead of #cancel: {after!r}"
    )


def test_a_real_missing_element_raises_rather_than_looking_like_success(browser) -> None:
    with pytest.raises(ToolError):
        browser.click("#no-such-element")


def test_the_adapter_satisfies_the_browser_protocol(browser) -> None:
    from errands.core import Browser

    required = [
        n for n, _ in inspect.getmembers(Browser, predicate=inspect.isfunction)
        if not n.startswith("_")
    ]
    absent = [n for n in required if not callable(getattr(browser, n, None))]
    assert not absent, f"OrvimaBrowser does not satisfy Browser: {absent}"
