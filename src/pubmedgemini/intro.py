"""The Introduction: one model call from the question alone, written while the run searches.

Ports of Incisive (commit 12630f7, `crates/incisive-app/src/`):

| Here | Incisive |
|---|---|
| `PROMPT`, `HEADING`, `TAG_RUNNING`, `TAG_FINISHED`, `CLOSING` | `intro_page.rs:9-22` (byte for byte) |
| `paragraphs` | `intro_page.rs:31-48` `paragraphs` |
| `STEP`, `MAX_TOKENS`, `TIME_LIMIT_SECONDS` | `runs/intro.rs:56-65` |
| `user_message` | `runs/intro.rs:159-161` (`wrap_source("question", q)`) |
| `write_intro` | `runs/intro.rs` `run_job` and the reply rules (l.543-554) |
| `start_intro` | `runs/intro.rs:336` `start` (detached; never waited for by the run's work) |

Differences (F3c plan §4.1, owner's answers 12 Nov 2026):

| Point | Incisive | Here |
|---|---|---|
| Model | `INTRO_MODEL` (Sonnet) | the run's model (Flash-Lite does every step) |
| Switch | `INTRO`, default off | always on |
| Effort | low | none (Gemini has no setting in our client) |
| Counted | the user's money caps, its own request | one attempt of the run's RUN_ATTEMPTS and the day's count (`model_calls` step `intro`) |
| Tries | one, 30 s | the same: `max_attempts=1`, deadline and attempt deadline 30 s from its start (O5 a) |
| Label | heading and tag | plus `BACKGROUND` (O1 a) |

The call never touches the run: every failure ends the row `failed` with a fixed reason (no
row is shown as nothing on the page), and `write_intro` never raises. The question was checked
for patient details before the run existed (`runs.create_run`); the model sees the question
only. Logs hold the run id, the status, a fixed reason and a token count: never the question
or the reply.
"""

import logging
import threading
import time
from collections.abc import Callable
from uuid import UUID

from psycopg_pool import ConnectionPool

from pubmedgemini.gemini.caller import DeadlineReached, ModelCaller, RunCapReached
from pubmedgemini.gemini.client import GeminiError, text_body
from pubmedgemini.gemini.structured import rust_trim
from pubmedgemini.researcher.loop import REFUSAL_FINISHES
from pubmedgemini.researcher.prompts import with_source_rule, wrap_source
from pubmedgemini.runs import intro_finish, intro_start

log = logging.getLogger("pubmedgemini.intro")

PROMPT = (
    "For the question below, write a simple, brief introduction in plain paragraphs:\n"
    "- what the question is asking;\n"
    "- why the question is important;\n"
    "- a few lines on the disease.\n"
    "\n"
    "Give no numbers, no studies, and no answer: don't say or hint which approach is better. "
    "Write nothing else, no title and no closing line. If you don't recognise an operation or "
    "abbreviation, or it could mean more than one thing, don't guess and don't say so: write "
    "only one plain sentence restating the question in its own words, and nothing else."
)
HEADING = "Introduction"
TAG_RUNNING = "A quick overview while we research your question and gather published data."
TAG_FINISHED = (
    "A quick overview written while we researched your question and gathered published data."
)
# Shown while the run works only, once the text is there.
CLOSING = "We are doing deep research to give you a well-researched answer."
# O1 (a): the model's own words from the question, not the studies.
BACKGROUND = (
    "Background only, written by the model from your question alone. It is not drawn from "
    "the studies and carries no citations."
)

STEP = "intro"
MAX_TOKENS = 800
TIME_LIMIT_SECONDS = 30

# The fixed reasons a row can end `failed` with (`runs/intro.rs` `reason`, as words of
# `[a-z_]`). `stale` is set at start-up by `runs.stop_unfinished`.
REASONS = frozenset({"time", "cap", "api", "truncated", "refused", "unusable", "empty", "store",
                     "internal", "stale"})

_WHITE_SPACE_LINE_END = "\r"


def user_message(question: str) -> str:
    """The one user message: the question as data."""
    return wrap_source("question", question)


def intro_body(question: str) -> dict:
    return text_body(with_source_rule(PROMPT), user_message(question), MAX_TOKENS)


def paragraphs(text: str) -> list[str]:
    """`intro_page.rs` `paragraphs`: split on blank lines, each line trimmed, empty paragraphs
    dropped; a single line break stays inside its paragraph. Lines as Rust's `str::lines`
    (split on LF, one CR before it dropped)."""
    out: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.endswith(_WHITE_SPACE_LINE_END):
            line = line[:-1]
        t = rust_trim(line)
        if not t:
            if current:
                out.append("\n".join(current))
                current = []
        else:
            current.append(t)
    if current:
        out.append("\n".join(current))
    return out


def _call(run_id: UUID, question: str, caller: ModelCaller,
          clock: Callable[[], float]) -> tuple[str, str | None, str | None, int]:
    """(status, reason, text, output tokens) of the one attempt."""
    deadline = clock() + TIME_LIMIT_SECONDS
    try:
        reply = caller.call(intro_body(question), step=STEP, run_id=run_id, deadline=deadline,
                            attempt_deadline=deadline, on_wait=lambda _s: None, max_attempts=1)
    except DeadlineReached:
        return "failed", "time", None, 0
    except RunCapReached:
        return "failed", "cap", None, 0
    except GeminiError:  # its kind is on the caller's `gemini step=intro` line
        return "failed", "api", None, 0
    out = reply.usage.output
    if reply.finish == "MAX_TOKENS":
        return "failed", "truncated", None, out
    if reply.block_reason or reply.finish in REFUSAL_FINISHES:
        return "failed", "refused", None, out
    if reply.finish != "STOP":
        return "failed", "unusable", None, out
    text = rust_trim(reply.text)
    if not text:
        return "failed", "empty", None, out
    return "done", None, text, out


def write_intro(run_id: UUID, question: str, *, caller: ModelCaller, pool: ConnectionPool,
                clock: Callable[[], float] = time.monotonic) -> str:
    """The run's Introduction: a `pending` row, one attempt, the row ended. Returns `done`,
    `failed`, or `not_started` (a row already existed, the run is gone, or the row could not be
    written). Never raises."""
    status, reason, out = "failed", "internal", 0
    try:
        try:
            started = intro_start(pool, run_id)
        except Exception as e:  # noqa: BLE001 - kind only
            log.warning("run kind=intro_not_started run_id=%s error=%s", run_id,
                        type(e).__name__)
            return "not_started"
        if not started:
            log.info("run kind=intro_not_started run_id=%s error=none", run_id)
            return "not_started"
        try:
            status, reason, text, out = _call(run_id, question, caller, clock)
        except Exception:  # noqa: BLE001 - anything else: a fixed reason, never its text
            status, reason, text = "failed", "internal", None
        try:
            intro_finish(pool, run_id, status, reason, text)
        except Exception:  # noqa: BLE001 - the row stays pending; start-up fails it `stale`
            status, reason = "failed", "store"
    except Exception:  # noqa: BLE001 - never reaches the run
        status, reason = "failed", "internal"
    log.info("run kind=intro run_id=%s status=%s reason=%s output_tokens=%d", run_id, status,
             reason or "none", out)
    return status


def start_intro(run_id: UUID, question: str, *, caller: ModelCaller, pool: ConnectionPool,
                clock: Callable[[], float] = time.monotonic) -> threading.Thread:
    """`write_intro` on its own daemon thread named `intro`, started; the run does not wait
    for it while it works."""
    t = threading.Thread(target=write_intro, args=(run_id, question),
                         kwargs={"caller": caller, "pool": pool, "clock": clock},
                         daemon=True, name="intro")
    t.start()
    return t
