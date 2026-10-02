"""Errands: narrow goals run as fixed, verified sequences.

    from errands import ErrandRunner, match, registry

    errands = registry("recipes/")
    runner  = ErrandRunner(browser, classify=policy.evaluate, approver=ask_human)
    result  = runner.run_goal("cancel my subscription", errands)

    if result.ok:
        print(f"done: {len(result.steps)} steps")
    else:
        print(f"stopped: {result.state.value} - {result.refused}")

Nothing here plans. A goal matches a recipe or it does not, and when it does
not, the answer is a refusal. Everything between the goal and the browser is a
step someone wrote and checked.
"""

from .core import (
    ApprovalRequest,
    Approver,
    Browser,
    Errand,
    ErrandError,
    ErrandResult,
    ErrandRunner,
    ErrandState,
    Match,
    PlanContext,
    Precondition,
    RefusalReason,
    Step,
    StepResult,
    match,
    score_match,
)
from .recipes import errand_from_dict, load_dir, load_json, load_module, registry

__all__ = [
    "ApprovalRequest",
    "Approver",
    "Browser",
    "Errand",
    "ErrandError",
    "ErrandResult",
    "ErrandRunner",
    "ErrandState",
    "Match",
    "PlanContext",
    "Precondition",
    "RefusalReason",
    "Step",
    "StepResult",
    "errand_from_dict",
    "load_dir",
    "load_json",
    "load_module",
    "match",
    "registry",
    "score_match",
]

__version__ = "0.1.0"