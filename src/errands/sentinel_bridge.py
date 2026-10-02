"""Connecting Errands' plan context to Sentinel's policy.

Kept in its own module so the dependency runs one way: Errands does not import
Sentinel, Sentinel does not import Errands. This file is the only place the two
meet, and it imports both. Errands stays testable with a 15-line fake;
`Policy.plan_text` stays usable from orvima without Errands installed.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from sentinel.policy import Policy

from .core import PlanContext


def _resolve_page_url(page_url: str | Callable[[], str] | None) -> str:
    """Accept a URL, a callable returning one, or nothing.

    The callable form is the one that matters: the URL is read per decision, so
    a classifier built once still sees the right page after a navigation. But a
    plain string is accepted too, because `OrvimaBrowser.url` is a property, so
    `page_url=browser.url` is the natural thing to write and it is a string by
    the time it arrives here. Calling that would raise `TypeError: 'str' object
    is not callable` from inside `classify` - at decision time, on the first
    gated action, with the traceback pointing nowhere near the wiring mistake.
    """
    if page_url is None:
        return ""
    if callable(page_url):
        return page_url()
    return str(page_url)


def make_classifier(
    policy: Policy,
    *,
    page_url: str | Callable[[], str] | None = None,
    item_resolver: Callable[[Mapping[str, object]], str] | None = None,
) -> Callable[[str, Mapping[str, Any], PlanContext], Any]:
    """Adapt `Policy.evaluate` to the signature `ErrandRunner` expects.

    The runner hands over a `PlanContext`; the policy wants a flat string. The
    wording lives in `PlanContext.as_plan_text` rather than here, because the
    context belongs to Errands and both a classifier and a human read it. The
    unused `Policy.plan_text` helper exists for callers that have loose strings
    rather than a context - orvima's gate, for one.

    `page_url` may be a callable, for a URL that changes between steps, or a
    plain string, for one that does not. See `_resolve_page_url`.
    """

    def classify(tool: str, args: Mapping[str, Any], context: PlanContext):
        return policy.evaluate(
            tool,
            dict(args or {}),
            page_url=_resolve_page_url(page_url),
            item_resolver=item_resolver,
            plan=context.as_plan_text(),
        )

    return classify


def describe_step(request: Any) -> str:
    """One line for a human deciding on an approval.

    The plan matters here more than the action does. "browse_click" tells nobody
    anything; "cancel my subscription, step 3 of 5, next: delete account" does.
    """
    detail = getattr(request, "detail", {}) or {}
    plan = detail.get("plan", "")
    reason = getattr(request, "reason", "")
    risk = getattr(request, "risk", "")
    parts = [f"risk={risk}"]
    if plan:
        parts.append(f"plan: {plan}")
    if reason:
        parts.append(reason)
    return " | ".join(parts)