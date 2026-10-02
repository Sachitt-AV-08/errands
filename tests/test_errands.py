"""Errands: matching, plan context, the three gates.

The fakes are deliberately crude - a browser that records calls and returns
worded pages. Everything interesting here is the runner's decisions, not the
browser.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from errands import (
    Errand,
    ErrandError,
    ErrandRunner,
    ErrandState,
    Precondition,
    RefusalReason,
    Step,
    errand_from_dict,
    load_dir,
    match,
    registry,
    score_match,
)


# ------------------------------------------------------------------- fakes ----


class FakeBrowser:
    """A browser with no browser in it.

    `pages` maps a selector to what the page looks like *after* acting on it, so
    a recipe can be tested end to end without a browser, a network, or a site.
    """

    def __init__(self, page: dict | None = None) -> None:
        self.page = page or {"url": "https://example.com/", "title": "Example", "items": []}
        self.calls: list[tuple[str, dict]] = []

    # -- the slice an errand uses
    def snapshot(self) -> dict:
        return self.page

    def _record(self, verb: str, **kwargs):
        self.calls.append((verb, kwargs))
        result = dict(self.page)
        result.update(self._after.get(verb, {}))
        self.page = result
        return {"ok": True, "url": result.get("url", ""), "text": " ".join(
            i.get("label", "") for i in result.get("items", []) if isinstance(i, dict)
        )}

    _after: dict = {}

    def click(self, selector: str, **_kw): return self._record("click", selector=selector)
    def type(self, selector: str, text: str, **_kw): return self._record("type", selector=selector, text=text)
    def fill(self, selector: str, text: str, **_kw): return self._record("fill", selector=selector, text=text)
    def extract(self, selector: str, **_kw): return self._record("extract", selector=selector)
    def wait_for(self, selector: str, timeout_ms: int = 10000, **_kw):
        return self._record("wait", selector=selector)
    def go_back(self, **_kw): return self._record("back")
    def scroll(self, direction: str = "down", **_kw): return self._record("scroll", direction=direction)

    def navigate(self, url: str): return self._record("navigate", url=url)


class FakePolicy:
    """Stands in for Sentinel. Records the plan it was handed."""

    def __init__(self, gate_on: str = "") -> None:
        #: gate any action whose plan contains this word
        self.gate_on = gate_on
        self.seen: list[tuple[str, dict, str]] = []

    def evaluate(self, tool, args=None, context=None, **_kw):
        """Matches the runner's contract: it hands over a `PlanContext`."""
        plan = context.as_plan_text() if context is not None else ""
        self.seen.append((tool, dict(args or {}), plan))
        needs = bool(self.gate_on) and self.gate_on in plan

        class D:
            needs_human = needs
            risk = "outward" if needs else "low"
            reason = f"plan mentions {self.gate_on!r}" if needs else "benign"

        return D()

    def plans(self) -> list[str]:
        return [p for _, _, p in self.seen]


def approver(verdict: bool = True):
    calls: list = []

    def ask(request):
        calls.append(request)
        return verdict

    ask.calls = calls  # type: ignore[attr-defined]
    return ask


# -------------------------------------------------------------- the recipes ----


def unsubscribe() -> Errand:
    return Errand(
        name="cancel_subscription",
        triggers=("cancel my subscription", "cancel subscription", "unsubscribe from the plan"),
        goal_template="cancel my subscription",
        preconditions=[Precondition(id="on_account", expect=("subscription", "membership"))],
        steps=[
            Step(id="open", tool="browse_click", args={"selector": "Manage plan"},
                 intent="open the plan settings page", expect=("subscription", "membership")),
            Step(id="cancel", tool="browse_click", args={"selector": "Cancel plan"},
                 intent="cancel the subscription", expect=("confirm", "are you sure", "cancelled")),
            Step(id="confirm", tool="browse_click", args={"selector": "Yes, cancel",
                 "gated": True},
                 intent="confirm cancellation", expect=("cancelled", "ended", "inactive")),
        ],
    )


def one_step() -> Errand:
    return Errand(
        name="read_orders",
        triggers=("check my orders", "list my orders"),
        steps=[Step(id="read", tool="browse_extract", args={"selector": "#orders"},
                    intent="read the order list", gated=False)],
    )


# ------------------------------------------------------------------ matching ----


def test_a_goal_that_names_the_errand_matches() -> None:
    found = match("cancel my subscription", [unsubscribe(), one_step()])
    assert found is not None and found.errand.name == "cancel_subscription"


def test_an_unrelated_goal_matches_nothing() -> None:
    """The refusal that matters. Errands must decline, not improvise."""
    assert match("book me a flight to Lisbon", [unsubscribe(), one_step()]) is None


def test_an_empty_goal_matches_nothing() -> None:
    assert match("", [unsubscribe(), one_step()]) is None


def test_two_recipes_fitting_equally_is_a_refusal() -> None:
    """Ambiguity gets a question, not a coin flip."""
    a = Errand(name="a", triggers=("cancel subscription",), steps=unsubscribe().steps)
    b = Errand(name="b", triggers=("cancel subscription",), steps=unsubscribe().steps)
    assert match("cancel subscription", [a, b]) is None


def test_triggers_score_on_coverage_not_raw_overlap() -> None:
    """A long rambling goal should not match a two-word trigger perfectly."""
    long_goal = (
        "I was wondering if you could possibly help me today by cancelling "
        "the subscription that I set up last month thanks very much"
    )
    score = score_match(long_goal, unsubscribe())
    assert 0.0 < score < 0.7, f"a rambling goal scored {score}, too close to a confident match"


def test_match_reports_the_nearest_misses_on_refusal() -> None:
    runner = ErrandRunner(FakeBrowser())
    result = runner.run_goal("book me a flight to Lisbon", [unsubscribe(), one_step()])
    assert result.state is ErrandState.REFUSED
    assert result.refused is RefusalReason.NO_MATCH
    assert result.detail["nearest"], "a refusal should say what it considered"


# ------------------------------------------------------------------ preflight ----


def test_preflight_refuses_when_the_page_is_not_what_the_recipe_expects() -> None:
    """Catching it up front, not three steps in."""
    browser = FakeBrowser({"url": "https://shop/", "title": "Shop",
                           "items": [{"ref": "e1", "label": "Add to cart"}]})
    result = ErrandRunner(browser).run(unsubscribe(), "cancel my subscription")

    assert result.state is ErrandState.REFUSED
    assert result.refused is RefusalReason.PRECONDITION_FAILED
    assert "subscription" in result.detail["missing"]
    assert browser.calls == [], "it ran steps against a page it knew was wrong"


def test_preflight_rejects_text_that_should_not_be_there() -> None:
    """The right page by every `expect`, but carrying a word that rules it out."""
    errand = Errand(
        name="thing", triggers=("thing",),
        preconditions=[Precondition(id="shape", expect=("account",),
                                    reject=("session expired",))],
        steps=[Step(id="s", tool="browse_click", args={"selector": "x"})],
    )
    browser = FakeBrowser({"url": "https://x/", "title": "Account",
                           "items": [{"ref": "e1", "label": "session expired, please sign in"}]})
    result = ErrandRunner(browser).run(errand)
    assert result.state is ErrandState.REFUSED
    assert "session expired" in result.detail["unexpected"]


def test_preflight_distinguishes_drift_from_a_missing_precondition() -> None:
    """A page that has changed is a different failure from a page never right."""
    errand = Errand(
        name="thing", triggers=("thing",),
        preconditions=[Precondition(id="shape", expect=("widget",), is_drift=True)],
        steps=[Step(id="s", tool="browse_click", args={"selector": "x"})],
    )
    result = ErrandRunner(FakeBrowser()).run(errand)
    assert result.refused is RefusalReason.DRIFT


def test_a_broken_precondition_probe_refuses_rather_than_passes() -> None:
    errand = Errand(
        name="thing", triggers=("thing",),
        preconditions=[Precondition(id="probe", check=lambda b: 1 / 0)],
        steps=[Step(id="s", tool="browse_click", args={"selector": "x"})],
    )
    result = ErrandRunner(FakeBrowser()).run(errand)
    assert result.state is ErrandState.REFUSED
    assert result.refused is RefusalReason.PRECONDITION_FAILED


# ---------------------------------------------------------------- plan context ----


def test_plan_context_is_handed_to_the_classifier_on_every_step() -> None:
    """The whole point of Errands: the classifier knows which step this is."""
    browser = FakeBrowser({"url": "https://x/", "title": "Account",
                           "items": [{"ref": "e1", "label": "subscription"}]})
    browser._after = {
        "click": {"items": [{"ref": "e1", "label": "subscription confirm yes are you sure cancelled"}]},
    }
    policy = FakePolicy()
    ErrandRunner(browser, classify=policy.evaluate).run(unsubscribe(), "cancel my subscription")

    assert len(policy.plans()) == 3
    assert all("cancel_subscription" in p for p in policy.plans())
    # The final step's plan should mention what it is about to do.
    assert "confirm cancellation" in policy.plans()[-1]


def _clicked_selectors(browser: FakeBrowser) -> list[str]:
    return [kw.get("selector") for _, kw in browser.calls if "selector" in kw]


def test_a_step_is_gated_by_the_plan_not_just_the_action() -> None:
    """A click on a benign-looking control is still gated when the plan is not.

    The gating phrase is "confirm cancellation", which appears in the *plan* of
    more than one step - the step before it names the confirmation as what comes
    next. That is deliberate: a click that is about to become a cancellation is
    already part of the cancellation, and prompting one step early is the cheap
    direction to be wrong in.
    """
    browser = FakeBrowser({"url": "https://x/", "title": "Account",
                           "items": [{"ref": "e1", "label": "subscription"}]})
    browser._after = {"click": {"items": [{"ref": "e1", "label": "subscription confirm cancelled"}]}}

    policy = FakePolicy(gate_on="confirm cancellation")
    ask = approver(True)
    result = ErrandRunner(browser, classify=policy.evaluate, approver=ask).run(
        unsubscribe(), "cancel my subscription"
    )

    assert result.ok, result.reason
    assert ask.calls, "no step reached the human"
    assert "cancel my subscription" in ask.calls[0].reason
    # The prompt happened at the first step whose plan mentioned the act, which
    # is step two: it is the click that sets the cancellation up.
    assert ask.calls[0].step_id == "cancel"


def test_the_irreversible_click_never_runs_without_a_human() -> None:
    """The invariant that matters, stated once and checked three ways."""
    browser = FakeBrowser({"url": "https://x/", "title": "Account",
                           "items": [{"ref": "e1", "label": "subscription"}]})
    browser._after = {"click": {"items": [{"ref": "e1", "label": "subscription confirm cancelled"}]}}
    policy = FakePolicy(gate_on="confirm cancellation")

    result = ErrandRunner(browser, classify=policy.evaluate).run(
        unsubscribe(), "cancel my subscription"
    )

    assert result.state is ErrandState.REFUSED
    assert result.refused is RefusalReason.UNSAFE
    assert "Yes, cancel" not in _clicked_selectors(browser)


def test_a_step_the_human_declines_stops_the_errand() -> None:
    browser = FakeBrowser({"url": "https://x/", "title": "Account",
                           "items": [{"ref": "e1", "label": "subscription"}]})
    browser._after = {"click": {"items": [{"ref": "e1", "label": "subscription confirm cancelled"}]}}
    policy = FakePolicy(gate_on="confirm cancellation")
    ask = approver(False)

    result = ErrandRunner(browser, classify=policy.evaluate, approver=ask).run(
        unsubscribe(), "cancel my subscription"
    )
    assert result.state is ErrandState.ABANDONED
    assert result.refused is RefusalReason.UNSAFE
    assert "Yes, cancel" not in _clicked_selectors(browser), "it kept going after the human said no"
    assert len(_clicked_selectors(browser)) == 1, "it ran more steps than it was allowed"


def test_a_gated_step_with_no_approver_is_refused_not_run() -> None:
    """Unattended means unattended. No approver, no gated step."""
    browser = FakeBrowser({"url": "https://x/", "title": "Account",
                           "items": [{"ref": "e1", "label": "subscription"}]})
    policy = FakePolicy(gate_on="confirm cancellation")
    result = ErrandRunner(browser, classify=policy.evaluate).run(
        unsubscribe(), "cancel my subscription"
    )
    assert result.state is ErrandState.REFUSED
    assert "Yes, cancel" not in _clicked_selectors(browser)


def test_plan_context_travels_with_the_approval_request() -> None:
    browser = FakeBrowser({"url": "https://x/", "title": "Account",
                           "items": [{"ref": "e1", "label": "subscription"}]})
    browser._after = {"click": {"items": [{"ref": "e1", "label": "subscription confirm cancelled"}]}}
    policy = FakePolicy(gate_on="confirm cancellation")
    ask = approver(True)
    ErrandRunner(browser, classify=policy.evaluate, approver=ask).run(
        unsubscribe(), "cancel my subscription"
    )
    detail = ask.calls[0].detail
    assert "plan" in detail and "cancel_subscription" in detail["plan"]


# ------------------------------------------------------------------ execution ----


def test_a_step_that_cannot_verify_is_a_failure() -> None:
    """Not a warning. The plan no longer describes the world."""
    errand = Errand(
        name="thing", triggers=("thing",),
        steps=[Step(id="s", tool="browse_click", args={"selector": "x"},
                    expect=("order confirmed",), gated=False)],
    )
    browser = FakeBrowser({"url": "https://x/", "title": "T", "items": []})
    result = ErrandRunner(browser).run(errand)
    assert result.state is ErrandState.FAILED
    assert "order confirmed" in result.reason


def test_reject_text_on_the_page_fails_a_step_that_looks_done() -> None:
    errand = Errand(
        name="thing", triggers=("thing",),
        steps=[Step(id="s", tool="browse_click", args={"selector": "x"},
                    expect=("ok",), reject=("error", "declined"), gated=False)],
    )
    browser = FakeBrowser({"url": "https://x/", "title": "T",
                           "items": [{"ref": "e1", "label": "declined"}]})
    result = ErrandRunner(browser).run(errand)
    assert result.state is ErrandState.FAILED
    assert "declined" in result.reason


def test_a_tool_that_raises_fails_the_step_not_the_process() -> None:
    class Broken(FakeBrowser):
        def click(self, selector: str, **_kw):
            raise RuntimeError("detached target")

    errand = Errand(
        name="thing", triggers=("thing",),
        steps=[Step(id="s", tool="browse_click", args={"selector": "x"}, gated=False)],
    )
    result = ErrandRunner(Broken()).run(errand)
    assert result.state is ErrandState.FAILED
    assert "detached target" in result.reason


def test_a_recipe_cannot_call_a_tool_outside_the_allowed_set() -> None:
    """`browse_eval` is not something a recipe gets to reach for."""
    errand = Errand(
        name="thing", triggers=("thing",),
        steps=[Step(id="s", tool="browse_eval", args={"expression": "steal()"}, gated=False)],
    )
    result = ErrandRunner(FakeBrowser()).run(errand)
    # REFUSED, not FAILED: the recipe was never eligible to make this call, so
    # this is a rejection of the recipe rather than an attempt that went wrong.
    assert result.state is ErrandState.REFUSED
    assert "cannot call" in result.reason


def test_unless_present_skips_a_step_that_is_already_done() -> None:
    """Not idempotent by accident - the recipe says when a step is a no-op."""
    errand = Errand(
        name="thing", triggers=("thing",),
        steps=[Step(id="login", tool="browse_type", args={"selector": "#pw", "text": "x"},
                    unless_present=("dashboard",), gated=False)],
    )
    browser = FakeBrowser({"url": "https://x/", "title": "Dashboard", "items": []})
    result = ErrandRunner(browser).run(errand)
    assert result.ok
    assert browser.calls == []
    assert result.steps[0].reason.startswith("precondition already met")


def test_navigation_is_allowed_as_a_step() -> None:
    errand = Errand(
        name="thing", triggers=("thing",),
        steps=[Step(id="go", tool="browse_navigate", args={"url": "https://example.com/"},
                    gated=False)],
    )
    browser = FakeBrowser()
    result = ErrandRunner(browser).run(errand)
    assert result.ok
    assert browser.calls == [("navigate", {"url": "https://example.com/"})]


def test_a_successful_run_reports_every_step() -> None:
    browser = FakeBrowser({"url": "https://x/", "title": "Account",
                           "items": [{"ref": "e1", "label": "subscription"}]})
    browser._after = {"click": {"items": [{"ref": "e1", "label": "subscription confirm cancelled"}]}}
    result = ErrandRunner(browser).run(unsubscribe(), "cancel my subscription")
    assert result.state is ErrandState.DONE
    assert [s.step_id for s in result.steps] == ["open", "cancel", "confirm"]
    assert all(s.ok for s in result.steps)
    assert result.as_dict()["state"] == "done"


def test_dynamic_args_are_computed_per_step() -> None:
    seen: list[str] = []

    def compute(context, browser):
        seen.append(context.step_id)
        return {"selector": f"#{context.step_id}"}

    errand = Errand(
        name="thing", triggers=("thing",),
        steps=[
            Step(id="a", tool="browse_click", args={"selector": "ignored"},
                 compute=compute, gated=False),
            Step(id="b", tool="browse_click", args={"selector": "ignored"},
                 compute=compute, gated=False),
        ],
    )
    ErrandRunner(FakeBrowser()).run(errand)
    assert seen == ["a", "b"]


# ------------------------------------------------------------------ loading ----


def test_a_recipe_loads_from_json(tmp_path: Path) -> None:
    raw = {
        "name": "book_parking",
        "triggers": ["book parking"],
        "goal_template": "book parking downtown",
        "preconditions": [{"id": "on_map", "expect": ["parking"]}],
        "steps": [{"id": "pick", "tool": "browse_click", "args": {"selector": "#lot"},
                   "intent": "choose a lot", "expect": ["duration"]}],
    }
    path = tmp_path / "parking.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    errand = load_dir(tmp_path)[0]
    assert errand.name == "book_parking"
    assert errand.steps[0].expect == ("duration",)


def test_a_typo_in_a_recipe_key_is_an_error_not_a_skipped_check() -> None:
    """A misspelled `expectd` must not produce a step that verifies nothing."""
    with pytest.raises(ValueError, match="unknown keys"):
        errand_from_dict({
            "name": "x", "triggers": ["x"],
            "steps": [{"id": "s", "tool": "browse_click", "expectd": ["y"]}],
        })


def test_duplicate_step_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate step ids"):
        errand_from_dict({
            "name": "x", "triggers": ["x"],
            "steps": [{"id": "s", "tool": "browse_click"}, {"id": "s", "tool": "browse_click"}],
        })


def test_a_recipe_with_no_steps_is_rejected() -> None:
    with pytest.raises(ValueError, match="no steps"):
        errand_from_dict({"name": "x", "triggers": ["x"], "steps": []})


def test_two_recipes_with_one_name_are_rejected(tmp_path: Path) -> None:
    for filename in ("a.json", "b.json"):
        (tmp_path / filename).write_text(json.dumps({
            "name": "same", "triggers": ["x"],
            "steps": [{"id": "s", "tool": "browse_click"}],
        }), encoding="utf-8")
    with pytest.raises(ErrandError) as exc:
        registry(tmp_path)
    assert exc.value.reason is RefusalReason.AMBIGUOUS


def test_a_missing_recipe_directory_is_an_error_not_an_empty_list() -> None:
    """An empty registry makes every goal match nothing, which looks like a bug."""
    with pytest.raises(ErrandError):
        load_dir(Path("/nonexistent/recipes"))


def test_the_shipped_recipes_all_load_and_are_distinct() -> None:
    errands = registry(Path(__file__).resolve().parents[1] / "recipes")
    assert errands, "no recipes shipped"
    names = [e.name for e in errands]
    assert len(set(names)) == len(names)
    for errand in errands:
        assert errand.triggers, f"{errand.name} has no triggers and can never match"
        assert errand.steps, f"{errand.name} has no steps"
        assert all(s.tool.startswith("browse_") for s in errand.steps)


def test_no_shipped_recipe_calls_eval() -> None:
    """The gate is there to constrain the recipe, not to be worked around."""
    errands = registry(Path(__file__).resolve().parents[1] / "recipes")
    for errand in errands:
        for step in errand.steps:
            assert step.tool != "browse_eval", f"{errand.name}/{step.id} reaches for eval"