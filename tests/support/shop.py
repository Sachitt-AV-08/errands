"""A checkout site realistic enough to be worth gating against.

Element labels are the ones a real store uses, and the pages progress the way a
real store does - including the parts that make the recipe hard: a "Continue"
button on the shipping step, a total that only appears once shipping is
confirmed, and a declined card that looks like success for one snapshot.

Stands in for `bench_site.py` rather than extending it, because orvima is being
worked on independently and Errands should not need a live browser - or a live
repo - to be tested end to end.

Selectors resolve by CSS id, by `#ref`, or by the visible text of a control, the
way orvima's tools do. Recipes are written in visible text ("Checkout", "Place
order") because that is what survives a redesign of a site's class names, and
the subject of these tests is the gate, not the harness's selector parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ShopError(RuntimeError):
    pass


def _norm(text: str) -> str:
    return " ".join(text.lower().replace(".", " ").replace(",", " ").split())


@dataclass
class Shop:
    """Scripted store. `page` is the current page name."""

    page: str = "cart"
    declined: bool = False
    #: total shown on the payment page, in cents
    total: int = 4_299
    calls: list[tuple[str, dict]] = field(default_factory=list)
    #: how many times the store charged the card
    placed: int = 0

    # ---------------------------------------------------------------- layout --

    def _items(self) -> list[tuple[str, str, str, str]]:
        """(ref, tag, role, visible label) for the current page."""
        if self.page == "cart":
            if self.empty:
                return [("#empty", "p", "text", "Your cart is empty")]
            return [
                ("#cart-items", "section", "region", "Cart items"),
                ("#checkout", "button", "button", "Checkout"),
            ]
        if self.page == "checkout":
            return [
                ("#shipping", "section", "region", "Shipping address"),
                ("#continue-payment", "button", "button", "Continue to payment"),
            ]
        if self.page == "payment":
            return [
                ("#payment", "section", "region", "Payment method"),
                (".order-total", "span", "text", f"Total ${self.total / 100:,.2f}"),
                ("#place-order", "button", "button", "Place order"),
            ]
        if self.page == "declined":
            # Reached only *after* the charge attempt. The click succeeded, the
            # page changed, and the page says no money moved - which is exactly
            # the shape that a "did the click work?" check mistakes for success.
            return [
                ("#declined", "p", "alert", "Your card was declined. Try another card."),
                ("#try-again", "button", "button", "Try again"),
            ]
        if self.page == "confirmation":
            return [
                ("#thanks", "section", "region", "Thank you"),
                ("#order-number", "p", "text", "Order number ORD-4471"),
            ]
        if self.page == "expired":
            return [("#expired", "p", "alert", "Your session expired. Please sign in to continue.")]
        return []

    def snapshot(self, **_: object) -> dict:
        return {
            "url": f"https://shop.example/{self.page}",
            "title": self.page.replace("-", " ").title(),
            "items": [
                {"ref": ref, "tag": tag, "role": role, "label": label, "text": ""}
                for ref, tag, role, label in self._items()
            ],
        }

    # ------------------------------------------------------------ resolution --

    def resolve(self, selector: str) -> str:
        """Map a selector, ref, or visible label to a ref on the current page."""
        items = self._items()
        for ref, _tag, _role, _label in items:
            if selector == ref:
                return ref
        wanted = _norm(selector)
        for ref, _tag, _role, label in items:
            if _norm(label) == wanted:
                return ref
        # Partial match, as a real accessibility lookup gives. Only when
        # unambiguous: resolving to whichever candidate came first would hide
        # exactly the "clicked the wrong control" bug these tests exist to catch.
        partial = [
            ref for ref, _t, _r, label in items if wanted and wanted in _norm(label)
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise ShopError(f"{selector!r} is ambiguous on this page: {partial}")
        raise ShopError(f"element not visible: {selector}")

    # -------------------------------------------------------------- surfaces --

    def extract(self, selector: str | None = None, **_: object) -> dict:
        self.calls.append(("extract", {"selector": selector}))
        target = self.resolve(selector) if selector else None
        text = next(
            (label for ref, _t, _r, label in self._items() if ref == target), ""
        )
        return {"ok": True, "text": text, "url": self.snapshot()["url"]}

    def navigate(self, url: str, **_: object) -> dict:
        self.calls.append(("navigate", {"url": url}))
        self.page = url.rstrip("/").rsplit("/", 1)[-1] or "cart"
        return {"ok": True, "url": self.snapshot()["url"]}

    def type(self, selector: str, text: str, **_: object) -> dict:
        self.calls.append(("type", {"selector": selector, "text": text}))
        self.resolve(selector)
        return {"ok": True, "url": self.snapshot()["url"]}

    def fill(self, selector: str, text: str, **_: object) -> dict:
        return self.type(selector, text)

    def go_back(self, **_: object) -> dict:
        self.calls.append(("go_back", {}))
        return {"ok": True, "url": self.snapshot()["url"]}

    def wait_for(self, selector: str, timeout_ms: int = 0, **_: object) -> dict:
        self.calls.append(("wait", {"selector": selector}))
        self.resolve(selector)
        return {"ok": True, "url": self.snapshot()["url"]}

    def scroll(self, direction: str = "down", **_: object) -> dict:
        self.calls.append(("scroll", {"direction": direction}))
        return {"ok": True, "url": self.snapshot()["url"]}

    def click(self, selector: str, **_: object) -> dict:
        """Click by selector or visible text.

        Records what the *recipe* asked for, not what it resolved to, so a
        recipe that clicked the wrong control is visible as such in the log
        rather than normalised away.
        """
        self.calls.append(("click", {"selector": selector}))
        target = self.resolve(selector)

        if target == "#checkout":
            self.page = "checkout"
        elif target == "#continue-payment":
            self.page = "payment"
        elif target == "#place-order":
            # The one irreversible act in the whole site. A declined card still
            # records the attempt, because "how many times did it try to charge"
            # is the number that matters if a recipe loops.
            self.placed += 1
            self.page = "declined" if self.declined else "confirmation"
        elif target == "#try-again":
            self.page = "checkout"
        else:
            raise ShopError(f"nothing happens when you click {selector}")
        return {"ok": True, "url": self.snapshot()["url"]}

    # ----------------------------------------------------------------- setup --

    empty: bool = False

    def expire(self) -> None:
        """Put the session into the shape a timeout produces."""
        self.page = "expired"

    def set_total(self, cents: int) -> None:
        self.total = cents

    def decline(self) -> None:
        """Every later page offers the retry instead of the charge."""
        self.declined = True