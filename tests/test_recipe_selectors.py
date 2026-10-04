"""Which of the shipped recipes' selectors can a real browser actually resolve?

A heuristic over selector strings cannot settle this: "Place order" is
grammatically identical to the descendant combinator `div p`, and only a real
engine knows which one it meant. So each selector is tried against Chromium.

This is the audit that found the shipped recipes unusable: six of seven
selectors across `place_order.json` and `cancel_subscription.json` are visible
button text, which `locator("Checkout")` reads as a <Checkout> tag name.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("orvima", reason="orvima not on path")

from orvima.browser import BrowserController  # noqa: E402

RECIPES = Path(__file__).resolve().parents[1] / "recipes"

# The selectors actually shipped, read from the recipes rather than copied here.
# A hardcoded list would go stale the moment a recipe is fixed, and the test
# would keep asserting against selectors that no longer exist.
def _shipped_selectors() -> list[tuple[str, str, str]]:
    found = []
    for path in sorted(RECIPES.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for step in data.get("steps", []):
            sel = step.get("args", {}).get("selector")
            if sel:
                found.append((path.name, step["id"], sel))
    return found


CASES = _shipped_selectors()

PAGE = """<!doctype html><html><body>
<button id="checkout">Checkout</button>
<button id="to-payment">Continue to payment</button>
<p class="order-total">Order total: $30.00</p>
<button id="place-order">Place order</button>
<button id="manage-plan">Manage plan</button>
<button id="cancel-plan">Cancel plan</button>
<button id="yes-cancel">Yes, cancel</button>
</body></html>"""


def test_the_shipped_recipes_only_use_selectors_a_browser_can_resolve() -> None:
    """The recipes are data, so their selectors are data too.

    They are written in visible text because the scripted shop in
    `tests/support/shop.py` resolves text, and its docstring says so outright:
    "the subject of these tests is the gate, not the harness's selector
    parsing." That is a fair thing for that harness to do and a dishonest thing
    for a recipe to rely on, because a recipe is meant to be run against a real
    site. Every selector here must survive a real Chromium.
    """
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "selectors.html"
        page.write_text(PAGE, encoding="utf-8")

        with BrowserController(
            headless=True,
            profile_dir=tempfile.mkdtemp(),
            base_url=page.as_uri(),
        ) as ctl:
            unresolvable = []
            for recipe, step, selector in CASES:
                try:
                    ctl.page.click(selector, timeout=1500)
                except Exception:
                    unresolvable.append(f"{recipe}:{step} -> {selector!r}")

    assert not unresolvable, (
        "these shipped selectors cannot be resolved by a real browser, so the "
        "recipes fail on the first action against any real site:\n  "
        + "\n  ".join(unresolvable)
    )


def test_the_audit_actually_covers_every_shipped_selector() -> None:
    """Guards the guard: a parse bug must not silently shrink the audit.

    `CASES` is derived from the recipes, so the audit above and this coverage
    check would happily agree on an empty list. This asserts the list is
    non-empty and that a known-bad selector is present in its raw form, which is
    the property the fix had to change.
    """
    assert CASES, "no selectors were parsed from the recipes at all"
    assert len(CASES) >= 7, f"expected the full set of steps, got {len(CASES)}: {CASES}"
    assert any(sel.startswith("text=") for _, _, sel in CASES), (
        "no selector uses text=, so the recipes were not updated - this is the "
        "defect this file was written to catch"
    )
