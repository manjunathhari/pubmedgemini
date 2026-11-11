import pytest

from pubmedgemini.lit.query import (
    ECHO_MESSAGE,
    ECHO_WORDS,
    EUROPEPMC_EXTRA_PUNCTUATION,
    MAX_QUERY_CHARS,
    QUERY_PUNCTUATION,
    contains_question,
    query_rejected_message,
    validate_query,
)

# Every case of Incisive tests/agent.rs query_validation_rejects_urls_newlines_and_overlong.
INCISIVE_OK = [
    '("indocyanine green"[tiab] OR ICG[tiab]) AND "anastomotic leak"[mh]',
    "hernia*[tiab] AND mesh[tiab] AND 2015:2025[dp]",
    "randomized controlled trial[pt] AND (Crohn's disease[mh] OR colitis/surgery[mh])",
    "leak, anastomotic[tiab] AND robot-assisted[tiab] AND 1.5[tiab]",
]
INCISIVE_BAD = [
    "",
    "   ",
    "ICG http://evil.example",
    "ICG HTTPS evil",
    "see www.example.com",
    "ICG\nAND leak",
    "ICG\rAND leak",
    "ICG\tleak",
    "ICG; DROP",
    "ICG {leak}",
    "ICG <source>",
    "ICG & leak",
    "ICG #1",
    "ICG leak\u2014",
    "café",
    '"unclosed quote',
    "(ICG AND leak",
    "ICG AND leak)",
    "ICG[tiab",
    "(ICG[tiab)]",
    '"a(" AND b)',
]


def test_constants_match_incisive():
    assert MAX_QUERY_CHARS == 1000
    assert QUERY_PUNCTUATION == " -\"()[]*/:,.'"
    assert EUROPEPMC_EXTRA_PUNCTUATION == "_"


@pytest.mark.parametrize("source", ["pubmed", "europepmc"])
def test_incisive_cases_carry_over(source):  # Review Focus 1
    for ok in INCISIVE_OK:
        assert validate_query(ok, source) is None, ok
    assert validate_query("a" * 1000, source) is None
    assert validate_query("a" * 1001, source) == "too_long"
    for bad in INCISIVE_BAD + ["hernia?", "ICG\u00a0leak", "x" * 999 + "é"]:
        assert validate_query(bad, source) is not None, repr(bad)
    assert validate_query('"a (b" AND c', source) is None
    assert [validate_query(q, source) for q in ["a http b", "a\nb", "a ; b", "(a", ""]] == [
        "url",
        "newline",
        "character",
        "unbalanced",
        "empty",
    ]


@pytest.mark.parametrize("source", ["pubmed", "europepmc"])
def test_kinds_come_in_incisive_order(source):
    assert validate_query("é" * 1001, source) == "too_long"  # length before character
    assert validate_query("a\nhttp", source) == "newline"  # newline before url
    assert validate_query("http é", source) == "url"  # url before character
    assert validate_query("WWW.x", source) == "url"
    assert validate_query("é (", source) == "character"  # character before unbalanced
    assert validate_query("\t", source) == "empty"  # whitespace only is empty
    assert validate_query("ICG\tleak", source) == "character"
    assert validate_query("ICG\rleak", source) == "newline"


def test_non_ascii_letters_and_digits_are_refused():
    # str.isalnum() accepts these; the rule must not.
    for q in ["café", "ICG ½", "٣ trials", "Ⅷ"]:
        assert validate_query(q, "pubmed") == "character", q
        assert validate_query(q, "europepmc") == "character", q


@pytest.mark.parametrize("source", ["pubmed", "europepmc"])
def test_brackets_must_nest(source):
    assert validate_query("([a])", source) is None
    assert validate_query("([a)]", source) == "unbalanced"
    assert validate_query("a]", source) == "unbalanced"
    assert validate_query('"[" a', source) is None


def test_underscore_is_allowed_for_europepmc_only():  # decision 6
    q = 'PUB_TYPE:"randomized controlled trial" AND PUB_YEAR:[2015 TO 2026]'
    assert validate_query(q, "europepmc") is None
    assert validate_query(q, "pubmed") == "character"


def test_message_is_incisives_text():
    assert query_rejected_message("character", "pubmed") == (
        "query rejected (character): use 1 to 1000 characters of letters, digits, spaces and "
        "- \" ( ) [ ] * / : , . ' with balanced quotes and brackets, no URLs or newlines"
    )


def test_message_names_the_underscore_for_europepmc_only():
    assert ", _ " in query_rejected_message("character", "europepmc")
    assert "_" not in query_rejected_message("character", "pubmed")
    assert query_rejected_message("url", "europepmc").startswith("query rejected (url): ")


# ---------- the echo guard (Incisive agent/mod.rs `contains_question`) ----------

Q = (
    "In adults with inguinal hernia is laparoscopic repair better than open mesh repair "
    "for chronic pain"
)


def test_echo_constants_match_incisive():
    assert ECHO_WORDS == 8
    assert ECHO_MESSAGE == (
        "query rejected (echo): it repeats the question's wording; "
        "use search terms and field tags instead"
    )


def test_echo_guard_cases():  # Review Focus 2
    assert contains_question("adults with inguinal hernia is laparoscopic repair better than", Q)
    assert contains_question(
        '"Adults, with INGUINAL hernia; is laparoscopic repair better than"[tiab]', Q)
    assert not contains_question(
        "inguinal hernia[tiab] AND laparoscop*[tiab] AND chronic pain[tiab]", Q)
    assert not contains_question("a b c d e f g", "a b c d e f g")  # fewer than 8 words
    assert not contains_question("adultswith inguinal hernia is laparoscopic repair better than", Q)


def test_incisive_echo_cases_carry_over():  # tests/agent.rs contains_question_detects_...
    q = "In adults having colorectal resection, does ICG angiography reduce anastomotic leak?"
    assert contains_question(
        "x ADULTS having  colorectal\tresection does icg ANGIOGRAPHY reduce y", q)
    assert contains_question(q, q)
    assert not contains_question("adults having colorectal resection does icg angiography", q)
    assert not contains_question(
        "adults colorectal having resection does icg angiography reduce", q)
    assert not contains_question(
        "adultsx having colorectal resection does icg angiography reduce", q)
    assert not contains_question("ICG for leak", "ICG for leak")
    assert not contains_question("", q)


def test_echo_guard_drops_punctuation_only_words_and_matches_at_the_end():
    assert not contains_question("x y open mesh repair for chronic pain", Q)  # 6 in a row
    assert contains_question("better than open mesh repair for chronic pain", Q)
    assert contains_question("better than - open mesh repair for chronic pain", Q)
    assert not contains_question("better than open mesh repair for chronic", Q)  # 7 words
