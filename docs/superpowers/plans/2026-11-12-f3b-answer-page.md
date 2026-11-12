# F3b: the answer page on the live writer (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax.

**Status (12 Nov 2026):** plan only, written by the Architect on branch `f3b-answer-page` (cut from `main` at `befa489`, nothing built). Before Task 1 the owner answers "Needs the owner" and signs off mock v6 on the Mock Board. The plan is written for the suggested option of each and says what changes for the others.

**Goal:** Keep everything that is live on `main` (the F3a writer, its stored answer, the look, Library, Activity) and carry the answer page the owner froze in the other F3 work (`f3-writer`, mock v5) onto it: "Why this conclusion" (every estimate with a 95% CI, beside outcome by outcome), "Counselling dashboard" (out of 100, beside compare the arms), the fold that keeps each pair level, and the strict drawing rules. It replaces the plain "long list" the run page shows today. Same answers, same calls, same database. No JavaScript.

**Architecture:** One new pure module, `src/pubmedgemini/answer_page.py`, ported from `f3-writer` with its behaviour unchanged, now reading main's `AnswerV3` (pydantic) instead of `f3-writer`'s `AnswerBody` (dataclasses). The run page (`templates/run.html`) draws its result when a stored answer exists, using main's `base.html`, header, menu, `static/app.css`, greys and the two fonts. The positions of the CI rows are numeric attributes of inline SVG lines, so the page needs no `style=` attribute and no per-page `<style>` block. The new rules go into `static/app.css`, in the agreed greys only.

**Spec:** `.10x/specs/2026-11-10-pubmedgemini-quick-design.md` §2a nos. 1 and 2, §4, §5, §6 (on `main`). Source of the page: branch `f3-writer` at `3eb82a1` (pushed, not merged, left as it is): `src/pubmedgemini/answer_page.py`, `templates/answer.html`, `tests/test_answer_page.py`, `tests/test_answer_route.py`, `docs/mocks/f3-answer-page-v5.html`, plan `docs/superpowers/plans/2026-11-12-f3-writer.md` ("Differences from Incisive" X1 to X40, "Deferred to F4"). Live writer: `main` at `befa489`, plan `docs/superpowers/plans/2026-11-11-f3a-writer.md`.

**Labels.** Each statement is **Fact** (with where it was checked), **Estimate**, or **Not checked**. Everything marked Fact was read or run by the Architect on 12 Nov 2026 on `main` `befa489` and `f3-writer` `3eb82a1`.

## Global Constraints

1. **Keep in step with `main`.** Before any branch is cut, before review, and before any merge: `git fetch origin main` and compare `FETCH_HEAD` with the branch's base (`git merge-base HEAD FETCH_HEAD`). Never branch from a local `main` that is behind `FETCH_HEAD`. The other chat may still change `main` (its own F3b list: the Introduction, the early summary, the trail under Details; Fact: `.10x/status.md`, `docs/build-log.md` 11-12 Nov "Deferred"). Before each review round and before asking for "merge it", bring `main` in with `git rebase FETCH_HEAD` (no force push of a branch someone else has pulled; if the branch was pushed, `git merge FETCH_HEAD` instead), re-run the full suite, and list in the pull request every file both sides touched (`run.html`, `web.py`, `app.css` and `test_ask_page.py` are the likely ones).
2. **This chat works only on `f3b-answer-page`.** `f3-writer` stays as it is (its duplicate writer is not carried). No push to `main`, no force push, nothing merged without the owner's own words "merge it".
3. **Nothing in the engine changes.** No change to the writer, its prompts, the researcher, the citation check, the stored JSON shape, the call budget (26 attempts a run, 21 free runs on a fresh day), the time limits (150 s writer gate, 420 s hard stop) or the migrations. The page reads what is stored. (Fact: `gemini/quota.py` `RUN_CALLS = 23`, `CALL_RESERVE = 3`, `RUN_ATTEMPTS`; `runner.py` `WRITER_MIN_SECONDS`.)
4. **Untrusted model text.** Every stored string is model text. Jinja autoescape only; no `|safe`, no `Markup` built from it. Attribute values computed by code are numbers formatted by one filter that accepts only `float` (`pct`); class values are fixed words from code (`hi`, `mo`, `lo`, `vl`, `on`, `off`, `A`, `B`).
5. **The look.** Greys only, the eight agreed values (Fact: `tests/test_look.py` `AGREED`, `test_css_uses_only_the_agreed_greys`); Hanken Grotesk for controls, labels, numbers and headings, Source Serif 4 for reading text; no `<style` in any page (Fact: `test_look.py` `test_base_links_stylesheet_and_has_no_inline_style`), no `style=` and no extra `<link>` on an answered run page (Fact: `tests/test_ask_page.py` `test_page_has_no_script_and_no_new_css`, lines 781-785), no `<script` (Fact: `test_look.py` `test_no_script_tags`).
6. **Never logged:** the question, any answer field, arm names, labels, numbers. The page module logs one line only: `answer_page kind=row_not_derived rows=<n>` (as on `f3-writer`).
7. **Keep out of the repo:** secrets, server names, the web address, real emails, question text, model replies, run outputs. The real live page the owner saved is used as a shape example only; none of its text is in this plan or in the mock.
8. **Tests first** for every task; `ruff check .` and the whole suite green with nothing skipped before each commit.

## Review Focus

1. **The drawing rules are unchanged.** `answer_page.py` on this branch must behave as on `f3-writer` for every input: the port changes types, not rules. The ported tests (108 in `test_answer_page.py`, including the random test with its own oracles) must pass unedited except for constructors.
2. **No inline style, no style block, no script** on the run page, with or without an answer; the SVG carries numbers only.
3. **Old answers still show.** Every answer already stored by F3a (written, unwritten with a reason, unreadable) renders without a 500.
4. **One bad row never fails the page** (the guard in `forest`, ported), and an answer that cannot be read still shows "The answer could not be shown." as today.
5. **Greys.** No colour carries meaning alone: certainty is written in words on every pill; the two arms are told apart by a solid against a hatched square and by name.
6. **Owner-chosen recovery rules (Task 6), if any, are allow-list rules with tests that show what they do not draw.**

## 1. What main stores for an answer, against what the page reads

All Fact unless marked. "main" is `src/pubmedgemini/writer/answer.py` at `befa489`; "f3w" is `src/pubmedgemini/answer_page.py` and `writer/answer.py` at `3eb82a1`.

**Storage.** Table `answers (run_id UUID PK, version SMALLINT = 3, body JSONB, created_at)` (`migrations/0005_answers.sql`), written with the run's final status in one transaction (`runs.py` `finish_with_answer`), read by `runs.get_run` into `RunView.answer` and parsed by `writer/answer.py` `read_body` into `AnswerV3` (`answer.py:478`, `:531`). The body is serde's form of Incisive's `AnswerV3`, Quick fields only.

| Page needs (f3w symbol) | f3w type | main field and type | Present on main? |
|---|---|---|---|
| outcome name, `rct_evidence`, `effect_text`, `why` (`outcome_cards`) | `Outcome` (dataclass, str) | `Outcome` (`answer.py:143`, pydantic) same names | Yes |
| outcome certainty (`pill_class`, sentences) | `str` | `Certainty` StrEnum (`answer.py:69`), values "High", "Moderate", "Low", "Very low" | Yes; a StrEnum is a `str`, so `_PILLS.get` and f-strings give the same text |
| evidence label (`EVIDENCE_LABELS.get(o.evidence_type)`) | `str` key, 6 kinds | `EvidenceType` StrEnum with `.label()` (`answer.py:84`), 9 kinds (adds `rct_meta_analysis`, `network_meta_analysis`, `observational`), set by code (`read.evidence_types`) | Yes; the port calls `o.evidence_type.label()` |
| number `label`, `value`, `unit`, `arm`, `ci_low`, `ci_high`, `p`, `pmid` | `Number` dataclass | `Number` (`answer.py:130`): `label: str`, `value: float`, `unit: str | None`, `arm: str | None`, `ci_low`, `ci_high: float | None`, `p: str | None`, `pmid: str` | Yes, field for field. A blank unit or arm is stored as `None` (`read.py:426`, `opt_text`); a non-finite number as `null` (`AnswerV3.to_body`, `_finite`) |
| trials row `pmid`, `label`, `design`, `n`, `setting`, `comparison`, `key_results`, `appraisal` (`study_of`, `comparison_of`, `arm_side`, trials list) | `Study` | `Study` (`answer.py:223`) same fields | Yes |
| bottom line `text`, `certainty`, `certainty_reason` | `BottomLine` | `BottomLine` | Yes |
| at a glance `verdict`, `key_numbers[label, value_text, pmid]`, `main_caveat` | `AtAGlance` | `AtAGlance`, `KeyNumber` | Yes |
| clinical practice four fields and `text` | `ClinicalPractice` | `ClinicalPractice` | Yes |
| conflicts, applicability, would_change, notable findings | same | same (`NotableFinding` also has `spans`, unused here) | Yes |
| guidelines | not in f3w | `GuidelineV3` list, with `pmid_removed` (Quick leaves it empty) | Extra on main; drawn as main's run page does today |
| references `n`, `pmid`, `title`, `journal`, `year`, `retracted` (`refs_of`, reference list) | `Reference` | `ReferenceV3` (`n` 1-based in selection order, built by code from the fetched records: `references_from`) | Yes |
| search trail (`trail`) | `SearchEntry` list in body | `AnswerV3.searches` and also `run_searches` rows (`RunView.searches`) | Yes; the page keeps using `RunView.searches`, as today |
| merge notes (`notes`) | `list[str]` | `AnswerV3.notes` | Yes |
| `empty_reason`, `answer is None` | same | same | Yes |
| citations in text: `split_citations(text, known)` | returns `("text", s)` / `("cite", ids)` tuples | returns `Text(text)` / `Cite(ids)` dataclasses (`answer.py:298`); the same rule (a bracket is a citation when one id is known or any id has 5+ bytes) | Same rule, different shape: one adapter in `cite_pieces` |
| `[not in the study list]` for an unknown PMID (X24, f3w D4: flag, never delete) | needed on f3w (no citation check there) | main's `check_citations` already removes unknown citations, numbers and trial rows (owner Q8, `answer.py:552`), and a selected study that cannot be fetched fails the run (D5) | The branch stays as a guard; on main it should never show |

**Citation form.** Stored text cites PMIDs, `[12345678]` form (Fact: the live page prints them so, `run.html` draws the raw text). The page turns each known PMID into its reference number with a link, `[<a href="#ref-1">1</a>]`, as `f3w` `cite_pieces` does. Nothing stored changes.

**Is the unit stored for ratios?** The field exists (`Number.unit`), and the writer's instruction asks only for "unit;" with no list of values (Fact: `writer/prompts.py:137`, Incisive's words, word for word). On the real live answer the owner saved, the three hazard-ratio lines print with no unit (Fact: the page prints `f" {n.unit}"` only when a unit is set, `web.py` `_number_line`), so Flash-Lite stored `unit` as null or blank for them, and put the measure in the label ("Hazard ratio for ...", "Stratified hazard ratio for ..."). Percentages came with unit `%`.

**What the strict rule does with them.** `f3w` `is_ratio` needs the unit to be exactly RR, OR or HR. Without it a hazard ratio is not a forest row: it is listed under its outcome as text, "Not drawn: unit not RR, OR or HR." (`listed_problem`, `_looks_ratio`), and when no number in the answer is a ratio or a difference with both bounds, the "Every estimate with a 95% CI" card is not drawn at all: the "Why this conclusion" pair shows "Outcome by outcome" alone at full width (`_pair`: an empty panel draws the other card alone). Nothing is wrong or hidden; the CI picture is simply absent. Options for the owner (N5): R0 keep the rule; R1 add an allow-list rule for a ratio named at the start of the label; R2 change the writer's instruction (an engine change). Suggestion R1.

**Existing live answers in the database** keep working: they are read with the same `read_body` the run page uses today; the page is computed at view time, so whatever rules the owner picks (R1, S1, S2) apply to old answers too. An unreadable body still shows "The answer could not be shown." and logs only its kind (Fact: `web.py` `_shown_answer`).

**What the live stored answer lacks:** nothing the page needs. The only gap is in what Flash-Lite writes into it (no unit on ratios, arm texts that do not repeat the comparison's words), not in what is stored.

## 2. Paper dry-run on the real live answer (first real evidence)

The Architect ran `f3w`'s own `answer_page.py`, unchanged, on the numbers, arms and comparison lines of the live page the owner saved (parsed from its printed lines; Fact, run 12 Nov 2026 in a scratch folder, nothing kept in the repo). The answer has 7 outcomes, 17 numbers and 7 trial rows.

| Kind | Count | What the page would show |
|---|---|---|
| Ratio rows (hazard ratios, no unit) | 3 | **0 drawn.** All 3 listed under their outcome, "Not drawn: unit not RR, OR or HR." The CI card is absent; "Outcome by outcome" stands alone at full width |
| Two-arm percent pairs (each arm a plain name) | 7 | All 7 in "Compare the arms", each value after its arm name |
| ... of which squares in "Out of 100" | **3** | the three pairs whose arm texts repeat words of one side of the trial's comparison line each |
| ... of which text only, "Not drawn as squares: its arms do not match the study's comparison." | 4 | see below |

Why the 4 pairs fall to text (Fact, `arm_side` returned None for both arms):
- **"Experimental group" / "Standard of care group"** (1 pair): the trial's comparison line names the two treatments and contains neither "experimental" nor "standard".
- **Two abbreviated arm names** (2 pairs, one trial; shown here in made-up form as "DAS group" / "SA group"): abbreviations that are not in the comparison line, which spells the treatments out.
- **"Group A" / "Group B"** (1 pair): the comparison line does name "(group A)" and "(group B)", but one-letter words are ignored by Incisive's `words` and "group" is on both sides, so neither arm scores.

The 3 pairs that get squares are genuine: their arm texts share a word with exactly one side (for example a drug name on the left, "standard" on the right where the comparison itself says "standard").

What simple, safe rules would recover (Fact, the same run with each rule added):

| Rule | Recovered here | Squares | Risk |
|---|---|---|---|
| **R1** A ratio whose unit is blank and whose label starts with "hazard ratio", "odds ratio", "risk ratio" or "relative risk", optionally after "stratified", "adjusted" or "pooled" (narrowed after the review: §4a A1) | 2 of 3 ratio rows drawn (the third has a one-sided interval, "(CI to ...)", and stays text, "Not drawn: no interval.") | — | Low: the draw checks still apply (above 0, inside its interval, width); a label that also names a difference is refused (F4 rule) |
| **S1** Group letters: an arm "Group X" matches the side of the comparison that contains "(group X)" and the other side does not | +1 pair | 4 | Low: the comparison itself names the letter on one side |
| **S2** "experimental" / "intervention" in the arm match the first side, "standard" / "control" / "standard of care" the second, **only when the comparison text itself contains that word on that side and not on the other** | +0 here | 4 (with S1) | Low; it did not help on this answer because the comparison did not use those words |
| **S3** As S2 but without needing the word in the comparison (first side taken as the intervention, Incisive's `arm_lines` assumption) | +1 | 5 (with S1) | Medium: rests on the writer always putting the intervention first; not suggested before F4's number check |
| none | the two abbreviated pairs | — | No safe rule: the abbreviations are not in the comparison |

Honest picture: on this real answer, as frozen, the page draws **no CI line and 3 of 7 pairs as squares**; everything else is shown as text with its reason. With R1 and S1 it draws **2 CI lines and 4 of 7 pairs**. One real answer is small evidence (Estimate: other topics will differ); the manual sign-off below asks the owner to run the four benchmark questions and count again.

One more thing seen on this answer (Fact): in one pair each arm carries its own p value against a historical control, not a p between the arms; "Compare the arms" prints both as written (in the form "p x · p y"). The p values are the writer's; the page does not interpret them. Recorded for F4.

## 3. The page

### Where the panels go (N1)

- **A. Replace the long list on the run page (suggested).** `/runs/{id}` stays the one address (Library links to it). While a run works, the page is as today (question, steps, refresh every 5 s). When a stored answer exists, the page is laid out as the frozen mock, in Incisive's order, inside `base.html`:
  1. the question as `<h1>`, a small line "Done · Quick · Decision support. The clinician decides." (status in words);
  2. **Steps**, folded (`<details>`, closed), in a small card (spec §6 item 3 puts the progress steps folded under the results line);
  3. the outcome line and "Run again" when the run is stopped, failed or partial (unchanged);
  4. At a glance (card), Bottom line with its certainty pill and the four Clinical practice lines (card);
  5. **Why this conclusion**: the CI card beside Outcome by outcome; **Counselling dashboard**: Out of 100 beside Compare the arms;
  6. as cards, each only when it has text: Conflicting evidence, Applicability, What would change this answer, Guidelines (main has them), The trials, Other notable findings (with the merge notes), Search trail, References;
  7. folded at the end, each a `<details>` in a card: **Selected studies, with their abstracts**, **Considered, not selected**, **Possible contradictions** (they are the record of the search; the same content as today);
  8. the model line and the privacy line.
  A run with no written answer shows its reason and the references (as today), then the folded studies.
- **B. A separate answer page** `/runs/{id}/answer`, linked from the run page ("Read the answer"), as `f3-writer` built it; the run page keeps its long list. Less change to main's run-page tests, but the owner still meets the long list first, and two pages show the same answer.
- **Suggestion: A.** It is what the owner asked for ("the idea was to get the look you have written") and keeps one page per question.

### Width at 700 px and above (N2)

`main` is 42 rem wide on every page (Fact: `app.css` `main`, `.site-header-inner`, `.site-header nav`). The pairs need about 1,100 px (the mock). Proposal: `base.html` gets an empty `{% block main_class %}{% endblock %}` inside `<main{% ... %}>`, which only the answered run page fills with `class="wide"`; `app.css` adds `@media (min-width:700px){main.wide{max-width:68rem}}`. Other pages are untouched (a test checks their `<main>` has no class). The header and menu stay 42 rem (mock v6 shows this). Option: widen the header with it. **Suggestion: main only**; the owner judges it on v6.

### The look rules this has to pass (Fact)

- No `<style` in any page and no `style=` on an answered run page (tests named in Global Constraint 5). `f3-writer`'s `answer.html` uses both: a per-page `<style>` block (the mock's, byte for byte) and `style="left:..%;width:..%"` on every drawn row, plus `style="margin-top:..px"` on cards. **So the template is rewritten; the derivation is not.**
- Colours: only the eight agreed greys in `app.css` (`test_css_has_only_greys`, `test_css_uses_only_the_agreed_greys`).
- Classes: `test_search_page` requires class values of letters, digits, spaces and hyphens on the results page; the answer page follows the same rule (empty `class=""` on off squares becomes `class="off"`).
- `.row` already means "links in here get a 44 px target" in `app.css` and `test_look.py` `test_only_marked_links_get_the_big_target`; the mock's compare rows are renamed `.cmp` so they never get it.

### The CI rows without inline style (inline SVG)

Each drawn row is one `<svg class="track" role="img" aria-label="...">` with no `viewBox`, so `x` values in percent are percent of the row's width and `y` values are pixels:

```html
<svg class="track" role="img" aria-label="Risk ratio 0.38, interval 0.28 to 0.51, below 1">
<line class="base" x1="0" x2="100%" y1="21.5" y2="21.5"/>
<line class="null" x1="76.9%" x2="76.9%" y1="0" y2="22"/>
<line class="span" x1="34.4%" x2="54.4%" y1="11" y2="11"/>
<line class="est" x1="44.6%" x2="44.6%" y1="3" y2="19"/>
<text x="0" y="35">0.1</text><text class="mid" x="76.9%" y="35">1</text><text class="end" x="100%" y="35">2</text>
</svg>
```

`app.css`: `.track line{shape-rendering:crispEdges}`, `.span` stroke `#333333` width 2 (the mock's thin line), `.est` stroke `#333333` width 5, from 3 to 19 px (the mock's 5 × 16 px bar), `.null` stroke `#666666` width 1 with `stroke-dasharray:3 3` (the dashed "no difference" mark), `.base` `#BDBDBD` (the mock's track rule), `text` Hanken Grotesk 11 px `#666666`, `.mid` centred, `.end` right-aligned. The numbers come from `ForestRow.left`, `left + width`, `est`, `null` and `Axis.*_text`, through `pct`. No JavaScript. The labels are inside the SVG, so they need no inline style either.

Evidence (Fact): mock v6 uses exactly this; rendered in Chromium (Playwright from `/opt/npm-tools`) at 1,100 px, 1x and 2x, and at 390 px, 2x: the line, bar, dashed mark and the three axis labels match the mock v5 positions (34.4 / 20 / 44.6 % on the 0.1 to 2 axis; the MD row on −8 to +2 with 0 at 80 %); edges are crisp at 1x; no sideways scroll at 390 px (scroll width = client width); the two cards of each pair are equal height at 1,100 px (704/704 and 593/593 px).

**Out of 100.** 100 plain `<i>` elements per arm, `class="on"` for the first *count*, `class="off"` for the rest, in a CSS grid of 10 (the mock's). No inline style is needed.

### Visual differences between the frozen mock v5 and the live look (N3, N4, N7)

The mock is frozen for layout and drawing; colours and fonts follow the live look.

| # | Mock v5 | Live look / v6 | Decision |
|---|---|---|---|
| V1 | warm ground `#f4f3ef`, near-black text, blue links | `#F4F4F4` ground, `#333333` text, grey underlined links | follows the look (decided, §2a no. 2) |
| V2 | system sans 16 px, monospace numbers | Hanken Grotesk 15 px for labels and numbers (tabular figures), Source Serif 4 for reading text | follows the look |
| V3 | arm A blue, arm B orange (squares and big numbers) | **arm A solid dark grey squares, arm B hatched dark grey squares**, a small key square before each "of 100 · arm", both big numbers `#333333` | **N3** owner |
| V4 | certainty pills coloured (red, amber, blue, green) | greys with the word always printed: High filled `#333333` white text; Moderate filled `#DDDDDD`; Low white with a solid outline; Very low white with a dashed outline | **N4** owner |
| V5 | light and dark mode by system setting | light only (main has no dark mode; `app.css` has no `prefers-color-scheme`) | **N7** owner |
| V6 | body 1,100 px; cards radius 12 px, rule `#dcd9d2` | `main.wide` 68 rem; cards as `app.css` (radius 10 px, `#BDBDBD`); inner boxes `#DDDDDD` | follows the look |
| V7 | no header or menu; "Home · Steps and studies" links | header, menu (Library current) from `base.html`; steps folded on the page | follows the look and N1 |
| V8 | inline `style` and a `<style>` block | `app.css` rules and SVG attributes | required by main's tests |
| V9 | compare rows `.row`, off squares `class=""` | `.cmp`, `class="off"` | required by main's tests |
| V10 | — | "Not drawn ..." lines in grey italics, so text fallbacks read as notes | new, small |

Spec §6 on main already says where Incisive uses colour this page uses "words, weight and pattern: filled or outlined labels, solid against hatched bars" (Fact). V3 and V4 as proposed follow that sentence; keeping two arm colours would need the owner to change §2a no. 2 and `test_look.py` `AGREED`.

### Mock v6 for the Mock Board

File: `docs/mocks/f3b-answer-page-v6.html` (on this branch; self-contained: the two fonts embedded as data, no script, no inline style attributes, only the eight greys; example numbers are mock v5's and are marked as examples; no owner text and no text from a real run). The Coordinator posts it. It shows, in order: the header and menu; an "example only" note; an example question; Steps folded; At a glance; Bottom line with the Moderate pill and the four practice lines; Why this conclusion (four ratio rows and one difference row as SVG, beside six outcomes of which two fold behind "Show 2 more outcomes"; one outcome body shows a "Not drawn: no interval." line); Counselling dashboard (two out-of-100 items, solid against hatched squares, beside Compare the arms with a text-only row "Not drawn as squares: its arms do not match the study's comparison."); the cards; References with a "Retracted" tag; the three folded study lists; the two closing lines. `docs/mocks/f3-answer-page-v5.html` is copied unchanged from `f3-writer` as the frozen reference.

What the owner checks on v6 (phone and computer): the drawing matches v5; greys read clearly; solid against hatched is easy to tell apart; the header width against the wider page; the order and folds.

## 4. Drawing rules: ported unchanged

`answer_page.py` is copied from `f3-writer` `3eb82a1` and only these change (Fact: every `f3w` symbol was read; 108 tests in `test_answer_page.py`, 31 in `test_answer_route.py`):

| f3w | main | Change |
|---|---|---|
| `from pubmedgemini.writer.answer import EVIDENCE_LABELS, AnswerBody, Number, Outcome, Study, WrittenAnswer, split_citations` | `AnswerV3, EvidenceType, Number, Outcome, Study, WrittenAnswer, split_citations, Text, Cite` | names |
| `split_citations(...)` yields `(kind, v)` | yields `Text` / `Cite` | `cite_pieces` maps `Text(t)` to `("text", t)` and `Cite(ids)` to the same known/unknown split |
| `EVIDENCE_LABELS.get(o.evidence_type)` | `o.evidence_type.label()` | one line |
| `_scrub` walks dataclasses with `dataclasses.replace` | pydantic models | walk `BaseModel` fields with `model_copy(update=...)`; **an `Enum` is returned as it is** (a StrEnum is a `str`, and scrubbing it would turn `Certainty`/`EvidenceType` into plain strings and break `.label()`) |
| `model_text_cleaned(body)` uses `dataclasses.replace(body, ...)` | `AnswerV3` | `body.model_copy(update={...})` |
| `from pubmedgemini.lit.text import drop_invisible` | not on main | carried (Task 1) |
| `body.references` of `Reference` | `ReferenceV3` | none (same field names) |
| guidelines not drawn | main stores them | `answer_view` adds `guidelines` as the run page shows them today |

Every rule stays: `is_ratio` (unit exactly RR, OR, HR), `is_difference`, `difference_measure`, `label_measure`, `arm_kind`, `DIFF_MARKERS`, `_COMPARISON_WORDS`, `_COMPARISON_MORE`, `draw_problem`, `listed_problem`, `forest` with its per-row guard, `ratio_axis` (ladder 0.05 to 50), `diff_axis`, `out_of_100_problem` (two sides, opposite matches, no difference or change in the label, not both zero), `compare`, `fold_pair` with the pixel weights, `answer_view`. The X1 to X40 rows of the `f3-writer` plan that concern the page (X13 to X16, X19 to X24, X28 to X40) hold as written; X24's `[not in the study list]` remains as a guard although main's citation check removes such citations before storing.

**Deferred to F4** (unchanged from the `f3-writer` plan, items 1 to 8): negated arms (closed for the squares by §4a N1); time points inside arm texts; unit HR on something that is not a ratio; same label, different measures; the squares rule is strict (now with real evidence, §2); which arm minus which on a difference; compare values print no CI; the cosmetic three. Added from §2: per-arm p values against a historical control shown as written.

## 4a. Differences after the independent review (12 Nov 2026)

What changed on `f3b-answer-page` after the Independent Code Reviewer's findings, against this plan as first written. Principle (owner-approved): draw by allow-list; anything doubtful is plain text with a true reason. All Fact (tests and commits on the branch).

| # | Plan as written | Built after the review | Why |
|---|---|---|---|
| A1 | R1: a blank unit and a label matching `^(?:(?:stratified\|adjusted\|pooled)\s+)?(hazard ratio\|odds ratio\|risk ratio\|relative risk)\b` | R1 is a strict shape (`label_ratio`). On the cleaned, case-folded label: it **starts** with at most one qualifier (stratified, adjusted, unadjusted, pooled, crude, multivariable, multivariate) and one ratio name (hazard ratio, odds ratio, risk ratio, relative risk, rate ratio); the name is followed by the end of the label or a separator (",", ":", "(", "—", "–", each after at most one space, or " for ", " of ", " in ", " at "); **and** the whole label holds, as a token, no change word (reduction, reduced, increase, increased, decrease, decreased, change, changed, improvement, gain, excess, delta, Δ, ∆, and plurals), no difference word (difference, diff, minus, MD, SMD, WMD), no negation or significance word (no, not, non, without, none, neither, nor, NS, n.s., nonsignificant, insignificant, NR, NE, NA, n/a; so "not reported", "not estimable", "not reached", "non-significant"), no measure abbreviation (HR, RR, IRR, aHR, aOR, ARR, RRR, NNT, NNH; OR in capitals); **and** "ratio" / "relative risk" occur once in all (no second measure). A blank-unit label that names a ratio anywhere but fails is also never a difference row (`r1_refused`), whatever its arm marker: it is listed, "Not drawn: unit not RR, OR or HR.", as before R1. A unit given as RR, OR or HR decides as before. Every value check is unchanged | R1 drew "Relative risk reduction ..." and "Relative risk increase ..." as risk ratios, and "Hazard ratio not reported; ...", "Odds ratio difference", "Risk ratio difference in ...", "Hazard ratio change from baseline" as ratios (review A1/B1) |
| W1 | A listed number with one interval end: "(0.5 to not given)" (the frozen derivation) | "(CI from 0.5)" / "(CI to 1.2)", as mock v6 and the live page on `main` print it | Coordinator ruling; listed text only, nothing drawn changes |
| D1 | Out of 100 kept the writer's order of the two arms when S1 or S2 placed them | The arm on the comparison's first side is shown first (solid squares), the second side second (hatched), whether the side came from the word score, S1 or S2; each name keeps its own count | "Group B" could be drawn solid when the writer listed it first (review D1) |
| F1 | Test and plan strings in the shape of the real answer | Made-up generic treatments ("Drug A then surgery (group A) vs surgery then drug A (group B)", "DAS group" / "SA group", "Example outcome at 3 years"); the plan describes the real answer's pairs without their wording | The repo is public and must not echo the owner's run |
| L1 | Leak tests for the page's own line | Also: a stored body that cannot be read logs exactly "run page answer kind=invalid", nothing of the body or the exception (mutation-checked) | Review: a mutation logging the exception's repr was not caught |
| N1 | Squares whenever the two arm texts match opposite sides (word score, S1, S2) | No squares when either side of the study's comparison or either arm text is worded with a negation: a whole word no, not, non, none, neither, nor, without, or a word "non" + a letter ("non-standard", "nonoperative"); "sham" and "placebo" are not negations (`worded_with_negation`). The pair stays a named text row in "Compare the arms", "Not drawn as squares: the comparison or an arm is worded with a negation." Checked after "two sides" and before the side match, so it applies to the word score, S1 and S2 alike. Closes item 1 of the `f3-writer` plan's "Deferred to F4" (negated arms) for the squares only; ratio and difference rows are unchanged | Coordinator ruling 1a: a word after "no" / "non" names the opposite of that side, so "Extended surgery vs no control arm" drew "Control group" as a side and "Experimental drug vs non-standard dosing schedule" drew "Standard group" (the reviewer's generator) |

The random property test states the strict R1 rule again, separately, as its oracle, carries the reviewer's labels, checks that a refused label is never a row of any kind, that Out of 100 shows the first side first and that no squares come from a negated side or arm (its own negation list, N1), and runs 4,000 cases (the S1 exercise floor fell to its limit at 3,000 when the label list grew).

## 5. Other things on `f3-writer` that main lacks

| Item on f3-writer | On main (Fact) | Carry? |
|---|---|---|
| NUL in the writer's reply removed before Postgres (review M4) | Done earlier and wider: `gemini/client.py:453` passes every string of every Gemini reply through `lit/text.py` `clean_json` (NUL removed, lone surrogates replaced) | **No** |
| Bidi controls and zero-width characters removed before save (`save_answer`) and again in the view (`model_text_cleaned`), and ignored in every classification key (X34) | Not on main: `lit/text.py` has no `INVISIBLE`/`drop_invisible`; nothing strips them | **Yes, in the view and the keys** (Tasks 1 and 2). Before save is an engine/data change: **N10** |
| One bad forest row never fails the page (`forest` guard, re-check M-B) and one pair's arithmetic never fails it (`out_of_100_problem`) | Not applicable before (plain list) | **Yes** (inside the port) |
| The random property test with its own oracles (fourth check F6) | — | **Yes** (Task 2) |
| Step lines from several writer threads all kept (review L10) | main has its own step lock (`runs.py` `_STEP_LOCK`) and stores the closing line with the answer | **No** (the writer is main's) |
| Shared structured reading, duplicate writer, its prompts, `runner` changes, quota numbers | main has its own (F3a) | **No** (dropped, as the owner said) |

**Owner decisions of this chat, checked on main:** search checklist present (Fact: `researcher/prompts.py:84` `SEARCH_CHECKLIST`); patient-details check present (Fact: `deid.py:135` `check_question`); "Run again" on stopped, failed, partial (Fact: `run.html`); per-run call cap present (Fact: `quota.py` `RUN_ATTEMPTS = 23 + 3`, owner D2); no JavaScript in code (Fact: no `<script` in any template; `test_no_script_tags`). **One contradiction, in words only:** main's spec §4 says "From F3 the answer page carries a small script of its own, served by the app, for its filters, tabs, folds and study sheet", and §6 lists an Introduction, a results card, a study sheet and "Copy as text"; the owner's frozen mock and his "no JavaScript" ruling in this chat have none of these (folds are `<details>`; X17, X18 accepted on 12 Nov). **N9.**

## 6. Calls, time, leaks, migration

- **Calls and the free day:** no change (the page makes no call). Fact: no code path in this plan touches `gemini/`.
- **Time limits:** no change.
- **Leak tests:** one new log line, `answer_page kind=row_not_derived rows=\d+`, added to `tests/test_logging.py` `APP_LINE_PATTERNS`; one new leak test renders a run page whose answer carries a marker in every field and asserts no marker reaches the log.
- **Migration:** none. No column, no table.

## File Structure

| File | Change |
|---|---|
| `src/pubmedgemini/lit/text.py` | add `INVISIBLE`, `drop_invisible` (from f3w) |
| `src/pubmedgemini/answer_page.py` | new: the port |
| `src/pubmedgemini/web.py` | `run_page` builds `answer_view(...)`; `pct` filter; `_shown_answer`, `_number_line`, `_num`, `_labelled`, `_reference_line` removed when unused |
| `src/pubmedgemini/templates/base.html` | `main_class` block |
| `src/pubmedgemini/templates/run.html` | the answer layout (N1 A); macros in `templates/_answer.html` |
| `src/pubmedgemini/static/app.css` | the answer page rules (greys only) |
| `tests/test_text.py` (or the existing lit text tests) | `drop_invisible` |
| `tests/test_answer_page.py`, `tests/fixtures/writer/whole.json` | ported (constructors only) |
| `tests/test_run_answer_layout.py` | new: from `test_answer_route.py`, on `/runs/{id}` |
| `tests/test_ask_page.py`, `tests/test_look.py`, `tests/test_logging.py` | deliberate edits listed per task |
| `docs/change-log.md`, `docs/build-log.md`, `.10x/status.md`, spec §4 and §6 | records |

## Tasks

### Task 0: Sync

- [ ] `git fetch origin main`; if `FETCH_HEAD` ≠ `befa489`, rebase `f3b-answer-page` on it (Global Constraint 1), read what changed in `run.html`, `web.py`, `app.css`, `writer/answer.py`, and stop to tell the Coordinator if any of them changed.
- [ ] Baseline: `. .venv/bin/activate && pip install -q -r requirements.lock; ruff check .; TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/postgres pytest -q -rs`. Expected on `befa489`: **1343 passed, 0 skipped** (Fact, 12 Nov 2026).

### Task 1: Invisible characters

**Files:** `src/pubmedgemini/lit/text.py`; test `tests/test_lit_text.py` (new) or the existing text tests.

**Interface:** `INVISIBLE: re.Pattern[str]` = `[‪-‮⁦-⁩‎‏؜​-‍⁠﻿]`; `def drop_invisible(s: str) -> str`.

- [ ] Tests first: `test_drop_invisible_removes_bidi_and_zero_width` (each code point in the class removed; "TEP​" gives "TEP"; "‮abc" gives "abc"); `test_drop_invisible_keeps_everything_else` (U+00A0, emoji, "é", "\n" unchanged); `test_clean_text_is_unchanged` (NUL and surrogate behaviour as before).
- [ ] Implement (copy from f3w). `ruff check . && pytest -q tests/test_lit_*.py`.
- [ ] Commit `feat: drop bidi controls and zero-width characters (for the answer page)`.

### Task 2: The page's derivation, ported

**Files:** create `src/pubmedgemini/answer_page.py`, `tests/test_answer_page.py`, `tests/fixtures/writer/whole.json` (the hernia fixture from f3w; Fact: main's `writer/read.py` `into_answer` reads it: 6 outcomes, 16 numbers, 8 trial rows, 0 labels defaulted).

**Interfaces (exact):**
```python
def answer_view(body: AnswerV3) -> dict[str, Any]
def model_text_cleaned(body: AnswerV3) -> AnswerV3
def refs_of(body: AnswerV3) -> dict[str, int]
def cite_pieces(text: str, refs: Mapping[str, int]) -> list[Piece]
def forest(w: WrittenAnswer, refs: Mapping[str, int]) -> Forest
def out_of_100(w: WrittenAnswer, refs: Mapping[str, int]) -> list[OutItem]
def compare(w: WrittenAnswer, refs: Mapping[str, int]) -> list[CompareGroup]
def outcome_cards(w: WrittenAnswer, refs: Mapping[str, int]) -> list[OutcomeCard]
def fold_pair(left: Panel, right: Panel) -> tuple[int, int]
def fmt_pct(x: float) -> str
def pill_class(certainty: str) -> str
```
All other f3w names keep their signatures with `AnswerBody` read as `AnswerV3`. `answer_view` adds `"guidelines": [(body, statement_pieces, year, pmid, pmid_removed)]` (only when the list is not empty).

- [ ] Tests first: copy `f3-writer:tests/test_answer_page.py`; change only the helpers `num`, `ratio`, `outcome`, `study`, `written`, `hernia` to build main's pydantic models (keyword arguments; `Certainty(...)`, `EvidenceType(...)`); no assertion changes. Add:
  - `test_scrub_keeps_enums` — `model_text_cleaned` on an answer with `Certainty.LOW` and `EvidenceType.RCT` keeps both types and `outcome_cards(...)[0].evidence_label == "RCTs"`.
  - `test_cite_pieces_reads_mains_segments` — `cite_pieces("A [33763744; 1] b.", {"33763744": 1}) == [("text", "A "), ("cite", [1]), ("unknown", 1), ("text", " b.")]` (a bracket with one known id is a citation as a whole, so the short "1" counts as unknown: the same result as f3w's rule on the same input), and `cite_pieces("LOS 2 [1, 3] days", {})` is one text piece.
  - `test_old_live_answer_shapes_render` — a body with `unit=None` on three "Hazard ratio for ..." numbers: `forest(...).ratios == []`, the three appear in `listed_numbers` with "unit not RR, OR or HR", and `answer_view` has one pair card for "Why this conclusion" (`left is None`).
  - `test_guidelines_in_the_view` — present with `pmid_removed` flag.
- [ ] `pytest -q tests/test_answer_page.py` FAIL, then implement (copy f3w, apply §4's table), then PASS, including `test_random_answers_keep_the_pages_promises`.
- [ ] Commit `feat: the answer page's derivation, ported unchanged onto the live answer`.

### Task 3: The stylesheet

**Files:** `src/pubmedgemini/static/app.css`; `tests/test_look.py`.

- [ ] Tests first: `test_answer_rules_present` (`.two`, `@media (min-width:700px)` with `minmax(0,1fr) minmax(0,1fr)`, `main.wide`, `svg.track`, `.track .null` with `stroke-dasharray`, `.dots`, `details.more`, `.pill.hi`, `.pill.vl` with `dashed`); `test_no_row_class_rules_added` (`.cmp` used, no new `.row` rule); the existing greys and font tests unchanged.
- [ ] Add the "new for the answer page" block of mock v6, word for word.
- [ ] Commit `feat: the answer page's rules in the shared stylesheet, greys only`.

### Task 4: The run page draws the answer (N1 A)

**Files:** `templates/base.html`, `templates/run.html`, new `templates/_answer.html` (macros), `web.py`; tests `tests/test_run_answer_layout.py` (new), `tests/test_ask_page.py` (deliberate edits).

**Interfaces:** `web.py`: `def _pct(x: object) -> str` (raises `TypeError` unless `float`/`int`, returns `fmt_pct(float(x))`; registered as filter `pct`); `run_page` context gains `"view": answer_view(read_body(v.answer))` or `{"unreadable": True}` (logged `run page answer kind=<kind>` as today), and `"wide": view is not None`.

- [ ] Tests first (`test_run_answer_layout.py`, from `test_answer_route.py`, on `/runs/{id}`):
  - `test_blocks_in_the_mocks_order` — indexes of "Bottom line", "Why this conclusion", "Every estimate with a 95% CI", "Outcome by outcome", "Counselling dashboard", "Out of 100 people like those in the trials", "Compare the arms", "The trials", "Search trail", "References", "Selected studies" increase; `page.count('class="two"') == 2`.
  - `test_no_style_no_script_no_new_link` — `"style=" not in page`, `"<style" not in page`, `"<script" not in page.lower()`, no `\son[a-z]+=`, `<link` count equal to a run without an answer.
  - `test_svg_rows_carry_code_numbers_only` — every `x1|x2` matches `^\d+(\.\d)?%$|^0$|^100%$`; the hernia first row has `x1="34.4%" x2="54.4%"` and the mark `x1="76.9%"`.
  - `test_hundred_squares_each` — each `.dots` has 100 `<i class="on|off">`, the first grid 9 `on`.
  - `test_outcomes_closed_and_fold` — 6 `<details class="oc">`, none `open`; "Show 2 more outcomes".
  - `test_steps_folded` and `test_studies_folded` — `<details class="fold">` holding the steps and the selected studies.
  - `test_main_is_wide_only_here` — answered run page `<main class="wide">`; home, library, activity, a working run page: `<main>`.
  - `test_model_text_escaped_everywhere` — hostile outcome, arm, label, comparison, reference title come out escaped; `<details` count equals code's.
  - `test_unreadable_answer_still_plain_line`, `test_unwritten_answer_shows_reason_and_references`, `test_no_numbers_note`, `test_run_again_unchanged`.
  - `test_old_run_without_answer_unchanged` (kept).
- [ ] Deliberate edits in `tests/test_ask_page.py` (each listed in the commit message): `test_answer_sections_in_order` (new headings and order), `test_notes_show_when_present` (notes under "Other notable findings"), `test_page_has_no_script_and_no_new_css` (kept, still passes), `test_unwritten_answer_shows_its_reason_and_references`, `test_a_pmid_that_is_not_ascii_digits_gets_no_link`, `test_a_lone_ci_end_is_shown` (now "(CI to x)" in the listed line), `test_numbers_print_as_plain_decimals` and `test_a_shown_number_is_the_stored_value_with_no_exponent` (move to `fmt_value` tests), `test_guidelines_show_with_each_field_and_the_flag`, `test_no_guidelines_section_when_the_list_is_empty`. Assertions on upstream escaping stay.
- [ ] Implement; `ruff check . && pytest -q`. All pass, nothing skipped.
- [ ] Commit `feat: the run page shows the answer as the frozen mock, in the live look`.

### Task 5: Logs and leaks

**Files:** `tests/test_logging.py`.
- [ ] Tests first: add `r"answer_page kind=row_not_derived rows=\d+"` to `APP_LINE_PATTERNS`; `test_answer_page_logs_nothing_of_the_answer` (a stored answer with markers in outcome, arm, label, comparison, effect text, reference title; GET the run page at DEBUG; no marker in `caplog.text`; a forced row failure logs exactly the one fixed line).
- [ ] Commit `test: the answer page logs a fixed kind and a count only`.

### Task 6: Recovery rules the owner chose (only N5/N6 answers; skip if R0/none)

**Files:** `answer_page.py`, `tests/test_answer_page.py`.
- R1: `def ratio_unit(n: Number) -> str | None` returns "RR"/"OR"/"HR" from the unit as today, or, when the unit is blank, from a label matching `^(?:(?:stratified|adjusted|pooled)\s+)?(hazard ratio|odds ratio|risk ratio|relative risk)\b` (case-insensitive, on `_display_key`); `is_ratio` and `_RATIO_NAMES` use it. **Narrowed after the review to the strict shape in §4a (A1).** Tests: `test_ratio_named_at_label_start_is_drawn` (three label forms), `test_ratio_named_later_is_not` ("Recurrence, hazard ratio unclear"), `test_ratio_label_with_a_difference_still_refused`, `test_unit_wins_over_label`, `test_one_sided_ratio_listed_no_interval`.
- S1: in `arm_side`, an arm whose key is exactly `group <letter>` matches the side whose key contains `(group <letter>)` when the other side does not. Tests: hit, both sides naming it (no match), letter absent (no match).
- S2: in `arm_side`, "experimental"/"intervention" → side 0 and "standard of care"/"standard"/"control" → side 1, only when that word is a whole word on that side of the comparison and not on the other; an arm holding words of both kinds does not match. Tests: hit, word absent from the comparison (no match), word on both sides (no match).
- The random test's oracles gain the same rules (its promises unchanged).
- [ ] Commit one per rule: `feat: <rule> (owner decision N5/N6)`.

### Task 7: Records and pull request

- [ ] Spec §4 (the script sentence replaced by "no JavaScript; folds are `<details>`", per N9), §6 (the page as built, pointing at mock v6), change log entry (plain words: what the owner sees), build log, `.10x/status.md`.
- [ ] Global Constraint 1 sync; full suite; push `f3b-answer-page`; open the pull request "F3b: the answer page on the live writer" listing files both chats touched. Stop for review and the owner's "merge it".

Size (Estimate): the port is about 1,600 lines of Python reused nearly as is (about 30 lines changed) and about 1,800 lines of tests reused with helper changes; the template (about 250 lines) and about 120 lines of CSS are rewritten; about 10 existing run-page tests are edited deliberately.

## Manual sign-off (owner, after the deploy)

1. Open three answers already in Library (from before the change). Each shows the new layout; nothing is missing compared with yesterday (steps and abstracts are in the folded parts at the bottom or top).
2. Ask the four benchmark questions. On each answer, on the computer and on the phone:
   - two columns on the computer, one on the phone, no sideways scrolling;
   - each CI row: the short bar inside the thin line, the dashed mark at 1 or 0, three axis labels; or a "Not drawn: ..." line that says why;
   - the solid and hatched squares are easy to tell apart and the counts match the percentages;
   - count, per answer: CI rows drawn / listed, pairs with squares / text only. Send the counts (they tell us how strict the rules are on real output; §2 is one answer only).
   - every `[n]` jumps to reference n.
3. "Free runs left today" goes down by about 1 per run, as before (nothing changed there).

## Needs the owner

Each in plain words, with options and one suggestion.

**N1. Where the answer goes.** A: the run page itself becomes the answer page (steps folded at the top, studies folded at the bottom). B: a separate answer page with a link. **Suggestion: A**, one page per question, as you asked.

**N2. Width.** The pairs need a wider page from 700 px. Widen only the answer's content (header and menu stay as on other pages), or widen the header too. **Suggestion: content only**; look at mock v6 and say if the narrow header bothers you.

**N3. The two arms in "Out of 100".** Greys only: solid squares for the first arm, hatched for the second, with the names beside them (as the spec's "solid against hatched"). Or keep the mock's blue and orange (this breaks the greys-only rule and its test). **Suggestion: greys.**

**N4. Certainty labels.** Greys with the word always printed: High dark filled, Moderate light filled, Low outlined, Very low dashed outline. Or the mock's colours. **Suggestion: greys.**

**N5. Hazard ratios written with no unit** (the real answer had three; none would be drawn). R0: keep the strict rule (they stay as text). R1: also accept a ratio named at the start of its label ("Hazard ratio for ...", "Stratified hazard ratio ...", "Odds ratio ...", "Relative risk ..."), still with every drawing check. R2: change the writer's instruction so it writes HR/OR/RR in the unit (an engine change; Incisive's words would no longer be word for word). **Suggestion: R1.** On the real answer it draws 2 of the 3 (the third has a one-sided interval and stays text).

**N6. Pairs whose arm names do not repeat the comparison's words** (4 of 7 on the real answer get no squares; they still show in "Compare the arms"). S1: "Group A"/"Group B" when the comparison says "(group A)" and "(group B)" (+1 here). S2: "Experimental"/"Standard" only when the comparison itself uses those words on the matching side (+0 here, safe). S3: "Experimental" always means the first treatment (+1 here, relies on the writer's order). **Suggestion: S1 and S2 now; S3 waits for F4's number check.**

**N7. Dark mode.** The mock had one; the live look does not. **Suggestion: light only for now**, as every other page; a grey dark mode can come with the look later.

**N8. Steps.** Folded at the top of the answer (as spec §6), or at the bottom with the studies. **Suggestion: folded at the top.**

**N9. The spec on main says the answer page will have a small script, a study sheet and "Copy as text".** Your frozen mock and your no-JavaScript ruling say none. **Suggestion: the spec follows the mock** (no script; folds are `<details>`); the Introduction and the early summary stay on the other chat's F3b list.

**N10. Hidden direction-changing characters** (bidi and zero-width) in the writer's text. Remove them on the page only (old and new answers alike; nothing stored changes), or also before saving (a change to the writer's path). **Suggestion: on the page only.**

**N11. Two chats, one page.** The other chat's F3b (Introduction, early summary) will also change the run page. Build this first and let that work build on it, or wait for it. **Suggestion: build this first** on `f3b-answer-page`, and keep it rebased on `main` (Global Constraint 1).

## Risks

1. **Merge collisions** with the other chat on `run.html`, `web.py`, `app.css`, `test_ask_page.py` (Estimate: likely if both build F3b at once). Mitigation: Global Constraint 1 and N11.
2. **The rules are strict on real output** (§2): many answers may draw few CI lines and few squares. Correct but plain. N5/N6 recover some; F4's number check is the real fix.
3. **Fold weights** were measured on mock v5's fonts (system sans 16 px); the live fonts are 15 px Hanken Grotesk and Source Serif 4. The pairs in v6 came out level at 1,100 px (Fact), but real text lengths may leave a gap of about one item (Estimate). Tuning changes only constants.
4. **SVG text** does not wrap; axis labels are short numbers, so this holds (Estimate).
5. **Python 3.13 in the workspace venv** against 3.12 in the spec (Fact: `.venv/lib/python3.13`); CI runs the image's Python (Not checked here).

## Not checked

- How the owner's phone renders v6 (only Chromium was used).
- The rules on the other three benchmark questions' real answers (only one real answer was available).
- Whether the other chat has started its own F3b page changes on an unpushed branch.

## Deferred to F4 (recorded after the independent re-check, 12 Nov 2026)

The re-check of this branch found no wrong picture that a model answering from abstracts would plausibly produce and no regression against the live pages. These items are recorded for F4, in addition to the list in the earlier F3 plan (time points or subgroups inside arm texts; same label, different measures; a difference row not saying which arm minus which; a compared value's interval not printed):

1. **A percentage or a log value under a ratio label.** "Hazard ratio for … " with 72 (65–79) is drawn pinned at the edge of the scale and marked as running past it; "… (log)" is drawn as the plain ratio. Guard to add for ratios recognised by label only: refuse labels containing "%", "percent", "log", "ln", "inverse" or "reciprocal" (except "(95% CI)"), and refuse a row whose whole interval lies outside 0.05–50.
2. **A change word with a ratio unit given.** "Relative risk reduction" with the unit written as "RR" is drawn as a risk ratio, because a given unit decides. Apply the change-word and negation ban when the unit is RR, OR or HR too.
3. **Order in "Compare the arms"** keeps its own grouping order, while "Out of 100" shows the comparison's first side first.
4. **The reason shown for a refused blank-unit ratio label** ("unit not RR, OR or HR") is true but does not say why the label was refused.
5. **One extreme ratio row squeezes the others** on the shared axis, down to a few pixels on a phone. The numbers sit beside each row.
6. **Strictness.** On made-up but realistic answers, about 70% of genuine ratios with a full interval get a line and about 69% of genuine pairs get squares when the comparison has no negation (none when it has). Labels such as "HR for …" and "Hazard ratio (HR) for …" stay as text. Loosen from real answers, not from guesses.

What code cannot remove: a plausible value under the wrong measure name (a survival of 0.76 labelled "Hazard ratio") is drawn as that measure. F4's number checks must match each drawn value, its unit and its arm against the cited abstract.
