"""The look batch: greys, two self-hosted fonts, a stylesheet instead of inline style, and a
header on every page. Nothing here is about behaviour; test_web, test_search_page and
test_ask_page cover that."""

import re
import tomllib
from pathlib import Path

import pytest
from test_ask_page import HAPPY_RUN, QUESTION, HoldStarter, ask_env

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "pubmedgemini" / "static"
CSS_PATH = STATIC / "app.css"
FONTS = STATIC / "fonts"
H = {"x-dev-user-email": "owner@example.com"}

AGREED = {"#333333", "#4d4d4d", "#8c8c8c", "#666666", "#bdbdbd", "#dddddd", "#f4f4f4", "#ffffff"}
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


def test_css_has_only_greys():
    text = css()
    colours = hex_colours()
    assert colours
    for c in colours:
        assert c[1:3] == c[3:5] == c[5:7], c
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
        out["run"] = e.run_page(r.headers["location"])
        e.starter.run(e.starter.started[0])
        out["run_done"] = e.run_page(r.headers["location"])
        out["message"] = e.run_page("/runs/not-a-uuid")
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
        assert re.search(r'<div class="tile"[^>]*>P</div>', head), name
        assert ">pubmedgemini<" in head, name
        assert ">Educational tool only<" in head, name


def test_no_script_tags(pages):
    for name in ("home", "results", "run", "run_done", "message"):
        assert "<script" not in pages[name].text.lower(), name


def test_run_page_still_refreshes_itself(pages):
    assert '<meta http-equiv="refresh" content="5">' in pages["run"].text
    assert "http-equiv" not in pages["run_done"].text
