"""The way back to SurgeryAi (frozen mock v1, owner, 20 Nov 2026): a slim "← SurgeryAi home"
line above the header on every page. The public repo holds no domain: the address is the
app's own host without its first label (pubmedai.<site> → https://<site>/), worked out per
request from X-Forwarded-Host, else Host. No such parent (localhost, an IP, a bare name, junk)
means no line."""

import pytest
from test_ask_page import H, ask_env, insert_run

from pubmedgemini.web import home_url_for

BAR = '<div class="home-bar"><a href="https://example.org/">&larr; SurgeryAi home</a></div>'


@pytest.mark.parametrize("headers,expected", [
    ({"host": "pubmedai.example.org"}, "https://example.org/"),
    ({"x-forwarded-host": "pubmedai.example.org", "host": "app:8000"}, "https://example.org/"),
    ({"x-forwarded-host": "PubMedAi.Example.ORG:443"}, "https://example.org/"),
    ({"x-forwarded-host": "a.b.example.co.uk"}, "https://b.example.co.uk/"),
    ({"host": "example.org"}, None),            # no parent to go to
    ({"host": "localhost:8000"}, None),
    ({"host": "testserver"}, None),
    ({"host": "127.0.0.1:8765"}, None),
    ({"host": "10.0.0.5"}, None),
    ({"host": "[::1]:8000"}, None),
    ({"host": 'x.evil"><script>.org'}, None),   # anything not a plain host name
    ({"host": "x..org"}, None),
    ({}, None),
])
def test_home_url_for(headers, expected):
    assert home_url_for(headers) == expected


def test_the_bar_is_on_every_page(db_url, upstream):
    h = H | {"x-forwarded-host": "pubmedai.example.org"}
    with ask_env(db_url, upstream) as env:
        rid = insert_run(env)
        for path in ("/", "/library", "/activity", "/key", f"/runs/{rid}"):
            t = env.c.get(path, headers=h).text
            assert t.count(BAR) == 1, path
            assert t.index(BAR) < t.index("<header"), path  # above the header


def test_no_bar_without_a_parent_site(db_url, upstream):
    with ask_env(db_url, upstream) as env:
        t = env.c.get("/", headers=H).text  # host "testserver"
    assert 'class="home-bar"' not in t


def test_the_brand_still_goes_to_pubmed_ai_home(db_url, upstream):
    h = H | {"x-forwarded-host": "pubmedai.example.org"}
    with ask_env(db_url, upstream) as env:
        t = env.c.get("/library", headers=h).text
    assert '<a class="sai-brand" href="/" aria-label="PubMed Ai home">' in t
