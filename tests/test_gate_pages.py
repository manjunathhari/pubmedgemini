"""The sign-in gate's custom pages (frozen mock: sign-in-page v2, 15 Nov)."""

import hashlib
import importlib.util
import re
from pathlib import Path

TEMPLATES = Path("gate/templates")
PAGES = ("sign_in.html", "error.html")

# sha256 of Google's files as they came in the owner's signin-assets.zip:
# "Theme=Light|Dark, Show text=Yes, Shape=Pill, Platform=Android+Web.svg".
GOOGLE_SHA256 = {
    "signin-light.svg": "5c7e883333d6365b5b44d20c0e038d4e1db35cf87a5b2e2e9803b36c7e113644",
    "signin-dark.svg": "2804e711a25b165d2bde07fb09516139896a96948e734ceed44f69d33a5435a2",
}
REFUSED_403 = (
    '(eq .StatusCode 403) (eq .Message "You do not have permission to access this resource.")'
)


def _build_module():
    spec = importlib.util.spec_from_file_location("build_gate_pages", "scripts/build_gate_pages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _page(name):
    return (TEMPLATES / name).read_text(encoding="utf-8")


def test_google_buttons_are_googles_files_unchanged():
    for name, digest in GOOGLE_SHA256.items():
        data = Path("gate/assets/google", name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest, name


def test_committed_pages_match_the_build():
    built = _build_module().build()
    assert set(built) == set(PAGES)
    for name in PAGES:
        assert built[name] == _page(name), (
            f"{name} is stale: run python scripts/build_gate_pages.py"
        )


def test_pages_have_no_script_and_no_outside_requests():
    for name in PAGES:
        text = _page(name)
        assert "<script" not in text.lower(), name
        assert "/static/" not in text, name
        refs = re.findall(r'(?:src|srcset|href)="([^"]*)"|url\(([^)]*)\)', text)
        for ref in (a or b for a, b in refs):
            assert ref.startswith("data:") or ref == "{{.Footer}}", (name, ref[:60])


def test_sign_in_form_keeps_the_proxy_contract():
    text = _page("sign_in.html")
    assert text.startswith('{{define "sign_in.html"}}')
    assert 'action="{{.ProxyPrefix}}/start"' in text
    assert '<input type="hidden" name="rd" value="{{.Redirect}}">' in text
    assert text.count('class="gbtn"') == 1


def test_error_page_splits_refused_from_other_403s():
    text = _page("error.html")
    assert text.startswith('{{define "error.html"}}')
    assert text.count(REFUSED_403) == 2  # the title and the card
    refused, other = text.split("{{- else}}", 1)
    assert "This account can&#39;t sign in" not in refused  # written plainly, not escaped
    assert "This account can't sign in" in refused
    assert "Sign-in didn't work" in other and "{{.Message}}" in other


def test_each_page_shows_googles_button_in_both_themes():
    light = _build_module()._data_uri(Path("gate/assets/google/signin-light.svg"), "image/svg+xml")
    dark = _build_module()._data_uri(Path("gate/assets/google/signin-dark.svg"), "image/svg+xml")
    for name in PAGES:
        text = _page(name)
        assert f'media="(prefers-color-scheme: dark)" srcset="{dark}"' in text, name
        assert f'<img src="{light}" width="234" height="52" alt="Sign in with Google">' in text, (
            name
        )


def test_pages_stay_small():
    for name in PAGES:
        assert len(_page(name).encode()) < 300_000, name


def test_no_private_strings_in_gate_files():
    for path in [*Path("gate/src").glob("*.html"), *(TEMPLATES / n for n in PAGES)]:
        text = re.sub(r"data:[^\"')]+", "", path.read_text(encoding="utf-8"))
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), path
        assert not [
            ip for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text) if ip != "127.0.0.1"
        ], path
        assert not re.search(r"https?://", text), path
