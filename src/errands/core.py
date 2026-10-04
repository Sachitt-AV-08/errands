"""Errand: a narrow goal run as a fixed, verified sequence of steps.

The design constraint that matters
---------------------------------
There is no generative planning here. An errand is a recipe someone wrote,
checked against a real site, and a goal it either matches or does not. When the
goal does not match, Errands says so plainly rather than improvising - the same
fail-closed rule the gate follows, applied to intent rather than to actions.

Why that matters concretely: a click on "Continue" is indistinguishable from a
click on "Place order" when judged one step at a time. Judged against a *plan*,
the difference is obvious - a recipe that has already passed a login wall and
reached the payment step expects a Continue; one that has not does not. So every
step is classified with the plan's context attached, which is why
`PlanContext` exists here and not only in the gate.

The three gates a step passes through
------------------------------------
1. `preflight`  - is this recipe still what the page looks like? Catches drift
   (a login wall, a changed price) *before* step one, not halfway through.
2. `step`       - is this particular action safe to run unattended? Delegates to
   Sentinel, with the plan's context attached.
3. `verify`     - did the step do what it claimed? A step that cannot prove
   itself is a step that reports failure rather than continuing.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


class ErrandState(str, Enum):
    """Where an errand got to. Terminal states are DONE, REFUSED, FAILED."""

    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"      # a step needs a human
    DONE = "done"
    REFUSED = "refused"      # preflight said this is not the errand we thought
    FAILED = "failed"        # a step ran and did not verify
    ABANDONED = "abandoned"  # a human said no


class RefusalReason(str, Enum):
    """Why Errands declined. Every refusal names itself."""

    NO_MATCH = "no_match"                    # no recipe fits the goal
    AMBIGUOUS = "ambiguous"                  # several recipes fit equally well
    PRECONDITION_FAILED = "precondition_failed"  # the page is not what we expected
    DRIFT = "drift"                          # the page changed mid-errand
    UNSAFE = "unsafe"                        # a step is not safe to run
    VERIFICATION_FAILED = "verification_failed"


class ErrandError(Exception):
    """Raised when an errand cannot proceed. Carries a machine-readable reason."""

    def __init__(self, reason: RefusalReason, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.detail = detail or {}


# --------------------------------------------------------------- contracts ----


class Browser(Protocol):
    """The slice of a browser an errand needs.

    Declared structurally so Errands does not depend on orvima. The real
    `BrowserController` satisfies it; the test fakes do too.
    """

    def snapshot(self) -> dict: ...
    def navigate(self, url: str) -> dict: ...
    def click(self, selector: str) -> dict: ...
    def type(self, selector: str, text: str) -> dict: ...
    def fill(self, selector: str, text: str) -> dict: ...
    def extract(self, selector: str) -> dict: ...
    def wait_for(self, selector: str, timeout_ms: int = 10000) -> dict: ...
    def go_back(self) -> dict: ...
    def scroll(self, direction: str = "down") -> dict: ...


class Approver(Protocol):
    """Asks a human about one action. Returns True to proceed."""

    def __call__(self, request: "ApprovalRequest") -> bool: ...


@dataclass
class ApprovalRequest:
    step_id: str
    tool: str
    args: dict
    risk: str
    reason: str
    detail: dict = field(default_factory=dict)


# ------------------------------------------------------------------ context ----


@dataclass
class PlanContext:
    """What the errand knows about itself, attached to every step.

    This is the context that makes an unremarkable control legible. A click on
    "Continue" is `low` in isolation and `outward` when the plan says the next
    step is payment, so the plan's position is part of the risk input.
    """

    errand: str
    step_id: str
    step_index: int
    step_count: int
    goal: str = ""
    #: what this step is for, in the recipe author's words
    intent: str = ""
    #: free-form facts the plan asserts about where it is
    notes: dict = field(default_factory=dict)
    history: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """One line a human can read in an approval dialog.

        Includes the goal, not just the errand's internal name. Someone
        answering a prompt should see the thing they were actually asked to do -
        "cancel_subscription step 2/3" means nothing on its own, and the person
        approving is the only check that the right thing is being cancelled.
        """
        parts = [f"{self.errand} step {self.step_index + 1}/{self.step_count} ({self.step_id})"]
        if self.goal:
            parts.append(f"goal: {self.goal}")
        if self.intent:
            parts.append(f"now: {self.intent}")
        if self.notes.get("next_step"):
            parts.append(f"next: {self.notes['next_step']}")
        return "; ".join(parts)

    def as_text(self) -> str:
        """The plan in words, for a human reading an approval dialog."""
        return " ".join(
            filter(
                None,
                [
                    f"errand {self.errand}",
                    f"goal {self.goal}" if self.goal else "",
                    f"step {self.step_id}",
                    f"intent {self.intent}" if self.intent else "",
                    " ".join(f"{k} {v}" for k, v in self.notes.items()),
                ],
            )
        )

    def as_plan_text(self) -> str:
        """The plan as the classifier consumes it.

        Distinct from `as_text` on purpose. A human wants the history and every
        note; a concept matcher wants the errand, the goal, this step's intent,
        and what comes next - because those are the four things that decide
        whether an unremarkable control is the last safe moment before a charge.

        Deliberately excludes the step *history*: "already did the login step"
        is true of nearly every step after the first and would drown the signal.
        """
        return " ".join(
            filter(
                None,
                [
                    self.errand,
                    self.goal,
                    self.step_id,
                    self.intent,
                    str(self.notes.get("next_step", "")) if self.notes else "",
                ],
            )
        ).lower()


# ------------------------------------------------------------------- steps ----


@dataclass
class Step:
    """One action, with the evidence that it worked.

    `verify` returning False is a *failure*, not a warning. An errand that
    cannot tell whether it worked must not continue as though it did - that is
    how an agent ends up clicking "Place order" twice on a page it misread.
    """

    id: str
    tool: str
    args: dict = field(default_factory=dict)
    intent: str = ""
    #: words that must appear on the page afterwards for this step to count
    expect: Sequence[str] = ()
    #: words whose presence means the step did *not* work
    reject: Sequence[str] = ()
    #: run this before the step and skip it if it returns False
    unless_present: Sequence[str] = ()
    #: allow this step to run unattended at all
    gated: bool = True
    #: a callable for dynamic args; `(context, browser) -> dict`
    compute: Callable[[PlanContext, Any], dict] | None = None

    def resolved_args(self, context: PlanContext, browser: Any) -> dict:
        if self.compute is not None:
            extra = self.compute(context, browser) or {}
            return {**self.args, **extra}
        return dict(self.args)


@dataclass
class Precondition:
    """A claim about the page, checked before step one.

    Drift detection lives here on purpose. A login wall that appears at step
    four means three steps were spent working around it; catching it up front
    turns a confusing failure into a clear refusal.
    """

    id: str
    #: all of these must be present for the errand to be viable
    expect: Sequence[str] = ()
    #: any of these means the page is not what the recipe was written against
    reject: Sequence[str] = ()
    #: run before the errand starts; False aborts
    check: Callable[[Any], bool] | None = None
    #: a mismatch is a drift (page changed) rather than a plain precondition
    is_drift: bool = False


@dataclass
class Errand:
    """A named goal with a verified way of achieving it."""

    name: str
    #: phrases that mean "yes, this is that errand"
    triggers: Sequence[str] = ()
    #: the thing being asked for, for display
    goal_template: str = ""
    preconditions: Sequence[Precondition] = ()
    steps: Sequence[Step] = ()
    #: how closely the goal must match; higher is stricter
    match_threshold: float = 0.5
    description: str = ""
    tags: Sequence[str] = field(default_factory=list)

    def step(self, step_id: str) -> Step:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"errand {self.name!r} has no step {step_id!r}")

    @property
    def step_count(self) -> int:
        return len(self.steps)

    def summary(self) -> str:
        return {
            "name": self.name,
            "description": self.description,
            "triggers": list(self.triggers),
            "steps": [
                {"id": s.id, "tool": s.tool, "intent": s.intent, "gated": s.gated}
                for s in self.steps
            ],
            "tags": list(self.tags),
        }


# ----------------------------------------------------------------- matching ----

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def score_match(goal: str, errand: Errand) -> float:
    """How well a goal matches an errand, in [0, 1].

    Token overlap against the triggers, not against the description. A
    description is written for a human reading a list; a trigger is written to be
    matched. Blurring the two is how you end up offering to unsubscribe from a
    newsletter because the goal mentioned "cancel".
    """
    goal_tokens = _tokens(goal)
    if not goal_tokens:
        return 0.0
    best = 0.0
    for trigger in errand.triggers:
        trigger_tokens = _tokens(trigger)
        if not trigger_tokens:
            continue
        shared = goal_tokens & trigger_tokens
        # Favour the trigger being *covered* by the goal, not the reverse: a
        # one-word trigger matched by a long rambling goal should not score 1.0.
        coverage = len(shared) / len(trigger_tokens)
        precision = len(shared) / len(goal_tokens)
        best = max(best, 0.7 * coverage + 0.3 * precision)
    return round(best, 4)


@dataclass
class Match:
    errand: Errand
    score: float

    @property
    def confident(self) -> bool:
        return self.score >= self.errand.match_threshold


def match(goal: str, errands: Sequence[Errand]) -> Match | None:
    """Pick the best errand, or refuse.

    Refuses when nothing clears the bar, and also when two do - an ambiguous
    goal gets a question, not a coin flip. Improvising is the failure mode this
    whole project exists to avoid.
    """
    if not errands:
        return None
    scored = sorted(
        (Match(e, score_match(goal, e)) for e in errands),
        key=lambda m: m.score,
        reverse=True,
    )
    best = scored[0]
    if not best.confident:
        return None
    runner_up = scored[1].score if len(scored) > 1 else 0.0
    # A near-tie means the goal really was ambiguous; better to ask.
    if best.score - runner_up < 0.1 and runner_up >= best.errand.match_threshold:
        return None
    return best

# ------------------------------------------------------------------ runner ----


@dataclass
class StepResult:
    step_id: str
    ok: bool
    risk: str = "safe"
    reason: str = ""
    output: Any = None
    detail: dict = field(default_factory=dict)


@dataclass
class ErrandResult:
    errand: str
    state: ErrandState
    steps: list[StepResult] = field(default_factory=list)
    reason: str = ""
    refused: RefusalReason | None = None
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.state is ErrandState.DONE

    def as_dict(self) -> dict:
        return {
            "errand": self.errand,
            "state": self.state.value,
            "reason": self.reason,
            "refused": self.refused.value if self.refused else None,
            "steps": [asdict(s) for s in self.steps],
            "detail": self.detail,
        }


def _page_text(browser: Any) -> str:
    """Everything readable on the page, lowercased, for expect/reject matching.

    The snapshot's ``body`` is included, and it has to be. A snapshot describes
    interactive elements: their labels, values, placeholders. Most of what a
    recipe asserts on is prose that no element owns - "Order total: $30.00",
    "payment declined", "your cart is empty". Reading only item fields meant
    verification could not see any of it, so on a real site every step whose
    `expect` named page prose failed, while the scripted shop in
    `tests/support/shop.py` passed because it puts the same words on items.

    Found by running the shipped recipe against a real browser: `review_total`
    expected "total", the page plainly read "Order total: $30.00", and verify
    reported "none of the expected text is on the page".
    """
    try:
        snap = browser.snapshot() or {}
    except Exception:
        return ""
    parts: list[str] = [
        str(snap.get("title") or ""),
        str(snap.get("url") or ""),
        str(snap.get("body") or ""),
    ]
    for item in snap.get("items", []) or []:
        if not isinstance(item, Mapping):
            continue
        for key in ("label", "text", "name", "placeholder", "value"):
            value = item.get(key)
            if value:
                parts.append(str(value))
    return " ".join(parts).lower()


def _absent(expected: Iterable[str], haystack: str) -> list[str]:
    """The listed words that are *not* on the page."""
    return [w for w in expected if w and str(w).lower() not in haystack]


def _risk_name(risk: Any) -> str:
    """The risk tier as a plain string, whichever way the classifier spells it.

    `Risk` is a `str` Enum, but `str(Risk.DESTRUCTIVE)` is `"Risk.DESTRUCTIVE"`
    on Python 3.11+, not `"destructive"`. An approval dialog showing the former
    is a dialog showing a Python repr to a person deciding on a purchase, so
    this normalises it rather than relying on either behaviour.
    """
    value = getattr(risk, "value", risk)
    return str(value) if value is not None else "unknown"


def _present(rejected: Iterable[str], haystack: str) -> list[str]:
    """The listed words that *are* on the page - i.e. the ones that tripped.

    A `reject` list means "any of these on the page means this step did not
    work", so the test for it is presence, and the report should name exactly
    which words tripped. Inverting this is the difference between catching
    "payment declined" and reporting it as missing.
    """
    return [w for w in rejected if w and str(w).lower() in haystack]


def _any_present(words: Iterable[str], haystack: str) -> bool:
    """True if at least one non-empty word is on the page.

    `expect` is an OR across alternatives, not an AND. A recipe that accepts
    either "subscription" or "membership" as proof it is on the right page
    should say so in one list; making it AND would mean every recipe needs a
    separate author per wording, and the ones that got it wrong would fail on
    the one site variation nobody tested.
    """
    return any(w and str(w).lower() in haystack for w in words)


class ErrandRunner:
    """Executes one recipe against one browser.

    Three gates per step, in order: preflight once up front, Sentinel per step,
    verification per step. Any of them failing stops the run - there is no
    "continue anyway", because a step that did not verify leaves the plan
    describing a world that is no longer true.
    """

    def __init__(
        self,
        browser: Any,
        *,
        classify: Callable[[str, Mapping[str, Any], PlanContext], Any] | None = None,
        approver: Approver | None = None,
        verify: Callable[[bool], bool] | None = None,
    ) -> None:
        """
        `classify` is Sentinel's entry point. Injected rather than imported so
        Errands stays runnable - and testable - with no ML dependency present;
        without one, steps still run and nothing is gated.
        `approver` asks a human. None means any gated step is refused, which is
        the correct default: unattended means unattended.
        """
        self.browser = browser
        self.classify = classify
        self.approver = approver
        self.verify = verify

    # -- gate 1: is this recipe still what the page looks like? ---------------

    def preflight(self, errand: Errand) -> None:
        """Raise `ErrandError` if the page is not what the recipe expects.

        All preconditions are checked before any is reported, and a drift
        condition outranks a plain miss when both fire. A session-expired page
        fails every `expect` in the recipe - there is no cart, no plan, no
        account on it - so reporting the first missing word would send someone
        looking for a cart that is not the problem. "Your session expired" is
        both the true cause and the more useful thing to read.
        """
        #: (precondition, missing expected words, unexpected words found, error)
        failed: list[tuple[Precondition, list[str], list[str], str | None]] = []
        page = _page_text(self.browser)

        for pre in errand.preconditions:
            if pre.check is not None:
                try:
                    if not pre.check(self.browser):
                        failed.append((pre, [], [], None))
                except Exception as exc:  # noqa: BLE001 - a broken probe is a refusal
                    raise ErrandError(
                        RefusalReason.PRECONDITION_FAILED,
                        f"precondition {pre.id!r} raised: {exc}",
                        {"precondition": pre.id, "error": str(exc)},
                    ) from exc
                continue

            missing = _absent(pre.expect, page) if (pre.expect and not _any_present(pre.expect, page)) else []
            unexpected = _present(pre.reject, page)
            if missing or unexpected:
                failed.append((pre, missing, unexpected, None))

        if not failed:
            return

        # Drift outranks a plain miss. A session-expired page has no cart, no
        # plan and no account on it, so every `expect` in the recipe fails; the
        # useful answer is the one rule that fired deliberately.
        chosen = next((f for f in failed if f[0].is_drift), failed[0])
        pre, missing, unexpected, error = chosen
        raise ErrandError(
            RefusalReason.DRIFT if pre.is_drift else RefusalReason.PRECONDITION_FAILED,
            (
                f"precondition {pre.id!r} not met: "
                f"found_none_of={missing or 'n/a'}, "
                f"unexpected={unexpected or 'none'}"
                + (f", error={error}" if error else "")
            ),
            {
                "precondition": pre.id,
                "missing": missing,
                "unexpected": unexpected,
                "also_failed": [f[0].id for f in failed if f[0] is not pre],
            },
        )

    # -- gate 2: is this action safe? ----------------------------------------

    def _gate(self, context: PlanContext, step: Step, args: Mapping[str, Any]):
        """Return (allowed, risk, reason). Absent classifier means allow."""
        if not step.gated or self.classify is None:
            return True, "safe", "no classifier attached"
        decision = self.classify(step.tool, args, context)
        needs_human = bool(getattr(decision, "needs_human", False))
        risk = _risk_name(getattr(decision, "risk", None))
        reason = str(getattr(decision, "reason", ""))
        if not needs_human:
            return True, risk, reason
        if self.approver is None:
            return False, risk, reason or "needs a human"
        request = ApprovalRequest(
            step_id=step.id,
            tool=step.tool,
            args=dict(args),
            risk=risk,
            reason=f"{reason} | {context.summary()}" if reason else context.summary(),
            detail={"plan": context.as_text()},
        )
        approved = bool(self.approver(request))
        return approved, risk, f"human {'approved' if approved else 'declined'}: {reason}"

    # -- gate 3: did it do what it claimed? ----------------------------------

    def _verify(self, step: Step) -> tuple[bool, str]:
        if not step.expect and not step.reject:
            return True, "nothing to verify"
        page = _page_text(self.browser)
        # Reject first. "Payment declined" on the page is a definitive answer to
        # "did this work?", whereas the absence of the success text is only
        # suggestive - the site may word it differently than the recipe author
        # saw. Reporting the definitive one saves someone reading "expected text
        # absent" and wondering whether the site broke.
        found = _present(step.reject, page)
        if found:
            return False, f"failure text on page: {found}"
        if step.expect and not _any_present(step.expect, page):
            return False, f"none of the expected text is on the page: {list(step.expect)}"
        return True, f"verified against {len(step.expect)} accepted phrase(s)"

    # -- run -----------------------------------------------------------------

    def run(self, errand: Errand, goal: str = "") -> ErrandResult:
        history: list[str] = []
        try:
            self.preflight(errand)
        except ErrandError as exc:
            return ErrandResult(
                errand=errand.name,
                state=ErrandState.REFUSED,
                reason=str(exc),
                refused=exc.reason,
                detail=exc.detail,
            )

        results: list[StepResult] = []
        for index, step in enumerate(errand.steps):
            context = PlanContext(
                errand=errand.name,
                step_id=step.id,
                step_index=index,
                step_count=len(errand.steps),
                goal=goal or errand.goal_template,
                intent=step.intent,
                notes=dict(errand.tags and {} or {}),
                history=list(history),
            )
            context.notes = {f"next_step": errand.steps[index + 1].intent
                             if index + 1 < len(errand.steps) else "end_of_errand",
                             "triggers": ", ".join(errand.triggers)}

            page = _page_text(self.browser)
            if step.unless_present and all(w.lower() in page for w in step.unless_present):
                results.append(StepResult(step.id, ok=True, reason="precondition already met, skipped"))
                history.append(f"{step.id}:skipped")
                continue

            args = step.resolved_args(context, self.browser)
            allowed, risk, why = self._gate(context, step, args)
            if not allowed:
                state = ErrandState.ABANDONED if self.approver is not None else ErrandState.REFUSED
                results.append(StepResult(step.id, ok=False, risk=risk, reason=why))
                return ErrandResult(
                    errand=errand.name,
                    state=state,
                    steps=results,
                    reason=f"step {step.id!r} not permitted: {why}",
                    refused=RefusalReason.UNSAFE,
                    detail={"step": step.id, "risk": risk, "plan": context.as_text()},
                )

            try:
                output = self._invoke(step, args)
            except ErrandError as exc:
                # Raised for a step the recipe is not allowed to make. That is a
                # refusal of the recipe, not a crash of the runner.
                results.append(StepResult(step.id, ok=False, risk=risk, reason=str(exc)))
                return ErrandResult(
                    errand=errand.name,
                    state=ErrandState.REFUSED,
                    steps=results,
                    reason=str(exc),
                    refused=exc.reason,
                    detail=exc.detail,
                )
            except Exception as exc:  # noqa: BLE001 - the tool failing is a step failure
                results.append(StepResult(step.id, ok=False, risk=risk, reason=f"tool raised: {exc}"))
                return ErrandResult(
                    errand=errand.name,
                    state=ErrandState.FAILED,
                    steps=results,
                    reason=f"step {step.id!r} raised: {exc}",
                    refused=RefusalReason.VERIFICATION_FAILED,
                    detail={"step": step.id, "error": str(exc)},
                )

            ok, why = self._verify(step)
            results.append(StepResult(step.id, ok=ok, risk=risk, reason=why, output=output))
            if not ok:
                return ErrandResult(
                    errand=errand.name,
                    state=ErrandState.FAILED,
                    steps=results,
                    reason=f"step {step.id!r} did not verify: {why}",
                    refused=RefusalReason.VERIFICATION_FAILED,
                    detail={"step": step.id},
                )
            history.append(f"{step.id}:ok")

        return ErrandResult(
            errand=errand.name,
            state=ErrandState.DONE,
            steps=results,
            reason=f"{len(results)} steps completed",
            detail={"goal": goal or errand.goal_template},
        )

    def _invoke(self, step: Step, args: Mapping[str, Any]) -> Any:
        """Call the browser. Only these verbs exist; a recipe cannot invent one."""
        allowed = {
            "browse_click": "click",
            "browse_type": "type",
            "browse_fill": "fill",
            "browse_extract": "extract",
            "browse_wait_for": "wait_for",
            "browse_go_back": "go_back",
            "browse_scroll": "scroll",
        }
        method = allowed.get(step.tool)
        if method is None:
            if step.tool == "browse_navigate":
                return self.browser.navigate(str(args.get("url", "")))
            raise ErrandError(
                RefusalReason.NO_MATCH,
                f"step {step.id!r} names tool {step.tool!r}, which recipes cannot call",
                {"tool": step.tool},
            )
        return getattr(self.browser, method)(**args)

    # -- goal in, result out -------------------------------------------------

    def run_goal(self, goal: str, errands: Sequence[Errand]) -> ErrandResult:
        """Match then run. This is the whole entry point a caller needs."""
        found = match(goal, errands)
        if found is None:
            near = sorted(
                ((score_match(goal, e), e.name) for e in errands), reverse=True
            )[:3]
            return ErrandResult(
                errand="",
                state=ErrandState.REFUSED,
                reason="no recipe matched the goal confidently",
                refused=RefusalReason.NO_MATCH,
                detail={"goal": goal, "nearest": [{"errand": n, "score": s} for s, n in near]},
            )
        return self.run(found.errand, goal)
