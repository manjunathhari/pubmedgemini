# F1: Search tools and the plain results page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After sign-in, type a search into one box and read PubMed and Europe PMC results as plain text (title, journal, year, design, PMID link, abstract). No model. ₹0 a search.

**Architecture:** The four literature tools of Incisive Quick (`search_pubmed`, `search_europepmc`, `get_abstracts`, `find_related`) as methods of one `LitTools` object, with typed results, so F2's researcher can call them as model tools unchanged. Under them: one outbound HTTP client (two allowed hosts, retries, body cap), one process-wide NCBI pacer, a PubMed client, a Europe PMC client, and a Postgres paper cache (`papers`). The page is one form that POSTs to `/search` and gets the results page back in the same response.

**Tech Stack:** As F0, plus `httpx` (runtime) and `python-multipart` (FastAPI form parsing). Python's `xml.etree.ElementTree` for efetch XML.

**Spec:** `.10x/specs/2026-11-10-pubmedgemini-quick-design.md` (§4 limits and logs, §5 the four tools, §7 `papers`, §8 one source down, §9 F1, §14). Reference code (read-only): Incisive `crates/incisive-app/src/lit/{mod,http,pubmed,europepmc,cache}.rs`, `src/agent/tools/{lit,mod}.rs`, `src/agent/search.rs`, `src/agent/screen.rs`, `migrations/0005_runs.sql`, `0007`, `0008`. Below, "Incisive `x.rs` `SYMBOL`" means that file and symbol, checked by the Architect on 10 Nov 2026 (Fact).

**Sync, not async:** every tool is a plain synchronous function on `httpx.Client`, because F0's routes, pool and identity code are all synchronous (`web.py`, `db.py`: Fact) and one style keeps F2 simple.

## Global Constraints

- Everything in F0's Global Constraints still holds.
- Search text, titles, abstracts, upstream URLs and the NCBI key are never logged. Log lines hold counts and fixed kinds only. `httpx` and `httpcore` log every request URL at INFO (Fact: `httpx/_client.py`, `logger.info('HTTP Request: %s %s ...', request.url)`), so both loggers are silenced exactly as F0 silences `psycopg`.
- Every httpx exception is turned into `LitError(kind)` with `raise ... from None`, so no traceback or message ever carries a URL.
- Every NCBI request carries `db=pubmed` and `tool=pubmedgemini`, plus `email` and `api_key` only when set (Incisive `pubmed.rs` `PubMed::url`). Parameters always go through httpx `params=` encoding; no URL is built by string joining.
- One NCBI pacer for the whole process: at least 0.334 s between NCBI attempts without a key, 0.1 s with one (Incisive `pubmed.rs` `default_min_interval`). The image runs one Uvicorn worker (Fact: `Dockerfile` CMD has no `--workers`), so one pacer on `app.state` is process-wide. Europe PMC is not paced (Incisive paces NCBI only: `pubmed.rs` `paced`).
- PMIDs are 1 to 9 ASCII digits before any use (Incisive `agent/tools/lit.rs` `valid_pmid`).
- Jinja autoescape is on for `.html` templates (Fact: Starlette `Jinja2Templates` uses `jinja2.select_autoescape()`, whose default enables `html`). No template uses `|safe`.
- The search page never reads search text from a URL. It is sent only as a POST body, because the gate and Traefik may log request lines with their query strings (oauth2-proxy request logging and Traefik's access log: Not checked on the VPS). F0's own request line logs the route template only (Fact: `web.py` `log_request` logs `request.scope["route"].path`), and Uvicorn's access log is off (Fact: `Dockerfile` `--no-access-log`).
- No Gemini code, no `runs`, `run_papers`, `answers` or `model_calls` tables.
- Work on branch `f1-search-tools`. Merging to `main` deploys. Do not merge without the owner's explicit "merge it".

## Review Focus

1. **Search text outside the query rule** (`hernia?`, `a ; b`, `café`, a URL, a newline, 1,001 characters): refused with a fixed kind before any request is sent. Tests: Task 3 `test_incisive_cases_carry_over`, Task 8 `test_rejected_query_is_400_and_sends_nothing`. Note that Python's `str.isalnum()` accepts `é`; the rule must use `c.isascii() and c.isalnum()`.
2. **Text that must never reach a log line:** the search text, the NCBI key (which httpx would print inside the URL), titles. Tests: Task 1 `test_httpx_loggers_are_silenced`, Task 2 `test_error_text_holds_no_url_or_params`, Task 8 `test_search_text_and_key_never_logged`.
3. **Markup in upstream text** (a `<script>` title from esummary, `<i>` inside Europe PMC titles, `p < 0.05` in an abstract): escaped on the page, never executed, stray `<` kept. Tests: Task 5 `test_strip_markup_keeps_stray_less_than`, Task 8 `test_script_title_is_escaped`.
4. **One upstream down, slow or misbehaving** (5xx, timeout, 429, a redirect to another host, an 8 MB+ body): retried or refused per Incisive's rules, and the page shows the other source with the §8 line. Tests: Task 2 `test_5xx_is_retried_twice_then_fails`, `test_429_is_not_retried`, `test_redirect_to_other_host_is_refused`; Task 8 `test_europepmc_down_shows_pubmed_only`, `test_both_down_is_503_plain`.
5. **Hostile or messy ids:** PMIDs with letters or 10 digits, duplicates, a related-articles list holding the paper itself, numeric ids in elink JSON, Europe PMC records with no PMID, the same paper in both sources with a DOI in different case, two different PMIDs sharing a DOI. Tests: Task 4 `test_elink_skips_self_duplicates_and_non_digits_and_caps`, Task 5 `test_merge_by_pmid_then_doi`, Task 7 `test_bad_pmids_rejected_before_any_request`.

## Numbers copied from Incisive (all Fact, checked in the files named)

| Rule | Value | Incisive file and symbol |
|---|---|---|
| Search tools, most results | 50 | `agent/tools/lit.rs` `MAX_SEARCH_RESULTS` |
| `get_abstracts`, most PMIDs a call | 20 | `agent/tools/lit.rs` `MAX_ABSTRACTS` |
| `find_related`, most neighbours | 30 | `agent/tools/lit.rs` `MAX_RELATED` |
| Abstract length in a tool result | no cut in the tool; F2's loop cuts a whole result at 30,000 characters | `agent/tools/lit.rs` `AbstractRecord` (no cap); `agent/agent_loop.rs` `RESULT_CHARS` |
| Request timeout | 20 s | `lit/http.rs` `REQUEST_TIMEOUT` |
| Retries | 2 after the first attempt, on 5xx, timeout or network error only (not 429, not 4xx); attempt n waits n × 0.5 s | `lit/http.rs` `MAX_RETRIES`, `is_transient_err`, `with_retry_backoff` default 500 ms |
| Redirects | at most 3, each hop on an allowed host | `lit/http.rs` `MAX_REDIRECTS`, `follow` |
| Body cap | 8 MiB | `lit/http.rs` `DEFAULT_MAX_BODY` |
| NCBI pacing | 334 ms (no key), 100 ms (key) | `lit/pubmed.rs` `default_min_interval` |
| esearch `retmax` clamp | 1 to 1,000 | `lit/pubmed.rs` `MAX_HITS` |
| esummary / efetch batch | 100 / 50 ids | `lit/pubmed.rs` `SUMMARY_BATCH`, `FETCH_BATCH` |
| Europe PMC `pageSize` clamp | 1 to 100 | `lit/europepmc.rs` `MAX_PAGE` |
| Query length | 1,000 characters | `agent/search.rs` `MAX_QUERY` |
| Cache served for / pruned after | 30 / 90 days | `lit/cache.rs` `FRESH_DAYS`, `PRUNE_DAYS` |

Incisive's `pubmed.rs` `Pacer` comment says NCBI 429s "are retried with backoff", but `http.rs` `is_transient_err` does not retry 429. F1 follows the code (no 429 retry) (Fact).

## Differences from Incisive, and from the probe

| Point | Incisive | pubmedgemini F1 | Why |
|---|---|---|---|
| Underscore in queries | refused (`agent/search.rs` `validate_query`, allowed set at lines 95 to 101) | **allowed in Europe PMC queries only** | Owner decision 6 (10 Nov 2026, relayed by the coordinator): Incisive's own Europe PMC tool text suggests `PUB_TYPE:` and `PUB_YEAR:`, which its rule refuses. PubMed field tags use brackets (`[pt]`, `[dp]`), not underscores, so PubMed keeps Incisive's exact rule. Incisive is not changed |
| Echo guard (8 words of the question) | in `LitTools::check_query` | not in F1 | It needs the run's question. In F1 the typed text *is* the search, by design. F2 adds it in the researcher using Incisive's `agent/mod.rs` `contains_question` (`ECHO_WORDS` = 8) |
| Full text, open-access links, Unpaywall | yes | none | Spec §12 (abstracts only). So no `oa_url`, `oa_is_pdf` columns and no `get_full_text` |
| `authors` column | JSON text | `TEXT[]` | Plain Postgres array; same content |
| `design` | worked out at screening, not stored | stored in `papers`, by Incisive's `agent/screen.rs` `EvidenceLevel::of` rule | Spec §7 lists `design`. Incisive's RCT / SR_MA / other (`agent/researcher.rs` `Design`) is the model's choice in the study list, not a code rule; F2 keeps it so. The stored value is advisory: it is worked out from the types the cache was given (PubMed's), and the page recomputes the label from the merged types on display; F1 never reads the column (review finding J; comment in `migrations/0002_papers.sql`) |
| User agent | `Incisive/<ver> (+repo URL)` | `pubmedgemini/0.1.0` | Sends no account name upstream |
| Pacer scope | per Cloud Run instance | per process (one worker) | One VPS |
| Timeout | whole request (reqwest) | a monotonic 20 s total per attempt, checked when the reply's head is in and after every body chunk; httpx's 20 s also bounds each connect and each read | httpx has no whole-request timeout. Review fix A. A single read that stalls just before the deadline can still wait out httpx's 20 s read timeout, so one attempt is bounded by about 40 s, not 20 (Estimate) |
| Cache prune | daily loop (`main.rs` `retention_loop`) | once at each start-up | The app restarts on every deploy; one fewer moving part |
| Europe PMC field of the wrong JSON type (a number where a string belongs) | the whole search fails (serde: `lit/europepmc.rs` `Raw` fields are `Option<String>`) | that field is treated as missing; the record is kept | Coordinator ruling (11 Nov 2026): kept, because it can only show more results. Recorded here after review finding I |
| NCBI `email` parameter | `tool` always, `email` only when configured (`pubmed.rs` module comment and `PubMed::url`) | the same: `tool=pubmedgemini` always, `email` only when `NCBI_EMAIL` is set | Not a difference from Incisive but from spec §4, which said every request carries `email`; the setting is optional (Task 1), so §4 is corrected to match |
| `search_pubmed_papers`, `search_europepmc_papers`, `cached_abstracts` | none | page-only methods on `LitTools` | They return whole papers (DOIs to merge on, Europe PMC's abstracts) or read the cache only. **F2 must not offer them to the model as tools**; the model gets the four Incisive tools only |
| An `api_key` in a redirect's Location to a host other than NCBI's | followed as written | dropped before the hop is sent | The NCBI key only ever goes to NCBI. Request parameters are never carried to a redirect target (as in Incisive: `Url::join`) |
| Abstracts on the page when PubMed's search could not be reached | no such page | the cache and Europe PMC's abstracts only; NCBI is not asked again | Review fix B: a second round of retries only made the page wait |
| `strip_markup` after a stray `<` | the text up to the next `>` is kept as written | the same, ported as it is | Coordinator ruling (10 Nov 2026): kept for F1, **to revisit**. An abstract can therefore hold a literal tag such as `<h4>`; the page shows it escaped as text (Jinja autoescape; test `test_nothing_from_upstream_is_rendered_as_html`) |

The probe (`probes/flash_lite_probe.py` `LitTools`) differs from Incisive in these ways, and F1 follows Incisive in each: Europe PMC `resultType=lite` (Incisive: `core`); journal from esummary `source` (Incisive: `fulljournalname`, then `source`); one retry that includes 429 with a 1 s wait; abstracts cut at 5,000 characters; `get_abstracts` by efetch only, with no esummary and no cache; elink without `cmd=neighbor_score`; Europe PMC paced at 0.2 s; abstract sections joined with one newline (Incisive: a blank line). The probe's request URLs and field names were otherwise confirmed live on 10 Nov (Fact, as reported by the coordinator; not re-run by the Architect).

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml`, `requirements.lock` | `httpx`, `python-multipart` join the runtime dependencies |
| `src/pubmedgemini/config.py` | Optional `NCBI_API_KEY`, `NCBI_EMAIL` |
| `src/pubmedgemini/logging_setup.py` | `httpx`, `httpcore` silenced |
| `src/pubmedgemini/lit/__init__.py` | Empty |
| `src/pubmedgemini/lit/http.py` | `LitError`, `NcbiPacer`, `LitClient` |
| `src/pubmedgemini/lit/query.py` | The query rule (one function) |
| `src/pubmedgemini/lit/paper.py` | `Paper`, `paper_key`, `merge_results` |
| `src/pubmedgemini/lit/design.py` | Design label from publication types and title |
| `src/pubmedgemini/lit/pubmed.py` | esearch, esummary, efetch, elink and their parsers |
| `src/pubmedgemini/lit/europepmc.py` | Europe PMC search, `to_paper`, `strip_markup` |
| `src/pubmedgemini/lit/cache.py` | `papers` read, upsert, prune |
| `src/pubmedgemini/lit/tools.py` | `LitTools`: the four tools and their typed results |
| `src/pubmedgemini/search.py` | The page's search: both sources, merge, abstracts |
| `src/pubmedgemini/web.py`, `main.py` | `POST /search`; wiring the clients at start-up |
| `src/pubmedgemini/templates/{home,results}.html` | The box; the results |
| `migrations/0002_papers.sql` | `papers` table |
| `tests/lit_fakes.py` | `httpx.MockTransport` that replays fixtures and records requests |
| `tests/fixtures/lit/*` | Trimmed real replies (list in Task 4) |
| `tests/test_lit_*.py`, `tests/test_search_page.py` | One test file per module |
| `compose.yaml`, `.env.example`, `tests/test_compose.py`, `infra/README.md` | Optional NCBI settings |

---

### Task 1: Dependencies, settings and quiet loggers

**Files:**
- Modify: `pyproject.toml`, `requirements.lock`, `src/pubmedgemini/config.py`, `src/pubmedgemini/logging_setup.py`
- Test: `tests/test_config.py`, `tests/test_logging.py` (existing `tests/test_lock.py` must pass unchanged)

**Interfaces:**
- Produces: `Settings.ncbi_api_key: SecretStr | None = None` (env `NCBI_API_KEY`), `Settings.ncbi_email: str | None = None` (env `NCBI_EMAIL`); a `mode="before"` validator turns an empty or all-space value into `None`. `QUIET_LOGGERS = ("psycopg", "psycopg.pool", "httpx", "httpcore")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_ncbi_settings_are_optional(monkeypatch):            # base env as in the F0 tests
    monkeypatch.delenv("NCBI_API_KEY", raising=False); monkeypatch.delenv("NCBI_EMAIL", raising=False)
    s = load_settings()
    assert (s.ncbi_api_key, s.ncbi_email) == (None, None)

def test_blank_ncbi_settings_are_none(monkeypatch):           # what compose passes when unset
    monkeypatch.setenv("NCBI_API_KEY", ""); monkeypatch.setenv("NCBI_EMAIL", "  ")
    s = load_settings()
    assert (s.ncbi_api_key, s.ncbi_email) == (None, None)

def test_ncbi_key_is_never_shown(monkeypatch):
    monkeypatch.setenv("NCBI_API_KEY", "k-q7-secret")
    s = load_settings()
    assert "k-q7-secret" not in repr(s) and s.ncbi_api_key.get_secret_value() == "k-q7-secret"

def test_httpx_loggers_are_silenced(settings_prod):            # Review Focus 2
    create_app(settings_prod)
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() > logging.CRITICAL
```

- [ ] **Step 2: Run** `pytest tests/test_config.py tests/test_logging.py -v`. Expected: FAIL (no `ncbi_api_key`; httpx logger at WARNING).
- [ ] **Step 3: Implement.** Move `httpx` from `dev` to `dependencies` and add `python-multipart`. Regenerate the lock with the command in its own header: `uv pip compile pyproject.toml --universal --python-version 3.12 --generate-hashes --no-header -o requirements.lock`, then put its four comment lines back at the top (Fact: `requirements.lock` lines 1 to 4). Check the diff adds `httpx`, `httpcore`, `certifi`, `python-multipart` and changes no existing pin except where uv requires it.
- [ ] **Step 4: Run** `ruff check . && pytest tests/test_config.py tests/test_logging.py tests/test_lock.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: optional NCBI settings, httpx and quiet request loggers`.

### Task 2: Outbound HTTP client and the NCBI pacer

**Files:**
- Create: `src/pubmedgemini/lit/__init__.py`, `src/pubmedgemini/lit/http.py`, `tests/lit_fakes.py`
- Test: `tests/test_lit_http.py`

**Interfaces:**
- Produces:
  - `BASE_HOSTS = ("eutils.ncbi.nlm.nih.gov", "www.ebi.ac.uk")` (Incisive `lit/http.rs` `BASE_HOSTS`, without the Unpaywall and Anthropic hosts F1 does not use). `NCBI_HOST = BASE_HOSTS[0]`.
  - `REQUEST_TIMEOUT_SECONDS = 20.0`, `MAX_RETRIES = 2`, `RETRY_BACKOFF_SECONDS = 0.5`, `MAX_REDIRECTS = 3`, `MAX_BODY_BYTES = 8 * 1024 * 1024`, `USER_AGENT = "pubmedgemini/0.1.0"`.
  - `class LitError(Exception)`: `kind: Literal["disallowed", "too_large", "too_many_redirects", "http", "timeout", "parse", "network"]` (Incisive `lit/http.rs` `LitError`, names from `agent/search.rs` `lit_kind`), `status: int | None`. `str()` is `kind` or `http 503`; never a URL.
  - `ncbi_min_interval(has_key: bool) -> float` (0.334 or 0.1).
  - `class NcbiPacer(gap_seconds: float, clock=time.monotonic, sleep=time.sleep)` with `wait() -> None`; a `threading.Lock` makes callers share one schedule (Incisive `pubmed.rs` `Pacer::wait`).
  - `class LitClient(transport: httpx.BaseTransport | None = None, pacer: NcbiPacer | None = None, sleep=time.sleep)` with `get_json(url: str, params: Mapping[str, str]) -> Any`, `get_text(url: str, params: Mapping[str, str]) -> str`, `close() -> None`.
- `tests/lit_fakes.py` produces `FakeUpstream(routes: dict[tuple[str, str], Callable | httpx.Response])`: an `httpx.MockTransport` keyed by (host, path) that records every request in `.sent`, plus `fixture(name) -> bytes` reading `tests/fixtures/lit/<name>`.

Rules: the httpx client has `follow_redirects=True`, `max_redirects=3`, the user agent and the timeout. A `request` event hook runs on every hop, redirects included (Fact: httpx `_send_handling_redirects` calls the request hooks inside its loop): it raises `LitError("disallowed")` unless scheme is `https`, host is in `BASE_HOSTS`, the port is default and there is no userinfo; for the NCBI host it then calls `pacer.wait()`. The retry loop around `send` repeats the hook, so retries are paced too. The body is streamed and refused past the cap.

- [ ] **Step 1: Write the failing tests**

```python
def test_other_host_is_refused_before_sending():                       # Review Focus 4
    up = FakeUpstream({}); c = LitClient(up.transport)
    with pytest.raises(LitError) as e: c.get_json("https://evil.example/x", {})
    assert e.value.kind == "disallowed" and up.sent == []

def test_plain_http_is_refused(): ...         # http://eutils.ncbi.nlm.nih.gov/... -> disallowed, nothing sent

def test_redirect_to_other_host_is_refused():
    up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(302, headers={"location": "https://evil.example/"})})
    with pytest.raises(LitError) as e: LitClient(up.transport).get_json(f"https://{NCBI_HOST}/x", {})
    assert e.value.kind == "disallowed" and len(up.sent) == 1

def test_5xx_is_retried_twice_then_fails():
    sleeps = []; up = FakeUpstream({(NCBI_HOST, "/x"): httpx.Response(503)})
    c = LitClient(up.transport, sleep=sleeps.append)
    with pytest.raises(LitError) as e: c.get_json(f"https://{NCBI_HOST}/x", {})
    assert (e.value.kind, e.value.status, len(up.sent), sleeps) == ("http", 503, 3, [0.5, 1.0])

def test_429_is_not_retried(): ...            # kind http, status 429, len(sent) == 1
def test_timeout_is_retried_then_timeout(): ...  # handler raises httpx.ReadTimeout: kind timeout, 3 attempts
def test_body_over_cap_is_too_large(): ...    # 8 MiB + 1 byte -> too_large

def test_error_text_holds_no_url_or_params():                          # Review Focus 2
    ...  # a 503 on get_json(url, {"term": "zq7", "api_key": "k-q7"})
    assert "zq7" not in str(e.value) + repr(e.value) and "k-q7" not in str(e.value) + repr(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__

def test_pacer_spaces_ncbi_attempts_only():
    clock = FakeClock(); pacer = NcbiPacer(0.334, clock=clock.now, sleep=clock.sleep)
    ...  # three NCBI calls then one Europe PMC call
    assert clock.slept == pytest.approx([0.334, 0.334])

def test_ncbi_min_interval():
    assert (ncbi_min_interval(False), ncbi_min_interval(True)) == (0.334, 0.1)
```

- [ ] **Step 2: Run** `pytest tests/test_lit_http.py -v`. Expected: FAIL, module missing.
- [ ] **Step 3: Implement** `lit/http.py` and `tests/lit_fakes.py` (with a small `FakeClock`).
- [ ] **Step 4: Run** `ruff check . && pytest tests/test_lit_http.py -v`. Expected: PASS.
- [ ] **Step 5: Commit** `feat: allowlisted literature HTTP client and NCBI pacer`.

### Task 3: The query rule

**Files:**
- Create: `src/pubmedgemini/lit/query.py`
- Test: `tests/test_lit_query.py`

**Interfaces:**
- Produces:
  - `MAX_QUERY_CHARS = 1000`.
  - `QUERY_PUNCTUATION = " -\"()[]*/:,.'"`: Incisive's allowed set, copied exactly (`agent/search.rs` `validate_query`, lines 95 to 101).
  - `EUROPEPMC_EXTRA_PUNCTUATION = "_"`: owner decision 6, a deliberate difference from Incisive. A comment above it says so and names the Incisive lines.
  - `QuerySource = Literal["pubmed", "europepmc"]`.
  - `validate_query(q: str, source: QuerySource) -> str | None`: `None` if accepted, else one fixed kind, checked in Incisive's order: `empty`, `too_long`, `newline`, `url` (`http` or `www.`, any case), `character` (outside ASCII letters and digits plus `QUERY_PUNCTUATION`, plus `EUROPEPMC_EXTRA_PUNCTUATION` when `source == "europepmc"`), `unbalanced` (double quotes, parentheses, square brackets; brackets inside quotes do not count).
  - `query_rejected_message(kind: str, source: QuerySource) -> str`: Incisive's text (`agent/tools/lit.rs` `check_query`), with `_` added to the list for Europe PMC: `query rejected (<kind>): use 1 to 1000 characters of letters, digits, spaces and - " ( ) [ ] * / : , . '` [`, _` for europepmc] ` with balanced quotes and brackets, no URLs or newlines`.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("source", ["pubmed", "europepmc"])
def test_incisive_cases_carry_over(source):                             # Review Focus 1
    # every case of Incisive tests/agent.rs query_validation_rejects_urls_newlines_and_overlong
    for ok in ['("indocyanine green"[tiab] OR ICG[tiab]) AND "anastomotic leak"[mh]',
               "hernia*[tiab] AND mesh[tiab] AND 2015:2025[dp]", ...]:
        assert validate_query(ok, source) is None
    assert validate_query("a" * 1000, source) is None and validate_query("a" * 1001, source) == "too_long"
    for bad in ["", "   ", "ICG http://evil.example", "see www.example.com", "ICG\nAND leak", "ICG\tleak",
                "ICG; DROP", "ICG <source>", "ICG & leak", "ICG #1", "ICG leak—", "café",
                '"unclosed quote', "(ICG AND leak", "ICG[tiab", "(ICG[tiab)]", '"a(" AND b)', "hernia?"]:
        assert validate_query(bad, source) is not None, bad
    assert validate_query('"a (b" AND c', source) is None
    assert [validate_query(q, source) for q in ["a http b", "a\nb", "a ; b", "(a", ""]] == \
        ["url", "newline", "character", "unbalanced", "empty"]

def test_underscore_is_allowed_for_europepmc_only():                     # decision 6
    q = 'PUB_TYPE:"randomized controlled trial" AND PUB_YEAR:[2015 TO 2026]'
    assert validate_query(q, "europepmc") is None
    assert validate_query(q, "pubmed") == "character"

def test_message_names_the_underscore_for_europepmc_only():
    assert ", _ " in query_rejected_message("character", "europepmc")
    assert "_" not in query_rejected_message("character", "pubmed")
```

- [ ] **Step 2: Run** `pytest tests/test_lit_query.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `lit/query.py`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: query rule from Incisive, with the underscore for Europe PMC (decision 6)`.

### Task 4: Paper model and the PubMed client

**Files:**
- Create: `src/pubmedgemini/lit/paper.py` (the `Paper` dataclass and `paper_key` only; `merge_results` comes in Task 5), `src/pubmedgemini/lit/pubmed.py`, fixtures below
- Test: `tests/test_lit_pubmed.py`

**Interfaces:**
- Produces:
  - `@dataclass class Paper: pmid: str | None; pmcid: str | None; doi: str | None; title: str; authors: list[str]; journal: str | None; year: int | None; pub_types: list[str]; abstract: str | None; retracted: bool; source: Literal["pubmed", "europepmc", "both"]` (Incisive `lit/mod.rs` `Paper`, without `oa_url`, `oa_is_pdf`). `paper_key(p: Paper) -> str`: PMID, else lower-case DOI, else `title:<lower-case title>` (Incisive `lit/mod.rs` `PaperId::key`).
  - `@dataclass(frozen=True) class SearchHits: count: int; pmids: tuple[str, ...]`; `@dataclass(frozen=True) class FetchedRecord: pmid: str; abstract: str | None; pub_types: tuple[str, ...]; retracted: bool`.
  - `class PubMed(client: LitClient, api_key: str | None, email: str | None)`:
    - `search(query: str, max_hits: int) -> SearchHits`: GET `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi`, params `db=pubmed, term, retmax` (clamped 1 to 1,000), `sort=relevance, retmode=json, tool[, email][, api_key]`.
    - `summaries(pmids: Sequence[str]) -> list[Paper]`: `esummary.fcgi`, `id` comma-joined in batches of 100, `retmode=json`.
    - `details(pmids: Sequence[str]) -> dict[str, FetchedRecord]`: `efetch.fcgi`, batches of 50, `rettype=abstract, retmode=xml`.
    - `related(pmid: str, max_ids: int) -> list[str]`: `elink.fcgi`, `dbfrom=pubmed, id, cmd=neighbor_score, linkname=pubmed_pubmed, retmode=json` (plus `db=pubmed` like every call).
  - Parsers, each a port of the Incisive function of the same name in `lit/pubmed.rs`: `parse_esearch(body) -> SearchHits` (an `ERROR` key or no `count` is `LitError("parse")`); `parse_esummary(body, requested) -> list[Paper]` (skip entries with `error` or no title; journal `fulljournalname` then `source`; year = leading 4 digits of `pubdate`, else `epubdate`; DOI and PMCID from `articleids`; retracted when a pub type is "Retracted Publication"); `parse_efetch(xml: str) -> list[FetchedRecord]` (PMID from `MedlineCitation/PMID` or `BookDocument/PMID`; `AbstractText` sections with their inner element text, ASCII whitespace collapsed, `LABEL: text`, joined by a blank line; retracted by pub type or a `CommentsCorrections RefType="RetractionIn"`); `parse_elink(body, pmid, max_ids) -> list[str]` (`ERROR` is parse; first linkset; `pubmed_pubmed` db or the first; links as `{"id", "score"}` objects or bare ids, strings or numbers; digits only; not `pmid`; no repeats; at most `max_ids`).
  - efetch entities: before parsing, every `&name;` that is not one of the five XML entities or a numeric reference becomes `&amp;name;` (so it stays visible as Incisive keeps it), except `&nbsp;`, which becomes a space (Incisive `parse_efetch`). ElementTree raises on undefined entities otherwise (Fact: `XMLParser._default`).

Fixtures in `tests/fixtures/lit/`, recorded once by hand with `curl` from a dev machine (no key, `tool=pubmedgemini`, search term `inguinal hernia mesh`), then trimmed to three records keeping every field the parser reads, and one title edited to `<script>alert(1)</script> Mesh repair` for Task 8: `esearch.json`, `esummary.json` (three ids plus one unknown id with `error`), `efetch.xml` (one structured abstract with an `<i>` and a `&thinsp;`; one record with a `RetractionIn` comment), `elink.json`, `esearch_error.json`. The exact `curl` lines go in `tests/fixtures/lit/README.md`. Fixtures hold public bibliographic data only.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_request_carries_tool_and_no_key_when_unset():
    up = fake_pubmed(); PubMed(LitClient(up.transport), None, None).search("hernia", 5)
    p = up.sent[0].url.params
    assert (p["db"], p["tool"], "api_key" in p, "email" in p) == ("pubmed", "pubmedgemini", False, False)

def test_key_and_email_sent_when_set():
    ...  # PubMed(..., "test-key", "dev@example.org") -> params api_key, email present

def test_text_is_parameter_encoded():
    PubMed(...).search('hernia[tiab] AND "mesh"&retmax=999', 5)
    p = up.sent[0].url.params
    assert p["term"] == 'hernia[tiab] AND "mesh"&retmax=999' and p.get_list("retmax") == ["5"]

def test_esearch_fixture():        assert hits.pmids == ("<id1>", "<id2>", "<id3>") and hits.count == <count>
def test_esearch_error_is_parse(): ...   # LitError kind "parse"
def test_esummary_fixture():       ...   # journal from fulljournalname, year from "2024 Mar", doi, pmcid; unknown id absent
def test_efetch_structured_abstract():
    rec = {r.pmid: r for r in parse_efetch(fixture("efetch.xml").decode())}["<id1>"]
    assert rec.abstract.startswith("BACKGROUND: ") and "\n\nMETHODS: " in rec.abstract
def test_efetch_retraction_comment_marks_retracted(): ...
def test_efetch_unknown_entity_kept_visible(): ...   # "&thinsp;" in text; "&nbsp;" -> " "
def test_efetch_malformed_is_parse(): ...            # "<PubmedArticleSet><PubmedArticle>" -> parse

def test_elink_skips_self_duplicates_and_non_digits_and_caps():          # Review Focus 5
    body = {"linksets": [{"linksetdbs": [{"linkname": "pubmed_pubmed",
            "links": [{"id": "123", "score": 9}, {"id": "9"}, 9, {"id": "x1"}, "77"]}]}]}
    assert parse_elink(body, "123", 30) == ["9", "77"]
    assert parse_elink(body, "123", 1) == ["9"]

def test_summaries_go_in_batches_of_100(): ...       # 150 ids -> 2 esummary requests
```

- [ ] **Step 2: Run** `pytest tests/test_lit_pubmed.py -v`. Expected: FAIL.
- [ ] **Step 3: Record and trim the fixtures; implement** `lit/paper.py` and `lit/pubmed.py`.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: PubMed client (esearch, esummary, efetch, elink)`.

### Task 5: Europe PMC client, merging and the design label

**Files:**
- Create: `src/pubmedgemini/lit/europepmc.py`, `src/pubmedgemini/lit/design.py`, fixture `tests/fixtures/lit/europepmc.json`
- Modify: `src/pubmedgemini/lit/paper.py` (add `merge_results`)
- Test: `tests/test_lit_europepmc.py`, `tests/test_lit_design.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class EpmcHits: count: int; papers: tuple[Paper, ...]`. `class EuropePmc(client: LitClient)` with `search(query: str, max_hits: int) -> EpmcHits`: GET `https://www.ebi.ac.uk/europepmc/webservices/rest/search`, params `query, resultType=core, format=json, pageSize` (clamped 1 to 100); `count` = `hitCount`, else the page length (Incisive `lit/europepmc.rs` `EuropePmc::search`).
  - `to_paper(raw: dict) -> Paper` (Incisive `to_paper`): PMID from `pmid`, else `id` when `source == "MED"`; PMCID only if `PMC` + digits; title through `strip_markup`, trailing `.` removed; journal `journalInfo.journal.title`; year `pubYear`, else `journalInfo.yearOfPublication`, else the first 4 characters of `firstPublicationDate`; pub types `pubTypeList.pubType`; abstract `abstractText` through `strip_markup`; retracted by pub type.
  - `strip_markup(s: str) -> str` (Incisive `strip_markup`): inline tags (`i b u em strong sub sup italic bold span a`) vanish, other tags become a space, a `<` not followed by a letter is kept, the named and numeric entities Incisive decodes are decoded, whitespace collapsed.
  - `merge_results(pubmed: list[Paper], epmc: list[Paper]) -> list[Paper]` (Incisive `merge_results`, `merge_into`, `Index::find`): same paper when the PMID matches, else the lower-case DOI, else the PMCID, never when both have different PMIDs; PubMed's values win, gaps are filled from Europe PMC, pub types are united, `retracted` is ORed, `source` becomes `"both"`; PubMed's order, then Europe-PMC-only records in their order.
  - `DESIGN_LABELS = ("guideline", "systematic review", "meta-analysis", "RCT", "prospective cohort", "other")`; `design_label(title: str, pub_types: Sequence[str]) -> str`: a port of Incisive `agent/screen.rs` `EvidenceLevel::of` (Ruling R-10c): non-research types (`NON_RESEARCH_TYPES`) force `other`; then guideline types, `Systematic Review`, `Meta-Analysis`, `Randomized Controlled Trial`, `Observational Study` (prospective cohort if the title has "prospective", else other); only then the title patterns `TITLE_GUIDELINE`, `TITLE_SR`, `TITLE_MA`, `title_says_rct`, `TITLE_PROSPECTIVE`; else `other`. Type names compared ignoring case after trimming.

- [ ] **Step 1: Write the failing tests**

```python
def test_europepmc_filter_query_passes_and_is_sent_encoded():            # decision 6
    q = 'PUB_TYPE:"randomized controlled trial" AND PUB_YEAR:[2015 TO 2026]'
    assert validate_query(q, "europepmc") is None
    up = fake_europepmc(); EuropePmc(LitClient(up.transport)).search(q, 5)
    r = up.sent[0]
    assert (r.url.host, r.url.path) == ("www.ebi.ac.uk", "/europepmc/webservices/rest/search")
    assert r.url.params["query"] == q
    assert (r.url.params["resultType"], r.url.params["format"], r.url.params["pageSize"]) == ("core", "json", "5")
    raw = r.url.query.decode()
    assert all(ch not in raw for ch in ' "[]') and "PUB_TYPE" in raw

def test_core_fixture_to_paper(): ...   # MED record with only `id` gets a PMID; preprint has DOI, no PMID; <i> gone, trailing "." gone

def test_strip_markup_keeps_stray_less_than():                            # Review Focus 3
    assert strip_markup("mortality p < 0.05") == "mortality p < 0.05"
    assert strip_markup("<i>H. pylori</i><h4>Methods</h4>x &amp; y") == "H. pylori Methods x & y"
    # Incisive quirk, kept: after a stray "<" the text up to the next ">" is kept as written
    assert strip_markup("p < 0.05 <i>x</i>") == "p < 0.05 <i>x"

def test_merge_by_pmid_then_doi():                                        # Review Focus 5
    a = paper(pmid="1", doi="10.1/X"); a2 = paper(pmid="1", source="europepmc", journal="J")
    b = paper(pmid=None, doi="10.1/x", source="europepmc"); c = paper(pmid="2", doi="10.1/X", source="europepmc")
    out = merge_results([a], [a2, b, c])
    assert [p.pmid for p in out] == ["1", "2"] and out[0].source == "both" and out[0].journal == "J"

# tests/test_lit_design.py: every assertion of Incisive tests/agent.rs evidence_level_from_pub_types_and_title,
# with EvidenceLevel names mapped to DESIGN_LABELS, e.g.
def test_design_label_cases():
    assert design_label("x", ["Randomized Controlled Trial"]) == "RCT"
    assert design_label("Randomized controlled trial of ICG", ["Letter", "Randomized Controlled Trial"]) == "other"
    assert design_label("x", ["Meta-Analysis", "Review"]) == "meta-analysis"
    assert design_label("A non-randomized comparison", []) == "other"
    assert design_label("A prospective study of ICG", ["Observational Study"]) == "prospective cohort"
    assert design_label("Guidelines for ICG use", []) == "guideline"
```

- [ ] **Step 2: Run** `pytest tests/test_lit_europepmc.py tests/test_lit_design.py -v`. Expected: FAIL.
- [ ] **Step 3: Record and trim** `europepmc.json` (core, same search term, four hits: one sharing a PMID with `esummary.json`, one MED record with `id` only, one preprint with a DOI and no PMID, one with `<i>` in its title); **implement**.
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: Europe PMC client, merge by PMID then DOI, design label`.

### Task 6: Paper cache

**Files:**
- Create: `migrations/0002_papers.sql`, `src/pubmedgemini/lit/cache.py`
- Modify: `src/pubmedgemini/main.py` (prune once in the start-up work, after `ensure_owner`)
- Test: `tests/test_lit_cache.py`, `tests/test_migrate.py` (`test_real_migrations_create_papers`)

`migrations/0002_papers.sql` (Incisive `migrations/0005_runs.sql` `paper_cache` plus `0007` `pmcid`, without `oa_url` and `oa_is_pdf`, plus `design`):

```sql
-- Public bibliographic data only; no user linkage.
CREATE TABLE papers (
    pmid       TEXT PRIMARY KEY CHECK (pmid ~ '^[0-9]{1,9}$'),
    pmcid      TEXT,
    doi        TEXT,
    title      TEXT NOT NULL,
    authors    TEXT[] NOT NULL DEFAULT '{}',
    journal    TEXT,
    year       INT,
    pub_types  TEXT[] NOT NULL DEFAULT '{}',
    design     TEXT NOT NULL CHECK (design IN ('guideline', 'systematic review', 'meta-analysis',
                                               'RCT', 'prospective cohort', 'other')),
    abstract   TEXT,
    retracted  BOOLEAN NOT NULL DEFAULT FALSE,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX papers_fetched_at ON papers (fetched_at);
```

**Interfaces:**
- Produces: `FRESH_DAYS = 30`, `PRUNE_DAYS = 90`; `get_many(pool, pmids: Sequence[str]) -> list[Paper]` (rows fetched within 30 days; `source="pubmed"`, as Incisive); `put_many(pool, papers: Sequence[Paper]) -> None` (one transaction; papers without a PMID skipped; on conflict `doi`, `pmcid`, `abstract` keep the stored value when the new one is null, `retracted` is sticky, the rest is replaced, `design` is recomputed with `design_label`, `fetched_at = now()`: Incisive `lit/cache.rs` `put_many`); `prune(pool) -> int` (deletes rows older than 90 days). Start-up logs `paper_cache pruned=<n>`.

- [ ] **Step 1: Write the failing tests** (each first runs `apply_migrations(pool, Path("migrations"))`)

```python
def test_put_then_get_round_trip(pool): ...            # every column back as written; authors and pub_types as lists
def test_stale_rows_are_not_served(pool):
    put_many(pool, [p]); pool_exec(pool, "UPDATE papers SET fetched_at = now() - interval '31 days'")
    assert get_many(pool, [p.pmid]) == []
def test_refresh_never_erases(pool): ...               # second put with abstract=None, doi=None, retracted=False keeps all three
def test_paper_without_pmid_is_skipped(pool): ...
def test_design_is_stored_from_the_rule(pool): ...     # pub_types ["Randomized Controlled Trial"] -> design 'RCT'
def test_prune_deletes_after_90_days(pool): ...        # 91 days old gone, 89 days old kept; returns 1
def test_pmid_must_be_digits(pool): ...                # raw INSERT of 'abc' raises psycopg.errors.CheckViolation
```

- [ ] **Step 2: Run** `pytest tests/test_lit_cache.py tests/test_migrate.py -v` with the local Postgres 17 from F0 Task 2. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: paper cache (papers table, 30-day freshness, 90-day prune)`.

### Task 7: The four tools

**Files:**
- Create: `src/pubmedgemini/lit/tools.py`
- Test: `tests/test_lit_tools.py`

**Interfaces:**
- Consumes: Tasks 2 to 6.
- Produces:
  - `MAX_SEARCH_RESULTS = 50`, `MAX_ABSTRACTS = 20`, `MAX_RELATED = 30`.
  - `class ToolInputError(Exception)`, `class ToolFailed(Exception)`; each has `kind` (`"input"`, `"failed"`: Incisive `agent/tools/mod.rs` `ToolError::kind`) and a fixed message, never the input. `ToolFailed` texts (Incisive `LitTools::failed`): for `http 404` or `parse`, `the database returned nothing usable for this input`; otherwise `the database could not be reached; try again later or use another tool`.
  - `@dataclass(frozen=True) class Item: pmid: str | None; title: str; year: int | None; journal: str | None; pub_types: tuple[str, ...]; pmcid: str | None = None; doi: str | None = None; retracted: bool = False`.
  - `@dataclass(frozen=True) class SearchResult: count: int; items: tuple[Item, ...]` with `to_json() -> str` giving `{"count", "items": [{pmid, title, year, journal, pub_types[, pmcid][, doi][, retracted: true]}]}` (Incisive `Item`: `pmcid`/`doi` only when present, `retracted` only when true).
  - `@dataclass(frozen=True) class AbstractRecord: pmid: str; title: str; journal: str | None; year: int | None; abstract: str | None; pub_types: tuple[str, ...]; doi: str | None; pmcid: str | None; retracted: bool`; `@dataclass(frozen=True) class AbstractsResult: papers: tuple[AbstractRecord, ...]; not_found: tuple[str, ...]; cached: int` with `to_json()` giving `{"papers": [...], "not_found": [...]}` (`cached` is for logs only).
  - `class LitTools(pubmed: PubMed, epmc: EuropePmc, pool: ConnectionPool)`:
    - `search_pubmed(query: str, max_results: int) -> SearchResult`: esearch, then summaries cache first (Incisive `LitTools::summaries`); items without `pmcid`/`doi`.
    - `search_europepmc(query: str, max_results: int) -> SearchResult`: items with `pmcid`, `doi`; at most `max_results`.
    - `get_abstracts(pmids: Sequence[str]) -> AbstractsResult`: Incisive `LitTools::fetch_papers` exactly: cache first; esummary for the rest (an error with nothing cached raises, otherwise it is logged and the cached papers return); only ids asked for; efetch details fill a missing abstract and pub types and OR `retracted`; what efetch returned is cached; if efetch fails, summaries return without abstracts and nothing is cached. Results in the order asked; unknown ids in `not_found`.
    - `find_related(pmid: str, max_results: int) -> SearchResult`: elink, then summaries; `count` = items returned.
  - Inputs checked before any request (Incisive `string_field`, `count_field`, `valid_pmid`): `query` a non-empty string after trimming, then `validate_query(query, "pubmed" | "europepmc")`; `max_results` an `int` (not `bool`) from 1 to the cap; `pmids` a list of 1 to 20 strings, each trimmed and valid, repeats dropped; `pmid` valid. Exact Incisive messages, e.g. ``` `max_results` must be a whole number from 1 to 50 ```, ``` `pmids` must be a list of 1 to 20 PubMed ids (digits only) ```, ``` `pmid` must be a PubMed id (1 to 9 digits) ```.
  - Log lines: `lit tool=<name> count=<n> items=<n>`, `lit tool=get_abstracts asked=<n> cached=<n> returned=<n>`, `lit tool=<name> error_kind=<kind>`, `lit kind=cache_read_failed`, `lit kind=cache_write_failed`. A cache error is a miss (Incisive `LitTools::cached`), never a failure.

- [ ] **Step 1: Write the failing tests**

```python
def test_search_pubmed_json_shape(tools):
    d = json.loads(tools.search_pubmed("inguinal hernia mesh", 3).to_json())
    assert set(d) == {"count", "items"} and set(d["items"][0]) == {"pmid", "title", "year", "journal", "pub_types"}
def test_search_europepmc_items_carry_ids(tools): ...    # the preprint item has "doi" and "pmid": null
def test_rejected_query_sends_nothing(tools, up):
    with pytest.raises(ToolInputError) as e: tools.search_pubmed("hernia?", 5)
    assert str(e.value).startswith("query rejected (character)") and up.sent == []
@pytest.mark.parametrize("n", [0, 51, True, "5", 2.5])
def test_max_results_out_of_range(tools, up, n): ...     # exact message, nothing sent
@pytest.mark.parametrize("ids", [[], ["12a"], ["1234567890"], [str(i) for i in range(1, 22)]])
def test_bad_pmids_rejected_before_any_request(tools, up, ids):                 # Review Focus 5
    with pytest.raises(ToolInputError, match="must be a list of 1 to 20 PubMed ids"): tools.get_abstracts(ids)
    assert up.sent == []
def test_get_abstracts_second_call_hits_cache(tools, up):
    first = tools.get_abstracts(["<id1>", "<id2>", " <id1> "]); n = len(up.sent)
    second = tools.get_abstracts(["<id1>", "<id2>"])
    assert first.papers[0].abstract and len(up.sent) == n and second.cached == 2
def test_get_abstracts_unknown_id_is_not_found(tools): ...
def test_efetch_down_gives_titles_and_caches_nothing(tools, pool): ...
def test_upstream_down_is_tool_failed_with_fixed_text(tools_503):
    with pytest.raises(ToolFailed) as e: tools_503.search_pubmed("hernia", 5)
    assert str(e.value) == "the database could not be reached; try again later or use another tool"
def test_find_related_returns_neighbours_without_the_paper(tools): ...
```

(`tools` is a fixture in `tests/conftest.py`: migrated `pool`, `FakeUpstream` serving the Task 4 and 5 fixtures, `NcbiPacer(0)`.)

- [ ] **Step 2: Run** `pytest tests/test_lit_tools.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `lit/tools.py`.
- [ ] **Step 4: Run** `ruff check . && pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat: the four literature tools with typed results`.

### Task 8: The search page

**Files:**
- Create: `src/pubmedgemini/search.py`, `src/pubmedgemini/templates/results.html`
- Modify: `src/pubmedgemini/main.py`, `src/pubmedgemini/web.py`, `src/pubmedgemini/templates/home.html`, `tests/conftest.py`, `tests/test_web.py` (one deliberate change, below)
- Test: `tests/test_search_page.py`, `tests/test_logging.py`

**Interfaces:**
- `create_app(settings, clock=time.monotonic, lit_transport: httpx.BaseTransport | None = None, ncbi_gap_seconds: float | None = None) -> FastAPI` (tests pass a fake transport and a gap of 0). In the lifespan, after the pool: one `NcbiPacer(ncbi_gap_seconds if given, else ncbi_min_interval(key is set))`, one `LitClient`, `PubMed(client, key, email)`, `EuropePmc(client)`, `app.state.lit = LitTools(...)`; the client is closed at shutdown. One start-up line `literature ncbi_api_key=true|false` (Incisive `main.rs`), never the key or the email.
- `search.py`: `PAGE_RESULTS = 20` (each source is asked for 20 and the page shows the first 20 merged, so one `get_abstracts` call, at its cap of 20, covers every shown paper with a PMID). `@dataclass(frozen=True) class ShownPaper: pmid: str | None; doi: str | None; title: str; journal: str | None; year: int | None; pub_types: tuple[str, ...]; design: str; abstract: str | None; retracted: bool`. `@dataclass(frozen=True) class PageResult: papers: tuple[ShownPaper, ...]; pubmed_count: int | None; epmc_count: int | None; not_reached: tuple[str, ...]; not_run: tuple[str, ...]; abstracts_failed: bool; cached: int`. `class QueryRejected(Exception)` (`kind`, `source`), `class BothUnreachable(Exception)`. `run_page_search(tools: LitTools, text: str) -> PageResult`: trims the text; checks it for each source; runs the accepted sources side by side in a two-thread `ThreadPoolExecutor`; `merge_results`; first 20; `get_abstracts` for those with a PMID; design by `design_label`.
- Routes:

| Route | Signed in and set up | Otherwise |
|---|---|---|
| `GET /` | 200 `home.html`. Never reads a query string | F0's 401 / 403 / 503 pages |
| `POST /search` (form field `q`, default `""`) | 200 `results.html`; 400 `results.html` with `query_rejected_message(kind, "pubmed")` when both sources refuse the text; 503 `message.html` when both are unreachable | F0's 401 / 403 / 503 pages |

- Page text (exact):
  - `home.html`: heading "pubmedgemini"; "Signed in as <email>"; `<form method="post" action="/search">` with `<label for="q">Search PubMed and Europe PMC</label>`, one `<input id="q" name="q" type="text" maxlength="1000" required>`, one `<button type="submit">Search</button>`; the line "What you type is sent to PubMed and Europe PMC as the search. Do not type anything that identifies a patient."; the line "Answers written from these papers arrive in the next stages." No `<script>`.
  - `results.html`: the same form with the text refilled (`value="{{ query }}"`, autoescaped); then one status line: both searched: "Searched PubMed and Europe PMC (free sources)"; one unreachable: "Europe PMC could not be reached; searched PubMed only" or "PubMed could not be reached; searched Europe PMC only" (spec §8); one source refused the text (only possible for PubMed, after decision 6): "PubMed does not take the underscore; searched Europe PMC only". Then "PubMed found N; Europe PMC found M. Showing the first K." (a source not searched is left out). Then per paper an `<article>`: `<h2>` title; "Journal · Year · design label"; "Publication types: A, B"; `<a href="https://pubmed.ncbi.nlm.nih.gov/{{ pmid }}/">PMID {{ pmid }}</a>`, or for a record without a PMID `<a href="https://doi.org/{{ doi|urlencode }}">DOI {{ doi }}</a>`; "Retracted" in bold when set; the abstract as paragraphs split on blank lines, or "No abstract available." If abstracts failed: "Abstracts could not be fetched just now; titles only." No papers: "No papers found."
  - Both unreachable (503): "PubMed and Europe PMC could not be reached. Please try again in a minute."
- Log lines (one per search, fixed kinds): `search kind=ok pubmed=<n> europepmc=<n> shown=<n> abstracts=<n> cached=<n>`; `search kind=partial down=<pubmed|europepmc> ...` (same counts); `search kind=partial not_run=pubmed ...`; `search kind=none`; `search kind=unavailable`; `search kind=rejected reason=<kind>`. The request line stays `request method=POST route=/search status=<code>`.
- **Owner decision, added after this plan was approved (owner, 10 Nov 2026):** on the search page only, if the typed text ends with a question mark, the route quietly drops the trailing question marks and the whitespace around them before checking and searching (`web.py` `drop_trailing_question_marks`). So "Is mesh better than suture?" searches as "Is mesh better than suture"; a `?` anywhere else is still refused with the query-rejected message, and text that is only question marks is empty input. The four tools, `run_page_search` and the query rule are unchanged and still refuse a `?`.
- `tests/test_web.py`: `test_home_shows_signed_in_email` changes its third assertion from "Search and answers arrive in the next stages." to "Answers written from these papers arrive in the next stages."; nothing else in F0's tests changes.

- [ ] **Step 1: Write the failing tests** (`client_search`: `client_dev` built with `lit_transport=FakeUpstream(...)` and `ncbi_gap_seconds=0`; variants for each upstream state)

```python
H = {"x-dev-user-email": "owner@example.com"}

def test_home_has_one_box_and_no_script(client_dev):
    r = client_dev.get("/", headers=H)
    assert r.text.count('name="q"') == 1 and 'method="post" action="/search"' in r.text and "<script" not in r.text

def test_search_shows_merged_results(client_search):
    r = client_search.post("/search", data={"q": "inguinal hernia mesh"}, headers=H)
    assert r.status_code == 200 and "Searched PubMed and Europe PMC (free sources)" in r.text
    assert r.text.count("https://pubmed.ncbi.nlm.nih.gov/<shared id>/") == 1      # in both sources, shown once
    assert "BACKGROUND: " in r.text and "RCT" in r.text and "https://doi.org/" in r.text

def test_script_title_is_escaped(client_search):                                 # Review Focus 3
    r = client_search.post("/search", data={"q": "mesh"}, headers=H)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text and "<script>alert" not in r.text

def test_europepmc_down_shows_pubmed_only(client_epmc_down):                     # Review Focus 4
    r = client_epmc_down.post("/search", data={"q": "mesh"}, headers=H)
    assert "Europe PMC could not be reached; searched PubMed only" in r.text
    assert "Searched PubMed and Europe PMC" not in r.text
def test_pubmed_down_shows_europepmc_only(client_pubmed_down): ...
def test_both_down_is_503_plain(client_both_down):
    r = client_both_down.post("/search", data={"q": "mesh"}, headers=H)
    assert r.status_code == 503 and "PubMed and Europe PMC could not be reached." in r.text and "Traceback" not in r.text
def test_no_results_says_no_papers_found(client_empty): ...
def test_underscore_searches_europepmc_only(client_search): ...   # q='PUB_TYPE:"review"' -> the not-run line; no esearch sent
def test_rejected_query_is_400_and_sends_nothing(client_search, upstream):       # Review Focus 1
    r = client_search.post("/search", data={"q": "hernia?"}, headers=H)
    assert r.status_code == 400 and "query rejected (character)" in r.text and upstream.sent == []
def test_search_needs_sign_in(client_prod):
    assert client_prod.post("/search", data={"q": "mesh"}).status_code == 401
def test_get_with_query_string_does_not_search(client_search, upstream):
    client_search.get("/?q=hernia", headers=H); assert upstream.sent == []
```

`tests/test_logging.py`:

```python
def test_search_text_and_key_never_logged(client_search_with_key, caplog):     # Review Focus 2
    caplog.set_level(logging.DEBUG)                     # root: every logger that propagates
    client_search_with_key.post("/search", data={"q": "zq7 hernia mesh"}, headers=H)
    for leak in ("zq7", "key-q7", "Mesh repair", "eutils", "ebi.ac.uk"):
        assert leak not in caplog.text, leak
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith(("search ", "request "))]
    assert lines[-1] == "request method=POST route=/search status=200"
    assert re.fullmatch(r"search kind=ok pubmed=\d+ europepmc=\d+ shown=\d+ abstracts=\d+ cached=\d+", lines[-2])
```

- [ ] **Step 2: Run** `pytest tests/test_search_page.py tests/test_logging.py tests/test_web.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `search.py`, the route, the wiring and both templates, in the style of F0's (one `<main>`, system font, no JavaScript, no external assets).
- [ ] **Step 4: Run** `ruff check . && pytest -v`. Expected: all PASS, nothing skipped.
- [ ] **Step 5: Commit** `feat: plain search page over PubMed and Europe PMC`.

### Task 9: Compose, settings on the VPS, pull request

**Files:**
- Modify: `compose.yaml`, `.env.example`, `tests/test_compose.py`, `infra/README.md`, `docs/change-log.md`

**Decisions:**
- `app` gets `NCBI_API_KEY: ${NCBI_API_KEY:-}` and `NCBI_EMAIL: ${NCBI_EMAIL:-}`. `:-` gives an empty value when the name is unset or empty, so compose starts without them and the app reads `None` (Task 1). Only `app` uses them. The compose header comment says: every variable uses the required form except these two optional ones.
- `.env.example` gains, after the required names: `# Optional: the app runs without them. A free NCBI key raises PubMed's limit from 3 to 10 requests a second.`, `NCBI_API_KEY=`, `NCBI_EMAIL=`.
- `tests/test_compose.py`, changed deliberately, not loosened: a new `OPTIONAL = ["NCBI_API_KEY", "NCBI_EMAIL"]`. The required-form test becomes `test_every_variable_uses_the_required_form_except_the_reviewed_optional_ones`: each `${...}` is exactly `NAME:?set NAME in .env` with `NAME` in `REQUIRED`, or exactly `NAME:-` with `NAME` in `OPTIONAL`; the names used equal `REQUIRED + OPTIONAL`; `.env.example` names equal `REQUIRED + OPTIONAL`. New `test_app_environment_keys_are_exactly_the_reviewed_ones` (`APP_ENV`, `DATABASE_URL`, `OWNER_EMAIL`, `NCBI_API_KEY`, `NCBI_EMAIL`) and `test_optional_variables_are_used_by_app_only`. New `@needs_compose test_optional_variables_may_be_unset`: `DUMMY` alone (no NCBI names) validates and `docker compose config --format json` shows `NCBI_API_KEY` as `""` for `app`. The parametrised empty and unset tests keep iterating `REQUIRED` only.
- `infra/README.md`, new section "Optional: the NCBI key". Nothing is needed on the VPS for F1 itself. To add a key later: check for `.env.previous` as the runbook already says before any hand edit; `nano /opt/pubmedgemini/.env` and add `NCBI_API_KEY=<key>` and `NCBI_EMAIL=<address>`; then `cd /opt/pubmedgemini && docker compose up -d app`. One step each.

- [ ] **Step 1: Write the changed tests.** Run `pytest tests/test_compose.py -v`. Expected: FAIL (`NCBI_API_KEY` not in `compose.yaml`).
- [ ] **Step 2: Change** `compose.yaml` and `.env.example`. Run `pytest -v` (the whole suite, `test_deploy.py` included: its env files carry only required names, expected to need no change: Not checked). Expected: all PASS, nothing skipped.
- [ ] **Step 3: Write** the runbook section and a change-log entry. Commit `feat: optional NCBI settings through compose; runbook step for the key`.
- [ ] **Step 4: Push the branch** and check CI is green with zero skipped tests.
- [ ] **Step 5: Open the pull request** `F1: search tools and the plain results page` from `f1-search-tools` into `main`. Stop. Wait for the owner's review and his "merge it". Merging deploys.

---

## Manual check for sign-off (owner, on the live page, after the deploy)

1. Sign in. Type `hernia`, press Search. Expected: "Searched PubMed and Europe PMC (free sources)", up to 20 papers, each PMID link opens the paper on PubMed, most papers show an abstract.
2. The same for `achalasia`, `cholecystitis` and `appendicitis`.
3. Search `hernia` again. Expected: the same list, a little faster (abstracts come from the cache).
4. On the VPS: `cd /opt/pubmedgemini && docker compose logs app | grep -ci achalasia`. Expected: `0`.

F1 is done when all four pass and the owner judges the page useful on its own.

## Not in F1

Any Gemini code, the researcher loop and its echo guard, the study list, the writer, the citation and number checks, runs and run history, the answer page, the `runs`, `run_steps`, `run_papers`, `answers` and `model_calls` tables, full text, open-access links. They start at F2 and F3.

## Notes after the first live search (10 and 11 Nov 2026)

- The first live search, without an NCBI key, got NCBI's 429 at exactly 3 requests a second (Fact, as reported by the coordinator). The owner added a free NCBI key on the server on 10 Nov 2026, which raises the limit to 10 a second (pacing 0.1 s).
- Changing the pacing gap, retrying a 429, or dropping the duplicate esummary call (the page's search and `get_abstracts` both ask esummary for the same PubMed ids) are engine changes. None is approved; none is made.
- Independent review of the branch (11 Nov 2026), findings A to J, fixed tests first: a total deadline per attempt (A); no second round of NCBI attempts when PubMed is down, and the "titles only" line only when a fetch failed and a paper has no abstract (B, F); deep JSON or XML, lone surrogates and NUL characters never give a 500, any cache-write error is logged only, and an unexpected error is a plain 500 page with one `kind=internal` line (C, D); redirects followed by hand as Incisive does (E); a cross-site form post is refused and the gate's cookie is SameSite=Lax (G); tests that pass for the wrong reason rewritten (H); this table (I, J).

## Carried from the Flash-Lite test (10 Nov), for F2

Fact as reported by the coordinator from the probe run; not re-run by the Architect.

- Model id `gemini-3.5-flash-lite`; input limit 1,048,576 tokens, output limit 65,536.
- `responseSchema` and function calling mode `AUTO` were accepted.
- The search loop used 8 of 8 turns.
- Loop, study list and one writer part together: 14 calls and about 68,000 prompt tokens.
- No 429 replies at 10 requests a minute spacing.
