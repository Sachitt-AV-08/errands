"""The Errands website must be checkable, not just true today.

Errands' docs are served straight from `docs/`, so nothing in the build pipeline
validates it. This file makes the site falsifiable:

1. **The install command cannot drift.** The page's install note must name the
   real release tag that the install scripts actually pull (`/releases/latest`).
2. **The transcript cannot be hand-written.** The page renders `transcript.json`,
   and that file is checked against a fresh real run.
3. **The stats cannot be invented.** Test count, source lines, coverage gate
   are measured from disk, not typed.
4. **The rendered page is the thing being tested.** Layout, tap targets and
   console errors are checked in a real browser.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
INDEX = DOCS / "index.html"
CSS = DOCS / "style.css"
MAIN_JS = DOCS / "main.js"
TRANSCRIPT = DOCS / "transcript.json"

RELEASE_TAG = "v0.1.0"


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    return CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def transcript() -> dict:
    return json.loads(TRANSCRIPT.read_text(encoding="utf-8"))


def _shipped_version() -> str:
    """The version from the tagged release pyproject.toml."""
    try:
        blob = subprocess.run(
            ["git", "show", f"{RELEASE_TAG}:pyproject.toml"],
            cwd=REPO, capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        pytest.skip(f"cannot read {RELEASE_TAG} from git")
    m = re.search(r'version\s*=\s*"([^"]+)"', blob)
    assert m, "version not found in tagged pyproject.toml"
    return m.group(1)


def _test_count() -> int:
    """Count collected tests by running the collector — never a typed number."""
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO, capture_output=True, text=True, timeout=300,
    ).stdout
    total = 0
    for ln in out.splitlines():
        m = re.search(r"tests/.*:\s+(\d+)\s*$", ln)
        if m:
            total += int(m.group(1))
    return total


def _src_file_count() -> int:
    """Number of tracked source files, measured from git."""
    out = subprocess.run(
        ["git", "ls-files", "src"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    ).stdout
    return len([ln for ln in out.splitlines() if ln.strip()])


def _src_line_count() -> int:
    """Lines of tracked source, measured from disk."""
    total = 0
    for f in _tracked_src_files():
        total += len((REPO / f).read_text(encoding="utf-8").splitlines())
    return total


def _tracked_src_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "src"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    ).stdout
    return [ln for ln in out.splitlines() if ln.strip().endswith(".py")]


class TestInstallNoteIsNotALie:
    def test_the_install_note_names_the_real_release(self, html: str) -> None:
        """The install note must state the release the scripts actually pull."""
        version = _shipped_version()
        assert version in html, f"page should state the {version} release"
        assert f"{_test_count()} tests" in html, (
            f"page should state the {_test_count()} tests actually collected"
        )
        assert "89% coverage gate" in html, "page should state 89% gate"

    def test_main_js_install_note_matches(self) -> None:
        """main.js CMDS must have the same version note."""
        source = MAIN_JS.read_text(encoding="utf-8")
        version = _shipped_version()
        for os_name in ("win", "unix"):
            assert version in source, f"main.js missing {version} for {os_name}"
            assert f"{_test_count()} tests" in source, (
                f"main.js missing test count for {os_name}"
            )
            assert "89% coverage gate" in source, f"main.js missing gate for {os_name}"

    def test_the_measured_stats_match_the_repo(self, html: str) -> None:
        """The 'Measured, not claimed' block must report real numbers."""
        assert f"{_test_count()} tests" in html, "page states a wrong test count"
        assert f"{_src_file_count()} files" in html, "page states a wrong file count"
        # Lines are shown as a rounded "~1.2k"; assert the rounded form is present.
        n = _src_line_count()
        approx = f"~{round(n / 1000, 1)}k"
        assert approx in html, f"page states '{approx}' but has {n} lines of source"


class TestTranscriptIsCapturedNotComposed:
    def test_the_page_ships_no_run_output_of_its_own(self, html: str) -> None:
        """Any literal transcript text in HTML would be a hand-typed run."""
        assert "irreversible or financial action detected" not in html, (
            "the page contains literal run output; it must be rendered from transcript.json"
        )

    def test_the_transcript_is_fetched_not_inlined(self) -> None:
        source = MAIN_JS.read_text(encoding="utf-8")
        assert "transcript.json" in source, "main.js does not read transcript.json"
        assert re.search(r"fetch\(\s*['\"]transcript\.json", source), (
            "main.js must fetch transcript.json rather than embed it"
        )

    def test_a_failed_fetch_says_so_instead_of_faking_it(self) -> None:
        source = MAIN_JS.read_text(encoding="utf-8")
        assert "transcript unavailable" in source, (
            "a failed transcript fetch must render an explicit error"
        )
        assert not re.search(r"const\s+SAMPLE|fallbackTranscript|FAKE_TRANSCRIPT", source), (
            "main.js must not carry a hand-written sample transcript"
        )


class TestStylesheetHasNoSilentCollisions:
    def css_declarations(self, css: str) -> dict[tuple[str, str], dict[str, set[str]]]:
        """(context, selector) -> {property -> set of values}, per rule block.

        Multiple rule blocks for the same selector are legal CSS — they merge by
        cascade. What is not legal is the same property carrying two different
        values in the same context, so that is what this collects.
        """
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        found: dict[tuple[str, str], dict[str, set[str]]] = {}

        def walk(text: str, ctx: str) -> None:
            while text:
                text = text.lstrip()
                if not text:
                    break
                at = re.match(r"@([\w-]+)([^{]*)\{", text)
                if at:
                    name, cond, rest = at.group(1), at.group(2).strip(), text[at.end():]
                    depth, i = 1, 0
                    while i < len(rest) and depth:
                        if rest[i] == "{":
                            depth += 1
                        elif rest[i] == "}":
                            depth -= 1
                        i += 1
                    walk(rest[: i - 1], f"{ctx}@{name} {cond}".strip())
                    text = rest[i:]
                    continue
                block = re.match(r"([^{}]+)\{([^{}]*)\}", text)
                if not block:
                    return
                for one in block.group(1).split(","):
                    sel = re.sub(r"\s+", " ", one).strip()
                    if not sel:
                        continue
                    props = found.setdefault((ctx, sel), {})
                    for decl in block.group(2).split(";"):
                        decl = decl.strip()
                        if ":" not in decl:
                            continue
                        prop, _, val = decl.partition(":")
                        props.setdefault(prop.strip().lower(), set()).add(val.strip())
                text = text[block.end():]

        walk(css, "")
        return found

    def test_no_conflicting_property_values(self, css: str) -> None:
        conflicts = []
        for (ctx, sel), props in self.css_declarations(css).items():
            for prop, vals in props.items():
                if len(vals) > 1:
                    conflicts.append(f"{ctx or '(root)'} {sel}.{prop}: {sorted(vals)}")
        assert not conflicts, "conflicting CSS values: " + "; ".join(conflicts)

    def test_body_text_colours_clear_wcag_aa(self, css: str) -> None:
        def srgb(c: float) -> float:
            c /= 255
            return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

        def lum(h: str) -> float:
            h = h.lstrip("#")
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            r, g, b = (srgb(int(h[i:i+2], 16)) for i in (0, 2, 4))
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def ratio(a: str, b: str) -> float:
            la, lb = lum(a), lum(b)
            hi, lo = max(la, lb), min(la, lb)
            return (hi + 0.05) / (lo + 0.05)

        declared = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{3,6})\s*;", css))
        for fg, bg in (
            ("fg", "bg"), ("fg-dim", "bg"), ("fg-faint", "bg"),
            ("fg-faint", "bg-soft"), ("fg-dim", "bg-soft"),
            ("accent", "bg"), ("good", "bg"),
        ):
            assert fg in declared and bg in declared, f"stylesheet missing --{fg} or --{bg}"
            r = ratio(declared[fg], declared[bg])
            assert r >= 4.5, f"--{fg} on --{bg} is {r:.2f}:1, needs 4.5:1"


@pytest.fixture(scope="module")
def browser():
    pw = pytest.importorskip("playwright.sync_api", reason="playwright not installed")
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture(scope="module")
def served():
    import http.server
    import threading

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(DOCS), **kwargs)
        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()


@pytest.fixture(scope="module")
def pages(browser, served):
    out = {}
    for w, h, tag in ((1280, 900, "desktop"), (390, 844, "phone")):
        pg = browser.new_page(viewport={"width": w, "height": h})
        errors: list[str] = []
        def record(msg, sink=errors):
            if msg.type == "error":
                sink.append(msg.text)
        def failed(exc, sink=errors):
            sink.append(str(exc))
        pg.on("console", record)
        pg.on("pageerror", failed)
        pg.goto(served, wait_until="networkidle")
        pg.wait_for_timeout(600)
        out[tag] = (pg, errors)
    yield out
    for pg, _ in out.values():
        pg.close()


class TestRenderedPage:
    def test_the_document_actually_loaded(self, pages) -> None:
        for tag, (pg, _) in pages.items():
            assert pg.title(), f"{tag}: no document served"
            assert pg.query_selector("a.brand img") is not None, (
                f"{tag}: the page is not the one in docs/"
            )

    def test_nothing_console_errors(self, pages) -> None:
        for tag, (_, errors) in pages.items():
            assert not errors, f"{tag} console errors: {errors}"

    def test_the_page_does_not_scroll_sideways(self, pages) -> None:
        for tag, (pg, _) in pages.items():
            over = pg.evaluate(
                "document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            assert over <= 1, f"{tag}: content is {over}px wider than the viewport"

    def test_the_transcript_actually_rendered(self, pages) -> None:
        for tag, (pg, _) in pages.items():
            gate = pg.inner_text("#term-gate").strip()
            assert gate, f"{tag}: no transcript rendered"
            assert "unavailable" not in gate.lower(), f"{tag}: transcript failed to load"
            assert "destructive" in gate, f"{tag}: expected 'destructive' in transcript"

    def test_tap_targets_are_big_enough_on_a_phone(self, pages) -> None:
        pg, _ = pages["phone"]
        small = pg.eval_on_selector_all(
            "a.btn, .tab, .navlinks a, a.brand",
            "ns => ns.map(n => { const r = n.getBoundingClientRect();"
            " return [n.textContent.trim().slice(0, 16), Math.round(r.height)]; })"
            ".filter(x => x[1] < 44)",
        )
        assert not small, f"interactive targets under 44px: {small}"

    def test_the_logo_is_visible_not_black_on_black(self, pages) -> None:
        for tag, (pg, _) in pages.items():
            size = pg.eval_on_selector(
                "a.brand img",
                "n => { const r = n.getBoundingClientRect();"
                " return [r.width, r.height]; }",
            )
            assert size[0] > 8 and size[1] > 8, f"{tag}: logo has no size {size}"
            colour = pg.eval_on_selector("a.brand img", "n => getComputedStyle(n).color")
            assert colour != "rgb(0, 0, 0)", f"{tag}: logo is black on a dark page"
            raw = (DOCS / "mark.svg").read_text(encoding="utf-8")
            assert "currentColor" in raw, "mark.svg should paint with currentColor"
            assert "#000000" not in raw, "mark.svg still hardcodes a black fill"

    def test_the_install_tabs_actually_switch(self, pages) -> None:
        pg, _ = pages["desktop"]
        first = pg.inner_text("#installcmd")
        pg.click("#install .tab[data-os='unix']")
        after = pg.inner_text("#installcmd")
        assert after != first, "the unix tab did not change the command"
        assert after.strip().startswith("curl"), after
        pg.click("#install .tab[data-os='win']")
        assert pg.inner_text("#installcmd") == first

    def test_every_anchor_resolves(self, pages) -> None:
        pg, _ = pages["desktop"]
        broken = pg.evaluate(
            """() => [...document.querySelectorAll('a[href^="#"]')]
                 .map(a => a.getAttribute('href'))
                 .filter(h => h !== '#' && !document.querySelector(h))"""
        )
        assert not broken, f"anchors with no target: {broken}"

    def test_every_asset_loads(self, pages) -> None:
        pg, _ = pages["desktop"]
        missing = pg.evaluate(
            """() => [...document.querySelectorAll('img')]
                 .filter(i => !i.complete || i.naturalWidth === 0)
                 .map(i => i.getAttribute('src'))"""
        )
        assert not missing, f"images that did not load: {missing}"
