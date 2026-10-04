# Errands

Narrow goals, run as fixed verified recipes. A goal matches a recipe or it does
not, and when it does not, Errands says so.

There is no generative planning here. That is the point. An errand is a sequence
someone wrote and checked against a real site, and the only judgement call in
the whole system is whether the goal is one of them.

```python
from sentinel import Classifier, HashingEmbedder, Policy

from errands import ErrandRunner, registry
from errands.orvima_bridge import OrvimaBrowser
from errands.sentinel_bridge import make_classifier

controller = BrowserController(headless=True)     # orvima
browser    = OrvimaBrowser(controller)
policy     = Policy(Classifier(HashingEmbedder()))

errands = registry("recipes/")
runner  = ErrandRunner(
    browser,
    # page_url is read per decision, so the classifier keeps seeing the right
    # page after a navigation. A plain string also works for a fixed URL.
    classify=make_classifier(policy, page_url=lambda: browser.url),
    approver=ask_a_human,
)
result = runner.run_goal("buy this item", errands)

if result.ok:
    print(f"done: {len(result.steps)} steps")
else:
    print(f"stopped: {result.state.value} - {result.refused}")
```

## Why plan context

A click on "Continue" is `low` risk in isolation and is the last safe moment
before a charge when the plan says payment is next. Nothing about the call
distinguishes those two cases:

```
browse_click(selector="text=Continue")     -> low,      runs unattended
browse_click(selector="text=Continue")     -> destructive, prompts
  ... same call, plan="place order payment"
```

So every step is classified with the plan attached - the errand, the goal, this
step's intent, and what comes next. `PlanContext` exists for that, and
`Policy.evaluate(..., plan=...)` is the input.

The plan can only **raise** the floor, never lower it, because the plan is
attacker-influenced text. A recipe that says "just browsing" cannot argue a
purchase is fine. And only submitting tools are escalated by plan: a plan that
mentions deleting an account must not make reading the order list dangerous, or
every step of every destructive errand would prompt and people would learn to
click through.

## The three gates

| Gate | When | What it catches |
|---|---|---|
| `preflight` | once, before step one | the page is not what the recipe was written against |
| Sentinel | every step | the action is not safe unattended, in the context of the plan |
| `verify` | after every step | the step did not do what it claimed |

Any of them stopping is the run stopping. There is no "continue anyway", because
a step that did not verify leaves the plan describing a world that is no longer
true.

`preflight` checks every condition before reporting one, and a drift condition
outranks a plain miss. An expired-session page has no cart, no plan and no
account on it, so every `expect` in the recipe fails at once; reporting "expected
cart, found none" sends someone hunting for the wrong thing.

## Refusals

Every refusal names itself, and `RefusalReason` is the machine-readable half:

| Reason | Meaning |
|---|---|
| `no_match` | no recipe fits the goal confidently |
| `ambiguous` | two recipes fit equally well - a question, not a coin flip |
| `precondition_failed` | the page is not what the recipe expects |
| `drift` | the page changed under the recipe |
| `unsafe` | a step needs a human and did not get one |
| `verification_failed` | a step ran and could not prove itself |

`no_match` and `ambiguous` are the ones this project exists for. An agent that
confidently starts a cancellation because the goal mentioned "cancel" is worse
than one that asks.

## Recipes are data

JSON, so a recipe checked against a real site gets reviewed and diffed the way a
data file does, rather than skimmed the way code does.

```json
{
  "name": "cancel_subscription",
  "triggers": ["cancel my subscription", "end my premium plan"],
  "preconditions": [{"id": "on_account_page", "expect": ["account", "settings"]}],
  "steps": [
    {"id": "open_plan", "tool": "browse_click", "args": {"selector": "text=Manage plan"},
     "intent": "open the subscription settings", "expect": ["subscription", "renews"]}
  ]
}
```

- `expect` - words that prove the step worked. **Any one** is enough; it is a
  list of acceptable wordings, not a conjunction.
- `reject` - words whose presence means it did not. Checked first, because
  "payment declined" is a definitive answer while a missing success phrase is
  only suggestive.
- `unless_present` - skip the step if all of these are already on the page.
- `gated` - `false` opts a step out of the gate. A read-only preamble.

A `selector` must be something a browser can actually resolve. `text=Checkout`
finds a control by its visible label; a bare `Checkout` does not work, because
Playwright reads it as a `<Checkout>` tag name and waits out its timeout. Both
shipped recipes were written as bare text until they were run against a real
Chromium and failed on their first action. `tests/test_recipe_selectors.py` now
resolves every selector a shipped recipe uses against a real browser, so a
recipe cannot ship a form that only the scripted shop understands.

Unknown keys are an error, not a warning. A misspelled `expectd` would otherwise
produce a step that verifies nothing and reports success.

A recipe can only call the read-and-navigate verbs in `_invoke`. It cannot reach
`browse_eval` - the gate is there to constrain the recipe, not to be worked
around by a recipe that reaches past it.

## Tests

```
python -m pytest tests -q
```

No test count is recorded here. Every figure this file used to quote went stale
within a day of being written, which is what a hand-maintained number is worth.

`tests/test_three_project_e2e.py` is the one that matters: a real Sentinel
`Policy`, a real Chromium, and the real shipped recipe. The shop is served over
HTTP by `tests/support/shop_server.py` because Chromium refuses relative
navigation between `file://` URLs, so a file-backed fixture cannot be walked by
clicking. The page records its own charge, so "the card was not charged" is
answered by the site rather than by the test's expectations.

Its controls are the point as much as its assertions.
`test_the_recipe_reaches_the_irreversible_step_when_human_says_yes` runs the
whole recipe with an approver who says yes to everything and requires the card
to end up charged. Without it, every refusal assertion would also hold for a
recipe that simply failed early and could never have charged anything - which is
exactly what happened before verification learned to read the page body.

`tests/test_end_to_end.py` covers the same ground against a scripted store,
which is what makes it fast enough to run on every edit. Its controls stop a
pass from being credited to the wrong rule:
`test_the_same_recipe_is_unrecognisable_as_a_purchase_without_plan_context` uses
a bare "Continue" on a non-transactional page, because anything with "pay" in the
label or "/checkout" in the URL is already gated by existing rules and would
prove nothing about plan context.

`tests/test_recipe_selectors.py` resolves every selector a shipped recipe uses
against a real browser, and `tests/test_shop_server.py` checks the fixture
itself - that a real click really does navigate, and that the charge handler
really does fire. Both exist because a green suite elsewhere was compatible with
a fixture that could not be walked and a page that could not be charged.

`tests/test_orvima_browser_contract.py` is the only file that opens a real
Chromium. The rest of the bridge is tested against `FakeController`, which is
shaped like `BrowserController` by hand and rots silently: rename a verb in
orvima and the rest of this suite stays green while the bridge is broken. This
file reads the forwarded verbs out of the bridge's own source and binds them to
the real signatures, so drift is a failure rather than a surprise. It is also the
only test that would notice a click landing on the wrong element, and the only
one pinning occlusion handling - a target under a sticky header is uncovered and
clicked in well under a second rather than waiting out a timeout.

Both files skip cleanly when orvima or Sentinel is not importable, so this suite
still runs standalone. `core.py` needs neither, which is the point: without the
gate installed, steps still run and nothing is gated.

## Measured, not assumed

The deterministic layer generalises to plan text: a bare "Continue" goes from
`low` to `destructive` on plan context alone, and the checkout flow prompts
exactly at the payment steps and nowhere else.

The classifier does not. Measured on 30 plan-level texts:

| | value |
|---|---|
| plan-level AUC (top-2 margin) | **0.5378** |
| step-level AUC (the original measurement) | 0.5035 |
| permutation p-value | 0.377 |

p = 0.377 means the result is not distinguishable from a coin flip, so the
hypothesis that longer structured text might make the similarity margin
discriminative is **refuted**. The 19/20 accuracy above comes from the concept
matcher reading plan text - rules generalising usefully - not from learning.

`ClassifierMode` therefore stays `ADVISORY`: the classifier records, reports, and
does not decide.

## Layout

```
src/errands/core.py            match, PlanContext, Step, ErrandRunner
src/errands/recipes.py         JSON/module loading, strict schema
src/errands/sentinel_bridge.py the only module importing both projects
src/errands/orvima_bridge.py   adapts orvima's BrowserController
recipes/*.json                 place_order, cancel_subscription
tests/support/shop.py          a scripted store
tests/fixtures/form_page.html a local page for the browser contract test
tests/test_orvima_browser_contract.py  the only test opening a real browser
```

`core.py` imports nothing from Sentinel. `sentinel_bridge.py` is the only file
that imports both, so Errands stays runnable and testable with no ML dependency
present - without one, steps still run and nothing is gated.