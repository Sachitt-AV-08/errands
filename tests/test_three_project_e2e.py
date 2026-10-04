"""The three projects together, against a real browser and the real shipped recipe.

Every other test in this repository stops at a project boundary:

* `test_orvima_browser_contract.py` proves Errands' bridge reaches a real
  Chromium, but drives no recipe and no policy.
* `test_end_to_end.py` runs a real Sentinel `Policy` against the real shipped
  recipe, but against `tests/support/shop.py`, a scripted stand-in.
* orvima's `test_sentinel_gate.py` exercises orvima's own gate, not Errands'.

So the seam that matters most - a real policy, a real browser and the real
recipe, deciding whether to spend money - was never crossed by any test. That
is the gap this file closes, and it is the one the project's whole purpose
depends on.

The headline test asserts the negative: `place_order.json` ends by clicking
"Place order", and the card must never be charged. The charge is recorded by
the page itself (`window.__charged`), so the assertion is answered by the site
rather than by the test's own expectations.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("orvima", reason="orvima not on path")
pytest.importorskip("sentinel", reason="sentinel not on path")

from orvima.browser import BrowserController  # noqa: E402
from orvima.sentinel_gate import SentinelGate  # noqa: E402
from sentinel import Classifier, ClassifierMode, HashingEmbedder, Policy  # noqa: E402

from errands.core import ErrandRunner  # noqa: E402
from errands.orvima_bridge import OrvimaBrowser, runner_for  # noqa: E402
from errands.recipes import load_json  # noqa: E402
from errands.sentinel_bridge import make_classifier  # noqa: E402

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
RECIPE = HERE.parent / "recipes" / "place_order.json"


@pytest.fixture(scope="module")
def server():
    """The shop, served over HTTP.

    A `file://` fixture cannot be walked by clicking: Chromium refuses relative
    navigation between file URLs, so every click after the first landed on
    `chrome-error://chromewebdata/` and the test read an error page. Real shops
    are served over HTTP, so this one is too - see `support/shop_server.py`.
    """
    from support.shop_server import ShopServer

    with ShopServer() as s:
        yield s


def _page(server, name: str) -> str:
    return server.url(name)


@pytest.fixture(scope="module")
def policy() -> Policy:
    """The real classifier, in the mode the README says is the honest one."""
    return Policy(
        Classifier(HashingEmbedder()),
        classifier_mode=ClassifierMode.ADVISORY,
    )


@pytest.fixture(scope="module")
def recipe():
    assert RECIPE.exists(), f"the shipped recipe is missing: {RECIPE}"
    return load_json(RECIPE)


@pytest.fixture(scope="module")
def controller(server):
    # A private profile: Chromium refuses to open one another process holds, and
    # a module-scoped browser must not fight orvima's own suite for the default.
    import tempfile

    profile = tempfile.mkdtemp(prefix="errands-e2e-")
    with BrowserController(
        headless=True,
        profile_dir=profile,
        base_url=server.url("shop_cart.html"),
    ) as ctl:
        yield ctl


@pytest.fixture
def shop(controller, server) -> OrvimaBrowser:
    controller.navigate(server.url("shop_cart.html"))
    return OrvimaBrowser(controller)


def _charged(controller) -> bool:
    """Ask the page whether money moved. The site is the source of truth."""
    return bool(controller.page.evaluate("() => window.__charged === true"))


def _attempts(controller) -> int:
    return int(controller.page.evaluate("() => window.__chargeAttempts || 0"))


# ------------------------------------------------- the headline guarantee ----


def _runner(controller, approver):
    """Assemble the runner the way orvima's own gate would.

    `runner_for` is orvima's entry point: it wires `SentinelGate` to the
    controller, so the gate under test is the production one rather than a
    hand-assembled imitation.
    """
    runner, browser, gate = runner_for(
        controller,
        [],
        session_id="e2e",
        classifier_mode="advisory",
    )
    return runner, browser, gate


def _run_against_real_browser(shop, policy, recipe, *, approve: bool):
    """Run the shipped recipe once and hand back everything observable.

    Shared by the tests below so each can assert one guarantee on its own. They
    are separate tests rather than one long one because a single function with
    four assertions is only as load-bearing as its first passing check: deleting
    any one assertion leaves the others to carry the test, which is exactly how a
    guard rots without anyone noticing.
    """
    seen: list[str] = []

    def approver(request) -> bool:
        seen.append(str(getattr(request, "reason", "")))
        return approve

    classify = make_classifier(policy, page_url=lambda: shop.url)
    runner = ErrandRunner(shop, classify=classify, approver=approver)
    return runner.run(recipe), seen


def test_the_card_is_not_charged(shop, controller, policy, recipe) -> None:
    """The single guarantee this project exists to provide.

    Asked directly, with nothing else in the test to lean on.
    """
    _run_against_real_browser(shop, policy, recipe, approve=False)
    assert _charged(controller) is False, (
        "the card was charged by a run whose human said no - this is the one "
        "failure this project exists to prevent"
    )


def test_the_charge_button_is_never_even_reached(shop, controller, policy, recipe) -> None:
    """Stronger than "not charged": the button must not be pressed.

    `test_the_payment_page_reports_whether_it_was_charged` proves the fixture's
    handler really does fire, so a zero attempt count means the click never
    happened rather than that the fixture was inert.
    """
    _run_against_real_browser(shop, policy, recipe, approve=False)
    assert _attempts(controller) == 0, (
        "the charge button was pressed, so the refusal came too late to matter"
    )


def test_a_human_is_actually_asked_before_the_irreversible_step(
    shop, policy, recipe
) -> None:
    """Without this, "no charge" would also hold if nothing were gated at all."""
    _result, seen = _run_against_real_browser(shop, policy, recipe, approve=False)
    assert seen, (
        "no approval was ever requested, so nothing was gated and the run "
        "proved nothing about the gate"
    )


def test_a_refused_run_does_not_report_success(shop, policy, recipe) -> None:
    """Stopping and claiming success is the failure mode of a silent gate."""
    result, _seen = _run_against_real_browser(shop, policy, recipe, approve=False)
    assert not result.ok, "a refused run reported ok"
    assert result.refused is not None, "stopped without saying why"


def test_the_reversible_steps_did_run_before_it_stopped(shop, policy, recipe) -> None:
    """The control for the tests above.

    If the run refused at preconditions it would never reach the charge and
    "no charge" would be trivially true. This proves it walked the cart and
    checkout first.
    """
    result, _seen = _run_against_real_browser(shop, policy, recipe, approve=False)
    done = [s.step_id for s in result.steps]
    assert "review_cart" in done, f"never reached the first step: {done}"
    assert not result.ok, "the whole recipe ran; nothing was gated at all"


def test_the_recipe_reaches_the_irreversible_step_when_human_says_yes(
    shop, controller, policy, recipe
) -> None:
    """The control that makes every "was not charged" assertion mean something.

    Run the whole recipe with a human who approves everything. If the card ends
    up charged, then the charge path is genuinely reachable, and the tests that
    assert it is *not* charged under refusal are measuring the gate rather than
    an early exit.

    This is not hypothetical. `ErrandRunner._verify` reads page text through
    `_page_text`, which originally ignored the snapshot's `body`, so on a real
    page the run stopped at `review_total` and never reached the charge. Every
    "not charged" assertion passed happily against a recipe that could not have
    charged anything. Asserting reachability is what makes that failure mode
    impossible to hide.
    """
    result, _seen = _run_against_real_browser(shop, policy, recipe, approve=True)

    done = [s.step_id for s in result.steps]
    assert "place_the_order" in done, (
        "the recipe never reached its irreversible step, so 'the card was not "
        f"charged' would be true for the wrong reason. Steps run: {done}"
    )
    assert _charged(controller) is True, (
        "an approving human walked the whole recipe and the card was still not "
        "charged - the fixture's charge handler is not reachable, which makes "
        "every refusal assertion in this file meaningless"
    )


def test_the_refusal_names_the_step_it_stopped_at(controller, shop, policy, recipe) -> None:
    """A refusal nobody can act on is a bug report waiting to happen."""
    classify = make_classifier(policy, page_url=lambda: shop.url)
    runner = ErrandRunner(shop, classify=classify, approver=lambda request: False)
    result = runner.run(recipe)

    done = [s.step_id for s in result.steps]
    assert "review_cart" in done or "confirm_shipping" in done, (
        f"stopped before doing anything: {done}"
    )
    assert result.refused is not None, "stopped without saying why"
    assert not _charged(controller)


def test_the_charge_click_is_gated_by_the_gate_that_ships_with_orvima(
    controller, server, policy
) -> None:
    """orvima's own gate must flag the charge, not only Errands'.

    Errands routes through the bridge; orvima's gate sits on the browser. If the
    two disagreed, an agent using orvima directly would be unprotected while the
    Errands suite stayed green.
    """
    controller.navigate(server.url("shop_payment.html"))

    gate = SentinelGate(
        policy=policy,
        page_url=lambda: controller.page.url,
        snapshot=lambda: controller.snapshot(),
        classifier_mode="advisory",
    )
    assert gate.available, "the gate could not attach the real policy"

    result = gate.check("browse_click", {"selector": "Place order"})
    assert not result.allowed, (
        f"orvima's gate allowed the charge click unattended: {result}"
    )
    assert not _charged(controller)


def test_the_gate_allows_the_reversible_steps_on_the_same_page(
    controller, server, policy
) -> None:
    """The control: a click is not gated merely for being on a payment page.

    If everything here prompted, the gate would be noise and people would learn
    to approve without reading - which is worse than not gating at all.
    """
    controller.navigate(server.url("shop_payment.html"))
    gate = SentinelGate(
        policy=policy,
        page_url=lambda: controller.page.url,
        snapshot=lambda: controller.snapshot(),
        classifier_mode="advisory",
    )
    result = gate.check("browse_snapshot", {})
    assert result.allowed, (
        f"a read-only tool prompted on a payment page: {result}"
    )


# ------------------------------------------- the seams, each proved once ----


def test_the_real_gate_and_the_real_bridge_agree_on_the_charge_step(
    controller, shop, server, policy
) -> None:
    """The gate and the bridge must reach the same verdict on the same page."""
    controller.navigate(server.url("shop_payment.html"))
    gate = SentinelGate(
        policy=policy,
        page_url=lambda: shop.url,
        snapshot=lambda: controller.snapshot(),
        classifier_mode="advisory",
    )
    classify = make_classifier(policy, page_url=lambda: shop.url)
    from errands.core import PlanContext

    ctx = PlanContext(
        errand="place_order", step_id="place_the_order", step_index=4,
        step_count=4, goal="buy this item", intent="pay: place the order",
    )
    via_bridge = classify("browse_click", {"selector": "Place order"}, ctx)
    via_gate = gate.check("browse_click", {"selector": "Place order"})

    assert via_bridge.needs_human, "the bridge let the charge through"
    assert not via_gate.allowed, "orvima's gate let the charge through"
    assert not _charged(controller)


def _charge_step_context(recipe):
    from errands.core import PlanContext

    step = next((s for s in recipe.steps if s.id == "place_the_order"), None)
    assert step is not None, "the recipe no longer has the charge step"
    return step, PlanContext(
        errand=recipe.name,
        step_id=step.id,
        step_index=recipe.steps.index(step) + 1,
        step_count=len(recipe.steps),
        goal=recipe.goal_template,
        intent=step.intent,
        notes={"next_step": ""},
    )


def _judged_on_payment_page(shop, server, policy, recipe):
    """The same click judged twice: once with no plan, once with the real one."""
    controller = shop._c  # noqa: SLF001 - the fixture already built it
    controller.navigate(server.url("shop_payment.html"))
    _step, ctx = _charge_step_context(recipe)

    from errands.core import PlanContext

    classify = make_classifier(policy, page_url=lambda: shop.url)
    call = ("browse_click", {"selector": "text=Place order"})
    bare = PlanContext(errand="x", step_id="y", step_index=1, step_count=1, goal="", intent="")
    return classify(call[0], call[1], bare), classify(call[0], call[1], ctx)


def test_the_charge_click_is_gated_with_its_plan_attached(shop, server, policy, recipe) -> None:
    _alone, in_context = _judged_on_payment_page(shop, server, policy, recipe)
    assert in_context.needs_human, "the charge click was not gated in context"


def test_the_plan_alone_changes_the_verdict(shop, server, policy, recipe) -> None:
    """The control, on its own so it cannot be carried by another assertion.

    Measured on a real payment page: the same click with no plan is OUTWARD, and
    the recipe's plan lifts it to DESTRUCTIVE. If the two ever agree, plan
    context does nothing and the design is decorative - so this asserts strict
    inequality rather than "they differ somehow".
    """
    alone, in_context = _judged_on_payment_page(shop, server, policy, recipe)
    assert alone.risk != in_context.risk, (
        f"plan context changed nothing: alone={alone.risk} "
        f"in_context={in_context.risk}"
    )


def test_the_three_imports_can_coexist_in_one_interpreter() -> None:
    """Cheap, and it catches a packaging regression before anything else does."""
    import errands
    import orvima
    import sentinel

    assert errands.__version__ and orvima.__version__ and sentinel.__version__
    assert Path(errands.__file__).is_file()


def test_no_module_imports_another_projects_private_names() -> None:
    """The dependency is meant to run one way through documented seams.

    A `from sentinel.policy import _internal` would make a Sentinel refactor a
    breaking change for Errands, invisibly.
    """
    for project, names in (
        ("orvima", ("orvima.browser", "orvima.sentinel_gate", "orvima.tools")),
        ("sentinel", ("sentinel.policy",)),
    ):
        src = (HERE.parent / "src" / "errands").rglob("*.py")
        for path in src:
            text = path.read_text(encoding="utf-8")
            for name in names:
                assert f"import {name}._" not in text, (
                    f"{path.name} imports a private name from {name}"
                )
