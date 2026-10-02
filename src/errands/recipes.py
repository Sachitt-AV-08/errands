"""Loading recipes from disk, so a recipe is data rather than code.

Two sources, both producing the same `Errand` objects:

* a Python module with a `RECIPES: list[Errand]` (or a `build()` function),
* a JSON file with the same shape as `Errand.summary()`.

The JSON route matters more than it looks. A recipe checked against a real site
is the kind of thing that needs review and diffing, and a review of a data file
is a review people actually perform. Executable recipe code would be reviewed as
code and mostly skipped.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Iterable, Sequence

from .core import Errand, Precondition, Step, ErrandError, RefusalReason

#: Key names allowed to become `Step.gated`. A recipe may mark a step ungated,
#: which is how a read-only preamble opts out of the gate; the gate still sees
#: it, it just does not prompt.
_PASSTHROUGH = {
    "id", "tool", "args", "intent", "expect", "reject",
    "unless_present", "gated", "notes",
}


def _step_from_dict(raw: dict) -> Step:
    unknown = set(raw) - _PASSTHROUGH
    if unknown:
        raise ValueError(f"step {raw.get('id')!r} has unknown keys: {sorted(unknown)}")
    return Step(
        id=str(raw["id"]),
        tool=str(raw["tool"]),
        args=dict(raw.get("args") or {}),
        intent=str(raw.get("intent", "")),
        expect=tuple(raw.get("expect") or ()),
        reject=tuple(raw.get("reject") or ()),
        unless_present=tuple(raw.get("unless_present") or ()),
        gated=bool(raw.get("gated", True)),
    )


def errand_from_dict(raw: dict) -> Errand:
    """Build one `Errand` from a plain dict.

    Strict about the keys it does not know. A typo like `expectd` should fail
    here, loudly, rather than silently produce a step with no verification - a
    recipe that cannot prove a step worked is worse than no recipe, because it
    reports success.
    """
    preconditions = []
    for pre in raw.get("preconditions") or ():
        if not isinstance(pre, dict):
            raise ValueError(f"precondition for {raw.get('name')!r} must be a dict")
        unknown = set(pre) - {"id", "expect", "reject", "is_drift", "notes"}
        if unknown:
            raise ValueError(f"precondition {pre.get('id')!r} has unknown keys: {sorted(unknown)}")
        preconditions.append(
            Precondition(
                id=str(pre.get("id") or "unnamed"),
                expect=tuple(pre.get("expect") or ()),
                reject=tuple(pre.get("reject") or ()),
                is_drift=bool(pre.get("is_drift", False)),
            )
        )
    steps = [_step_from_dict(s) for s in raw.get("steps") or ()]
    if not steps:
        raise ValueError(f"errand {raw.get('name')!r} has no steps")
    ids = [s.id for s in steps]
    if len(set(ids)) != len(ids):
        raise ValueError(f"errand {raw.get('name')!r} has duplicate step ids: {ids}")
    return Errand(
        name=str(raw["name"]),
        triggers=tuple(raw.get("triggers") or ()),
        goal_template=str(raw.get("goal_template", "")),
        preconditions=tuple(preconditions),
        steps=tuple(steps),
        match_threshold=float(raw.get("match_threshold", 0.5)),
        description=str(raw.get("description", "")),
        tags=tuple(raw.get("tags") or ()),
    )


def load_json(path: str | Path) -> Errand:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return errand_from_dict(raw)


def load_module(path: str | Path) -> list[Errand]:
    """Import a `.py` recipe file and pull `RECIPES` or `build()` out of it."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(f"recipe_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import recipe module {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, "build"):
        return list(module.build())
    recipes = getattr(module, "RECIPES", None)
    if recipes is None:
        raise ValueError(f"recipe module {path.name} defines neither RECIPES nor build()")
    return list(recipes)


def load_dir(path: str | Path) -> list[Errand]:
    """Load every `.json` and `.py` recipe in a directory.

    A broken recipe fails the whole load. Skipping it would leave the errand
    list quietly incomplete, and the symptom - "unsubscribe from newsletter"
    offered for "delete account" - is exactly the kind of mistake that is
    hardest to trace back to a silently skipped file.
    """
    path = Path(path)
    if not path.is_dir():
        raise ErrandError(
            RefusalReason.NO_MATCH, f"no recipe directory at {path}", {"path": str(path)}
        )
    out: list[Errand] = []
    for entry in sorted(path.iterdir()):
        if entry.suffix == ".json":
            out.append(load_json(entry))
        elif entry.suffix == ".py" and not entry.name.startswith("_"):
            out.extend(load_module(entry))
    return out


def registry(*sources: str | Path | Iterable[Errand]) -> list[Errand]:
    """Build one errand list from mixed sources, rejecting duplicate names."""
    out: list[Errand] = []
    for source in sources:
        items: Sequence[Errand]
        if isinstance(source, (str, Path)):
            p = Path(source)
            items = load_dir(p) if p.is_dir() else ([load_json(p)] if p.suffix == ".json" else load_module(p))
        else:
            items = list(source)
        out.extend(items)
    names = [e.name for e in out]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ErrandError(
            RefusalReason.AMBIGUOUS,
            f"duplicate errand names: {sorted(dupes)}",
            {"duplicates": sorted(dupes)},
        )
    return out