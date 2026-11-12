"""Writer replies for tests: the hand-written fixtures in tests/fixtures/writer (the frozen
mock's hernia numbers; no live reply) and small builders that change one field of them."""

import copy
import json
from pathlib import Path
from typing import Any

from gemini_fakes import reply

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "writer"
PART_FILES = {"trials": "trials.json", "outcomes": "outcomes.json", "context": "context.json",
              "summary": "summary.json"}


def fixture(name: str) -> dict:
    """The parsed JSON of tests/fixtures/writer/<name>."""
    return json.loads((FIXTURES / name).read_text())


def fixture_part(word: str) -> dict:
    """One part's reply, by its word (`trials`, `outcomes`, `context`, `summary`)."""
    return fixture(PART_FILES[word])


def whole(bottom_certainty: str | None = None, outcome_certainty: str | None = None,
          number: dict | None = None) -> dict:
    """`whole.json` with the bottom line's certainty, the first outcome's certainty or the
    first outcome's first number changed."""
    w = fixture("whole.json")
    if bottom_certainty is not None:
        w["bottom_line"]["certainty"] = bottom_certainty
    if outcome_certainty is not None:
        w["outcomes"][0]["certainty"] = outcome_certainty
    if number is not None:
        w["outcomes"][0]["numbers"][0].update(number)
    return w


def outcomes_part(number_overrides: dict | None = None, extra_key: str | None = None) -> dict:
    """`outcomes.json` with the first number changed, or an extra key on the first number."""
    p = fixture_part("outcomes")
    n = p["outcomes"][0]["numbers"][0]
    if number_overrides is not None:
        n.update(number_overrides)
    if extra_key is not None:
        n[extra_key] = "extra"
    return p


def json_reply(value: Any, finish: str = "STOP", out: int = 100) -> dict:
    """A Gemini 200 body whose text is `value` as JSON."""
    return reply([{"text": json.dumps(value, ensure_ascii=False)}], finish=finish, out=out)


def text_reply(text: str, finish: str = "STOP") -> dict:
    return reply([{"text": text}], finish=finish)


def deep(value: Any) -> Any:
    return copy.deepcopy(value)
