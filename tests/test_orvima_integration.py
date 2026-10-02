"""Errands driving orvima: adapter, approver, and the assembly.

Imports the real `orvima.sentinel_gate`. That is the point - a bridge tested
only against a fake of the thing it bridges to is a bridge that will not work
against it.

The headline test is `test_a_ref_click_is_decided_rather_than_refused_for_being_unknown`.
orvima's own tool docstrings tell the agent to address elements by ref, and a
gate with no snapshot must refuse every one of them, because `e12` carries no
text and its risk is genuinely unknown. That is correct and it is also unusable:
it prompts on nearly every interaction. Binding the page is what fixes it, and
this is the test that says the fix works.
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("orvima", reason="orvima not on path")
pytest.importorskip("sentinel", reason="sentinel not on path")

from errands.orvima_bridge import (  # noqa: E402
    GateApprover,
    OrvimaBrowser,
    ToolError,
    runner_for,
)

import errands.core as core  # noqa: E402


# ------------------------------------------------------- a controller stand-in ----


class FakePage:
    """orvima's controller keeps the URL on an internal page object."""

    def __init__(self, url: str) -> None:
        self.url = url


class FakeController:
    """Shaped like `orvima.browser.BrowserController`, not like a real browser."""

    def __init__(self, url: str = "https://shop.example/cart", items=None) -> None:
        self.page = FakePage(url)
        self.items = items if items is not None else [
            {"ref": "#place", "tag": "button", "role": "button",
             "label": "Place order", "text": ""},
            {"ref": "#total", "tag": "span", "role": "text",
             "label": "Total $43.00", "text": ""},
        ]
        self.calls: list[tuple[str, dict]] = []
        self.snapshot_raises = False

    def snapshot(self) -> dict:
        if self.snapshot_raises:
            raise RuntimeError("detached: target closed")
        return {"url": self.page.url, "title": "Shop", "items": self.items}

    def _act(self, verb: str, **kw):
        self.calls.append((verb, kw))
        return {"ok": True}

    def navigate(self, url: str, **_): return self._act("navigate", url=url)
    def click(self, selector: str, **_): return self._act("click", selector=selector)
    def type(self, selector: str, text: str, **_):
        return self._act("type", selector=selector, text=text)
    def fill(self, selector: str, text: str, **_):
        return self._act("fill", selector=selector, text=text)
    def extract(self, selector: str, **_): return self._act("extract", selector=selector)
    def wait_for(self, selector: str, timeout_ms: int = 10000, **_):
        return self._act("wait", selector=selector)
    def go_back(self, **_): return self._act("back")
    def scroll(self, direction: str = "down", **_): return self._act("scroll", direction=direction)


def gate_for(controller, **kw):
    from errands.orvima_bridge import build_gate

    return build_gate(OrvimaBrowser(controller), **kw)


# ------------------------------------------------------------------ adapter ----


def test_the_adapter_passes_orvimas_browser_methods_through() -> None:
    """The nine verbs a recipe may use, and nothing exotic."""
    controller = FakeController()
    browser = OrvimaBrowser(controller)
    browser.click("Checkout")
    browser.type("#email", "a@b.c")
    browser.fill("#card", "4242")
    browser.extract("#total")
    browser.wait_for("#place")
    browser.go_back()
    browser.scroll("down")
    browser.navigate("https://shop.example/payment")

    verbs = [v for v, _ in controller.calls]
    assert verbs == ["click", "type", "fill", "extract", "wait", "back",
                     "scroll", "navigate"]


def test_the_adapter_reports_the_url_the_controller_only_holds_internally() -> None:
    """The gate needs this and the controller does not expose it."""
    controller = FakeController(url="https://shop.example/checkout")
    assert OrvimaBrowser(controller).url == "https://shop.example/checkout"


def test_the_url_falls_back_to_the_snapshot_when_the_page_object_is_absent() -> None:
    class NoPage:
        page = None

        def snapshot(self):
            return {"url": "https://shop.example/payment"}

    assert OrvimaBrowser(NoPage()).url == "https://shop.example/payment"


def test_a_browser_raising_never_looks_like_success() -> None:
    class Broken(FakeController):
        def click(self, selector: str, **_):
            raise RuntimeError("target closed")

    with pytest.raises(ToolError, match="click failed"):
        OrvimaBrowser(Broken()).click("#place")


def test_a_failing_snapshot_degrades_to_empty_rather_than_raising() -> None:
    """The prompt is shown *because* something needs a human.

    If building the evidence for the prompt then crashes, the request must still
    reach the queue. A gate that dies while assembling its own case approves
    nothing and explains nothing.
    """
    controller = FakeController()
    controller.snapshot_raises = True
    assert OrvimaBrowser(controller).snapshot_safe() == {}


# ------------------------------------------------------- the headline result ----


def test_a_ref_click_is_decided_rather_than_refused_for_being_unknown() -> None:
    """The reason this module exists.

    Without a snapshot, `browse_click(ref="e12")` is refused: a ref carries no
    text, so its risk is unknown. Fail-closed and correct - and unusable, since
    orvima's tools recommend refs. With the page bound, the same call is decided
    on the element's actual text.
    """
    controller = FakeController(url="https://shop.example/cart")
    items = [{"ref": "#next", "tag": "button", "role": "button",
              "label": "Next page", "text": ""}]
    controller.items = items

    unbound = gate_for(FakeController(url="https://shop.example/cart"))
    refused = unbound.check("browse_click", {"ref": "#next"}, session_id="s1")
    assert refused.allowed is False
    assert "no resolver" in refused.reason

    bound = gate_for(controller)
    decided = bound.check("browse_click", {"ref": "#next"}, session_id="s1")
    assert decided.allowed is True, decided.reason
    assert decided.risk == "low"


def test_a_risky_ref_is_caught_once_the_page_is_bound() -> None:
    """The other direction, and the reason to be careful about it.

    A dangerous button addressed by ref is invisible to a gate with no page. Bound
    to the page it is caught. This is the argument for wiring the snapshot, and
    it is why the unbound behaviour is a usability problem rather than a safe
    default.
    """
    controller = FakeController(url="https://shop.example/account")
    controller.items = [{"ref": "#x", "tag": "button", "role": "button",
                         "label": "Delete account", "text": ""}]

    unbound = gate_for(FakeController(url="https://shop.example/account"))
    assert unbound.check("browse_click", {"ref": "#x"}, session_id="s1").allowed is False

    bound = gate_for(controller)
    decided = bound.check("browse_click", {"ref": "#x"}, session_id="s1")
    assert decided.allowed is False
    assert decided.risk in ("outward", "destructive")


def test_two_browsers_do_not_share_page_context() -> None:
    """Why the gate is built per browser rather than taken from the api singleton.

    A singleton gate carries one `page_url` callable. Two sessions with two
    browsers would each be judged against whichever page that callable happened
    to return - the same class of bug as the shared `_seen_refs`, one level up.
    """
    a = FakeController(url="https://shop.example/cart")
    b = FakeController(url="https://shop.example/admin")
    a.items = [{"ref": "#x", "tag": "button", "role": "button", "label": "Next", "text": ""}]
    b.items = [{"ref": "#x", "tag": "button", "role": "button",
                "label": "Delete all users", "text": ""}]

    gate_a = gate_for(a)
    gate_b = gate_for(b)

    assert gate_a.check("browse_click", {"ref": "#x"}, session_id="sa").risk == "low"
    assert gate_b.check("browse_click", {"ref": "#x"}, session_id="sb").risk in (
        "outward", "destructive"
    )


# --------------------------------------------------------------- the approver ----


def approval(tool="browse_click", args=None, risk="destructive", plan="place order payment"):
    return core.ApprovalRequest(
        step_id="place_the_order", tool=tool, args=args or {"ref": "#place"},
        risk=risk, reason="irreversible", detail={"plan": plan},
    )


def answer_with(approver, verdict: bool, delay: float = 0.0):
    """Reply to whatever the approver queues, through the approver itself."""
    def run():
        for _ in range(2000):
            pending = approver.gate.pending(approver.session_id)
            if pending:
                time.sleep(delay)
                approver.resolve(pending[0].id, verdict)
                return
            time.sleep(0.01)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_approving_lets_the_step_proceed() -> None:
    controller = FakeController(url="https://shop.example/payment")
    approver = GateApprover(gate_for(controller), "s1", timeout=5)
    answer_with(approver, True)
    assert approver(approval()) is True


def test_declining_stops_the_step() -> None:
    approver = GateApprover(gate_for(FakeController(url="https://shop.example/payment")), "s1", timeout=5)
    answer_with(approver, False)
    assert approver(approval()) is False


def test_an_approval_that_waits_too_long_times_out() -> None:
    """Nobody answered. That is a refusal, not a shrug."""
    gate = gate_for(FakeController())
    approver = GateApprover(gate, "s1", timeout=0.2)
    started = time.monotonic()
    assert approver(approval()) is False
    assert time.monotonic() - started < 3


def test_an_expired_request_is_refused_rather_than_honoured() -> None:
    """The `lapsed` distinction.

    A human answering after the TTL pressed the button, and having it silently
    ignored is worse than refusing - so this asserts the *gate* refuses the late
    yes, and separately that the approver does not hang on it.
    """
    gate = gate_for(FakeController(), approval_ttl=0.1)
    approver = GateApprover(gate, "s1", timeout=5)
    answer_with(approver, True, delay=0.3)

    assert approver(approval()) is False, "an approval older than its TTL was honoured"


def test_an_approval_is_invalidated_when_the_element_changes() -> None:
    """The TOCTOU case, end to end.

    A human reviews one button, and the page re-renders before they answer. The
    approval was a decision about that button. Executing it against whatever now
    occupies the ref is not that decision, so the approver refuses it - before
    the click, not after.
    """
    controller = FakeController(url="https://shop.example/payment")
    controller.items = [{"ref": "#place", "tag": "button", "role": "button",
                         "label": "Place order", "text": ""}]
    gate = gate_for(controller)

    request = approval(args={"ref": "#place"})
    # Queue it now so the gate records the ref fingerprint.
    from orvima.sentinel_gate import GateResult

    queued = gate.request_approval(
        "s1", "browse_click", {"ref": "#place"},
        GateResult(allowed=False, pending=True, risk="destructive", reason="irreversible"),
        controller.snapshot(),
    )

    # The page moves while the human is deciding.
    controller.items = [{"ref": "#place", "tag": "button", "role": "button",
                         "label": "Delete account", "text": ""}]
    gate.resolve(queued, True)

    approver = GateApprover(gate, "s1", timeout=0.3)
    events: list[dict] = []
    approver.on_event = events.append
    assert approver(request) is False


def test_events_are_emitted_and_a_broken_handler_cannot_fail_an_errand() -> None:
    """Observability must never be able to break a purchase."""
    gate = gate_for(FakeController(url="https://shop.example/payment"))

    def explode(event):
        raise RuntimeError("handler is broken")

    approver = GateApprover(gate, "s1", timeout=0.2, on_event=explode)
    assert approver(approval()) is False, "a raising event handler changed the verdict"

    seen: list[dict] = []
    approver2 = GateApprover(gate, "s2", timeout=0.2, on_event=seen.append)
    approver2(approval())
    kinds = [e["type"] for e in seen]
    assert kinds[0] == "approval_requested"
    assert "approval_timeout" in kinds


# ----------------------------------------------------------------- assembly ----


def test_runner_for_returns_the_collaborators_not_just_the_runner() -> None:
    """A caller needs the gate afterwards - to render the queue, or read stats."""
    controller = FakeController(url="https://shop.example/cart")
    runner, browser, gate = runner_for(
        controller, [], session_id="s1", approval_timeout=1.0
    )
    assert isinstance(browser, OrvimaBrowser)
    assert gate is not None
    assert runner.classify is not None, "Sentinel is installed, so the runner should classify"
    assert runner.approver is not None


def test_the_assembled_runner_gates_a_live_purchase_and_never_places_it() -> None:
    """The whole stack, on a goal with a shipped recipe.

    No human answers, so the run must stop before the charge. This is the
    end-to-end claim that the three projects are actually joined up.
    """
    from errands import Errand, Step, registry
    from pathlib import Path

    recipes = registry(Path(__file__).resolve().parents[1] / "recipes")
    controller = FakeController(url="https://shop.example/cart")
    controller.items = [
        {"ref": "#cart", "tag": "section", "role": "region", "label": "Cart items", "text": ""},
        {"ref": "#checkout", "tag": "button", "role": "button", "label": "Checkout", "text": ""},
    ]

    placed: list[str] = []

    def click(selector: str, **_):
        placed.append(selector)
        return {"ok": True}

    controller.click = click  # type: ignore[method-assign]

    runner, _browser, _gate = runner_for(
        controller, recipes, session_id="s1", approval_timeout=0.3
    )
    result = runner.run_goal("buy this item", recipes)

    assert not result.ok
    assert "Place order" not in placed, "an order was placed with nobody asked"
    # Exactly one unattended click: cart -> checkout, which the recipe marks
    # `gated: false` because nothing is charged by moving to the checkout page.
    # The next step is the first that asks, and nobody answered, so it stopped
    # there. Asserting the list rather than just "no purchase" pins down that the
    # gate is not simply refusing everything - which would also protect the
    # purchase, and just as uselessly.
    assert placed == ["Checkout"], placed