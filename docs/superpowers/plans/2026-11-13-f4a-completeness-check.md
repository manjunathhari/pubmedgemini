# F4a: the completeness check, code only (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax.

**Status (13 Nov 2026):** plan only, written by the Architect on branch `f4a-completeness-check`, equal to local `main` at `3d35839`. Nothing built. Baseline on this branch (Fact, run 13 Nov): `ruff check .` clean; `pytest -q -rs` 2230 passed, 0 skipped. Before Task 1 the owner answers "Needs the owner" (§8). The plan is written for the suggested option of each and says what changes for the others. Mock `docs/mocks/f4a-completeness-v3.html` is frozen (owner, 13 Nov).

**Goal:** After an answer is stored and the reader already sees it, code alone counts (1) how many of the answer's structured numbers are found in the abstract of the study each one cites, and (2) how many of the model's trial names agree with the record PubMed gave. One box, the last on the answer page, shows "Completeness check" with the steps Pending, Running, Done, and at Done one line of counts. Owner (13 Nov): "the number check is the completeness check. once the output is in. At the end a pill can show completeness pending and completeness checked. this way user is not interrupted". "Code only".

**Architecture:** One migration (one small table). One new pure module `src/pubmedgemini/completeness.py` (the two rules, the counts, and `run_check`, which never raises). The pending row is inserted in the same transaction as the answer (`runs.finish_with_answer`); the runner calls `run_check` on the run's own thread right after the answer is stored, with the answer and the records it already holds in memory. The run page reads the row and draws the box with the mock's seven `.steps` rules, added to `static/app.css`. No model call, no network call, no script.

**Labels.** Each statement is **Fact** (with where it was checked), **Estimate**, or **Not checked**. Fact means read or run by the Architect on 13 Nov 2026 on this branch at `3d35839`.

## 1. What was decided, and where

| # | Decision | Where (Fact) |
|---|---|---|
| F4a-1 | The number check is the completeness check; it runs once the output is in; a pill shows pending and checked; the user is not interrupted | owner's words, 13 Nov (above) |
| F4a-2 | Code only: no model call, no prompt change, no change to the researcher or the writer | owner, 13 Nov |
| F4a-3 | Mock v3 is frozen: one box, last on the page, after References and the three folds, before the closing lines; name, then "Pending › Running › Done", the current step dark; at Done one small line of counts; no second line while Pending or Running | `docs/mocks/f4a-completeness-v3.html` line 123 |
| F4a-4 | The row changes on reload only. The run page keeps its 5 s refresh after the answer is stored until the check is Done, then stops; a hard cap ends it whatever happens | owner, 13 Nov |
| F4a-5 | Counts only. The box names no number, lists no correction, has no corrected view. The answer is never changed; nothing is deleted or un-drawn | owner, 13 Nov |
| spec §5 row 5 | "Number check. Each number in the answer is traced to the abstract text; the rest are marked 'not traced'. Nothing is removed. Code, 0 calls" | `.10x/specs/2026-11-10-pubmedgemini-quick-design.md` l.104 |

**Difference from the spec, by F4a-5:** nothing is marked "not traced" on the page; the box shows counts. Task 5 brings spec §5 and §6 in line. **Not a port:** Incisive's `trace_numbers` was not read for this plan (Not checked); the rule in §3 is our own, stated in full here.

## 2. What is live that the check rests on (Fact, `3d35839`)

- **The structured numbers.** `writer/answer.py` `Number` (`label`, `value: float`, `unit`, `arm`, `ci_low`, `ci_high: float | None`, `p: str | None`, `pmid: str`), held in `Outcome.numbers`. Every line, square and listed number the answer page draws comes from these (`answer_page.py` `forest`, `out_of_100_groups`, `listed_numbers`, `arm_lines`). Each has exactly one `pmid` field: a number cannot cite several studies.
- **After the citation check every remaining number cites a fetched, selected study.** `writer/answer.py` `check_citations` drops a `Number` and a `Study` row whose `pmid` is not in `selected`; `runner.py` `_Run.go` passes `[s.pmid for s in studies]`, and `studies` holds only the selected studies whose record was fetched. So "a number that cites no study" cannot reach the check; the rule still handles it (not found).
- **Other numbers on the page are text, not structure.** `AtAGlance.key_numbers` hold `value_text` (free text, several numbers and a `[PMID]` marker inside; `pmid` is `""` when read from the one-text form, `writer/read.py` `parse_glance` l.314). `effect_text`, `key_results`, the bottom line and the rest are prose. `Study.n` is the one other structured figure (participants).
- **The trial names.** `Study.label` ("short name with year and journal", `writer/prompts.py` `SYSTEM` l.142), shown in "The trials" (`answer_page.py` `answer_view`, `TrialView`).
- **The writer is never shown an author.** `writer/prompts.py` `study_block` gives title, journal, year, publication types, DOI, retracted, abstract. `StudyInput` has no authors field. So a surname in a label comes from the model's memory, or the label is an acronym or words from the title. The year the writer saw is the record's `year`.
- **Where PubMed's first author and year are.** `lit/paper.py` `Paper.authors: list[str]` and `Paper.year`. `lit/pubmed.py` (esummary parser, l.182-188) fills `authors` from each author's `name`, in PubMed's order, in PubMed's form (surname, then initials). They are stored in `papers.authors TEXT[]` and `papers.year` (`migrations/0002_papers.sql`), the shared 30-day cache, not per run. Only records fetched from PubMed are cached (`lit/tools.py` `_fetch_papers` → `_store(fetched)`, the only caller of `put_many`).
- **The run already holds these records in memory.** `runner.py` `_Run.go`: `papers = self.tools.fetch_papers([...])`, a `dict[str, Paper]` with `authors`, `year` and `abstract`, the same records the writer's `StudyInput`s are built from. **So no new network call and no new read are needed.**
- **The abstract the writer saw** is cut at 5,000 bytes (`writer/prompts.py` `STUDY_CAPS.abstract`, `study_block`). The check reads the whole stored abstract (§3.1 note).
- **Where the answer is stored.** `runner.py` `_Run.end` → `runs.finish_with_answer` (run status, `answers` row, the line "Answer ready", delete of the early summary, one transaction). After it the run is final and the page stops refreshing (`run.html` `{% if active %}<meta http-equiv="refresh" content="5">`; `web.py` `run_page` `active = v.status in ("queued", "running")`).
- **How F3c wired its side work.** The Introduction: own thread, own table, joined in `run_one`'s `finally`; pending rows fail `stale` in `runs.stop_unfinished` at start-up (`main.py` l.74). The early summary: a callback, every error caught and logged by class. The check copies the same habits.
- **The look tests.** `tests/test_look.py` `AGREED` (the eight greys), `test_no_script_tags`, `test_run_page_still_refreshes_itself`; `tests/test_ask_page.py` `test_page_has_no_script_and_no_new_css`; `tests/test_run_answer_layout.py` `test_new_states_have_no_script_and_no_new_css` (every class on the page must have a rule in `app.css`). The mock's `.steps` rules use only `#BDBDBD`, `#8C8C8C`, `#333333`, `#FFFFFF`: all agreed.
- **The mock at phone width.** Rendered by the Architect with headless Chrome at 390 px wide: the name and the three steps sit on one row, the counts line under it, no sideways scroll (Fact, screenshot, not kept in the repo). At 360 px: Not checked; `.steps` has `flex-wrap:wrap`, so a narrower screen wraps the steps rather than scrolling (Estimate).
- **Model calls.** `gemini/quota.py` `RUN_CALLS = 23` (its comment already counts "number completion 1 (F4)", a later stage), `CALL_RESERVE = 3`, `RUN_ATTEMPTS = 26`, `runs_left_for`. F4a touches none of them (§5).

## 3. The two rules

### 3.1 The number check

**What is counted.** One count per `Number` entry in `Outcome.numbers` of the stored answer (after the citation check). Total = how many entries there are. (`Study.n`, the key numbers' text and all prose numbers are not counted: §9, and O1.)

**Where it looks.** The abstract of the study the entry cites: `papers[n.pmid].abstract`. Nothing else: not the title, not another study's abstract.

**Reading the abstract** (`abstract_numbers(text) -> list[Decimal]`):
1. A middle dot between two digits (`·`, U+00B7; also U+2219, U+22C5) becomes `.` (the "0·40" some journals print).
2. Thousands commas go: a run of 1 to 3 digits followed by one or more groups of `,` and exactly 3 digits, not touching another digit or comma, loses its commas ("1,250" → "1250").
3. A number is `\d+(?:\.\d+)?` or `\.\d+`, taken only where the character before is not a letter, digit, underscore or dot. So "CD4", "T2" and the tail of "1.2.3" are not numbers; "n=120", "(14.3%)", "p<0.05", "10-20" and "−0.03" give 120, 14.3, 0.05, 10, 20 and 0.03.
4. **Signs are dropped on both sides:** a hyphen before a digit is a minus in one abstract and a range dash in the next ("0.61-0.94"), so the check compares sizes only. The Unicode minus (U+2212) and the dashes need no handling for the same reason.

**The answer's figure** (`figure(v: float) -> tuple[Decimal, int] | None`): the absolute value as the shortest decimal that reads back (as `answer_page.py` `_plain_float`), and its decimals `d` (0 for a whole number: JSON cannot tell 14 from 14.0). A non-finite value gives None.

**When a figure is found** (`figure_found(v, numbers) -> bool`), the suggested rule (O1 b): some number `t` in the abstract has
- `t == |v|` (so "0.40" finds 0.4, "1,250" finds 1250, "14.0%" finds 14), **or**
- `t` has more decimals than `d` and `|t − |v|| ≤ 0.5 × 10^−d` (the abstract's figure, rounded to the answer's decimals: "14.29" finds 14.3; "14.3" finds 14). The half-way case counts either way.

**When an entry is found** (`number_found(n, abstract) -> bool`): its `value` is found, **and** each of `ci_low` and `ci_high` that is given is found, by the same rule in the same abstract. `p` is not checked (it is text "as written").

**The edges.**

| Case | Result |
|---|---|
| The entry's `pmid` is not among the run's fetched records | not found (cannot happen after the citation check; guarded) |
| The study has no abstract, or a blank one | not found |
| The value is not finite | not found |
| The answer has no structured numbers | total 0 (wording: O5) |
| The same number listed twice | counted twice |

**What true numbers this rule misses (reported "not found" though right):**
- a figure worked out, not printed: 14% for "6 of 42", a difference or a sum of two printed figures, an NNT;
- a change of unit or scale: 0.14 for "14%", hours for minutes;
- a number in words ("twelve"), a decimal comma ("0,40"), thousands split by a space ("1 250"), powers of ten;
- a figure with more decimals than the abstract prints (14.30 cannot be told from 14.3, but 14.29 for "14.3" is not found: right, the model added precision);
- a figure that is only in the title or the full text.

**What false matches it can make (reported "found" though wrong):**
- **any equal figure anywhere in that abstract counts.** A value of 5 is found by "5 years", "5 mg" or "COVID-19"'s 19 for 19; small whole numbers are nearly always found. The rule does not read the unit, the arm or the words around the number;
- a wrong sign (−3.2 for 3.2) is found;
- a figure given to the wrong arm or the wrong outcome of the right study is found;
- with rounding (O1 b), a whole number N is found by any printed figure from N−0.5 to N+0.5;
- a figure past the 5,000 bytes the writer was shown is found (the check reads the whole abstract; Estimate: rare, few abstracts are that long).

So the count says "this figure is printed in the cited abstract", never "this figure is right". The change log and the sign-off say so in those words.

**Options (O1, an owner decision: it changes what the count means).**

| | Rule | Gain | Cost |
|---|---|---|---|
| a | Exact only (`t == |v|`) | fewest false matches | a correctly rounded figure reads "not found", and the box cannot say which |
| **b** | **Exact, or the abstract's figure rounded to the answer's decimals** (suggested) | rounded figures are found | whole numbers match a window of ±0.5 |
| c | b, and a percentage worked out from printed counts ("6 of 42") | fewer misses | about 60 more lines and many more false matches (any two counts in the abstract); it is the later stage's "number completion" |

### 3.2 The trial-name check

**What is counted.** One count per row of `WrittenAnswer.studies` (the trials table). Total = how many rows.

**What it is compared with.** The record the run fetched for the row's `pmid`: `Paper.year` and `Paper.authors[0]` (PubMed's first author), and for option T2 also `Paper.title` and `Paper.abstract`.

**Folding** (`fold(s) -> str`): NFKD, combining marks dropped, case-folded, and `ø→o`, `ł→l`, `đ→d`, `æ→ae`, `œ→oe`, `ß→ss`. Words are runs of letters.

**The year matches** when the record has a year and the label holds it as a number of its own (`(?<!\d)\d{4}(?!\d)` equal to `Paper.year`). No year on the record, or none in the label: no match.

**The surname** (`first_surname(authors) -> list[str]`): the first author's name, folded, without its last token when that token is initials as written by PubMed (1 to 3 capital letters) and without a trailing "Jr", "Sr", "2nd", "3rd"; split into words. A group author (no initials) keeps all its words. The surname matches when one of its words of 4 letters or more is a whole word of the folded label (or, when it has no such word, any of its words: "Li").

**Group authors (review fix B1, owner 13 Nov).** When the first author is a group (no initials: "The EXAMPLE Study Group", "Example Trial Investigators"), the words study, group, trial, trials, collaborative, collaboration, collaborators and investigators are left out before the surname is compared, so only the group's distinctive words can match ("WRONGNAME Study 2019" no longer passes on "study"). A group author made of those words alone never matches by the surname; the title-or-abstract route below still applies. A person's surname (a name with initials) is never left out.

**The name matches** (suggested rule T2): the surname matches, **or** the label's leading name is printed in the study's own title or abstract. The leading name is the first word-like token of the label (letters, digits and inner hyphens, such as "EXAMPLE-2") that is not the year and not one of "the", "a", "trial", "study", "et", "al"; it must be at least 3 characters and appear as a whole token, case-folded, in the title or abstract.

**A hyphen and a space read the same (review fix A1, owner 13 Nov).** The leading name is also looked for by its first letters-and-digits piece ("EXAMPLE" of "EXAMPLE-2"; at least 3 characters, not a number, not one of the filler words) among the title's and abstract's tokens split at hyphens too. So "EXAMPLE-2 2019" matches a record that prints "EXAMPLE 2", "EXAMPLE-II" matches "EXAMPLE II", and "EXAMPLE 2019" matches "EXAMPLE-2" or "EXAMPLE-trial". The year is still required, and the surname route is not changed by this.

**A row matches** when the year matches and the name matches.

**The edges.** No record for the `pmid`: no match (guarded; cannot happen). No authors on the record: only the title-or-abstract route can match. Two rows for one study: counted twice.

**What it misses:** a surname spelt another way (transliteration), a label naming the last or a middle author, a trial acronym that the abstract does not print, a label whose year is the online-first year when PubMed gives the print year (the writer was shown PubMed's year, so Estimate: rare).
**What it passes wrongly:** under T2 a label whose first word is an ordinary word of the title ("Laparoscopic 2019") matches; the year alone carries the check there. Known false match from the hyphen rule (accepted by the owner, 13 Nov): only the name's first piece is compared, so "EXAMPLE-3 2019" matches a record that prints "EXAMPLE-2" (and "EXAMPLE-2" matches "EXAMPLE-22" or "the EXAMPLE trial, 2 arms"). Two more known false matches, found by the re-review and accepted by the owner at merge (13 Nov): (1) the label's first word matches any hyphen-joined part of a printed word, not only its start, so a wrong surname with the right year passes when it sits inside a hyphenated word of the title or abstract; (2) a group first author whose distinctive words are all short can match a label on a short common word such as "the". Both only ever turn a wrong trial name into a match; neither touches the answer. Possible later narrowing: match at the start of a printed word only, and ignore short common words for group authors.

**Options (O2, an owner decision).**

| | Rule | Gain | Cost |
|---|---|---|---|
| T1 | Surname and year only (the owner's first wording) | strict; catches every name from memory that is wrong | the writer is shown no authors, so a label that is a trial acronym or title words reads "no match" though nothing is wrong, and the box cannot say which |
| **T2** | **Year, and the surname or a name printed in the study's own title or abstract** (suggested) | acronym names pass; a made-up surname or acronym still fails | a label starting with a title word passes on the year alone |
| T3 | Year only | simplest | says nothing about the name |

## 4. Storage, states and where it runs

### Migration `0007_run_checks.sql` (new; 0001-0006 not edited)

```sql
CREATE TABLE run_checks (
    run_id        UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed')),
    reason        TEXT CHECK (reason ~ '^[a-z_]{1,32}$'),   -- a fixed kind, only when failed
    numbers_found INT, numbers_total INT, names_match INT, names_total INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    CHECK ((status = 'done') = (numbers_found IS NOT NULL AND numbers_total IS NOT NULL
                                AND names_match IS NOT NULL AND names_total IS NOT NULL)),
    CHECK (numbers_found BETWEEN 0 AND numbers_total AND names_match BETWEEN 0 AND names_total),
    CHECK ((status = 'failed') = (reason IS NOT NULL))
);
```

Holds four counts and fixed words: no model text, no question. Its own table, as `run_intros`: `answers` is written once and never changed (F4a-5), and `runs` is read by Library and Activity. (Estimate: 20 lines.)

### States

| Stored | Set by | Page (mock) |
|---|---|---|
| `pending` | `finish_with_answer`, in the answer's own transaction, only when a written answer is stored | Pending dark |
| `running` | `check_begin`, at the start of `run_check` | Pending passed, Running dark |
| `done` + four counts | `check_finish` | Pending and Running passed, Done dark, the counts line |
| `failed` + reason (`internal`, `store`, `stale`) | `check_finish` on any error; `stop_unfinished` at start-up | O3 |
| no row | every run before this stage; every run with no written answer | no box (O4) |

- **The pending row and the answer are stored together**, so the first page that shows the answer also shows the box and keeps refreshing. The insert sits inside a savepoint (a nested `conn.transaction()`): if it alone fails, the answer is still stored, the page shows no box, and one line is logged. **The check can never keep an answer from being stored.**
- **Where it runs:** on the run's own thread, in `_Run.go`, right after `self.end(a, written, None, ...)` returns for a written answer; `run_check(pool, run_id, written, papers)`. The run is already final in the database, so the next question is not held up ("one at a time" is read from `runs`, `runs.active_run`). No new thread. It reads the answer and the records in memory: the same `written` that was stored (the stored copy is also passed through `scrub_json`, which removes only NUL, lone surrogates, bidi and zero-width characters: Fact, `lit/text.py`) and the same `papers` the writer's studies were built from.
- **How long:** pure Python over at most a few dozen numbers and abstracts; no network, no model (Estimate: a few milliseconds). **Honest consequence: Pending and Running will almost never be seen.** The first page that shows the answer will nearly always show Done. The three steps are built as the mock shows; they will matter if a later stage adds slower work.
- **It can never fail or delay a run:** `run_check` catches everything, ends the row `failed` with a fixed reason and logs the error's class only. If even that write fails, the row stays `pending` or `running` and the page's cap (below) takes over. `run_one` behaves exactly as today around it (its `except` blocks never see a check error).
- **A restart of the app mid-check:** the thread is gone. `stop_unfinished` (start-up) sets every `pending` or `running` check to `failed`, reason `stale`, in its existing transaction. The check is not re-run (O4 option c would add that).
- **The refresh and its hard cap:** the page refreshes every 5 s while the run is active (as today) **or** while the check is `pending` or `running` and its row is younger than `CHECK_WAIT_SECONDS = 60`, measured by the database's own clock (`now() - run_checks.created_at`, read in `get_run`). Past 60 s the page stops refreshing and shows a `pending` or `running` row as it shows `failed` (O3): it never claims Running for a check that is not, and never refreshes for ever. The row itself is not changed by a page view. At most 12 extra refreshes (Fact by arithmetic).

### Logs (counts and fixed kinds only; added to the allow-list)

- `run kind=check run_id=<uuid> status=done numbers_found=<n> numbers_total=<n> names_match=<n> names_total=<n>`
- `run kind=check run_id=<uuid> status=failed reason=<kind> error=<Class>`
- `run kind=check_not_started run_id=<uuid> error=<Class>` (the savepoint insert failed)

No step line is added: "Answer ready" stays the run's closing line (owner: the pill only).

## 5. Gemini quota: zero calls (Fact by design, pinned by tests)

- `completeness.py` imports nothing from `pubmedgemini.gemini` or `pubmedgemini.lit.tools`/`http`; it takes plain data. Test: Task 2 `test_module_imports_no_client`.
- `gemini/quota.py` `RUN_CALLS`, `CALL_RESERVE`, `RUN_ATTEMPTS`, `runs_left_for` and `gemini/caller.py` `ModelCaller` are not edited. `tests/test_gemini_quota.py` `test_runs_left_is_21_on_a_fresh_day` and `tests/test_runs.py` `test_full_run_has_an_introduction_and_14_attempts` pass **unedited** (14 `model_calls` rows for a full run, before and after).
- `model_calls_step_check` is not touched: no new step.
- The comment "number completion 1 (F4)" in `quota.py` stays: that call belongs to a later stage, not this one.

## Global Constraints

1. **Keep in step with `main`.** `git fetch origin main` before any work, before review and before asking for "merge it"; compare `FETCH_HEAD` with `git merge-base HEAD FETCH_HEAD`. If `main` moved: rebase before the branch is pushed, merge after; re-run the whole suite. (Not checked by the Architect on 13 Nov: no fetch was run; the branch equals local `main`.)
2. Work only on `f4a-completeness-check`. No push to `main`, no force push, nothing merged without the owner's own words "merge it".
3. **No engine change.** Nothing in the researcher, the writer, the prompts, the citation check, the stored answer (`answers.body`, its shape and `IntegrityV3`), the cap (26), free runs (21) or the time limits changes. `finish_with_answer` gains one optional argument; its behaviour without it is byte-for-byte today's.
4. **The check never fails, delays or changes a run or its answer.** Every exception is caught and ends in `failed` or in an untouched row; nothing is deleted, un-drawn or rewritten.
5. **Never logged:** the question, any answer field, a label, a number from the answer, an author, an abstract. Logs hold the four counts, run ids and fixed kinds.
6. **The look:** the mock's markup and its seven `.steps` rules; no `<style`, no `style=`, no `<script`, no new `<link>`; the agreed greys only. One row at 390 px, no sideways scroll.
7. **Untrusted text:** the box prints only words fixed in code and integers; no model text, no author name, reaches it.
8. **Keep out of the repo:** secrets, server names, the web address, real emails, question text, model replies, run outputs. Tests and mocks use made-up text (Example Journal, "Surname 2019", EXAMPLE-2).
9. Tests first in every task; one commit per task; `ruff check .` and the whole suite green with nothing skipped before each commit.
10. **Old runs:** a run without a `run_checks` row renders exactly as today (the finished page's existing tests pass unedited).
11. **Small stage:** no refactor outside what the check needs. `answer_page.py` is not edited.

## Review Focus

| # | Item | Tests that pin it |
|---|---|---|
| 1 | The number rule is exactly §3.1: formats, rounding one way only, CI ends, every edge | Task 2 `test_abstract_numbers` (table), `test_figure_found` (table), `test_number_found_needs_every_given_ci_end`, `test_number_edges` |
| 2 | The rule's known false matches and misses are written down as tests, so nobody mistakes them for bugs later | Task 2 `test_known_false_matches_are_as_documented`, `test_known_misses_are_as_documented` |
| 3 | The name rule is exactly §3.2 | Task 2 `test_first_surname` (table), `test_year_must_be_the_records`, `test_name_matches` (table) |
| 4 | The check never fails, delays or changes a run or its stored answer | Task 3 `test_a_failing_check_never_changes_the_run` (parametrised), `test_the_stored_answer_is_byte_identical_with_and_without_the_check`; Task 1 `test_a_refused_check_row_still_stores_the_answer` |
| 5 | Zero model calls; 26 and 21 unchanged | Task 2 `test_module_imports_no_client`; existing `test_full_run_has_an_introduction_and_14_attempts`, `test_runs_left_is_21_on_a_fresh_day` unedited |
| 6 | The page never lies and never refreshes for ever | Task 4 `test_refresh_stops_at_done`, `test_refresh_stops_after_the_cap`, `test_a_stuck_check_is_not_shown_as_running`; Task 1 `test_start_up_fails_unfinished_checks` |
| 7 | The box is the mock: place, markup, words, the CSS rules word for word | Task 4 `test_box_is_last_before_the_closing_lines`, `test_box_markup_is_the_mocks` (three states), `test_steps_rules_are_the_mocks` |
| 8 | Counts only: no label, number or name from the answer in the box or the logs | Task 4 `test_box_holds_only_fixed_words_and_counts`; Task 5 leak tests |
| 9 | Old runs and runs without a written answer show no box and are unchanged | Task 4 `test_no_check_row_no_box`; existing finished-page tests unedited |

## Tasks

### Task 1: Migration 0007 and the store

**Files:** Create `migrations/0007_run_checks.sql`. Modify `src/pubmedgemini/runs.py`. Test `tests/test_runs.py`, `tests/test_migrate.py`, `tests/test_logging.py` (the reviewed-columns test).

**Interfaces:**
- `@dataclass(frozen=True) class CheckRow: status: str; numbers_found: int | None; numbers_total: int | None; names_match: int | None; names_total: int | None; age_s: float` (seconds since `created_at`, by the database's clock).
- `RunView.check: CheckRow | None = None` (added last, with a default: no constructor changes).
- `finish_with_answer(..., closing_line=None, check: bool = False) -> bool`: when applied and `check`, `INSERT INTO run_checks (run_id) VALUES (%s)` inside a nested `conn.transaction()`; a database error there is caught, the savepoint rolled back, `run kind=check_not_started run_id=… error=<Class>` logged, and the answer still commits.
- `check_begin(pool, run_id: UUID) -> bool`: `pending` → `running`, `started_at = now()`; False when there is no pending row.
- `check_finish(pool, run_id: UUID, *, counts: tuple[int, int, int, int] | None, reason: str | None) -> bool`: only while `pending` or `running`; `counts` given → `done`; else `failed` with `reason`. Exactly one of the two, else ValueError before any write.
- `stop_unfinished`: also `UPDATE run_checks SET status = 'failed', reason = 'stale', finished_at = now() WHERE status IN ('pending', 'running')`, in its transaction.
- `get_run`: one more read, `SELECT status, numbers_found, numbers_total, names_match, names_total, EXTRACT(EPOCH FROM now() - created_at) FROM run_checks WHERE run_id = %s`.

**Tests first:**
- `test_migration_0007_adds_run_checks`: the table exists; `done` without counts is refused; `found > total` is refused; `failed` without a reason is refused; a status `checked` is refused.
- `test_answer_with_check_stores_a_pending_row_together`: `finish_with_answer(check=True)` → answer row and a `pending` check row; `check=False` (the default) → no check row; a run no longer running → neither.
- `test_a_refused_check_row_still_stores_the_answer`: a check row inserted by hand first (so the insert hits the primary key) → the answer, the status and "Answer ready" are stored; the one log line.
- `test_check_moves_pending_running_done_once`: `check_begin` True then False; `check_finish(counts=(16, 16, 4, 4))` True then False; `get_run(...).check` holds the counts.
- `test_check_finish_failed_keeps_no_counts`; `test_check_finish_needs_counts_or_reason`.
- `test_start_up_fails_unfinished_checks`: one pending, one running, one done → the first two `failed` `stale`, the third untouched.
- `test_a_deleted_run_takes_its_check_row`.
- `test_no_model_text_in_any_table_but_the_reviewed_ones`: deliberate edit, adds `("run_checks", "status")`, `("run_checks", "reason")`.

- [ ] Tests, FAIL, implement, `ruff check . && pytest -q -rs` PASS. Commit `feat: the completeness check's table and store (migration 0007)`.

### Task 2: The two rules (pure code)

**Files:** Create `src/pubmedgemini/completeness.py`. Test `tests/test_completeness.py` (new).

**Interfaces** (all pure; nothing here logs, stores or sends):
- `abstract_numbers(text: str) -> list[Decimal]` (§3.1 steps 1 to 4).
- `figure(v: float) -> tuple[Decimal, int] | None`.
- `figure_found(v: float, numbers: Sequence[Decimal]) -> bool`.
- `number_found(n: Number, abstract: str | None) -> bool`.
- `fold(s: str) -> str`; `first_surname(authors: Sequence[str]) -> list[str]`; `year_matches(label: str, year: int | None) -> bool`; `name_matches(label: str, paper: Paper) -> bool`; `trial_name_matches(label: str, paper: Paper | None) -> bool`.
- `@dataclass(frozen=True) class Counts: numbers_found: int; numbers_total: int; names_match: int; names_total: int`.
- `check_answer(w: WrittenAnswer, papers: Mapping[str, Paper]) -> Counts`. Reads only; `w` and `papers` are not changed.
- Every regular expression is linear (no nested repeats), so a hostile abstract cannot stall it.

**Tests first** (made-up text only):
- `test_abstract_numbers` (table): `"14.3% vs 20.1%"` → 14.3, 20.1; `"0·40"` → 0.40; `"1,250 patients"` → 1250; `"12,345,678"` → 12345678; `"n=120"`, `"p<0.05"`, `"(.05)"`; `"10-20"` → 10, 20; `"−0.03"` → 0.03; `"CD4"`, `"T2"`, `"v1.2.3"` → the documented tokens only; `"0,40"` → 0, 40 (a documented miss).
- `test_figure_found` (table): 0.4 in "0.40"; 14.0 in "14%"; 1250.0 in "1,250"; 14.3 in "14.29" (rounding); 14.0 in "14.3"; 14.3 **not** in "14"; 14.29 **not** in "14.3"; −3.2 in "3.2"; NaN and inf → False; half-way 0.5 in "0.45" and "0.55" → True.
- `test_number_found_needs_every_given_ci_end`: value and both ends printed → True; one end not printed → False; ends `None` → the value alone decides; `p` ignored.
- `test_number_edges`: `abstract=None`, `""`, spaces → False; a `pmid` not in `papers` → counted, not found.
- `test_known_false_matches_are_as_documented`: value 5 against "followed for 5 years" → True; a figure of the other arm → True. `test_known_misses_are_as_documented`: 14.3 against "6 of 42" → False; 0.14 against "14%" → False; "twelve" → False.
- `test_first_surname` (table, made-up names in PubMed's form): `"Surname AB"` → [surname]; `"van der Example JC"` → [van, der, example]; `"Example-Other A"` → [example, other]; `"Exämple Å"` → [example]; `"Surname AB Jr"` → [surname]; `"The EXAMPLE Study Group"` → all words; `[]` → [].
- `test_year_must_be_the_records`: "Surname 2019, Example Journal" with 2019 → True; with 2020 → False; record year None → False; "20190" → False.
- `test_name_matches` (table, T2): surname in the label → True; folded accents → True; another surname → False; "EXAMPLE-2 2019" with "EXAMPLE-2" in the abstract → True, absent → False; a label that is only a year → False; no authors and no title hit → False.
- `test_check_answer_counts`: a made-up answer with 3 numbers (2 printed) and 2 trial rows (1 matching) → `Counts(2, 3, 1, 2)`; the answer object compares equal to a deep copy made before the call.
- `test_module_imports_no_client`: the module's source imports neither `pubmedgemini.gemini` nor `pubmedgemini.lit.tools` / `lit.http` / `httpx`.
- `test_a_long_hostile_abstract_is_quick`: 200,000 characters of digits, commas and dots finish under 1 s.

- [ ] Tests, FAIL, implement, PASS. Commit `feat: the completeness check's two rules (numbers in the cited abstract, trial names against PubMed's record)`.

*If the owner picks O1 a:* drop the rounding branch and its table rows. *O1 c:* stop and re-plan (it is not this stage's size). *O2 T1:* `name_matches` is the surname alone. *T3:* `trial_name_matches` is `year_matches`.

### Task 3: The runner runs the check

**Files:** Modify `src/pubmedgemini/completeness.py` (`run_check`), `src/pubmedgemini/runner.py`. Test `tests/test_runs.py`.

**Interfaces:**
- `completeness.run_check(pool, run_id: UUID, w: WrittenAnswer, papers: Mapping[str, Paper]) -> None`. Never raises. `check_begin` False → return (no row: the answer was not stored, or a restart got there first). Then `check_answer`, `check_finish(counts=…)`, the `done` log line. Any exception: `check_finish(reason="internal")` (itself guarded; `store` when the finishing write is what failed), the `failed` log line with the error's class.
- `runner.py` `_Run.end(...)` gains `check: bool = False`, passed to `finish_with_answer`, and returns whether the answer was stored. `_Run.go`'s last line becomes: `if self.end(a, written, None, status, None, check=True): run_check(self.pool, self.run_id, written, papers)`. Every other `end(...)` call is unchanged (no written answer: no check row).

**Tests first:**
- `test_a_written_run_ends_with_a_done_check`: a full fake run → `run_checks` `done`, counts equal to `check_answer` on the same fake answer and fake records; `started_at` and `finished_at` set.
- `test_runs_without_a_written_answer_have_no_check_row` (parametrised over the runner's endings with no written answer: nothing selected, unfetched, no time, truncated, unreadable, failed).
- `test_a_failing_check_never_changes_the_run` (parametrised: `check_answer` raising; `check_begin` raising; `check_finish` raising on the first call; the database refusing the row): the run's status, error kind, step lines and `answers.body` equal those of the same run with the check untouched; the check row is `failed` with its reason, or still `pending`/`running` in the last case.
- `test_the_stored_answer_is_byte_identical_with_and_without_the_check`: `answers.body` as JSON text, with `run_check` replaced by a no-op and without.
- `test_the_check_adds_no_step_line_and_no_model_call`: the step lines end with "Answer ready"; `model_calls` count as before.
- `test_full_run_has_an_introduction_and_14_attempts`: existing, unedited, still passes.

- [ ] Tests, FAIL, implement, PASS. Commit `feat: the run checks its stored answer for completeness (code only, never fails a run)`.

### Task 4: The box on the answer page

**Files:** Modify `src/pubmedgemini/static/app.css`, `src/pubmedgemini/web.py`, `src/pubmedgemini/templates/run.html`. Test `tests/test_look.py`, `tests/test_run_answer_layout.py`, `tests/test_ask_page.py`.

**CSS:** a new block at the end of `app.css`, headed `/* ---- the completeness check (F4a): the .steps rules of docs/mocks/f4a-completeness-v3.html ---- */`, holding these seven rules of the mock (its lines 96-101 and 103), word for word: `.steps`, `.steps .name`, `.steps .st`, `.steps .st.was`, `.steps .st.now`, `.steps .ar`, `.steps .sub[data-x]`. The mock's block also has four rules its markup never uses (`.check ul`, `.check .line`, `details.fold>summary.steps::before`, `.steps .brk`: left from earlier versions; Fact: line 123 uses none of them). They are **not** copied (dead rules; a Coordinator ruling if the mock's whole block is wanted instead).

**`web.py`:**
- `CHECK_WAIT_SECONDS = 60`; fixed words `CHECK_NAME = "Completeness check"`, `CHECK_STEPS = ("Pending", "Running", "Done")`, `CHECK_NOT_COMPLETED` (O3).
- `_check_box(v: RunView) -> dict | None`: None without a row. Shown state: `done`; `failed` (also a `pending` or `running` row with `age_s >= CHECK_WAIT_SECONDS`); else the stored `pending` or `running`. Returns `{"steps": [(word, cls), …], "line": str | None, "wait": bool}`; `cls` is `"st now"`, `"st was"` or `"st"`, fixed words from code. `wait` is True only for a shown `pending` or `running`.
- The counts line (O5 a): `"{f} of {t} numbers found"` or `"No numbers to check"` when `t == 0`; `"{m} of {t} trial names match"` or `"No trial names to check"`; joined by `" · "`.
- `run_page` context: `"check": _check_box(v) if wide else None`, `"refresh": active or bool(check and check["wait"])`.

**`run.html`:**
- Head block: `{% if refresh %}<meta http-equiv="refresh" content="5">{% endif %}`.
- In the `wide` branch, after the "Possible contradictions" card and before `run.down_line` and the closing lines, exactly the mock's line 123 shape:
  `<section class="card"><div class="steps"><span class="name">Completeness check</span><span class="st …">Pending</span> <span class="ar">›</span> <span class="st …">Running</span> <span class="ar">›</span> <span class="st …">Done</span>{% if check.line %} <span class="sub" data-x>{{ check.line }}</span>{% endif %}</div></section>`
- The other branch (working page, runs without a readable answer) is not touched.

**Tests first:**
- `test_steps_rules_are_the_mocks`: each of the seven rules, read from the mock file, is in `app.css` character for character; the four unused ones are not.
- `test_box_markup_is_the_mocks` (pending, running, done; rows inserted by hand): the `<section class="card"><div class="steps">…` string for `done` equals the mock's line 123 with the test's counts; `pending` has `st now` on Pending only and no `sub`; `running` has `st was`, `st now`, `st` and no `sub`.
- `test_box_is_last_before_the_closing_lines`: its index is after "Possible contradictions (" (and after "Selected studies" and "References") and before "Searched PubMed and Europe PMC".
- `test_refresh_stops_at_done`: `done` → no `http-equiv`; `pending` and `running` (young) → the 5 s refresh, on a run whose status is `done`.
- `test_refresh_stops_after_the_cap` and `test_a_stuck_check_is_not_shown_as_running`: a `running` row with `created_at` 61 s ago → no refresh, no `st now`, the O3 line; the row in the database is unchanged by the page view.
- `test_failed_check_shows_the_plain_line` (O3 a): `st was`, `st was`, `st`; the line; no counts.
- `test_zero_totals_read_plainly` (O5 a).
- `test_no_check_row_no_box`: an answered run with no row, a run with no answer, a partial run with an empty reason, the working page → no `class="steps"`.
- `test_box_holds_only_fixed_words_and_counts`: a made-up answer whose labels and numbers carry markers → none inside the box's `<section>`.
- `test_new_states_have_no_script_and_no_new_css`: extended with the three states (every class has a rule; no `<style`, `style=`, `<script`, extra `<link>`).
- `test_run_page_still_refreshes_itself` (`test_look.py`): existing, unedited, still passes (a finished run with a `done` check has no refresh).
- Phone width: the suite has no browser (Fact: no `playwright` in `tests/`). The mock was rendered at 390 px (Fact, §2); the built page is checked by the owner (sign-off item 3).

- [ ] Tests, FAIL, implement, PASS. Commit `feat: the completeness check box on the answer page (mock v3)`.

### Task 5: Leak tests and records

**Files:** `tests/test_logging.py`, `docs/change-log.md`, `docs/build-log.md`, `.10x/status.md`, spec §5 (row 5), §6, §7 (table list), §9.

**Tests first:**
- `test_every_log_line_matches_the_allowlist` (existing): the three new patterns of §4 added, nothing else loosened.
- `test_nothing_private_is_logged_by_the_check`: markers in a number's label, a trial label, an abstract and a made-up author name; a full fake run; none in `caplog`; the `done` line matches its pattern.
- `test_a_failed_check_logs_only_its_class` (mutation-checked: a version that logs `str(e)` fails it).
- `test_a_written_run_under_uvicorn_logs_no_writer_text` (existing): still passes; its wait for the page to stop refreshing now also waits for the check.

**Records, in plain words:** change log (what the box says, and in one sentence what the count means and does not mean, §3.1); build log; status (F4a built); spec §5 row 5 reworded to counts only (F4a-5), §6 the box as the last item, §7 `run_checks`.

- [ ] Tests, PASS. Push only when the owner says so; open PR `F4a: the completeness check (code only)`. Stop; wait for "merge it".

Size (Estimate): about 190 lines of code, 420 of tests, 20 of SQL, 8 of CSS.

## 6. Manual sign-off (owner, after the deploy)

1. Home still says "Free runs left today: about 21".
2. Ask one benchmark question. When the answer shows, scroll to the bottom: the last box reads "Completeness check" with Done dark and one line of counts. (Pending or Running may never be seen: the check takes a blink.)
3. On the phone: the name and the three steps on one row; no sideways scroll.
4. Open an older answer from Library: no box, nothing else changed.
5. Pick one answer where the counts are not full. Compare by eye two of its numbers with the abstracts on the same page. Tell us whether the count was fair. (This is the only real test of the rule: Not checked on any real answer.)
6. On the server: the day's `run kind=check` lines hold only ids and counts.

## 7. Risks

1. **A full count reads as "the numbers are right".** It means only "each figure is printed somewhere in the cited abstract" (§3.1 false matches). Mitigation: the words in the change log; the sign-off item 5. The box itself has no room for the caveat (frozen mock).
2. **A count that is not full cannot be acted on from the page:** the box does not say which number (F4a-5). The reader must compare by eye.
3. **Trial names:** the writer never sees authors, so labels may often be acronyms or title words; under T1 the count would look bad for no fault (O2).
4. **Pending and Running are nearly invisible** (milliseconds). Built as the mock shows; little is gained until a slower check exists.
5. **The 5 s refresh** now can run a few seconds after the answer shows; it may move the reader's place once (Not checked on phones, as F3c).
6. **The paper cache is shared and refreshed.** The check uses the records in memory at the run, so a later refresh of `papers` cannot change a stored count.

## 8. Needs the owner

**O1. When does a number count as "found"?** (§3.1) (a) only when the abstract prints the very same figure; (b) the same figure, or the abstract's figure rounded to the decimals the answer shows (14.29 → 14.3); (c) also a percentage worked out from printed counts (6 of 42 → 14%). All three also need each given interval end to be printed, and none reads the unit or the arm. **Suggestion: (b).** Scope with it: (i) the outcome numbers only; (ii) also each trial's participant count `n`. **Suggestion: (i)** for now; (ii) is about 10 more lines.

**O2. When does a trial name "match"?** (§3.2) T1 PubMed's first-author surname and year only; T2 the year, and the surname or a name printed in that study's own title or abstract; T3 the year only. **Suggestion: T2**, because the writer is never shown the authors.

**O3. What the box shows when the check itself broke or was cut off by a restart** (the mock has only three steps). (a) the same row, Pending and Running outlined as passed, Done left grey, no step dark, and one line: "The check could not be completed. The answer is unchanged."; (b) no box at all, as on an older answer; (c) a fourth dark step "Not completed" in place of Done. **Suggestion: (a)** (no new CSS, and it does not hide the failure).

**O4. Answers stored before this stage** (no check was run). (a) no box; (b) the box with a line "Not checked: this answer was written before the check existed."; (c) run the check on old answers at start-up from the stored answer and the paper cache (cache rows older than 90 days are gone, so some would be checked against nothing). **Suggestion: (a).**

**O5. An answer with no structured numbers, or no trial rows.** (a) "No numbers to check" / "No trial names to check" in place of that half of the line; (b) the literal "0 of 0 numbers found". **Suggestion: (a).**

**O6. The cap on waiting:** the page gives up refreshing 60 s after the answer is stored and then shows O3's line. (a) 60 s; (b) 30 s; (c) 120 s. **Suggestion: (a).**

### Answered by the owner, 13 Nov

The owner said "Yes" to every suggestion, and added: "The answer should not change. Keep the answer, no need for rerun."

- **O1:** (b) the exact figure, or the abstract's figure rounded to the answer's decimals. Scope (i): the outcome numbers only.
- **O2:** T2: the year, and PubMed's first-author surname or a name printed in that study's own title or abstract.
- **O3:** (a) the same row with no dark step and one line: "The check could not be completed. The answer is unchanged."
- **O4:** (a) older answers with no check show no box.
- **O5:** (a) "No numbers to check" / "No trial names to check".
- **O6:** (a) the refresh stops at most 60 seconds after the answer is stored.
- **Added by the owner:** the stored answer is never changed by the check, and the check never starts or offers a re-run.

## 9. Not in this stage

- **Prose numbers:** figures inside `effect_text`, `key_results`, the bottom line, clinical practice, notable findings and the At a glance key numbers (`value_text`) are not checked. They are text; tracing them is the spec's later number work.
- **The participants count `n`** (unless O1 ii) and **`p` values**.
- **Naming the numbers or names that were not found**, marking them on the page, or any "not traced" mark.
- **Corrections:** no corrected view, no "Reload with corrections", no change to the stored answer, no number completion.
- **Any model call**, any prompt change, any change to the researcher or the writer.
- **Re-running the check** on old answers or after a restart.
- **A step line** for the check, and any change to Library or Activity.

## 10. Not checked

- The rule on any real answer (this workspace holds none, and none may enter the repo). How often Flash-Lite rounds, derives a percentage, or names a trial by acronym is unknown.
- Incisive's `trace_numbers` (not read; F4a is not a port).
- The box at widths under 390 px, and on the owner's phone browser.
- Whether `origin/main` moved since `3d35839` (no fetch run for this plan).
- Scroll position across the meta refresh on phone browsers (as F3c).
- The stale HTML comment at the top of the frozen mock (it still says "F3c mock v1"); copied unchanged because the mock is frozen.
