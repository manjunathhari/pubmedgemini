import pytest

from pubmedgemini.lit.design import DESIGN_LABELS, design_label


def test_labels():
    assert DESIGN_LABELS == (
        "guideline", "systematic review", "meta-analysis", "RCT", "prospective cohort", "other")


# Every assertion of Incisive tests/agent.rs evidence_level_from_pub_types_and_title (the
# rank assertions aside: F1 stores the label only), EvidenceLevel mapped to DESIGN_LABELS.
@pytest.mark.parametrize(
    ("title", "types", "label"),
    [
        ("x", ["Practice Guideline"], "guideline"),
        ("x", ["Guideline"], "guideline"),
        ("x", ["Systematic Review"], "systematic review"),
        ("x", ["Meta-Analysis", "Review"], "meta-analysis"),
        ("x", ["Randomized Controlled Trial"], "RCT"),
        ("A prospective cohort", ["Journal Article"], "prospective cohort"),
        ("x", ["Observational Study"], "other"),
        ("A prospective study of ICG", ["Observational Study"], "prospective cohort"),
        ("A retrospective review", ["Observational Study"], "other"),
        ("ICG: a meta-analysis", [], "meta-analysis"),
        ("A randomised trial", [], "RCT"),
        ("A non-randomized comparison", [], "other"),
        ("Consensus statement on ICG", [], "guideline"),
        ("Case report", ["Case Reports"], "other"),
        ("Adherence to guidelines in a retrospective cohort", ["Journal Article"], "other"),
        ("Comment on: meta-analysis of ICG angiography", ["Comment"], "other"),
        ("Randomized controlled trial of ICG", ["Letter", "Randomized Controlled Trial"], "other"),
        ("A Delphi consensus on ICG use", ["Journal Article"], "other"),
        ("ICG trial: a systematic review of designs", ["Randomized Controlled Trial"], "RCT"),
        ("Guidelines for ICG use", [], "guideline"),
        ("European clinical practice guidelines on ICG", [], "guideline"),
        ("Consensus guideline on ICG", [], "guideline"),
        ("Randomised trials are hard", [], "RCT"),
        ("Randomized patients in surgery", [], "other"),
        ("A prospective look at ICG", [], "other"),
        ("Non-randomized controlled trial", [], "other"),
    ],
)
def test_design_label_cases(title, types, label):
    assert design_label(title, types) == label


def test_type_names_ignore_case_and_spaces():
    assert design_label("x", ["  randomized controlled TRIAL "]) == "RCT"
    assert design_label("x", [" letter", "Meta-Analysis"]) == "other"
    assert design_label("x", ["Consensus Development Conference, NIH"]) == "guideline"


def test_types_decide_in_incisive_order():
    assert design_label("x", ["Randomized Controlled Trial", "Meta-Analysis"]) == "meta-analysis"
    assert design_label("x", ["Meta-Analysis", "Systematic Review"]) == "systematic review"
    assert design_label("x", ["Systematic Review", "Guideline"]) == "guideline"


def test_title_patterns():
    assert design_label("Quasi-randomized trial of ICG", []) == "other"
    assert design_label("A nonrandomised trial", []) == "other"
    assert design_label("A non randomized trial; then a randomized trial", []) == "RCT"
    assert design_label("ICG metaanalyses", []) == "meta-analysis"
    assert design_label("A prospective study", []) == "prospective cohort"
    assert design_label("-- Guideline: ICG", []) == "guideline"
    assert design_label("Systematic reviews of ICG", []) == "other"  # Incisive: singular only
