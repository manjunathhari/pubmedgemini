"""The look: the SurgeryAi palette (Look A by day, Look B by night, frozen on the Mock Board
13 Nov), two self-hosted fonts, a stylesheet instead of inline style, and a header on every
page. Nothing here is about behaviour; test_web, test_search_page and
test_ask_page cover that."""

import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest
from test_ask_page import HAPPY_RUN, QUESTION, HoldStarter, ask_env

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "pubmedgemini" / "static"
CSS_PATH = STATIC / "app.css"
FONTS = STATIC / "fonts"
H = {"x-dev-user-email": "owner@example.com"}

# Look A (day) and Look B (night): every colour is a custom property, set once for each look.
LOOK_A = {
    "--page": "#eef0f3", "--card": "#ffffff", "--line": "#dde3ea", "--line2": "#bac5d1",
    "--faint": "#7d8a98", "--muted": "#5b6773", "--strong": "#2c3e52", "--ink": "#101a24",
    "--head": "#13263c", "--head-line": "#13263c", "--head-ink": "#f7f4ec",
    "--head-muted": "#93a0ad", "--head-accent": "#5aa0ee", "--accent": "#1f6bd0",
    "--btn": "#13263c", "--btn-hover": "#101a24", "--btn-ink": "#ffffff",
}
LOOK_B = {
    "--page": "#1b2836", "--card": "#223142", "--line": "#2f4053", "--line2": "#46586c",
    "--faint": "#8392a1", "--muted": "#a3b0bd", "--strong": "#c8d1da", "--ink": "#e8edf2",
    "--head": "#101a24", "--head-line": "#2f4053", "--head-ink": "#f7f4ec",
    "--head-muted": "#93a0ad", "--head-accent": "#5aa0ee", "--accent": "#5aa0ee",
    "--btn": "#5aa0ee", "--btn-hover": "#7cb4f2", "--btn-ink": "#0a1420",
}
AGREED = set(LOOK_A.values()) | set(LOOK_B.values())
COLOUR_NAMES = ("red", "blue", "green", "orange", "yellow", "purple", "pink", "teal", "navy")
HEX = re.compile(r"#([0-9a-fA-F]{3,8})\b")


def css() -> str:
    return CSS_PATH.read_text(encoding="utf-8")


def expand(h: str) -> str:
    h = h.lower()
    return "#" + "".join(c * 2 for c in h) if len(h) == 3 else "#" + h


def hex_colours() -> set[str]:
    found = set()
    for m in HEX.finditer(css()):
        # 4- and 8-digit (alpha) colours and odd lengths are not in the agreed set
        assert len(m.group(1)) in (3, 6), m.group(0)
        found.add(expand(m.group(1)))
    return found


def _block(text: str, start: int) -> str:
    """The body of the brace block that opens at or after `start`."""
    i = text.index("{", start)
    depth, j = 0, i
    while True:
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[i + 1:j]
        j += 1


def _props(body: str) -> dict[str, str]:
    return {k: v.strip().lower() for k, v in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)}


def test_colours_live_only_in_the_two_looks():
    text = re.sub(r"/\*.*?\*/", "", css(), flags=re.DOTALL)
    root = re.search(r"(^|\n):root\s*\{", text)
    assert root
    day = _block(text, root.start())
    dark = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{", text)
    assert dark
    night = _block(text, text.index(":root", dark.end()))
    assert _props(day) == LOOK_A
    assert _props(night) == LOOK_B
    assert "color-scheme: light dark" in day
    # outside the two blocks no colour is written: only var(--...)
    rest = text.replace(day, "").replace(night, "")
    assert not HEX.search(rest), HEX.search(rest)
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", text))
    assert used and used <= set(LOOK_A)


def test_css_has_no_other_colour_forms():
    text = css()
    assert hex_colours()
    for fn in ("rgb(", "rgba(", "hsl(", "hsla("):
        assert fn not in text.lower()
    for name in COLOUR_NAMES:
        # a colour value: after a colon, before a semicolon, brace or space-separated value
        assert not re.search(rf":[^;{{}}]*\b{name}\b", text, re.IGNORECASE), name


def test_css_uses_only_the_agreed_greys():
    assert hex_colours() <= AGREED


def test_alpha_hex_is_caught(monkeypatch):
    monkeypatch.setattr(CSS_PATH.__class__, "read_text", lambda self, **kw: "a{color:#33333380}")
    with pytest.raises(AssertionError):
        hex_colours()


def test_long_text_wraps_in_cards():
    assert "overflow-wrap: anywhere" in css()
    assert "min-width: 0" in css()


def test_lists_and_inline_links_styles():
    text = css()
    assert not re.search(r"\bul\s*\{[^}]*Source Serif", text)
    assert not re.search(r"(^|\n)a\s*\{[^}]*min-height", text)


def _rules():
    """(selector list, body) for each plain rule in app.css, comments removed."""
    text = re.sub(r"/\*.*?\*/", "", css(), flags=re.DOTALL)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", text)]


def test_only_marked_links_get_the_big_target(pages):
    bare = {"a", "p a", "p > a", "li a"}
    for sel, body in _rules():
        parts = {" ".join(x.split()) for x in sel.split(",")}
        big = "min-height" in body or "display: inline-block" in body
        if big and parts & bare:
            raise AssertionError(f"bare link selector gets the big target: {sel}")
        if "min-height" in body and any(re.search(r"(^|\W)a\b", x) for x in parts):
            assert all(".row" in x or ".item" in x for x in parts if re.search(r"\ba\b", x)), sel

    class Scan(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.bad = [], []
            self.p_text = self.p_marked = self.in_a = 0

        def handle_starttag(self, tag, attrs):
            classes = (dict(attrs).get("class") or "").split()
            self.stack.append((tag, classes))
            if tag == "p":
                self.p_text = self.p_marked = 0
            if tag == "a":
                self.in_a += 1
                if any(c in ("row", "item") for _, cl in self.stack for c in cl):
                    self.p_marked = 1

        def handle_endtag(self, tag):
            if tag == "a":
                self.in_a -= 1
            if tag == "p" and self.p_marked and self.p_text:
                self.bad.append(1)
            while self.stack and self.stack[-1][0] != tag:
                self.stack.pop()
            if self.stack:
                self.stack.pop()

        def handle_data(self, data):
            if self.in_a == 0 and data.strip() and any(t == "p" for t, _ in self.stack):
                self.p_text = 1

    marked = 0
    for name in ("home_after", "home_active", "results", "run", "run_done"):
        scan = Scan()
        scan.feed(pages[name].text)
        assert not scan.bad, name
        marked += len(re.findall(r'class="(?:row|item)"', pages[name].text))
    assert marked


def test_fonts_are_self_hosted():
    text = css()
    assert "http://" not in text and "https://" not in text
    faces = re.findall(r"@font-face\s*\{[^}]*\}", text)
    assert len(faces) == 2
    for face in faces:
        urls = re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", face)
        assert urls
        for u in urls:
            assert u.startswith("fonts/") and (STATIC / u).is_file(), u
        assert "font-display: swap" in face
    assert "100 900" in text and "200 900" in text
    assert (FONTS / "LICENSE-hanken-grotesk.txt").is_file()
    assert (FONTS / "LICENSE-source-serif-4.txt").is_file()


@pytest.fixture
def pages(db_url, upstream):
    """Home, a results page, a run page (working), a finished run page and a message page."""
    with ask_env(db_url, upstream, HAPPY_RUN, starter=HoldStarter()) as e:
        out = {"home": e.c.get("/", headers=H)}
        out["results"] = e.c.post("/search", data={"q": "hernia mesh"}, headers=H)
        r = e.ask(QUESTION)
        out["home_active"] = e.c.get("/", headers=H)
        out["run"] = e.run_page(r.headers["location"])
        e.starter.run(e.starter.started[0])
        out["run_done"] = e.run_page(r.headers["location"])
        out["message"] = e.run_page("/runs/not-a-uuid")
        out["home_after"] = e.c.get("/", headers=H)
        out["client"] = e.c
        yield out


def test_base_links_stylesheet_and_has_no_inline_style(pages):
    for name in ("home", "results", "run", "run_done", "message"):
        text = pages[name].text
        assert 'href="/static/app.css"' in text, name
        assert "<style" not in text, name


def test_static_css_is_served(pages):
    r = pages["client"].get("/static/app.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "@font-face" in r.text


def test_static_font_is_served(pages):
    r = pages["client"].get("/static/fonts/hanken-grotesk-latin-wght-normal.woff2")
    assert r.status_code == 200
    assert "font/woff2" in r.headers["content-type"] or "woff2" in r.headers["content-type"]
    assert r.content[:4] == b"wOF2"


def test_static_files_are_packaged():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    listed = data["tool"]["setuptools"]["package-data"]["pubmedgemini"]
    assert "static/*.css" in listed and "static/fonts/*" in listed


def test_header_on_every_page(pages):
    for name in ("home", "results", "run", "run_done", "message"):
        blocks = re.findall(r"<header\b.*?</header>", pages[name].text, re.DOTALL)
        assert len(blocks) == 1, name
        head = blocks[0]
        assert re.search(r'<div class="tile"[^>]*>SAi</div>', head), name
        assert ">PubMed Ai<" in head, name
        assert "pubmedgemini" not in pages[name].text.split("</header>")[0], name
        assert ">Educational tool only<" in head, name


def test_no_script_tags(pages):
    for name in ("home", "results", "run", "run_done", "message"):
        assert "<script" not in pages[name].text.lower(), name


def test_run_page_still_refreshes_itself(pages):
    assert '<meta http-equiv="refresh" content="5">' in pages["run"].text
    assert "http-equiv" not in pages["run_done"].text


def test_controls_and_labels_use_the_sans_font():
    """Controls, labels and inputs are Hanken Grotesk wherever they sit, also inside a <p>
    (which is Source Serif 4): one rule names them all and comes after the `p` rule."""
    rules = _rules()
    serif_at = next(i for i, (sel, body) in enumerate(rules)
                    if sel == "p" and "Source Serif" in body)
    named = {"label", "button", "input", "textarea"}
    found = set()
    for i, (sel, body) in enumerate(rules):
        parts = {" ".join(x.split()) for x in sel.split(",")}
        m = re.search(r"font-family:\s*([^;]+)", body)
        if i > serif_at and m and m.group(1).strip().startswith('"Hanken Grotesk"'):
            found |= parts & named
    assert found == named, named - found
    # a later `font:` shorthand on a control would reset the family to the serif it inherits
    for sel, body in rules[serif_at:]:
        parts = {" ".join(x.split()).split("[")[0].split(":")[0] for x in sel.split(",")}
        if parts & named:
            assert not re.search(r"(^|[;\s])font:", body), sel


# ---------- the answer page's rules (F3b plan Task 3, mock v6) ----------


def _rule_body(selector: str, text: str) -> str:
    m = re.search(r"(?:^|[}\s])" + re.escape(selector) + r"\s*\{([^}]*)\}", text)
    assert m, selector
    return m.group(1)


def test_answer_rules_present():
    text = css()
    assert re.search(r"(^|[}\s])\.two\s*\{", text)
    assert re.search(r"@media \(min-width:700px\)\{\.two\{grid-template-columns:"
                     r"minmax\(0,1fr\) minmax\(0,1fr\)", text)
    assert re.search(r"@media \(min-width:700px\)\{main\.wide\{max-width:68rem\}\}", text)
    assert "svg.track{" in text
    assert "stroke-dasharray" in _rule_body(".track .null", text)
    assert re.search(r"(^|[}\s,])\.dots\{", text)
    assert "details.more{" in text
    assert _rule_body(".pill.hi", text)
    assert "dashed" in _rule_body(".pill.vl", text)
    # no colour carries meaning alone: the two arms are solid against hatched (N3)
    assert "repeating-linear-gradient" in _rule_body(".B .dots i.on,.B i.key", text)


def test_no_row_class_rules_added():
    """The compare rows are `.cmp`: a `.row` means "links in here get the 44px target"."""
    text = css()
    assert ".cmp{" in text
    rows = [sel for sel, _ in _rules() if re.search(r"\.row\b", sel)]
    assert rows == [".row > a, .item a"]


# ---------- F4a: the completeness check's .steps rules (mock f4a-completeness-v3) ----------

F4A_MOCK = ROOT / "docs" / "mocks" / "f4a-completeness-v3.html"
STEPS_SELECTORS = (".steps", ".steps .name", ".steps .st", ".steps .st.was", ".steps .st.now",
                   ".steps .ar", ".steps .sub[data-x]")
UNUSED_SELECTORS = (".check ul", ".check .line", "details.fold>summary.steps::before",
                    ".steps .brk")


def _mock_f4a_rules() -> dict[str, str]:
    """selector -> the whole rule line, from the mock's F4a block."""
    block = F4A_MOCK.read_text(encoding="utf-8").split(
        "/* ---- new for the completeness check (F4a) ---- */", 1)[1].split("</style>", 1)[0]
    lines = [ln for ln in block.splitlines() if ln.strip()]
    return {ln.split("{", 1)[0]: ln for ln in lines}


# the frozen mocks were drawn in greys; the stylesheet writes each grey as its look's property
GREY_TO_VAR = {"#F4F4F4": "var(--page)", "#FFFFFF": "var(--card)", "#DDDDDD": "var(--line)",
               "#BDBDBD": "var(--line2)", "#8C8C8C": "var(--faint)", "#666666": "var(--muted)",
               "#4D4D4D": "var(--strong)", "#333333": "var(--ink)"}


def _as_vars(rule: str) -> str:
    return re.sub(r"#[0-9A-Fa-f]{6}\b", lambda m: GREY_TO_VAR[m.group(0).upper()], rule)


def test_steps_rules_are_the_mocks():
    rules = {k: _as_vars(v) for k, v in _mock_f4a_rules().items()}
    assert set(rules) == set(STEPS_SELECTORS) | set(UNUSED_SELECTORS)
    lines = css().splitlines()
    for sel in STEPS_SELECTORS:  # character for character, each on its own line, once
        assert lines.count(rules[sel]) == 1, sel
    for sel in UNUSED_SELECTORS:  # the mock's four rules its markup never uses are not copied
        assert rules[sel] not in css() and sel + "{" not in css(), sel
    assert css().count(".steps") == len(STEPS_SELECTORS) + 1  # the seven rules and the heading
    head = ("/* ---- the completeness check (F4a): the .steps rules of "
            "docs/mocks/f4a-completeness-v3.html ---- */")
    assert css().rstrip().endswith(rules[".steps .sub[data-x]"])  # the block is the file's end
    assert css().index(head) < css().index(rules[".steps"])


def _lum(hexc: str) -> float:
    def ch(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hexc[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# (rule selector, property, background property, floor): text that must stay readable
PAIRS = [
    (".site-name", "color", "--head", 4.5),
    (".site-note", "color", "--head", 4.5),
    (".site-header nav a", "color", "--head", 4.5),
    (".site-header a:focus-visible", "outline-color", "--head", 3.0),
    ("main a", "color", "--card", 4.5),
    ("button", "color", "--btn", 4.5),
    ("button:hover", "background", "--btn-ink", 4.5),
    ("body", "color", "--page", 4.5),
]


def _prop_of(selector: str, prop: str) -> str:
    for sel, body in _rules():
        if selector in [" ".join(x.split()) for x in sel.split(",")]:
            m = re.search(rf"(?:^|;|\s){re.escape(prop)}\s*:\s*var\((--[a-z0-9-]+)\)", body)
            if m:
                return m.group(1)
    raise AssertionError(f"{selector} has no {prop}: var(--...)")


@pytest.mark.parametrize("look", [LOOK_A, LOOK_B], ids=["day", "night"])
def test_text_on_its_background_is_readable_in_both_looks(look):
    for selector, prop, bg, floor in PAIRS:
        fg = _prop_of(selector, prop)
        assert _ratio(look[fg], look[bg]) >= floor, (selector, prop)


def test_ask_button_sits_10px_below_the_question_box():
    # the owner, 13 Nov: no space at all between the question box and the Ask button
    assert re.search(r"(^|\n)form p > textarea\s*\{\s*margin-bottom:\s*10px;\s*\}", css())


DESCRIPTION = ('An AI search agent for clinical questions. It searches PubMed and Europe PMC, picks the studies '
               'that answer your question, and sets out what they show in one clear, readable answer, with every point linked to its source.')


def test_ask_page_says_what_pubmed_ai_is(pages):
    # the owner, 13 Nov (Mock Board, "PubMed Ai: name and description", frozen)
    text = pages["home"].text
    assert text.index("<h1>PubMed Ai</h1>") < text.index(f'<p class="lead">{DESCRIPTION}</p>') \
        < text.index("Signed in as")


def test_headings_labels_and_buttons_use_the_landing_pages_weight():
    rules = {" ".join(sel.split()): body for sel, body in _rules()}
    assert "font-weight: 500" in rules["h1"] and "font-weight: 500" in rules["h2"]
    assert "font-weight: 500" in rules["label"]
    assert "font-weight: 500" in rules["button"]
    assert "color: var(--muted)" in rules[".lead"] and "margin-top: 6px" in rules[".lead"]
