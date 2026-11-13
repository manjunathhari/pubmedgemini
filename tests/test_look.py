"""The look: the SurgeryAi style kit (13 Nov; its tokens.css and components.css), day and night,
two self-hosted fonts, weights 300/400/500 only, a stylesheet instead of inline style, and the
kit's header (round SAi badge, wordmark, day/night switch) on every page. Nothing here is about
behaviour; test_web, test_search_page and test_ask_page cover that."""

import re
import tomllib
from html.parser import HTMLParser
from pathlib import Path

import pytest
from page_scripts import HEAD_LINKS, HEAD_SCRIPT, SWITCH_BUTTON, without_theme_scripts
from test_ask_page import HAPPY_RUN, QUESTION, HoldStarter, ask_env

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "pubmedgemini" / "static"
CSS_PATH = STATIC / "app.css"
FONTS = STATIC / "fonts"
H = {"x-dev-user-email": "owner@example.com"}

# Day and night: the kit's tokens.css under the app's names. Every colour is a custom property,
# set once for each look; the dots, the SAi artwork and its fade follow the look too.
LOOK_A = {
    "--page": "#f7f4ec", "--card": "#ffffff", "--line": "#eee8dc", "--line2": "#e4ded0",
    "--faint": "#6e7884", "--muted": "#5b6773", "--strong": "#2c3e52", "--ink": "#101a24",
    "--head": "#f7f4ec", "--head-line": "#e4ded0", "--head-ink": "#101a24",
    "--head-muted": "#1f6bd0", "--head-accent": "#1f6bd0", "--accent": "#1f6bd0",
    "--btn": "#13263c", "--btn-hover": "#101a24", "--btn-ink": "#ffffff",
    "--note": "#e3edf8", "--accent-fill": "#1e5aa0", "--on-accent-fill": "#ffffff",
    "--dots": "url(stipple-day.svg)", "--hero-art": "url(sai-hero-day-pale.webp)",
    "--hero-fade": "1",
}
LOOK_B = {
    "--page": "#101a24", "--card": "#16222e", "--line": "#1c2a38", "--line2": "#22303e",
    "--faint": "#7d8b99", "--muted": "#93a0ad", "--strong": "#c8d1da", "--ink": "#eef2f6",
    "--head": "#101a24", "--head-line": "#22303e", "--head-ink": "#eef2f6",
    "--head-muted": "#5aa0ee", "--head-accent": "#5aa0ee", "--accent": "#5aa0ee",
    "--btn": "#5aa0ee", "--btn-hover": "#7cb4f2", "--btn-ink": "#0a1420",
    "--note": "#17283b", "--accent-fill": "#1e5aa0", "--on-accent-fill": "#ffffff",
    "--dots": "url(stipple-night.svg)", "--hero-art": "url(sai-hero-night.webp)",
    "--hero-fade": ".7",
}
AGREED = {v for v in (*LOOK_A.values(), *LOOK_B.values()) if v.startswith("#")}
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


def _looks() -> tuple[str, str, str]:
    """The bodies of the day block, the night block (phone set to night, unless the switch chose
    day) and the forced-night block (the switch chose night), comments removed."""
    text = re.sub(r"/\*.*?\*/", "", css(), flags=re.DOTALL)
    root = re.search(r"(^|\n):root\s*\{", text)
    assert root
    day = _block(text, root.start())
    dark = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*"
                     r":root:not\(\[data-theme=\"light\"\]\)\s*\{", text)
    assert dark
    night = _block(text, text.index(":root", dark.start()))
    forced = re.search(r"(^|\n):root\[data-theme=\"dark\"\]\s*\{", text)
    assert forced
    return day, night, _block(text, forced.start())


def test_colours_live_only_in_the_two_looks():
    text = re.sub(r"/\*.*?\*/", "", css(), flags=re.DOTALL)
    day, night, forced = _looks()
    assert _props(day) == LOOK_A
    assert _props(night) == LOOK_B
    assert "color-scheme: light dark" in day
    # outside the blocks no colour is written: only var(--...)
    rest = text.replace(day, "").replace(night, "").replace(forced, "")
    assert not HEX.search(rest), HEX.search(rest)
    used = set(re.findall(r"var\((--[a-z0-9-]+)\)", text))
    assert used and used <= set(LOOK_A)


def test_both_night_blocks_hold_the_same_values():
    """The switch's forced night is the phone's night, value for value, so neither can drift."""
    _, night, forced = _looks()
    assert _props(forced) == _props(night) == LOOK_B
    assert night.split() == forced.split()
    # the only other theme rule: day chosen on a phone set to night sets the controls to light
    text = re.sub(r"/\*.*?\*/", "", css(), flags=re.DOTALL)
    light = re.findall(r':root\[data-theme="light"\]\s*\{([^}]*)\}', text)
    assert [b.strip() for b in light] == ["color-scheme: light;"]


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


KIT_FILES = ("stipple-day.svg", "stipple-night.svg", "sai-hero-day-pale.webp",
             "sai-hero-night.webp", "icons/sai-badge-day.svg", "icons/sai-badge-night.svg",
             "icons/sai-badge-day-180.png", "icons/lock.svg", "icons/moon.svg", "icons/sun.svg")


def test_static_files_are_packaged():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    listed = data["tool"]["setuptools"]["package-data"]["pubmedgemini"]
    assert "static/*.css" in listed and "static/fonts/*" in listed
    # every file under static/ is matched by a package-data pattern (else the wheel drops it)
    for f in STATIC.rglob("*"):
        if f.is_file():
            rel = "static/" + f.relative_to(STATIC).as_posix()
            assert any(Path(rel).match(pat) and rel.count("/") == pat.count("/")
                       for pat in listed), rel
    for name in KIT_FILES:
        assert (STATIC / name).is_file(), name
    # only the two artwork files in use: no PNG artwork, no building picture, no foot band
    assert sorted(p.name for p in STATIC.glob("*.webp")) == ["sai-hero-day-pale.webp",
                                                            "sai-hero-night.webp"]
    assert not list(STATIC.glob("*.png")) and not list(STATIC.rglob("foot-*"))


def test_kit_svgs_are_plain_drawings():
    for f in STATIC.rglob("*.svg"):
        text = f.read_text(encoding="utf-8").lower()
        assert "<script" not in text and not re.search(r"\son[a-z]+\s*=", text), f.name
        assert "href" not in text and "http://www.w3.org/2000/svg" in text, f.name


@pytest.mark.parametrize("path,kind", [
    ("/static/stipple-day.svg", "image/svg+xml"), ("/static/sai-hero-night.webp", "image/webp"),
    ("/static/sai-hero-day-pale.webp", "image/webp"), ("/static/icons/sai-badge-day-180.png",
                                                      "image/png"),
    ("/static/icons/sai-badge-night.svg", "image/svg+xml"), ("/static/icons/moon.svg",
                                                            "image/svg+xml")])
def test_kit_files_are_served(pages, path, kind):
    r = pages["client"].get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path


def test_css_points_only_at_files_that_exist():
    for u in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css()):
        assert not u.startswith(("/", "http", "data:")) and (STATIC / u).is_file(), u


BADGE = ('<span class="sai-badge" aria-hidden="true"><span class="sai-badge-in">'
         '<span class="sai-s">S</span>Ai</span></span>')
WORDMARK = ('<div class="sai-wm" role="img" aria-label="PubMed Ai"><span class="sai-wm-base">PubMed'
            '</span><span class="sai-wm-ai">Ai</span></div>')


def test_header_on_every_page(pages):
    """The kit's header: the round SAi badge, the wordmark PUBMED Ai (read out as "PubMed Ai"),
    "Educational tool only", and the day/night switch."""
    for name in ("home", "results", "run", "run_done", "message"):
        blocks = re.findall(r"<header\b.*?</header>", pages[name].text, re.DOTALL)
        assert len(blocks) == 1, name
        head = blocks[0]
        assert head.count(BADGE) == 1 and head.count(WORDMARK) == 1, name
        assert head.index(BADGE) < head.index(WORDMARK), name
        assert ">Educational tool only<" in head, name
        assert head.count(SWITCH_BUTTON) == 1 and head.count("<button") == 1, name
        assert head.index(WORDMARK) < head.index(SWITCH_BUTTON), name
        assert "pubmedgemini" not in pages[name].text.split("</header>")[0], name
        assert 'class="tile"' not in head, name
    rules = {" ".join(sel.split()): body for sel, body in _rules()}
    assert "text-transform: uppercase" in rules[".sai-wm-base"]
    assert "letter-spacing: .08em" in rules[".sai-wm-base"]
    wm = _rule_body(".sai-wm", css())  # the first .sai-wm rule; the 359px one only shrinks it
    assert "font-size: 19px" in wm and "font-weight: 300" in wm
    assert "font-size: 1.18em" in rules[".sai-s"]
    badge = rules[".sai-badge"]
    assert "width: 34px" in badge and "height: 34px" in badge and "border-radius: 50%" in badge
    assert re.search(r"@media \(max-width: 359px\) \{ \.sai-wm \{ font-size: 16px; \} \}", css())


def test_favicons_are_the_sai_badge(pages):
    for name in ("home", "results", "run_done", "message"):
        head = pages[name].text.split("</head>", 1)[0]
        assert re.findall(r"<link[^>]*>", head) == HEAD_LINKS, name


def test_no_script_tags_but_the_switch(pages):
    """No JavaScript, except the day/night switch's two scripts, pinned in page_scripts."""
    for name in ("home", "home_active", "results", "run", "run_done", "message"):
        text = pages[name].text
        bare = without_theme_scripts(text).lower()
        assert "<script" not in bare, name
        assert not re.search(r"<[^>]*\son[a-z]+\s*=", text), name  # no inline event attributes
        assert "javascript:" not in bare, name
    # the templates hold exactly the two scripts, both in base.html, neither loads a file
    templates = ROOT / "src" / "pubmedgemini" / "templates"
    for f in templates.glob("*.html"):
        text = f.read_text(encoding="utf-8")
        if f.name == "base.html":
            assert re.findall(r"<script\b[^>]*>", text) == ["<script>", "<script>"]
            assert text.count(HEAD_SCRIPT) == 1 and "src=" not in text
        else:
            assert "<script" not in text.lower(), f.name


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
    # the seven rules, the heading, and the kit's two weight overrides (.card .steps .name and
    # .card .steps .st.now, 13 Nov), which sit before the block
    assert css().count(".steps") == len(STEPS_SELECTORS) + 3
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
    (".sai-brand", "color", "--head", 4.5),
    (".sai-badge", "color", "--head-ink", 4.5),
    (".site-note", "color", "--head", 4.5),
    (".site-header nav a", "color", "--head", 4.5),
    (".site-header nav a.current", "color", "--head", 4.5),
    (".sai-daynight", "color", "--head", 4.5),
    ("main a", "color", "--card", 4.5),
    ("main a", "color", "--page", 4.5),
    ("button", "color", "--btn", 4.5),
    ("button:hover", "background", "--btn-ink", 4.5),
    ("body", "color", "--page", 4.5),
    (".sai-label", "color", "--page", 4.5),
    (".sai-note", "color", "--note", 4.5),
    (".tag", "color", "--accent-fill", 4.5),
    (".muted", "color", "--card", 4.5),
    (".muted", "color", "--page", 4.5),
    ("main .eyebrow", "color", "--card", 4.5),
]
# the focus ring: the accent against everything it is drawn on
RING = [("--accent", bg) for bg in ("--page", "--card", "--note")]


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
        assert _ratio(look[fg], look[bg]) >= floor, (selector, prop, bg)
    for fg, bg in RING:
        assert _ratio(look[fg], look[bg]) >= 3.0, (fg, bg)


def test_focus_ring_is_the_accent():
    rules = {" ".join(sel.split()): body for sel, body in _rules()}
    ring = rules["a:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible"]
    assert "outline: 3px solid var(--accent)" in ring
    # the frozen F3b block draws the summary ring in --ink; a later, more specific rule makes it blue
    assert "outline-color: var(--accent)" in rules["main summary:focus-visible"]
    assert not any("--head-ink" in body for sel, body in _rules() if "focus" in sel)


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


def test_headings_labels_and_buttons_use_the_kits_weights():
    """The kit: titles and section headings light (300), card titles, labels, buttons, links and
    b/strong medium (500), big numbers light; reading text is the serif at 17px."""
    rules = {" ".join(sel.split()): body for sel, body in _rules()}
    assert "font-weight: 300" in rules["h1"] and "font-weight: 300" in rules["h2"]
    assert "font-weight: 500" in rules[".card h2, main > article > h2"]
    assert "font-weight: 500" in rules["h3"] and "font-weight: 500" in rules["b, strong"]
    assert "font-weight: 500" in rules["label"]
    assert "font-weight: 500" in rules["button"]
    assert "font-weight: 500" in rules[".site-header nav a"]
    assert "font-weight: 400" in rules["body"]
    assert "font-weight: 500" in rules["th"]
    assert "color: var(--muted)" in rules[".lead"] and "margin-top: 6px" in rules[".lead"]
    reading = rules[":where(main, .card, article, details) > p"]
    assert "font-size: 17px" in reading and "line-height: 1.45" in reading
    button = rules["button"]
    assert "border-radius: 26px" in button and "min-height: 52px" in button
    assert "border-radius: 18px" in rules[".card, main > article"]
    assert "border: 1px solid var(--line2)" in rules[".card, main > article"]


# ---------- weights: 300, 400 and 500 only (the kit), also over the two frozen mock blocks ----------

F3B_MOCK = ROOT / "docs" / "mocks" / "f3b-answer-page-v6.html"


def _frozen_lines() -> set[str]:
    """Every rule line the stylesheet copies word for word from the F3b and F4a mocks."""
    f3b = F3B_MOCK.read_text(encoding="utf-8").split("/* ---- new for the answer page (F3b)", 1)[1]
    f3b = f3b.split("*/", 1)[1].split("</style>", 1)[0]
    f3b = re.sub(r"#[0-9A-Fa-f]{6}\b", lambda m: GREY_TO_VAR[m.group(0).upper()], f3b)
    f4a = [_as_vars(v) for v in _mock_f4a_rules().values()]
    return {ln.strip() for ln in (*f3b.splitlines(), *f4a) if ln.strip()}


def _specificity(sel: str) -> tuple[int, int, int]:
    s = re.sub(r":where\([^)]*\)", "", sel)
    ids = len(re.findall(r"#[\w-]+", s))
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+", s))
    tags = len(re.findall(r"(?:^|[\s>+~])([a-z][\w-]*)", s))
    return ids, classes, tags


def test_every_600_or_700_is_frozen_and_overridden():
    """A weight of 600 or 700 appears only in a rule copied word for word from a frozen mock, and
    for each such selector a rule of higher specificity on the same element sets 300/400/500."""
    frozen = _frozen_lines()
    lines = css().splitlines()
    heavy = [ln for ln in lines if re.search(r"font-weight:\s*[67]00", ln)]
    assert heavy  # the frozen blocks still carry theirs
    rules = _rules()
    for ln in heavy:
        assert ln.strip() in frozen, ln
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", ln.split("@media", 1)[0]):
            if not re.search(r"font-weight:\s*[67]00", body):
                continue
            for one in (" ".join(x.split()) for x in sel.split(",")):
                plain = one.replace(">", " > ").split()
                ok = False
                for osel, obody in rules:
                    if not re.search(r"font-weight:\s*[345]00", obody):
                        continue
                    for other in (" ".join(x.split()) for x in osel.split(",")):
                        oth = other.replace(">", " > ").split()
                        if oth[-len(plain):] == plain and _specificity(other) > _specificity(one):
                            ok = True
                assert ok, f"{one} keeps a weight of 600/700"
    # outside the frozen lines no rule sets a weight other than 300, 400 or 500
    for ln in lines:
        if ln.strip() not in frozen:
            for w in re.findall(r"font-weight:\s*(\w+)(?!\s*\d)", ln):  # not @font-face ranges
                assert w in ("300", "400", "500"), ln
    assert "bold" not in css()


def test_the_overrides_come_before_the_f4a_block():
    text = css()
    head = "/* ---- the completeness check (F4a)"
    for sel in ("main .eyebrow", "main .pill", "main details.oc .t", "main .big", "main .cmp b",
                "main details.fold > summary", ".card .steps .name", ".card .steps .st.now"):
        assert text.index(sel + " {") < text.index(head), sel
    assert "font-weight: 300" in _rule_body("main .big", text)


# ---------- the kit's parts on the pages ----------

def test_label_above_the_title_on_ask_and_answer_pages(pages):
    home = pages["home"].text
    assert '<p class="sai-label">Ask the evidence</p>\n<h1>PubMed Ai</h1>' in home
    done = pages["run_done"].text
    assert done.count('<p class="sai-label">PubMed Ai answer</p>\n<h1>') == 1
    for name in ("results", "run", "message"):
        assert "sai-label" not in pages[name].text, name
    label = _rule_body(".sai-label", css())
    for bit in ("color: var(--accent)", "font-weight: 500", "letter-spacing: .16em",
                "text-transform: uppercase", "font-size: 11.5px"):
        assert bit in label, bit


def test_patient_warnings_are_note_boxes(pages):
    home = pages["home"].text
    ask = ('<p class="sai-note">Google may use what you type here, and its reviewers may read it '
           '(Gemini free tier). Do not type anything that identifies a patient.</p>')
    search = ('<p class="sai-note">What you type is sent to PubMed and Europe PMC as the search. '
              'Do not type anything that identifies a patient.</p>')
    assert home.count(ask) == 1 and home.count(search) == 1
    assert home.index(ask) < home.index("Key-word search") < home.index(search)
    note = _rule_body(".sai-note", css())
    for bit in ("background: var(--note)", "color: var(--muted)", "border-radius: 16px"):
        assert bit in note, bit


def test_sai_artwork_only_on_the_ask_page(pages):
    art = '<div class="sai-hero-art" aria-hidden="true"></div>'
    home = pages["home"].text
    assert home.count(art) == 1
    assert home.index('<div class="sai-hero">') < home.index(art) < home.index("<h1>PubMed Ai</h1>")
    # the owner, 13 Nov: laid out like the landing page's hero: label, title and description together at the
    # foot of the block (phone) or on the left beside the artwork (desktop), no building picture
    assert home.index("<h1>PubMed Ai</h1>") < home.index('<p class="lead">') \
        < home.index("</div>", home.index('<p class="lead">')) < home.index("Signed in as")
    assert '<main class="home">' in home
    for name in ("results", "run", "run_done", "message", "home_active"):
        if name != "home_active":
            assert "sai-hero" not in pages[name].text, name
    rule = _rule_body(".sai-hero-art", css())
    mask_oval = "radial-gradient(ellipse 60% 55% at 60% 38%, var(--ink) 40%, transparent 100%)"
    mask_down = "linear-gradient(to bottom, var(--ink) 50%, transparent 92%)"
    for prop in ("-webkit-mask-image", "mask-image"):
        assert f"{prop}: {mask_oval}, {mask_down};" in rule, prop
    assert "mask-composite: intersect" in rule and "-webkit-mask-composite: source-in" in rule
    assert "z-index: -1" in rule and "pointer-events: none" in rule
    # the owner, 13 Nov: the landing page's exact size (kit README item 4): phone box full width x 700px,
    # artwork 440px tall at centre top; from 900px a 700 x 790 box, 60px above, 20px from the right, cover
    assert "background: var(--hero-art) center top / auto 440px no-repeat" in rule
    assert "height: 700px" in rule and "top: 0" in rule
    wide = css().split("@media (min-width: 900px)", 1)[1]
    desk = _rule_body(".sai-hero-art", wide)
    # 20px from the screen's right edge: the home page's main has 64px side padding on desktop, so -44px
    for bit in ("top: -60px", "right: -44px", "width: 700px", "height: 790px",
                "background-position: center", "background-size: cover"):
        assert bit in desk, bit
    assert "isolation: isolate" in _rule_body(".sai-hero", css())
    assert "min-height: 640px" in _rule_body(".sai-hero", css())  # the kit's phone hero
    hero_wide = _rule_body(".sai-hero", wide)
    assert "min-height: 560px" in hero_wide
    for bit in ("max-width: 1440px", "padding-left: 64px", "padding-right: 64px"):
        assert bit in _rule_body("main.home", wide), bit
    head = _rule_body("main.home .sai-hero h1", wide)
    assert "font-size: 58px" in head and "line-height: 1.04" in head and "letter-spacing: -0.03em" in head
    assert "z-index: 1" in _rule_body(".site-header", css())  # the header stays above the artwork


def test_dot_background():
    rule = _rule_body("body::before", css())
    for bit in ("position: fixed", "z-index: -1", "pointer-events: none",
                "background: var(--dots) 0 0 / 480px 480px repeat"):
        assert bit in rule, bit


def test_labels_in_reading_text_are_the_sans():
    # serif at 500 hardly differs from 400; the labels at the start of answer lines must stand out
    rules = {" ".join(sel.split()): body for sel, body in _rules()}
    assert "Hanken Grotesk" in rules["p b, li b, p strong, li strong"]


def test_ask_and_search_sit_side_by_side_on_desktop(pages):
    # the owner, 13 Nov: "make this side by side; user can choose whichever"
    home = pages["home"].text
    pair = home.index('<div class="sai-pair">')
    assert home.index("<h2>Key-word search") < home.index("</div>", pair) \
        < home.index("</main>")  # both cards inside the pair
    assert home.index("Signed in as") < pair < home.index('<label for="question"') \
        < home.index("<h2>Key-word search")
    assert "display: grid" in _rule_body(".sai-pair", css())  # stacked on phones
    wide = css().split("@media (min-width: 900px)", 1)[1]
    assert "grid-template-columns: minmax(0, 1fr) minmax(0, 1fr)" in _rule_body(".sai-pair", wide)
    assert "max-width: none" in _rule_body("main.home > .sai-pair", wide)


def test_the_two_cards_end_level(pages):
    # the owner, 13 Nov: the two note boxes line up; the search card says where its searches run
    home = pages["home"].text
    runs = '<p class="sai-runs">Free runs left today: about '
    where = '<p class="sai-runs">All searches run on regular PubMed and Europe PMC.</p>'
    assert runs in home and where in home
    assert home.index(runs) < home.index('<p class="sai-note">Google may use')
    assert home.index(where) < home.index('<p class="sai-note">What you type is sent')
    assert "margin-top: auto" in _rule_body(".sai-pair .card > .sai-runs", css())


def test_search_card_says_no_ai_model_and_hides_its_label_on_the_ask_page(pages):
    home = pages["home"].text
    assert '<h2>Key-word search <span class="sai-aside">No AI model</span></h2>' in home
    assert "with exactly what you type" not in home
    rule = _rule_body(".sai-pair label[for=\"q\"]", css())  # read by screen readers, not shown
    for bit in ("position: absolute", "width: 1px", "height: 1px", "overflow: hidden", "clip-path: inset(50%)"):
        assert bit in rule, bit
    assert "color: var(--muted)" in _rule_body(".sai-aside", css())
    assert "font-weight: 400" in _rule_body(".sai-aside", css())


def test_search_box_and_button_line_up_with_the_ask_ones_on_desktop():
    # the owner, 13 Nov: "re align the box and the pill"; checked in a browser at 1440px (same tops)
    wide = css().split("@media (min-width: 900px)", 1)[1]
    assert "position: absolute" in _rule_body('.sai-pair p:has(> label[for="q"])', wide)
    assert "height: 116px" in _rule_body(".sai-pair textarea, .sai-pair input#q", wide)
    assert "margin-top: -10px" in _rule_body(".sai-pair .card > h2 + form", wide)
