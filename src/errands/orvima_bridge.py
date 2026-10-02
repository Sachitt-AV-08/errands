"""Driving Errands through orvima: a live browser, a real gate, one approval queue.

This is the third side of the triangle. `core.py` knows how to run a recipe and
`sentinel_bridge.py` knows how to ask Sentinel a question, but neither knows
what a browser or an approval queue actually look like. This module supplies
both, and it is the only file that imports all three projects.

Nothing here is imported by `core.py`, so Errands still runs with no browser and
no ML dependency present. Import this module when you have a real browser.

The wiring problem this exists to solve
----------------------------------------
A gate wants two things from the world: the current page URL, and a snapshot to
resolve element refs against. orvima's `api.get_gate()` builds a **singleton**
gate with neither, so every ref-addressed click is refused - a ref carries no
text, so its risk is genuinely unknown. That is fail-closed and correct, but it
means the gate prompts on nearly every interaction, because orvima's own tool
docstrings recommend refs.

The deeper problem is that page context cannot be a constructor argument on a
singleton. Each session has its own browser, so `page_url` as a single captured
callable is ambiguous the moment two sessions run at once. The same class of
bug as the `_seen_refs` one, one level up.

So the gate used here is built **per browser**, with that browser's page wired
in. The cost is that it is a different instance from the api singleton, and
therefore a different approval queue - see `GateApprover` and the note on
`queue` below.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

from sentinel.policy import Policy

from .core import ApprovalRequest, Errand, ErrandRunner, PlanContext
from .sentinel_bridge import make_classifier


# --------------------------------------------------------------- browser -----


class OrvimaBrowser:
    """Adapts an orvima `BrowserController` to Errands' `Browser` protocol.

    Thin on purpose. orvima's controller already has `snapshot`, `navigate`,
    `click`, `type`, `fill`, `extract`, `wait_for`, `go_back` and `scroll` with
    compatible signatures, so this exists to do two things it cannot do alone:

    * expose the current URL, which the gate needs and the controller only
      carries internally (`self.page.url`);
    * never let a browser exception escape as something that looks like success.
      A failing click raises `ToolError`, which the runner already treats as a
      step failure - this just makes the message name the tool.
    """

    def __init__(self, controller: Any) -> None:
        self._c = controller

    # -- context the gate needs ------------------------------------------

    @property
    def url(self) -> str:
        """Current URL, or "" when the browser cannot say.

        Never guesses. An empty string means "no page context", which the policy
        treats as an unknown page - the fail-closed direction - rather than
        defaulting to something that looks safe.
        """
        page = getattr(self._c, "page", None)
        for attr in ("url",):
            value = getattr(page, attr, None)
            if isinstance(value, str) and value:
                return value
        try:
            snap = self._c.snapshot() or {}
        except Exception:
            return ""
        value = snap.get("url")
        return value if isinstance(value, str) else ""

    def snapshot(self) -> dict:
        return self._c.snapshot() or {}

    def snapshot_safe(self) -> dict:
        """A snapshot for an approval prompt, degrading to {} rather than raising.

        The prompt is shown *because* something needs a human. If taking the
        snapshot for the prompt then fails, the request must still reach the
        queue - a gate that crashes while building its own evidence approves
        nothing and explains nothing.
        """
        try:
            return self.snapshot()
        except Exception:
            return {}

    # -- the actions a recipe may take -----------------------------------

    def _call(self, verb: str, **kwargs: Any) -> Any:
        try:
            return getattr(self._c, verb)(**kwargs)
        except TypeError as exc:
            raise ToolError(f"{verb} rejected arguments {sorted(kwargs)}: {exc}") from exc
        except Exception as exc:
            raise ToolError(f"{verb} failed: {type(exc).__name__}: {exc}") from exc

    def navigate(self, url: str) -> Any:
        return self._call("navigate", url=url)

    def click(self, selector: str) -> Any:
        return self._call("click", selector=selector)

    def type(self, selector: str, text: str) -> Any:
        return self._call("type", selector=selector, text=text)

    def fill(self, selector: str, text: str) -> Any:
        return self._call("fill", selector=selector, text=text)

    def extract(self, selector: str) -> Any:
        return self._call("extract", selector=selector)

    def wait_for(self, selector: str, timeout_ms: int = 10000) -> Any:
        return self._call("wait_for", selector=selector, timeout_ms=timeout_ms)

    def go_back(self) -> Any:
        return self._call("go_back")

    def scroll(self, direction: str = "down") -> Any:
        return self._call("scroll", direction=direction)


class ToolError(RuntimeError):
    """A browser call failed. Always a step failure, never a silent success."""


# --------------------------------------------------------------- approval ----


class GateApprover:
    """Errands' `Approver` protocol, implemented on orvima's `SentinelGate`.

    Three jobs, in order:

    1. queue the request so a human sees it in the same UI as everything else;
    2. wait for the verdict, bounded, honouring expiry;
    3. re-validate before saying yes.

    Step 3 is the one worth arguing for. The approval refers to a page as it was
    when the request was queued, and between queuing and answering the page can
    change. `GateApprover` therefore re-runs `gate.revalidate()` against the page
    as it is *now*, so an approval cannot be spent on an element that has since
    been replaced by something more dangerous. The runner's own verification
    would catch some of this afterwards; catching it before the click lands is
    the difference between a refusal and a consequence.
    """

    def __init__(
        self,
        gate: Any,
        session_id: str,
        *,
        timeout: float = 300.0,
        poll: float = 0.05,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        """
        `gate` needs `request_approval`, `pending`, `lapsed`, `resolve` and
        `revalidate`. orvima's `SentinelGate` has all five. Duck-typed on
        purpose so this does not import orvima - which keeps the dependency
        one-way and testable with a 20-line fake.

        `on_event` receives a dict per state change, for a caller that wants to
        stream progress. Errands does not log; it does not know what a log is.
        """
        self.gate = gate
        self.session_id = session_id
        self.timeout = timeout
        self.poll = poll
        self.on_event = on_event or (lambda event: None)
        self._lock = threading.Lock()
        #: request id -> verdict. The authority on "did a human answer", for the
        #: same reason `AgentLoop._approvals` exists: `gate.resolve()` pops the
        #: request, so from the waiting thread's side an answered request is
        #: indistinguishable from one that was dropped. See `resolve()`.
        self._verdicts: dict[str, bool] = {}

    def resolve(self, request_id: str, approved: bool) -> bool:
        """Record a human's answer. **This, not `gate.resolve`, is the entry point.**

        Callers that answer approvals must come through here so the verdict is
        recorded before the gate drops the request. Calling `gate.resolve()`
        directly still works for rendering, but the waiter will then read the
        absence as a drop and refuse - which is safe, and surprising enough to
        be worth a docstring.

        The gate is consulted **first**, because it is the thing that knows
        about expiry. Recording the verdict first would mean a yes given after
        the TTL was honoured locally even though the gate had already refused it,
        quietly resurrecting the exact bug the TTL exists to prevent.
        """
        handled = self.gate.resolve(request_id, approved)
        if handled is None:
            # Expired, or already answered. Nothing to record.
            return False
        with self._lock:
            self._verdicts[request_id] = approved
        return approved

    def __call__(self, request: ApprovalRequest) -> bool:
        from orvima.sentinel_gate import GateResult  # local: optional dependency

        plan = str((request.detail or {}).get("plan", ""))
        gate_result = GateResult(
            allowed=False,
            pending=True,
            risk=request.risk,
            reason=request.reason,
        )
        snap = getattr(request.detail, "snapshot", None)
        if not isinstance(snap, dict):
            snap = (request.detail or {}).get("snapshot")

        request_id = self.gate.request_approval(
            self.session_id, request.tool, request.args, gate_result, snap
        )
        self._emit("approval_requested", request_id, request)
        verdict = self._wait(request_id)
        self._emit("approval_resolved", request_id, request, approved=verdict)
        if not verdict:
            return False

        # Approved. Re-check against the page as it is now.
        recheck = self.gate.revalidate(
            self.session_id, request.tool, request.args, approved_risk=request.risk,
            plan=plan,
        )
        if not recheck.allowed:
            self._emit(
                "approval_invalidated", request_id, request,
                reason=recheck.reason, risk=recheck.risk,
            )
            return False
        self._emit("approval_revalidated", request_id, request, risk=recheck.risk)
        return True

    def _wait(self, request_id: str) -> bool:
        """Block until answered, expired, or timed out.

        Order matters, and the first check is the one that is easy to omit.
        Leaving the pending queue is **not** a verdict: `gate.resolve()` pops the
        request, so by the time this loop wakes, an answered request looks
        identical to a dropped one. Reading that as a refusal is safe but wrong
        - the human pressed the button. So the verdict side-channel is checked
        first, `lapsed` second, and only then is absence read as a drop.
        """
        deadline = time.monotonic() + self.timeout
        while True:
            with self._lock:
                if request_id in self._verdicts:
                    return self._verdicts.pop(request_id)
            if self.gate.lapsed(request_id):
                self._emit("approval_expired", request_id, None)
                return False
            pending = any(r.id == request_id for r in self.gate.pending(self.session_id))
            if not pending:
                # Gone, and never answered through us: something resolved it
                # behind the approver's back.
                self._emit("approval_dropped", request_id, None)
                return False
            if time.monotonic() > deadline:
                self._emit("approval_timeout", request_id, None, timeout=self.timeout)
                return False
            time.sleep(self.poll)

    def _emit(self, kind: str, request_id: str, request: ApprovalRequest | None, **extra: Any) -> None:
        event = {
            "type": kind,
            "request_id": request_id,
            "session_id": self.session_id,
            "tool": request.tool if request else "",
            "step_id": request.step_id if request else "",
            "risk": request.risk if request else "",
            "reason": request.reason if request else "",
            **extra,
        }
        try:
            self.on_event(event)
        except Exception:
            # Observability must never be able to fail an errand. A caller whose
            # event handler raises gets silence, not a broken purchase.
            pass


# ---------------------------------------------------------------- assembly ---


def build_gate(
    browser: OrvimaBrowser,
    *,
    classifier_mode: str = "off",
    approval_ttl: float = 300.0,
) -> Any:
    """An orvima gate bound to *this* browser.

    Built per browser on purpose - see the module docstring. The page URL and
    snapshot are the two things that turn "unknown, refuse" into "read the
    element and decide", and neither can be shared across sessions.
    """
    from orvima.sentinel_gate import make_gate

    return make_gate(
        classifier_mode=classifier_mode,
        page_url=lambda: browser.url,
        snapshot=lambda: browser.snapshot_safe(),
        approval_ttl=approval_ttl,
    )


def runner_for(
    controller: Any,
    errands: list[Errand],
    *,
    session_id: str,
    classifier_mode: str = "off",
    approval_timeout: float = 300.0,
    on_event: Callable[[dict], None] | None = None,
) -> tuple[ErrandRunner, OrvimaBrowser, Any]:
    """Assemble a runner wired to a live browser and a live gate.

    Returns the runner plus the browser and gate it is using, because a caller
    almost always needs the gate afterwards - to render the approval queue, or
    to inspect stats. Handing back a runner with its collaborators hidden makes
    the next step guesswork.

    Example:

        runner, browser, gate = runner_for(controller, errands, session_id=s.id)
        result = runner.run_goal("buy this item", errands)
    """
    browser = OrvimaBrowser(controller)
    gate = build_gate(browser, classifier_mode=classifier_mode)
    policy = getattr(gate, "_policy", None)

    classify: Callable[[str, Mapping[str, Any], PlanContext], Any] | None = None
    if isinstance(policy, Policy):
        classify = make_classifier(policy, page_url=lambda: browser.url)
    # If Sentinel is not installed, `classify` stays None and the runner allows
    # every step. That is a real gap and it is reported rather than hidden:
    # Errands refuses gated steps when there is no classifier *and* an approver
    # is absent, so an unattended run still cannot buy anything.

    approver = GateApprover(
        gate, session_id, timeout=approval_timeout, on_event=on_event
    )
    runner = ErrandRunner(browser, classify=classify, approver=approver)
    return runner, browser, gate