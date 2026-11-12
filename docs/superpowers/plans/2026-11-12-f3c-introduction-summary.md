# F3c: the Introduction panel and the early summary (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax.

**Status (12 Nov 2026):** plan only, written by the Architect on branch `f3c-introduction-summary`, cut from `main` at `57ecea4` (`git fetch origin main`: `FETCH_HEAD` = local `main` = `57ecea4`). Nothing built. Baseline on this branch (Fact, 12 Nov): `ruff check .` clean; `pytest -q -rs` 2132 passed, 0 skipped. Before Task 1 the owner answers "Needs the owner" (§7) and signs off mock `docs/mocks/f3c-introduction-summary-v1.html` on the Mock Board. The plan is written for the suggested option of each and says what changes for the others.

**Goal:** Finish F3 (owner, 12 Nov 2026: "finish F3: small introduction panel and summary page"). Two small things, both as Incisive Quick does them:
1. **The Introduction:** one extra Gemini call at the start of a run, from the question alone, shown as a short background panel while the run works and folded at the top of the finished page.
2. **The early summary:** the writer's summary part (and its context part), citation-checked, shown on the working run page as soon as it is read, marked as early, and replaced by the full answer when it is stored.

**Architecture:** One migration (two small tables and one `model_calls` step). One new module `src/pubmedgemini/intro.py` (prompt, call, store). The runner starts the Introduction on its own thread and passes a callback to the writer's existing `on_part` hook. The run page reads both and draws them with existing classes only: no CSS change, no script, no `style=`.

**Labels.** Each statement is **Fact** (with where it was checked), **Estimate**, or **Not checked**. Fact means read or run by the Architect on 12 Nov 2026: pubmedgemini at `57ecea4`, Incisive (read-only) at `12630f7` under `crates/incisive-app/src/`.

## 1. What was already decided, and where

| # | Decision | Where recorded (Fact) |
|---|---|---|
| §2a no. 3 | "The **Introduction** panel is in (folded, written while the search runs). Replaces 'not carried over' in §6." | spec `.10x/specs/2026-11-10-pubmedgemini-quick-design.md` §2a; `docs/build-log.md` 11 Nov (evening) "the Introduction panel is in" |
| D3 | "The Introduction counts inside the run's cap; built in F3b" | F3a plan `docs/superpowers/plans/2026-11-11-f3a-writer.md` "Decisions" table; build log 11-12 Nov |
| Q5 | "The cap of 26 already counts the Introduction and number completion (23 + 3)." Given as "Plan's suggestion, put to the owner; no objection." | F3a plan "Owner's answers"; build log 11-12 Nov |
| D8 | "Early summary as Incisive (a draft store), built in F3b; F3a hands on a checked summary part as soon as it is ready" | F3a plan "Decisions"; spec §5 "Early summary" ("storing and showing it is F3b (D8)") |
| D9 | "The search trail stays on the page under Details (F3b)" | F3a plan; it is **already done**: the trail card is on the finished page (`templates/run.html` "Search trail"). Nothing in F3c. |
| §4 | `RUN_CALLS` 23 itemised with "Introduction 1 (F3b)" | spec §4; code `gemini/quota.py` comment above `RUN_CALLS` |
| §6 | Answer page order: "1. Question as the title ... 2. Introduction (folded). 3. Results card ... the progress steps folded beneath." | spec §6 |
| N9 | No JavaScript on the answer page; folds are `<details>` | spec §4; F3b plan N9 |
| N11 | F3b built first; "the Introduction and the early summary stay on the other chat's F3b list" | F3b plan N9, N11 |

Not found in any record (Fact: searched `docs/`, `.10x/` for "introduction", "intro", "early"): the Introduction's prompt, model, label, failure handling; how the early summary is marked; whether its numbers show. Those are §7.

Records out of date (Fact): `.10x/status.md` says F3b "not merged"; it is on `main` (`57ecea4` holds `answer_page.py`, `run.html` layout). `docs/change-log.md` header still says F3a "is not merged". Task 5 corrects both.

## 2. What these are in Incisive (read-only, `12630f7`)

### 2.1 The Introduction

| Point | Incisive (Fact unless marked) |
|---|---|
| What makes it | One model call, a **detached job**, not part of `team.rs`: `runs/intro.rs` `start` (l.336), called from `ask.rs:645` after the run is accepted and its worker spawned ("Detached and not waited for: it can neither fail nor slow the run") |
| Switch | `INTRO=off` by default (`config.rs:448-458`, `runs/intro.rs:75` `IntroSettings.on`; `infra/README.md:622`). Whether it is on in Incisive's live deployment: **Not checked** |
| Model | `INTRO_MODEL`, default `claude-sonnet-5-5` (`runs/intro.rs:70`), not the run's model |
| Prompt | `intro_page.rs:9-14` `PROMPT` (byte-exact test l.220), as the **system** prompt, with `SOURCE_RULE` appended (`agent/llm.rs:651` `turn_request`) |
| Input | **The question only**, as `wrap_source("question", question)` (`runs/intro.rs:159-161` `user_message`). No studies, no search results |
| Limits | `MAX_TOKENS = 800` (l.60); effort low (l.62); one attempt, no retry (l.103-110 `client`); 30 s `TIME_LIMIT` (l.65) |
| Accepted reply | stop reason end-turn, not cut off, text trimmed and not empty (l.543-554). Else the row ends `failed` with a fixed reason (`reason` module l.142-156) |
| Stored | `incisive.run_intros` (run_id, status `pending/done/failed/skipped/cap_blocked`, reason, text, cost) (`migrations/0015_run_intros.sql`) |
| Failure | Never touches the run (module doc l.3-10). Failed, skipped, blocked: "the page shows as nothing at all". A pending row older than 5 min is failed by recovery (l.68, `recover_stale`) |
| Logs | ids, fixed reasons, token counts only (l.396, l.539-542) |
| On the page | `app/intro.rs` `with_results` (l.196): **in front of the results card**. While the run works and no early answer shows: an open card, heading "Introduction", tag `TAG_RUNNING`, the paragraphs (revealed word by word by script, `Reveal`), then `CLOSING` once complete (`intro_card`, l.219-262). After the run, or once early sections show (`app.rs:1427-1434`, `working && !folded`): one folded row "Introduction" with "Read" on the right, `TAG_FINISHED` and the paragraphs (`intro_fold`, l.266-288). After a run that ended with the intro still pending, the page asks for it up to 20 times (`wait_for_intro`, l.148) |
| Fixed words | `HEADING` "Introduction"; `TAG_RUNNING` "A quick overview while we research your question and gather published data."; `TAG_FINISHED` "A quick overview written while we researched your question and gathered published data."; `CLOSING` "We are doing deep research to give you a well-researched answer."; `READ` "Read" (`intro_page.rs:16-24`) |
| Grounding | **None.** It is the model's own text from the question alone. The prompt forbids numbers, studies and any answer, but nothing in code checks the reply for them. It carries no citations, and the page does not say it is not from the studies |

**Clinically:** the Introduction is ungrounded model text. "A few lines on the disease" comes from the model's training, not from the selected studies, is not checked by any code, and on our side is written by Flash-Lite, not Sonnet. It must read as background, never as evidence (§7 O1).

The prompt, word for word (`intro_page.rs:9-14`):

```
For the question below, write a simple, brief introduction in plain paragraphs:
- what the question is asking;
- why the question is important;
- a few lines on the disease.

Give no numbers, no studies, and no answer: don't say or hint which approach is better. Write nothing else, no title and no closing line. If you don't recognise an operation or abbreviation, or it could mean more than one thing, don't guess and don't say so: write only one plain sentence restating the question in its own words, and nothing else.
```

### 2.2 The early summary (S1c)

| Point | Incisive (Fact) |
|---|---|
| When | Only with `QUICK_PARALLEL=on` (default off, `config.rs:409`; live setting **Not checked**), i.e. the four-part writer, which is what we run always (D1) |
| Source | `writer_v3.rs:2025-2035` `PartEvent::{Summary, Context, Withdraw}`; the summary **and the context** part are reported the moment each is read |
| Check | `team.rs:798-821` `early_body`: `early_answer(summary, context)` (`writer_v3.rs:2041`), then **the same citation check** (`check_citations`) and Quick's number trace (`trace_numbers`, "not traced" marks) the final answer gets. So it is **not** shown before the citation check |
| Store | `team.rs:833-870` `store_early`: an upsert of `{summary, context, answer}` JSON into `incisive.run_drafts` part `early` on every event, each write limited to 2 s (`EARLY_WRITE_LIMIT`), skipped if slow. On `Withdraw` (a part failed for good: the one piece will be written) the row is **deleted** and nothing more is stored ("owner D1 A: never show text the final answer will not hold") |
| Cleared | when the run ends, whatever the end (`runs/worker.rs:486` `clear_answer_parts`); recovery sweeps old rows (`drafts.rs` `sweep`) |
| Read | only while the run is running with no answer (`runs/api.rs:80-89`; `app.rs:1262-1270`); a part-failed run ("rewriting") hides it |
| Page | `app/page.rs:697-768` `early_page_view` / `:774` `early_layout_view`: At a glance (with its key-number tiles), Bottom line, Clinical practice from the summary; Applicability and What would change from the context; grey "Being written" placeholders for Why this conclusion, Counselling dashboard, the trials (`wait_card`, `BEING_WRITTEN`, `AFTER_OUTCOMES`). The results card and Introduction fold into rows above it (`app.rs:1424-1434`). The final page replaces it when the answer is stored. No word on the page says "early" or "may change": the placeholders are the only sign |
| Not checked on it | the merge checks (`merge_checks`: glance key numbers named in the outcome table, bottom-line certainty not above every outcome's) run only on the merged answer (`team.rs:597-602`) |

## 3. What is live on our side (Fact, `57ecea4`)

- **Writer:** `writer/parts.py` `write_parts(..., on_part=None)` already sends `("summary", wire)`, `("context", wire)` and `("withdraw", None)` under its lock, nothing after a withdraw, and a raising callback is logged by kind and ignored (`_safe`, l.281). `runner.py:333` passes `on_part=None`. `writer/read.py:501` `early_answer(summary, context)` exists and is tested to equal the merged fields (`test_writer_read.py:380` `test_early_answer_fields_equal_the_merged`). `writer/answer.py` `check_citations` exists.
- **Parts order and pacing:** `Section.ALL` is trials, outcomes, context, summary; the pacer starts them 0, 6, 12, 18 s apart (`gemini/quota.py` `GeminiPacer`, gap 60/10; F3a test `test_parts_start_through_the_pacer`). So the summary is the last sent.
- **Cap:** `quota.py:28-34` `RUN_CALLS = 23` (comment counts "Introduction 1 (F3b)"), `CALL_RESERVE = 3`, `RUN_ATTEMPTS = 26`; `runs_left_for` (l.160) gives 21 on a fresh day of 500. `caller.py:138` `ModelCaller.call` makes up to `MAX_ATTEMPTS = 3` attempts on transport kinds, with no per-call setting.
- **Steps allowed in `model_calls`:** researcher, study list and writer steps only (`migrations/0005_answers.sql`, `model_calls_step_check`). An `intro` attempt would be refused by the database today.
- **Time:** `researcher/loop.py` `Timing.quick`: wrap-up at 210 s, hard stop 420 s; writer gate 150 s (`WRITER_MIN_SECONDS`).
- **Run page:** `web.py:465` `run_page`; `templates/run.html` has two branches: `wide` (finished, readable answer: title, sub line, Steps fold, outcome, At a glance, Bottom line, pairs, cards, folded study lists) and the working/plain branch (`h1` pubmedgemini, Question card with "Working. This page refreshes itself...", Steps card). `<meta http-equiv="refresh" content="5">` while `active` (l.4-6).
- **Look tests:** `tests/test_look.py` `test_no_script_tags`, `test_base_links_stylesheet_and_has_no_inline_style`, `test_css_uses_only_the_agreed_greys`; `tests/test_ask_page.py:798` `test_page_has_no_script_and_no_new_css`. Every class the mock uses (`card`, `fold`, `sub`, `muted`, `tag`, `pill`, `row`, `foot`) is already in `static/app.css` (checked by script against the mock).
- **Leak tests:** `tests/test_logging.py` allow-list of log lines (l.250-380), `test_no_model_text_in_any_table_but_the_reviewed_ones` (l.539), writer and page leak tests (l.771-1009).

## 4. What we will do, and every difference

### 4.1 The Introduction

| Point | Incisive | F3c (suggested) | Options | Q |
|---|---|---|---|---|
| Model | Sonnet (own setting) | Flash-Lite, the run's model (spec §2 no. 1: Flash-Lite does every step) | none: owner rule | |
| Switch | `INTRO` default off | always on, no setting (§2a no. 3 "is in") | (a) no setting; (b) a setting | none |
| Prompt | `PROMPT`, word for word | the same, word for word, as `systemInstruction`, with `SOURCE_RULE` appended (`with_source_rule`) | | |
| Input | `wrap_source("question", q)` | the same (`researcher/prompts.py` `wrap_source`) | | |
| Output cap | 800 tokens, effort low | 800 `maxOutputTokens`; no effort setting (Gemini has none in our client; Flash-Lite's thought tokens were 0 in the probe: Fact as recorded, F3a plan "What the 10 Nov probe proved") | | |
| Attempts | 1, 30 s | 1, 30 s: `caller.call(..., max_attempts=1, deadline=attempt_deadline=start+30)` | (a) 1 attempt; (b) the caller's 3 transport attempts | O5 |
| When | detached at accept time | its own daemon thread, started by `run_one` right after `set_running`, before research | | |
| Counted | under the user's money caps, its own request | as one of the run's attempts (`run_id`), in the day count (`model_calls` step `intro`) (D3) | | |
| Accepted reply | end-turn, not cut, non-empty | finish `STOP`, not `MAX_TOKENS`, no block or refusal, trimmed text non-empty | | |
| Failure | row `failed`, nothing shown, run untouched | the same; any exception in the thread (cap, daily limit, network, database) ends the row `failed` with a fixed reason and never reaches the run | | |
| Stale row | recovery after 5 min | start-up: every `pending` row becomes `failed` `stale` (`stop_unfinished`) | | |
| Reveal | word by word by script | none: the text appears whole at the next 5 s refresh (no script, N9) | | |
| Label | heading, tag | heading, tag, **plus one added line**: "Background only, written by the model from your question alone. It is not drawn from the studies and carries no citations." | (a) add the line; (b) Incisive's words only | O1 |
| Closing line while working | `CLOSING` | the same | (a) keep; (b) drop ("deep research" is a strong word for a free Quick run) | O4 |
| Folded row | "Introduction" + "Read" | "Introduction" (our fold shows "+"; no "Read") | | |
| Place | in front of the results card | working page: after the Question card, before Steps; finished page: after the title and sub line, before the Steps fold (spec §6 item 2) | | |
| Run ended with it pending | page asks 20 more times | the page stops refreshing when the run ends; a late Introduction shows on the next visit | | |

### 4.2 The early summary

| Point | Incisive | F3c (suggested) | Options | Q |
|---|---|---|---|---|
| Parts shown | summary and context | the same (D8 "as Incisive") | (a) summary and context; (b) summary only | O2 |
| Check before showing | citation check + number trace | citation check (`check_citations` against the fetched selected studies, as the final answer). No number trace exists yet (F4); the final page has none either | | |
| Key numbers in At a glance | shown as tiles | **held back** until the full answer, with the line "The key numbers are shown with the full answer." Verdict and caveat shown | (a) show, as Incisive; (b) hold back | O3 |
| Lines and squares | none (placeholders) | none: the outcomes part is not in the early answer, so nothing can be drawn | | |
| Marking | none in words | a tag "Early" on each early card, and one card at the top: "Early summary · The summary was written first. Its citations have been checked against the selected studies. The outcomes, the trials and the check that the parts agree are still being written, so this summary may still change or be withdrawn. The full answer replaces it on this page." | | (mock) |
| Placeholders | one "Being written" card per missing section | one muted line: "Still being written: ..." (no new CSS) | | |
| Withdraw | row deleted | the same, in the callback, under the writer's lock (so a delete is never overtaken by an earlier upsert) | | |
| Store | `run_drafts` part `early`, 2 s limit | new table `run_early`, one row a run, `statement_timeout` 2 s | | |
| Cleared | at the end of the run | in the same transaction as the run's end (`finish_run`, `finish_with_answer`), and all rows at start-up (`stop_unfinished`) | | |
| Steps and Introduction | fold into rows | the Steps card becomes the same `Steps (n)` fold as the finished page; the Introduction folds | | |
| Replacement | final page on the next poll | the next 5 s refresh after "Answer ready" shows the finished (wide) page | | |

## 5. The smallest design

### Storage: migration `0006_intro_early.sql` (new; 0001-0005 not edited)

```sql
CREATE TABLE run_intros (
    run_id      UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
    reason      TEXT CHECK (reason ~ '^[a-z_]{1,32}$'),   -- a fixed kind, never model text
    text        TEXT,                                     -- only when done
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE TABLE run_early (
    run_id     UUID PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    body       JSONB NOT NULL,                            -- {summary, context, answer: AnswerV3 body}
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE model_calls DROP CONSTRAINT model_calls_step_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_step_check CHECK (step IN (<0005's list>, 'intro'));
```

Why two tables and not columns on `runs` or `answers`: `answers` is written once with the final status (`finish_with_answer`) and holds only `version = 3`; `runs` is read by Library and Activity, and the early row must disappear when the run ends. Two tables keep both reads unchanged. (Estimate: about 25 lines of SQL.)

### Calls, cap and free runs (arithmetic)

- One Introduction attempt a run, at most (O5 a). Fact: `RUN_CALLS = 23` already holds "Introduction 1".
- Typical run: 8 researcher + 1 list + 4 parts = 13, now **14** attempts.
- Worst case without transport retries: F3a's 21 (Fact: `quota.py` comment) + 1 = **22**, still under 23; F4's number completion makes 23.
- Cap per run: 26 = 23 + 3, **unchanged**. With O5 (b) the Introduction could take up to 3 attempts, eating the whole transport reserve of 3: that is why O5 (a) is suggested.
- Free runs shown: `runs_left_for(500, 0) = 1 + (500 - 26) // 23 = 1 + 20 = 21`, **unchanged**; the admission rule is unchanged.
- Real capacity of a fresh day (Estimate): 500 / 14 ≈ 35 typical runs, against 500 / 13 ≈ 38 today. The screen still says 21.
- Early summary: **no call** (it reuses the summary and context replies).

### Time and pacing

- The Introduction's attempt is the run's first on the shared pacer (10 a minute, one start every 6 s). The researcher's first turn therefore starts up to **6 s** later. The wrap-up (210 s) and hard stop (420 s) are counted from the run's start and do not move, so research loses up to 6 s of its 210 s (Estimate: under 3%). Its prompt is a few hundred tokens against 250,000 a minute.
- The Introduction ends by 30 s after start (its own deadline). The four writer parts start long after (they need the study list), so the 0/6/12/18 s pacing of the parts is unaffected (Estimate: research takes well over 30 s; Fact for the real runs of 11 Nov is not in the records).
- `run_one` waits for the Introduction thread (at most 35 s) in its `finally`, after the run's status is stored and before `caller.forget(run_id)`, so the run's attempt count is dropped only once nothing more can be sent for it. "One question at a time" is read from the database (`active_run`), so this wait never blocks the next question.
- Early-summary writes: one upsert per event (at most two) and at most one delete, each under a 2 s statement timeout, done in the callback under the writer's lock. A part waiting for that lock waits at most about 2 s (Estimate), never past the hard stop.

### Code

- `gemini/client.py`: `text_body(system: str, user: str, max_tokens: int) -> dict` (no tools, no schema).
- `gemini/caller.py`: `ModelCaller.call(..., max_attempts: int = MAX_ATTEMPTS)`; default unchanged.
- `src/pubmedgemini/intro.py` (new): words, the call, the store. Port table at the top.
- `runs.py`: `intro_start`, `intro_finish`, `store_early`, `clear_early`; end-of-run deletes; `RunView.intro`, `RunView.early`.
- `runner.py`: start the thread; `EarlyStore` callback; join before `forget`.
- `web.py` `run_page` and `templates/run.html` (+ `_answer.html` macros shared by the finished page and the early page).

## Global Constraints

1. **Keep in step with `main`.** `git fetch origin main` before any work, before review and before asking for "merge it"; compare `FETCH_HEAD` with `git merge-base HEAD FETCH_HEAD`. Never branch from a local `main` behind `FETCH_HEAD`. If `main` moved: `git rebase FETCH_HEAD` before the branch is pushed, `git merge FETCH_HEAD` after; re-run the whole suite; list in the PR every file both sides touched (`runner.py`, `runs.py`, `web.py`, `run.html`, `test_logging.py` are the likely ones).
2. Work only on `f3c-introduction-summary`. No push to `main`, no force push, nothing merged without the owner's own words "merge it".
3. **Engine changes are only these:** the Introduction call (prompt word for word), `max_attempts` on the caller, the `on_part` callback. Nothing else in the researcher, writer prompts, citation check, stored answer shape, cap (26), free runs (21) or time limits (210 s, 420 s, 150 s) changes.
4. **No check and no Introduction may fail a run.** Every exception in the Introduction thread and in the early callback is caught, logged by class or fixed kind only, and the run goes on. A test proves each path (Task 3).
5. **Never logged:** the question, the Introduction text, the early body, any answer field, Google's message. New log lines are fixed words, ids, counts and kinds, added to the allow-list in `test_logging.py`.
6. **Untrusted model text:** autoescape only, no `|safe`, no `Markup`; the Introduction is plain paragraphs (no citation links: a bracket in it stays text).
7. **The look:** existing classes only; no `<style`, no `style=`, no `<script`, no new `<link>`; greys only. Phone width with no sideways scroll.
8. **Keep out of the repo:** secrets, server names, the web address, real emails, question text, model replies, run outputs. Test and mock text is made up (treatment A and B, example condition).
9. Tests first in every task; `ruff check .` and the whole suite green with nothing skipped before each commit.
10. Old runs: a run without a `run_intros` row shows no Introduction; every stored answer renders exactly as today (the finished page's existing tests pass unedited).

## Review Focus

1. **A failed, slow or capped Introduction never fails, stops or slows the run** beyond the one pacer slot. Tests: Task 3 `test_intro_failure_never_fails_the_run` (parametrised), `test_intro_cap_reached_leaves_the_run_alone`.
2. **The early summary never outlives the run or a withdraw**, and is never shown next to a final answer. Tests: Task 1 `test_finish_clears_the_early_row_in_the_same_transaction`, Task 3 `test_withdraw_deletes_the_early_summary`, Task 4 `test_finished_page_never_shows_the_early_summary`.
3. **The early summary is citation-checked** with the same check and the same selected list as the final answer. Test: Task 3 `test_early_summary_is_citation_checked`.
4. **The arithmetic:** 26 and 21 unchanged; one Introduction attempt. Tests: Task 2 `test_intro_sends_one_attempt_only`; existing `test_runs_left_is_21_on_a_fresh_day` unedited.
5. **No text in logs**, with markers in the Introduction and the early body. Task 5.
6. **Clinical marking:** the Introduction carries the background line; early cards carry "Early"; no key numbers early (O3 b). Task 4.

## Tasks

### Task 1: Migration 0006 and the store

**Files:** Create `migrations/0006_intro_early.sql`. Modify `src/pubmedgemini/runs.py`. Test `tests/test_runs.py`, `tests/test_migrate.py`, `tests/test_logging.py` (the reviewed-columns test).

**Interfaces:**
- `intro_start(pool, run_id: UUID) -> bool`: inserts `pending`; False if a row exists or the run is gone.
- `intro_finish(pool, run_id: UUID, status: Literal["done", "failed"], reason: str | None, text: str | None) -> bool`: only while `pending`.
- `store_early(pool, run_id: UUID, body: dict) -> bool`: upsert, only while the run is `running` and has no `answers` row; `SET LOCAL statement_timeout = '2s'` in its transaction.
- `clear_early(pool, run_id: UUID) -> None`.
- `finish_run` and `finish_with_answer` also `DELETE FROM run_early WHERE run_id = %s` in their transaction when applied; `stop_unfinished` deletes every `run_early` row and fails every pending `run_intros` row with `stale`.
- `RunView.intro: str | None` (text only when `done`), `RunView.early: dict | None` (only when the run is `queued`/`running` and has no answer). Fields added with defaults at the end, so no constructor changes.

**Tests first:**
- `test_migration_0006_adds_tables_and_the_intro_step`: `run_intros`, `run_early` exist; a `model_calls` row with step `intro` inserts; a step `introx` is refused.
- `test_intro_row_moves_pending_to_done_once`: second `intro_finish` returns False; `get_run(...).intro` is the text.
- `test_failed_intro_shows_nothing`: `failed` row → `RunView.intro is None`.
- `test_store_early_only_while_running`: True while running; False after `finish_run`; no row after.
- `test_finish_clears_the_early_row_in_the_same_transaction`: an early row, then `finish_with_answer` → no `run_early` row, answer stored; a body the database refuses → early row still there and status unchanged (rolled back together).
- `test_start_up_clears_early_and_fails_pending_intros`.
- `test_no_model_text_in_any_table_but_the_reviewed_ones`: deliberate edit, adds `("run_intros","status")`, `("run_intros","reason")`, `("run_intros","text")`, `("run_early","body")`.

- [ ] Tests, FAIL, implement, PASS. Commit `feat: tables for the Introduction and the early summary (migration 0006)`.

### Task 2: The Introduction call

**Files:** Create `src/pubmedgemini/intro.py`. Modify `gemini/client.py`, `gemini/caller.py`, `scripts/check_incisive_texts.py`. Test `tests/test_intro.py`, `tests/test_gemini_caller.py`, `tests/test_gemini_client.py`.

**Interfaces:**
- `client.text_body(system: str, user: str, max_tokens: int) -> dict` = `{"systemInstruction": ..., "contents": [user_text(user)], "generationConfig": {"maxOutputTokens": max_tokens}}`.
- `ModelCaller.call(body, *, step, run_id, deadline, on_wait, attempt_deadline=None, max_attempts: int = MAX_ATTEMPTS) -> Reply`.
- `intro.py`: `PROMPT`, `HEADING`, `TAG_RUNNING`, `TAG_FINISHED`, `CLOSING` (Incisive's words, `intro_page.rs:9-24`); `BACKGROUND` (O1 a, new words); `STEP = "intro"`; `MAX_TOKENS = 800`; `TIME_LIMIT_SECONDS = 30`; `user_message(question: str) -> str` (`wrap_source("question", question)`); `paragraphs(text: str) -> list[str]` (port of `intro_page.rs:31-48`: split on blank lines, lines trimmed, `\r` dropped as `str::lines`); `write_intro(run_id: UUID, question: str, *, caller: ModelCaller, pool, clock) -> str` (returns the row's final status, never raises); `start_intro(run_id, question, *, caller, pool, clock) -> threading.Thread` (daemon, name `intro`).
- Reasons (fixed): `time` (DeadlineReached), `cap` (RunCapReached), `api` (any GeminiError; its kind goes to the log only), `truncated` (`MAX_TOKENS`), `refused` (block reason or refusal finish), `unusable` (finish not `STOP`), `empty`, `store`, `internal`.

**Tests first:**
- `test_prompt_is_incisive_word_for_word`: `PROMPT` equals the text in §2.1; `scripts/check_incisive_texts.py` (run by hand; not in CI, no Incisive checkout there) gains `intro_page.rs` `PROMPT`, `HEADING`, `TAG_RUNNING`, `TAG_FINISHED`, `CLOSING` and prints `equal` against the read-only checkout.
- `test_intro_body`: system is `PROMPT + "\n\n" + SOURCE_RULE`; the one user text is `<source id="question">\n...\n</source>`; `maxOutputTokens == 800`; no `tools`, no `responseSchema`.
- `test_intro_sends_one_attempt_only`: a 503 → one request only, row `failed` `api`; the caller's default still retries (existing tests unedited).
- `test_intro_has_30_seconds`: the attempt timeout passed to the client is at most 30; a pacer start past 30 s → `failed` `time`, nothing sent.
- `test_intro_reply_rules` (parametrised): `MAX_TOKENS` → `truncated`; block → `refused`; `SAFETY` → `refused`; `OTHER` → `unusable`; `"  \n "` → `empty`; `STOP` with text → `done`, text trimmed.
- `test_intro_counts_in_the_run_cap_and_the_day`: one `model_calls` row, step `intro`, same `run_id`; at the cap → `failed` `cap`, nothing sent.
- `test_paragraphs_split_as_incisive`: Incisive's `paragraphs_split_on_blank_lines` case (`intro_page.rs:247-253`).

- [ ] Tests, FAIL, implement, PASS. Commit `feat: the Introduction call (Incisive's prompt, one attempt, 30 s, 800 tokens)`.

### Task 3: The runner starts the Introduction and stores the early summary

**Files:** Modify `src/pubmedgemini/runner.py`. Test `tests/test_runs.py`, `tests/writer_fakes.py` (route `intro`: system starts with `intro.PROMPT`).

**Interfaces:**
- In `run_one`, after `set_running`: `t = start_intro(...)`; in `finally`, before `caller.forget`: `t.join(TIME_LIMIT_SECONDS + 5)`.
- `class EarlyStore` (in `runner.py`): `__init__(pool, run_id, a: _Answer, selected: list[str])`; `__call__(ev: PartEvent) -> None`. On `summary`/`context`: keep the wire part; `written = early_answer(summary, context)`; `check_citations(written, selected, IntegrityV3())`; body `{"summary": bool, "context": bool, "answer": AnswerV3(question, created_at, answer=written, references, searches, elapsed_s=0).to_body()}`; `store_early`. On `withdraw`: `clear_early`. Every error caught, logged `run kind=early_not_stored error=<Class>`.
- `write_parts(..., on_part=EarlyStore(self.pool, self.run_id, a, [s.pmid for s in studies]))`.
- Log lines: `run kind=intro run_id=<uuid> status=done|failed reason=<k> output_tokens=<n>`, `run kind=early_stored run_id=<uuid> summary=true|false context=true|false bytes=<n>`, `run kind=early_withdrawn run_id=<uuid>`, `run kind=early_not_stored error=<Class>`.

**Tests first:**
- `test_full_run_has_an_introduction_and_14_attempts`: `done`; `run_intros` `done`; 14 `model_calls` rows, one `intro`.
- `test_intro_failure_never_fails_the_run` (parametrised: 503, 429 with a long asked wait, `MAX_TOKENS`, blocked, database refusing the row, an exception inside `write_intro`): run ends `done` with its answer; intro row `failed` with the reason.
- `test_intro_cap_reached_leaves_the_run_alone`: caller cap set so the intro's attempt is the one refused → run outcome as without the intro.
- `test_early_summary_is_citation_checked`: summary cites one selected and one unknown PMID → the stored early body keeps the first, removes the second (same text as the final answer's bottom line for the same reply).
- `test_early_summary_stored_then_cleared_at_the_end`: a callback spy sees the row while the outcomes part is held back; after the run, no row.
- `test_withdraw_deletes_the_early_summary`: trials unreadable twice → no row from the moment of withdraw; the one-piece answer stored.
- `test_a_failing_early_store_never_fails_the_run`: `store_early` raising → run `done`.

- [ ] Tests, FAIL, implement, `ruff check . && pytest -q` PASS. Commit `feat: the run writes the Introduction and stores the checked early summary`.

### Task 4: The page

**Files:** Modify `src/pubmedgemini/web.py` (`run_page`), `templates/run.html`, `templates/_answer.html` (macros `glance_card(view, early)`, `bottom_card(view, early)`, `applicability_card(view, early)`, `would_change_card(view, early)`). Test `tests/test_ask_page.py`, `tests/test_run_answer_layout.py`.

**Rules** (mock v1):
- Context gains `intro_paragraphs: list[str]` (from `RunView.intro` via `intro.paragraphs`, after `drop_invisible`) and `early: dict | None` (`answer_view(read_body(v.early["answer"]))` plus the two flags; an unreadable early body is ignored, logged `run page early kind=<k>`).
- Working page, no early summary: Question card; the Introduction card (`<h2>Introduction</h2>`, `p.sub` TAG_RUNNING, `p.muted` BACKGROUND, paragraphs, `<p><b>CLOSING</b></p>`) when there is text; the Steps card as today.
- Working page with an early summary: Question card; Introduction as `details.fold` (TAG_FINISHED); Steps as `details.fold` "Steps (n)"; the "Early summary" card; At a glance (verdict and caveat only, the line "The key numbers are shown with the full answer."), Bottom line with pill and practice (when `summary`); Applicability and What would change (when `context`); each `<h2>` with `<span class="tag">Early</span>`; the muted "Still being written: ..." line naming what is missing.
- Finished page: the Introduction fold between the sub line and the Steps fold, only when there is text. Everything else byte-identical.

**Tests first:**
- `test_working_page_shows_the_introduction_open_with_its_labels`.
- `test_no_introduction_row_no_panel` (old runs, failed, pending).
- `test_introduction_text_is_escaped_and_never_linked`: `<script>` → `&lt;script&gt;`; `[12345678]` stays text, no `<a`.
- `test_early_summary_marked_and_without_key_numbers`: "Early summary", three `tag">Early<`, verdict present, key-number label absent, no `<svg`, no `class="dots"`.
- `test_early_context_cards_only_when_context_arrived`.
- `test_finished_page_never_shows_the_early_summary`: a stored answer and a leftover early row (inserted by hand) → no "Early".
- `test_finished_page_has_the_introduction_fold_first`: index of "Introduction" < index of "Steps (".
- `test_page_has_no_script_and_no_new_css`: extended to the two new working states.
- Phone width: no test in the suite uses a browser (Fact: no `playwright` in `tests/`); checked on the mock with Playwright at 375 px (scroll width 375, no script: Fact, 12 Nov) and by the owner (sign-off item 4).

- [ ] Tests, FAIL, implement, PASS. Commit `feat: the Introduction panel and the early summary on the run page`.

### Task 5: Leak tests and records

**Files:** `tests/test_logging.py`, `docs/change-log.md`, `docs/build-log.md`, `.10x/status.md`, spec §4 (comment "Introduction 1 (F3c)"), §5, §6, §7, `gemini/quota.py` comment.

**Tests first:**
- `test_nothing_private_is_logged_with_intro_and_early`: markers in the question, the Introduction reply and the summary part's text never appear in `caplog`; the four new lines match the allow-list.
- `test_every_log_line_matches_the_allowlist` (existing): new patterns added, nothing else loosened.
- `test_a_written_run_under_uvicorn_logs_no_writer_text`: extended with the intro and summary markers.

- [ ] Tests, PASS. Records in plain words (status: F3b live, F3c built; change log header). Push only when the owner says so; open PR `F3c: the Introduction panel and the early summary`. Stop; wait for "merge it".

Size (Estimate): about 250 lines of code, 350 of tests, 25 of SQL; no CSS.

## 6. Manual sign-off (owner, after the deploy)

1. Home still says "Free runs left today: about 21".
2. Ask one benchmark question. Within about 30 s the working page shows the Introduction, with the background line. Read it: it should give no numbers, no studies and no answer.
3. While the writer works, the page shows "Early summary" with "Early" tags, no key numbers, then the full page replaces it (it may last only a few seconds: Estimate).
4. On the phone: no sideways scroll on either working state; the page refresh does not lose your place (Not checked: whether the 5 s refresh keeps the scroll position on your phone's browser).
5. The finished page: "Introduction" folded at the top, opens on tap.
6. On the server: `cd /opt/pubmedgemini && docker compose logs app | grep -ci appendicitis`. Expected `0`.

## 7. Needs the owner

**O1. The Introduction is the model's own words from your question alone, not from the studies.** Incisive labels it only "A quick overview while we research...". (a) add one line: "Background only, written by the model from your question alone. It is not drawn from the studies and carries no citations."; (b) Incisive's words only. **Suggestion: (a).**

**O2. What the early summary shows.** (a) as Incisive (your D8): At a glance, Bottom line and Clinical practice, then Applicability and What would change when that part arrives; (b) the summary part only. **Suggestion: (a).**

**O3. Numbers in the early summary.** Its citations are checked, but no number check exists yet (F4), and the check that At a glance and the certainty agree with the outcomes runs only on the full answer. (a) show At a glance's key numbers early, as Incisive; (b) hold them back until the full answer ("The key numbers are shown with the full answer."); verdict, caveat and bottom line still show. **Suggestion: (b) until F4,** then look again.

**O4. Incisive's closing line while working:** "We are doing deep research to give you a well-researched answer." (a) keep word for word; (b) leave it out. **Suggestion: (a).**

**O5. How many tries the Introduction gets.** (a) one, as Incisive (it never touches the run's spare 3); (b) up to three on a busy or failed connection. **Suggestion: (a).**

**O6. Mock v1** on the Mock Board: sign off, or say what to change.

## 8. For the Coordinator: the Mock Board post

Post `docs/mocks/f3c-introduction-summary-v1.html` (branch `f3c-introduction-summary`) as **"F3c mock v1: Introduction and early summary"**, with this text:

> One page, three screens of the same made-up run, example words only. **A1:** the page while it works, the Introduction open (with the line saying it is background from your question, not from the studies). **A2:** later, the summary part is ready: the Introduction and Steps fold, "Early summary" cards tagged "Early", no key numbers yet, and a line saying what is still being written. **B:** the finished page's top, the Introduction folded above Steps; the rest is mock v6. Please check on your phone: the labels read clearly; "Early" is easy to see; you are content that no key numbers show early (O3); the order. Then answer O1 to O6.

## 9. Risks

1. **Ungrounded text on a clinical page** (Introduction). Mitigation: O1 label, no citations, folded on the finished page. Flash-Lite may still write a number or lean to an answer; nothing checks it (as Incisive).
2. **The early summary differs from the final one** when a part fails and the one piece is written: withdrawn at once, but a reader may have seen it. Same as Incisive.
3. **The early summary may show only a few seconds** (the summary part is sent last, 18 s after the first part: Fact; how long the outcomes part takes on Flash-Lite: Not checked). Little gain for the code; D8 decided it.
4. **Up to 6 s less research** from the pacer slot (Estimate).
5. **The 5 s refresh** reloads the page under a reader (Not checked on phones).
6. **Two chats change `run.html` and `runner.py`.** Global Constraint 1.

## 10. Not checked

- Whether `INTRO` and `QUICK_PARALLEL` are on in Incisive's live deployment.
- Flash-Lite's behaviour on the Introduction prompt (no real run; this workspace cannot reach Gemini).
- Whether Gemini counts thinking against `maxOutputTokens: 800` on Flash-Lite (probe: 0 thought tokens on another prompt).
- Timing of the summary part against the outcomes part on real runs.
- Scroll position across the meta refresh on phone browsers.
