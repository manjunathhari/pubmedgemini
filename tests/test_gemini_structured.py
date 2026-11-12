"""Task 2 (F3): the shared reading of a structured reply (Incisive `agent/llm.rs`
`read_structured`, `parse_strict`, `answer_shaped`, `parse_reply`, `text_fault`)."""

import json

import pytest

from pubmedgemini.gemini.client import Reply, Usage, to_openapi
from pubmedgemini.gemini.structured import Unreadable, read_structured
from pubmedgemini.researcher import studylist
from pubmedgemini.researcher.studylist import study_list_schema


def reply_text(text: str, finish: str = "STOP") -> Reply:
    return Reply(finish=finish, block_reason="", content=None, text=text, calls=(),
                 usage=Usage(0, 0, 0))


def has_a(v):
    return v if isinstance(v, dict) and isinstance(v.get("a"), int) else None


def category(text: str, typed=has_a, finish: str = "STOP") -> str:
    with pytest.raises(Unreadable) as e:
        read_structured(reply_text(text, finish), typed)
    return e.value.category


def test_strict_then_one_answer_shaped_value():
    assert read_structured(reply_text('{"a": 1}'), has_a) == {"a": 1}
    assert read_structured(reply_text('```json\n{"a": 2}\n```'), has_a) == {"a": 2}
    assert read_structured(reply_text('Here it is: {"a": 3} as asked [1].'), has_a) == {"a": 3}
    # two values: no single answer; the whole text is not one JSON value (text_fault: syntax)
    assert category('First {"a": 1} then {"a": 2}') == "syntax"
    # one JSON value holding two answers: valid JSON, no single answer (shape)
    assert category('[{"a": 1}, {"a": 2}]') == "shape"


def test_a_value_the_reader_refuses_is_skipped_in_prose():
    assert read_structured(reply_text('{"b": 1} and then {"a": 4}'), has_a) == {"a": 4}


def test_cut_off_is_never_read():
    assert category('{"a": 1}', finish="MAX_TOKENS") == "truncated"


def test_typed_none_is_shape():
    assert category('{"a": "one"}') == "shape"


def test_not_json_is_syntax():
    assert category("no json here") == "syntax"


def test_lone_surrogate_is_refused():
    assert category('{"a": 1, "b": "\\ud800"}') == "syntax"  # serde refuses it while parsing


def test_nan_is_not_json():
    assert category('{"a": NaN}') == "syntax"


def test_studylist_reexports_the_same_unreadable():
    assert studylist.Unreadable is Unreadable


def test_study_list_schema_has_no_property_ordering():
    # D7 (owner, 12 Nov 2026): Gemini's default order; F2's schema is unchanged.
    assert "propertyOrdering" not in json.dumps(to_openapi(study_list_schema()))
