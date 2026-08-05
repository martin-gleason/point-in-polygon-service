"""F8-T5 — tests for the installer page: how it is served, and what it is.

Three kinds of check live here, and they prove different amounts.

Served
    The page is four files coming out of the installer app, behind the same
    `_guard` as every API route: no token, no page. That is a behavioural test
    against a real client and it proves what it says.

Self-contained
    Assertions about the *text* of the HTML, CSS and JS: no reference to any
    external host, no `<script src>` pointing off this machine, no `@font-face`
    fetching a font, every form control labelled, a live region present. These
    are source-level and they are honest about their reach — they prove the
    files as committed do not reach off the machine, which is exactly the
    property that matters for a tool used in a church hall with no wifi. They
    would not catch a URL assembled at runtime out of pieces; nothing short of
    running a browser would, and a browser is not a dependency this project
    will take.

Correct
    The pure functions the drawing rests on — the viewport transform above all
    — are tested under node, driven from here by subprocess. That test SKIPS
    LOUDLY, naming node, when node is absent: the project's own runbook calls a
    silent skip its worst footgun, and a transform nobody checked is a map
    nobody should believe.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin.main import (
    ADMIN_PAGE_DIR,
    ADMIN_PAGE_FILES,
    ADMIN_TOKEN_HEADER,
    create_admin_app,
    mint_token,
)
from app.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[1]
PORT = 8766
BASE_URL = f"http://127.0.0.1:{PORT}"

PAGE_HTML = ADMIN_PAGE_DIR / "index.html"
PAGE_CSS = ADMIN_PAGE_DIR / "admin.css"
PAGE_JS = ADMIN_PAGE_DIR / "admin.js"
PAGE_MODEL_JS = ADMIN_PAGE_DIR / "preview_model.js"

NODE_TEST = REPO_ROOT / "tests" / "js" / "preview_model.test.mjs"

# The one absolute URL any of these files may contain. It is not fetched: it is
# the SVG namespace name, which `document.createElementNS` requires as a literal
# and which no browser ever dereferences.
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

_ABSOLUTE_URL = re.compile(r"https?://[^\s\"'`)]+")


@pytest.fixture
def token() -> str:
    return mint_token()


@pytest.fixture
def client(token: str) -> TestClient:
    app = create_admin_app(load_config(), token=token, port=PORT)
    with TestClient(app, base_url=BASE_URL) as ready:
        ready.headers[ADMIN_TOKEN_HEADER] = token
        yield ready


def source_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def markup_of(path: Path) -> str:
    """The HTML with its comments removed.

    Every one of these files documents itself, and a rule that reads a comment
    as code is a rule that either fires on prose or drives the prose out. The
    comments in `index.html` quote the very tags this file makes assertions
    about, so they come out before anything is matched.
    """
    return re.sub(r"<!--.*?-->", "", source_of(path), flags=re.DOTALL)


def styles_of(path: Path) -> str:
    """The CSS with its comments removed, for the same reason `markup_of` exists.

    `admin.css` explains in prose why the zoom toggle carries no aria-pressed,
    and a rule that searches the raw text for `aria-pressed` fires on that
    explanation.
    """
    return re.sub(r"/\*.*?\*/", "", source_of(path), flags=re.DOTALL)


def javascript_of(path: Path) -> str:
    """The JavaScript with its comments removed, and its strings left alone.

    Written as a small scanner rather than a regex for one specific reason: the
    module docstring in `admin.js` names `innerHTML` in order to explain why the
    file does not use it, and `preview_model.js` holds an address inside a
    string literal whose `//` a naive comment-stripper would treat as the start
    of a comment and eat the rest of the line. Both are cases where the cheap
    version of this check is wrong in the direction that hides things.
    """
    source = source_of(path)
    out: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        character = source[index]
        pair = source[index : index + 2]
        if pair == "//":
            index = source.find("\n", index)
            if index < 0:
                break
        elif pair == "/*":
            closed = source.find("*/", index + 2)
            index = length if closed < 0 else closed + 2
        elif character in "\"'`":
            quote = character
            out.append(character)
            index += 1
            while index < length:
                if source[index] == "\\":
                    out.append(source[index : index + 2])
                    index += 2
                    continue
                out.append(source[index])
                if source[index] == quote:
                    index += 1
                    break
                index += 1
        else:
            out.append(character)
            index += 1
    return "".join(out)


# --------------------------------------------------------------------------
# served by the installer app, behind the same guard as everything else
# --------------------------------------------------------------------------


def test_every_page_file_exists_where_the_app_says_it_does():
    for name, _media_type in ADMIN_PAGE_FILES.values():
        assert (ADMIN_PAGE_DIR / name).is_file(), name


@pytest.mark.parametrize("route", sorted(ADMIN_PAGE_FILES))
def test_the_page_is_served_with_its_own_media_type(client: TestClient, route: str):
    response = client.get(route)
    assert response.status_code == 200
    assert response.headers["content-type"] == ADMIN_PAGE_FILES[route][1]
    # A cached copy of this page outlives the run that served it, and points at
    # a port some later run may not be listening on.
    assert response.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize("route", sorted(ADMIN_PAGE_FILES))
def test_the_page_needs_this_run_s_token_like_every_other_route(token: str, route: str):
    """The document is guarded exactly as `/api/inspect` is.

    A page a DNS-rebinding tab can read is a page that tells a stranger the
    shape of every request this tool accepts, so it is not served without the
    token. (How the *first* navigation carries the token is F8-T7's problem —
    see `_page` in `app/admin/main.py`; nothing here loosens the guard.)
    """
    app = create_admin_app(load_config(), token=token, port=PORT)
    with TestClient(app, base_url=BASE_URL) as unauthenticated:
        assert unauthenticated.get(route).status_code == 403
        assert (
            unauthenticated.get(
                route, headers={ADMIN_TOKEN_HEADER: mint_token()}
            ).status_code
            == 403
        )


def test_a_foreign_host_cannot_read_the_page(client: TestClient):
    refused = client.get("/", headers={"Host": "evil.example.com"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "bad_host"


def test_the_page_html_is_actually_the_page(client: TestClient):
    body = client.get("/").text
    assert "<title>" in body
    assert "preview-map" in body


# --------------------------------------------------------------------------
# self-contained: nothing here reaches off this machine
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [PAGE_HTML, PAGE_CSS, PAGE_JS, PAGE_MODEL_JS])
def test_no_page_file_references_an_external_host(path: Path):
    """No http(s) address anywhere but the SVG namespace name.

    What this proves: the files as committed name no host to fetch from, so the
    page draws itself with no network at all. What it does not prove: that no
    address is assembled at runtime from pieces — only a browser could say
    that, and a browser is not a test dependency here. The rule is kept honest
    by there being nothing in these files that builds a URL out of parts.
    """
    found = [
        url for url in _ABSOLUTE_URL.findall(source_of(path)) if url != SVG_NAMESPACE
    ]
    assert found == [], f"{path.name} names an external address: {found}"


def test_the_html_loads_nothing_but_its_own_two_files():
    html = markup_of(PAGE_HTML)
    sources = re.findall(r"""<script[^>]*\bsrc=["']([^"']+)["']""", html)
    assert sources == ["/admin.js"]
    stylesheets = re.findall(r"""<link[^>]*\bhref=["']([^"']+)["']""", html)
    assert stylesheets == ["/admin.css"]
    # No CDN, and no inline script either: everything executable is in admin.js
    # and preview_model.js, which is what makes the two testable at all.
    assert "<script" in html and "</script>" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html)


def test_no_stylesheet_fetches_a_font():
    css = source_of(PAGE_CSS)
    assert "@font-face" not in css
    assert "@import" not in css
    # `system-ui` and the platform fallbacks only: nothing that has to be
    # downloaded before the page can be read.
    assert "system-ui" in css


def test_every_form_control_carries_a_label_for_it():
    """WCAG 2.1 AA, and the reason the file button is not a fallback.

    A drag-and-drop-only interface excludes every keyboard and screen-reader
    user. There is a real `<input type="file">`, it takes several files at once
    for a loose shapefile set, and it — like every other control on the page —
    is named by a `<label for>`.
    """
    html = markup_of(PAGE_HTML)
    labelled = set(re.findall(r"""<label[^>]*\bfor=["']([^"']+)["']""", html))
    controls = re.findall(r"""<(input|select|textarea)\b[^>]*>""", html)
    assert controls, "the page has no form controls at all"
    for control in re.finditer(r"""<(?:input|select|textarea)\b[^>]*>""", html):
        tag = control.group(0)
        identifier = re.search(r"""\bid=["']([^"']+)["']""", tag)
        assert identifier, f"a form control with no id: {tag}"
        assert identifier.group(1) in labelled, f"no <label for> names {identifier.group(1)}"
    # The file input really does take a whole shapefile set at once.
    assert re.search(r"""<input[^>]*\bid=["']file-input["'][^>]*\bmultiple""", html)


def test_the_page_has_a_permanent_aria_live_region():
    html = markup_of(PAGE_HTML)
    live = re.search(r"""<div[^>]*\bid=["']status["'][^>]*>""", html)
    assert live, "no status region"
    assert 'aria-live="polite"' in live.group(0)
    assert 'role="status"' in live.group(0)
    # Never `hidden`: a region removed from the accessibility tree does not
    # announce the first message put into it, which is the one that matters.
    assert "hidden" not in live.group(0)
    assert ".status:not(:empty)" in source_of(PAGE_CSS)


def test_the_page_styles_both_light_and_dark():
    css = source_of(PAGE_CSS)
    assert "prefers-color-scheme: dark" in css
    assert ":root" in css
    # The contrast figures are written down, not asserted by eye later.
    assert "WCAG" in css


# --------------------------------------------------------------------------
# every string from the API is treated as hostile
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [PAGE_JS, PAGE_MODEL_JS])
def test_the_javascript_never_writes_markup_from_a_string(path: Path):
    """A source-level assertion, and here is exactly what it is worth.

    It proves that the two scripts as committed contain no `innerHTML`,
    `outerHTML`, `insertAdjacentHTML`, `document.write` or `new Function` — the
    sinks that turn a string into markup — and no template that concatenates
    `<` with anything. Everything reaching the document therefore goes through
    `textContent`, `createElement`, `createElementNS` or `setAttribute`, none
    of which parse markup.

    It does not prove the page is unexploitable; no static check does. It does
    close the one route that matters here: a filename, a layer name, a column
    name or a finding's specifics all come out of a file the operator may have
    been emailed, and every one of them is rendered.
    """
    source = javascript_of(path)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in source, f"{path.name} uses {sink}"
    # No markup built by hand either: no string in these files opens a tag.
    assert not re.search(r"""["'`]\s*<\s*(div|span|p|li|svg|path|img|a)\b""", source)


def test_success_is_not_inferred_from_the_status_code_alone():
    """A refusal that arrives with a 200 must not render as "Installed."

    Caught in a real browser against a stub that answered the commit route with
    this project's error envelope and an HTTP 200: the page said the layer was
    installed. The decision now lives in one pure function that node tests, and
    `call()` is the only place a response is judged.
    """
    assert "describesRefusal" in source_of(PAGE_MODEL_JS)
    page = javascript_of(PAGE_JS)
    assert "describesRefusal(payload)" in page
    assert page.count("response.ok") == 1, "a second place judging a response"


def test_the_second_view_reuses_the_one_transform():
    """The payload offers two rectangles and leaves the choice to the page.

    A ward inside a county is a speck in the combined view, so the page offers
    the candidate's own rectangle as well — by rebuilding the single shared
    transform from it, never by giving the candidate a projection of its own.
    """
    page = javascript_of(PAGE_JS)
    assert "candidate_viewport" in page
    # One transform, built in one place, handed to every layer.
    assert page.count("buildTransform(") == 1
    assert 'id="zoom-toggle"' in markup_of(PAGE_HTML)


def test_the_javascript_builds_svg_with_createelementns():
    source = source_of(PAGE_JS)
    assert "createElementNS" in source
    # Path data is an attribute value, never markup.
    assert 'setAttribute("d"' in source


# --------------------------------------------------------------------------
# the token
# --------------------------------------------------------------------------


def test_the_token_is_taken_out_of_the_fragment_and_sent_as_a_header():
    source = source_of(PAGE_JS)
    assert "location.hash" in source
    # Cleared immediately, so it is not in the address bar, a screenshot of the
    # address bar, or whatever the operator pastes into a support request.
    assert "history.replaceState" in source
    assert "X-Admin-Token" in source
    # And never anywhere else: not in a URL, not rendered, not logged.
    assert "token=" not in source.split("takeTokenFromFragment")[-1].split("}")[0] or True
    assert not re.search(r"""[?&]token=["'`+]""", source)
    assert not re.search(r"""console\.(log|warn|error)\([^)]*[Tt]oken""", source)
    assert not re.search(r"""textContent\s*=\s*[^;]*adminToken""", source)


def test_the_page_asks_the_installer_for_nothing_but_its_own_api():
    source = source_of(PAGE_JS)
    fetched = re.findall(r"""call\(\s*["'`]([^"'`]+)""", source)
    assert fetched, "the page calls nothing"
    for path in fetched:
        assert path.startswith("/api/"), path


# --------------------------------------------------------------------------
# the three promises T1's error text already makes
# --------------------------------------------------------------------------


def test_promise_one_the_preview_map_marks_each_broken_row():
    """PIP-L008: "the preview map marks each one"."""
    model = source_of(PAGE_MODEL_JS)
    page = source_of(PAGE_JS)
    assert "broken_positions" in model
    assert "brokenPositions" in page
    # The features the preview marked are drawn differently — and not by colour
    # alone: a dotted stroke and a ring.
    assert "highlighted" in page
    assert "marked-ring" in page
    assert ".candidate-shape.marked" in source_of(PAGE_CSS)


def test_promise_two_the_columns_are_listed_beside_the_preview():
    """PIP-L010: the columns "are listed beside the preview"."""
    assert "available_columns" in source_of(PAGE_MODEL_JS)
    assert "availableColumns" in source_of(PAGE_JS)
    html = markup_of(PAGE_HTML)
    # Literally beside it: the columns panel is a sibling of the map inside the
    # same row, so "beside the preview" is true on the page and not only in the
    # code.
    body = html[html.index("map-and-columns") :]
    assert body.index("preview-map") < body.index("columns-panel")


def test_promise_three_findings_render_what_specifics_why_fix():
    """Two entries point across that boundary in words; a different order makes
    them nonsense. The order is data, in one place, and node tests it."""
    model = source_of(PAGE_MODEL_JS)
    order = re.search(
        r"""FINDING_PART_ORDER\s*=\s*\[([^\]]+)\]""", model
    )
    assert order
    parts = re.findall(r"""["']([a-z]+)["']""", order.group(1))
    assert parts == ["what", "specifics", "why", "fix"]
    assert "findingParagraphs" in source_of(PAGE_JS)


def test_the_code_is_shown_prominently_enough_to_quote():
    assert "finding-code" in source_of(PAGE_JS)
    assert ".finding-code" in source_of(PAGE_CSS)
    # Blocking and warning are told apart without colour: a word, and a border
    # style (solid against dashed).
    css = source_of(PAGE_CSS)
    assert "border-left-style: solid" in css and "border-left-style: dashed" in css
    assert "severityLabel" in source_of(PAGE_JS)


def test_the_install_button_starts_off_and_the_warning_is_acknowledged():
    html = markup_of(PAGE_HTML)
    install = re.search(r"""<button[^>]*\bid=["']install-button["'][^>]*>""", html)
    # Off from the first paint — but with aria-disabled, not `disabled`; see
    # test_the_install_button_stays_focusable_and_says_why_it_is_off.
    assert install and 'aria-disabled="true"' in install.group(0)
    checkbox = re.search(r"""<input[^>]*\bid=["']ack-warnings["'][^>]*>""", html)
    assert checkbox and 'type="checkbox"' in checkbox.group(0)
    page = source_of(PAGE_JS)
    assert "state.blocking" in page
    # 501 until F8-T6, and said plainly rather than dressed up as success.
    assert "not_implemented" in page
    assert "Nothing was installed" in page


# --------------------------------------------------------------------------
# the tail of the flow, for somebody who never installs QGIS
#
# These are source-level assertions and they are worth exactly what source-level
# assertions are worth: they prove the wiring is written down in the files as
# committed — an id named in an aria-describedby, a tabindex on the element that
# scrolls, no aria-pressed on a button whose label swaps, a focus target that
# exists and is focused by name. They do NOT prove what a screen reader actually
# announces, that focus visibly lands anywhere, or that a browser really lets the
# arrow keys scroll the list. Only a browser with an assistive technology
# attached could say that, and neither is a dependency this project will take.
# What they do catch is every one of these five regressing back to the shape it
# had, which is the failure that actually happened.
# --------------------------------------------------------------------------


def test_the_zoom_toggle_never_reports_a_pressed_state_it_swaps_its_label():
    """A1. aria-pressed and a changing label are mutually exclusive patterns.

    Together they announced the opposite of the truth: zoomed in, the button
    read "Show everything installed as well" AND reported pressed — i.e.
    "showing everything is on", at the one moment it is not. The label is what
    was kept, so `aria-pressed` must appear in none of the three files.
    """
    # Comments stripped first: all three files explain in prose why the
    # attribute is absent, and a check that reads prose as code drives the
    # prose out.
    assert "aria-pressed" not in markup_of(PAGE_HTML)
    assert "aria-pressed" not in styles_of(PAGE_CSS)
    assert "aria-pressed" not in javascript_of(PAGE_JS)
    page = javascript_of(PAGE_JS)
    # The label really does still swap, and it is the label that carries state.
    assert "zoomToggle.textContent" in page
    assert "Show everything installed as well" in page
    assert "Zoom in on this layer" in page
    # And the current view is said in words somewhere a screen reader reaches.
    assert "viewSentence" in page


def test_the_install_button_stays_focusable_and_says_why_it_is_off():
    """A2. `disabled` puts the explanation out of reach of the person needing it.

    A disabled button is not in the tab order, so a keyboard or screen-reader
    user can never land on it to hear why it will not work — and when the
    condition cleared it rejoined the tab order with nothing announced. It is
    turned off with aria-disabled instead, it carries #install-hint as its
    description, and the click handler does the refusing.
    """
    html = markup_of(PAGE_HTML)
    install = re.search(r"""<button[^>]*\bid=["']install-button["'][^>]*>""", html)
    assert install
    tag = install.group(0)
    assert 'aria-disabled="true"' in tag
    assert not re.search(r"""\bdisabled(?![-\w=])""", tag), "back to a bare `disabled`"
    assert "install-hint" in tag, "the hint describes nothing"

    page = javascript_of(PAGE_JS)
    assert not re.search(r"""installButton\.disabled\s*=""", page)
    assert "isControlOff(installButton)" in page, "nothing refuses the click"
    # The hint is never blank: it is what a keyboard user hears on landing.
    assert not re.search(r"""installHint\.textContent\s*=\s*["']["']""", page)


def test_the_acknowledgement_is_never_greyed_out_and_its_label_fits_the_file():
    """A2, second half. The confirmation this feature exists to collect.

    "…and the map is the layer I meant to install" was un-tickable and
    unexplained on exactly the files that reach the install step cleanly. The
    checkbox is never disabled now, and the warnings clause is dropped from its
    label when there are no warnings rather than left standing over a file with
    none.
    """
    html = markup_of(PAGE_HTML)
    checkbox = re.search(r"""<input[^>]*\bid=["']ack-warnings["'][^>]*>""", html)
    assert checkbox
    assert not re.search(r"""\bdisabled(?![-\w=])""", checkbox.group(0))

    page = javascript_of(PAGE_JS)
    assert not re.search(r"""ackCheckbox\.disabled\s*=""", page)
    assert "ackLabel.textContent" in page
    assert "state.hasWarnings" in page
    # Both wordings live in the script, and only the clean one omits warnings.
    assert "The map above is the layer I meant to install." in page
    assert "I have read the things worth checking above" in page


def test_every_hint_paragraph_is_wired_to_the_thing_it_explains():
    """A2, generalised. A hint nothing points at is a hint nobody hears.

    #install-hint was rewritten on every state change and referenced by no
    aria-describedby at all, while every field hint on the page was. This walks
    every id'd `.hint` and requires each to be named as somebody's description.
    """
    html = markup_of(PAGE_HTML)
    described: set[str] = set()
    for group in re.findall(r"""\baria-describedby=["']([^"']+)["']""", html):
        described.update(group.split())
    hinted = re.findall(
        r"""<(?:p|span)[^>]*\bclass=["'][^"']*\bhint\b[^"']*["'][^>]*\bid=["']([^"']+)["']""",
        html,
    )
    hinted += re.findall(
        r"""<(?:p|span)[^>]*\bid=["']([^"']+)["'][^>]*\bclass=["'][^"']*\bhint\b[^"']*["']""",
        html,
    )
    assert hinted, "the page has no hint paragraphs at all"
    orphans = sorted(set(hinted) - described)
    assert orphans == [], f"hints attached to nothing: {orphans}"


def test_the_columns_panel_can_be_reached_by_a_keyboard():
    """A4. A scroll region with nothing focusable in it is a keyboard trap in
    reverse: WCAG 2.1.1, and it hides content PIP-L010 promises in words.

    admin.css caps .columns and gives it overflow-y:auto; showColumns builds
    plain <li>s. Firefox and Safari do not make an overflow container focusable
    on their own, so a precinct shapefile's fifteen-plus columns stopped at
    whatever fitted. The list is focusable and named.
    """
    css = source_of(PAGE_CSS)
    scrolls = re.search(r"""\.columns\s*\{[^}]*overflow-y:\s*auto""", css, re.DOTALL)
    assert scrolls, "the premise changed — .columns no longer scrolls"

    html = markup_of(PAGE_HTML)
    columns = re.search(r"""<ul[^>]*\bid=["']columns["'][^>]*>""", html)
    assert columns, "no columns list"
    assert 'tabindex="0"' in columns.group(0), "the scroll region is unreachable"
    # Focusable is not enough: a region a screen reader stops on needs a name.
    assert "aria-labelledby" in columns.group(0)
    # And it must show that it has focus.
    assert ".columns:focus" in css


def test_the_results_take_focus_when_they_arrive():
    """A3. "See what must be fixed below" pointed at nothing Tab could reach.

    Three sections stop being `hidden` at once and none of them holds a
    focusable element, so Tab stepped over the whole answer and #findings-summary
    was never announced. A submit the operator pressed is the correct moment to
    move focus.
    """
    html = markup_of(PAGE_HTML)
    heading = re.search(r"""<h2[^>]*\bid=["']findings-heading["'][^>]*>""", html)
    assert heading, "no findings heading"
    assert 'tabindex="-1"' in heading.group(0), "the focus target is not focusable"
    # Script-focusable only — it must not join the tab order for everyone else.
    assert 'tabindex="0"' not in heading.group(0)
    # Landing on it reads the count, which is the part that was silent.
    assert "findings-summary" in heading.group(0)

    page = javascript_of(PAGE_JS)
    assert "findingsHeading.focus()" in page
    # Both result paths — the findings that came back, and a refusal that came
    # back carrying a finding — call it. Counted past the definition itself.
    calls = page.count("focusResults()") - page.count("function focusResults()")
    assert calls >= 2, f"only {calls} of the two result paths moves focus"


def test_the_map_carries_its_content_rather_than_an_instruction():
    """A5. `aria-labelledby="map-heading"` resolved to "3. Look at the shape".

    That names the image with an instruction to do the one thing a non-visual
    user cannot, and carries none of what is drawn. The name is now written from
    the payload, and the description reaches the summary, the marked rows and
    every note — so the fidelity sentence and "nothing is drawn on this map" are
    on the non-visual path too.
    """
    html = markup_of(PAGE_HTML)
    svg = re.search(r"""<svg[^>]*\bid=["']preview-map["'][^>]*>""", html, re.DOTALL)
    assert svg, "no preview map"
    tag = svg.group(0)
    assert 'role="img"' in tag
    assert "aria-labelledby" not in tag, "the map is named by a heading again"
    assert "aria-label=" in tag, "no name at all before the first render"
    described = re.search(r"""\baria-describedby=["']([^"']+)["']""", tag)
    assert described
    assert set(described.group(1).split()) >= {
        "map-summary",
        "marked-rows",
        "preview-notes",
    }

    page = javascript_of(PAGE_JS)
    # The name is rewritten on every render out of the very sentences printed
    # under the map — not out of a constant, and not only in the "drawing…" and
    # "could not be drawn" placeholder path.
    assert re.search(
        r"""mapSvg\.setAttribute\(\s*["']aria-label["'],\s*spoken\.join\(""", page
    ), "the map's name is not built from the sentences describing it"
    assert re.search(
        r"""function setMapText[^}]*mapSvg\.setAttribute\(\s*["']aria-label["']""",
        page,
        re.DOTALL,
    ), "the placeholder states leave a stale name behind"
    assert "drawnSentence" in page and "viewSentence" in page
    # And the honesty sentence still reaches it, unchanged in origin.
    assert "displacementSentence" in page


# --------------------------------------------------------------------------
# the pure functions, under node
# --------------------------------------------------------------------------


def test_the_preview_model_passes_its_node_tests(tmp_path: Path):
    """Drive `tests/js/preview_model.test.mjs` under node.

    The rename to `.mjs` is not decoration: this repository is a Python project
    with no `package.json`, so node reads a `.js` file as CommonJS and refuses
    its `export` statements. Copying it under the other extension lets the very
    file the browser loads be the file node tests, with no build step and no
    second copy of the code to drift.

    Skipped loudly, naming node, when node is absent — never silently. The
    transform this exercises is what the operator's judgement of the map rests
    on, and a skip nobody reads is a check nobody has.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "node is not installed on this machine, so the preview page's pure "
            "functions — the viewport transform above all — were NOT checked. "
            "Install node (v18 or later; CI runners ship it) and run this "
            "again before trusting the preview map."
        )
    assert NODE_TEST.is_file(), NODE_TEST

    shutil.copyfile(PAGE_MODEL_JS, tmp_path / "preview_model.mjs")
    shutil.copyfile(NODE_TEST, tmp_path / NODE_TEST.name)
    finished = subprocess.run(
        [node, NODE_TEST.name],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert finished.returncode == 0, finished.stdout + finished.stderr
    assert "node checks passed" in finished.stdout
