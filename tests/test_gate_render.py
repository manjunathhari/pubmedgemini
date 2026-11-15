"""Render the gate's pages with Go's html/template, as oauth2-proxy v7.15.6 does.

A template that does not parse stops oauth2-proxy from starting, so this runs in CI.
"""

import json
import shutil
import subprocess

import pytest

REFUSED = "You do not have permission to access this resource."
CSRF = "Login Failed: Unable to find a valid CSRF token. Please try again."
SIGN_IN = {
    "ProxyPrefix": "/oauth2",
    "StatusCode": 200,
    "Title": "Sign In",
    "Redirect": "/",
    "ProviderName": "Google",
    "Version": "v7.15.6",
}


def _error(status, message, **extra):
    data = {
        "Title": "Forbidden",
        "Message": message,
        "ProxyPrefix": "/oauth2",
        "StatusCode": status,
        "Redirect": "/",
        "RequestID": "",
        "Footer": "",
        "Version": "v7.15.6",
    }
    data.update(extra)
    return {"page": "error.html", "data": data}


def render(*cases):
    go = shutil.which("go")
    assert go, "Go is needed to check the gate's pages (GitHub's ubuntu runner has it)"
    result = subprocess.run(
        [go, "run", "tests/gate_render/main.go", "gate/templates"],
        input=json.dumps(list(cases)),
        capture_output=True,
        check=False,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def pages():
    return render(
        {"page": "sign_in.html", "data": SIGN_IN},
        _error(403, REFUSED),
        _error(403, CSRF, RequestID="abc123"),
        _error(
            500,
            "Oops! Something went wrong. For more information contact your server administrator.",
            Title="Internal Server Error",
        ),
        _error(403, REFUSED, Footer="https://example.org/"),
        {"page": "sign_in.html", "data": {**SIGN_IN, "Redirect": '/x"><script>alert(1)</script>'}},
        _error(403, "<b>bold</b>"),
        _error(403, REFUSED, Footer="javascript:alert(1)"),
    )


def test_sign_in_page_renders_with_the_form(pages):
    page = pages[0]
    assert (
        '<form method="GET" action="/oauth2/start"><input type="hidden" name="rd" value="/">'
        in page
    )
    assert "<h1>PubMed Ai</h1>" in page and "Sign in with Google" in page


def test_refused_account_sees_the_not_on_the_list_card(pages):
    page = pages[1]
    assert "<title>Not on the list yet · PubMed Ai</title>" in page
    assert "This account can&#39;t sign in" in page or "This account can't sign in" in page
    assert "Use another Google account:" in page
    assert 'action="/oauth2/start"' in page
    assert "Back to SurgeryAi" not in page  # no address set, no button


def test_other_403_shows_the_general_card_with_its_message(pages):
    page = pages[2]
    assert (
        "Sign-in didn" in page and "can't sign in" not in page and "can&#39;t sign in" not in page
    )
    assert CSRF in page and "Reference: abc123" in page


def test_500_shows_the_general_card(pages):
    assert "Sign-in didn" in pages[3] and "Something went wrong" in pages[3]


def test_back_to_surgeryai_uses_the_configured_address(pages):
    assert '<a class="ghost" href="https://example.org/">Back to SurgeryAi</a>' in pages[4]


def test_redirect_is_escaped(pages):
    page = pages[5]
    assert "<script>" not in page
    assert 'value="/x&#34;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in page


def test_message_is_escaped(pages):
    assert "&lt;b&gt;bold&lt;/b&gt;" in pages[6] and "<b>bold" not in pages[6]


def test_unsafe_back_address_is_blocked(pages):
    assert "javascript:" not in pages[7]
