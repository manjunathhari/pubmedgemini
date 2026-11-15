"""Build the sign-in gate's two pages (oauth2-proxy custom templates).

The gate shows these before anyone is signed in, so nothing behind it (our /static)
can load. Each page is therefore self-contained: the fonts and Google's official
"Sign in with Google" button are inlined as data URIs.

Sources: gate/src/*.html. Output: gate/templates/sign_in.html and error.html
(committed; mounted read-only into the gate). Run after changing a source:

    python scripts/build_gate_pages.py
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "gate" / "src"
OUT = ROOT / "gate" / "templates"
FONTS = ROOT / "src" / "pubmedgemini" / "static" / "fonts"
GOOGLE = ROOT / "gate" / "assets" / "google"
PAGES = ("sign_in.html", "error.html")

LOCK = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>'
)


def _data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build(src_dir: Path = SRC) -> dict[str, str]:
    """Return {page name: finished template text}."""
    head = (src_dir / "head.html").read_text(encoding="utf-8")
    head = head.replace(
        "@@FONT_HANKEN@@", _data_uri(FONTS / "hanken-grotesk-latin-wght-normal.woff2", "font/woff2")
    ).replace(
        "@@FONT_SERIF@@", _data_uri(FONTS / "source-serif-4-latin-wght-normal.woff2", "font/woff2")
    )
    button = (src_dir / "google_button.html").read_text(encoding="utf-8").strip()
    button = button.replace(
        "@@GOOGLE_LIGHT@@", _data_uri(GOOGLE / "signin-light.svg", "image/svg+xml")
    ).replace("@@GOOGLE_DARK@@", _data_uri(GOOGLE / "signin-dark.svg", "image/svg+xml"))
    pages = {}
    for name in PAGES:
        text = (src_dir / name).read_text(encoding="utf-8")
        text = (
            text.replace("@@HEAD@@", head.strip())
            .replace("@@GOOGLE_BUTTON@@", button)
            .replace("@@LOCK@@", LOCK)
        )
        if "@@" in text:
            raise ValueError(f"{name}: a placeholder was left unfilled")
        pages[name] = text
    return pages


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in build().items():
        (OUT / name).write_text(text, encoding="utf-8")
        print(f"wrote gate/templates/{name} ({len(text.encode())} bytes)")


if __name__ == "__main__":
    main()
