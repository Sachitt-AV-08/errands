"""End to end: a goal, a shipped recipe, a scripted store, the real Sentinel.

These are the tests G6 exists to produce. Everything else in the suite uses a
fake classifier and so proves nothing about whether the gate actually stops a
purchase - it only proves the runner asks the classifier. Here the classifier is
Sentinel's real `Policy`, wired through the real bridge, and the assertions are
about money: what was charged, what was charged without asking, and what the
human was shown when they were asked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from errands import ErrandRunner, ErrandState, RefusalReason, registry  # noqa: E402
from shop import Shop  # noqa: E402

pytest.importorskip("sentinel", reason="sentinel not on path")

from errands.sentinel_bridge import make_classifier  # noqa: E402
from sentinel import Classifier, ClassifierMode, HashingEmbedder, Policy  # noqa: E402

RECIPES = Path(__file__).resolve().parents[1] / "recipes"


@pytest.fixture(scope="module")
def errands():
    return registry(RECIPES)


@pytest.fixture
def policy():
    # OFF, not ADVISORY: the deterministic layer carries safety, and leaving the
    # classifier off makes it unambiguous that these results come from the rules.
    return Policy(Classifier(HashingEmbedder()), classifier_mode=ClassifierMode.OFF)


@pytest.fixture
def page_url():
    return lambda: ""


def run(shop: Shop, errands, policy, *, approver=None, goal: str = "buy this item"):
    runner = ErrandRunner(
        shop,
        classify=make_classifier(policy, page_url=lambda: shop.snapshot()["url"]),
        approver=approver,
    )
    return runner.run_goal(goal, errands)


def clicked(shop: Shop) -> list[str]:
    return [kw.get("selector") for verb, kw in shop.calls if verb == "click"]


# ------------------------------------------------------------------ refusals --


def test_an_unrelated_goal_is_refused_without_touching_the_store(errands, policy) -> None:
    shop = Shop()
    result = run(shop, errands, policy, goal="book a flight to Lisbon")
    assert result.state is ErrandState.REFUSED
    assert result.refused is RefusalReason.NO_MATCH
    assert shop.calls == [], "the store was opened for a goal no recipe covers"


def test_an_empty_cart_is_refused_before_any_click(errands, policy) -> None:
    """The precondition exists so this is a clear message, not a confusing failure."""
    empty = Shop(page="cart", empty=True)

    result = run(empty, errands, policy)
    assert result.state is ErrandState.REFUSED
    # The report names the rule that tripped, not the page text that tripped it.
    assert any("empty" in w for w in result.detail["unexpected"]), result.detail
    assert clicked(empty) == []


def test_a_login_wall_is_reported_as_drift_not_as_a_missing_cart(errands, policy) -> None:
    """The diagnosis has to name the actual problem.

    An expired-session page has no cart, no plan and no account on it, so every
    `expect` in the recipe fails at once. Reporting "expected cart, found none"
    sends someone hunting for a cart that was never the issue; the deliberate
    `is_drift` rule is the one that knows better.
    """
    shop = Shop(page="cart")
    shop.expire()

    result = run(shop, errands, policy)
    assert result.state is ErrandState.REFUSED
    assert result.refused is RefusalReason.DRIFT
    assert result.detail["precondition"] == "not_logged_out"
    assert result.detail["unexpected"], "the drift rule should say what it saw"
    assert "also_failed" in result.detail, "the other failures should still be reported"


def test_a_login_wall_is_caught_at_the_start_not_mid_flow(errands, policy) -> None:
    shop = Shop(page="cart")
    shop.expire()

    result = run(shop, errands, policy)
    assert result.state is ErrandState.REFUSED
    assert result.refused is RefusalReason.DRIFT
    assert shop.calls == [], "steps ran before noticing the session had expired"


# ------------------------------------------------------- the checkout itself --


def test_a_declined_card_fails_the_recipe_rather_than_reporting_success(
    errands, policy
) -> None:
    """The failure mode that looks most like a win.

    The click on "Place order" succeeds, the page changes, and the page says the
    card was declined. A runner that only checks "did the click work" reports a
    purchase that never happened - or worse, retries into a second attempt at a
    card that has already been refused.
    """
    shop = Shop()
    shop.decline()

    asked: list = []

    def ask(request):
        asked.append(request)
        return True

    result = run(shop, errands, policy, approver=ask)

    assert result.state is ErrandState.FAILED
    assert result.refused is RefusalReason.VERIFICATION_FAILED
    assert "declined" in result.reason
    assert shop.placed == 1, "the recipe retried the charge after a decline"


def test_the_final_click_is_gated_on_plan_context_alone(errands, policy) -> None:
    """The whole reason plan context exists.

    "Continue to payment" matches no risk concept on its own, and neither does
    "Place order" need the page URL to be read as money. What makes these steps
    legible is the *plan*: the errand is a purchase, and the plan says so. Take
    the plan away and the same clicks are an ordinary shopping flow.
    """
    shop = Shop()

    def refuse_at_the_charge(request):
        if request.step_id == "place_the_order":
            return False
        return True

    result = run(shop, errands, policy, approver=refuse_at_the_charge)

    assert result.state is ErrandState.ABANDONED
    assert shop.placed == 0, "an order was placed despite the human saying no"
    assert "Place order" not in clicked(shop)
    # The earlier prompt happened and was allowed through, so this is the gate
    # working rather than a recipe that refused to start.
    assert "Continue to payment" in clicked(shop)


def test_the_same_recipe_is_unrecognisable_as_a_purchase_without_plan_context(
    errands, policy
) -> None:
    """The control for the test above, so the credit cannot come from elsewhere.

    A bare "Continue" on a non-transactional page: no risk word in the label, no
    transactional URL, nothing about the call itself. This is the case that was
    auto-approved on a live checkout page and is the whole reason plan context
    exists. Every other comparison in this file has at least one other rule
    that could have produced the prompt; this one has only the plan.
    """
    call = ("browse_click", {"selector": "Continue"})
    benign_page = "https://shop.example/account/addresses"

    without_plan = policy.evaluate(*call, page_url=benign_page)
    with_plan = policy.evaluate(
        *call,
        page_url=benign_page,
        plan="place order checkout buy this item payment confirm shipping",
    )

    assert without_plan.needs_human is False, (
        "the action alone was already gated, so plan context proved nothing: "
        f"{without_plan.reason}"
    )
    assert with_plan.needs_human is True
    assert with_plan.risk.value == "destructive"

    # And the same call under a plan that is not a purchase stays ungated, so
    # the plan is not simply a way to gate everything.
    unrelated = policy.evaluate(
        *call,
        page_url=benign_page,
        plan="search results show more results filter the list",
    )
    assert unrelated.needs_human is False, unrelated.reason


def test_a_human_saying_yes_lets_the_purchase_through(errands, policy) -> None:
    """The other direction. A gate that can never allow anything is not a gate."""
    shop = Shop()
    asked: list = []

    def ask(request):
        asked.append(request)
        return True

    result = run(shop, errands, policy, approver=ask)

    assert result.state is ErrandState.DONE, result.reason
    assert shop.placed == 1
    # Two prompts, not one. The first is "you are entering payment", the second
    # is "this charges the card". Prompting a step early is the cheap direction
    # to be wrong in; the alternative is waving through a click that turned out
    # to be part of a purchase.
    assert [r.step_id for r in asked] == ["confirm_shipping", "place_the_order"]


def test_the_human_is_shown_the_total_before_agreeing_to_charge(errands, policy) -> None:
    """The prompt has to carry the number, not just the word "checkout".

    A person asked to approve an abstract action cannot tell whether it is the
    right one. Asked to approve "$43.00, for the widget you asked me to buy", they
    can.
    """
    shop = Shop()
    shop.set_total(4_299)
    asked: list = []

    def ask(request):
        asked.append(request)
        return True

    run(shop, errands, policy, approver=ask)

    request = asked[0]
    assert request.risk in ("outward", "destructive"), request.risk
    assert "buy this item" in request.reason
    assert "place the order" in request.detail["plan"].lower()


def test_the_steps_before_the_charge_run_without_asking(errands, policy) -> None:
    """Prompting on every step would make the gate noise, and noise gets waved at."""
    shop = Shop()
    asked: list = []

    def ask(request):
        asked.append(request)
        return True

    run(shop, errands, policy, approver=ask)

    asked_selectors = [r.args.get("selector") for r in asked]
    assert "#checkout" not in asked_selectors
    assert "#continue-payment" not in asked_selectors
    assert "Place order" in asked_selectors


def test_the_total_is_read_before_the_charge_is_proposed(errands, policy) -> None:
    """The read step exists so the total is known before anyone approves."""
    shop = Shop()

    def ask(request):
        return True

    result = run(shop, errands, policy, approver=ask)
    assert result.state is ErrandState.DONE
    verbs = [verb for verb, _ in shop.calls]
    extract_at = verbs.index("extract")
    place_at = next(
        i for i, (verb, kw) in enumerate(shop.calls)
        if verb == "click" and kw.get("selector") == "Place order"
    )
    assert extract_at < place_at, "the total was read after the charge was proposed"


# ---------------------------------------------------------- policy behaviour --


def test_plan_context_raises_the_risk_of_an_unremarkable_click(policy) -> None:
    """Isolated, so a regression here is unambiguous.

    The identical call is benign under a search plan and severe under a payment
    plan. Nothing about the action changed - only what the agent said it was for.
    """
    call = ("browse_click", {"selector": "Continue to payment"})

    benign = policy.evaluate(*call, page_url="https://shop.example/cart",
                             plan="search results show more results")
    risky = policy.evaluate(*call, page_url="https://shop.example/checkout",
                            plan="place order checkout buy this item payment")

    assert benign.risk is not risky.risk, (
        f"plan context changed nothing: both {benign.risk.value}"
    )
    assert risky.risk.value in ("outward", "destructive")


def test_plan_context_cannot_make_a_read_dangerous(policy) -> None:
    """Otherwise every step of a destructive errand is gated, including the reads.

    A recipe that ends in a charge spends most of its steps reading the cart.
    Gating those because the plan mentions paying would train the user to click
    through prompts, which is worse than not prompting at all.
    """
    d = policy.evaluate(
        "browse_extract", {"selector": ".order-total"},
        page_url="https://shop.example/payment",
        plan="place order payment buy this item",
    )
    assert d.needs_human is False, d


def test_plan_context_cannot_lower_an_existing_risk(policy) -> None:
    """A plan is attacker-controlled text; it must never be a way to argue "safe"."""
    d = policy.evaluate(
        "browse_click", {"selector": "Place order"},
        page_url="https://shop.example/payment",
        plan="just browsing reading the page no action",
    )
    assert d.needs_human is True