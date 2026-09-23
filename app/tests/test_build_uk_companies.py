"""Tests for app/scripts/build_uk_companies.py — the slug guesser, the
identity verifier, and the corroboration rules that stop a resolving slug from
being trusted on its own.

These are the parts that decide what lands in companies.yaml, and the repo has
already been burned twice by a slug that resolved but was the wrong board
(Coveo's "coveodeven" dev board; Treewalk's first slug 404ing while a different
slug worked). Guessing slugs for short British names is worse: "wise", "tide",
"curve", "plum", "cleo" and "sky" are ordinary English words.

All offline — no network, no ATS calls.

Marked real_filters: the UK/QA counting inside verify() is only meaningful
against the live filters.yaml, since the rest of the suite pins a test config
(see conftest.py).
"""
import pytest

from app.scripts import build_uk_companies as build

pytestmark = pytest.mark.real_filters


# --- slug generation -------------------------------------------------------
def test_slug_variants_cover_brand_and_domain_shapes():
    # The two shapes UK boards use most that the repo's older guesser missed:
    # a bare brand word, and a squashed domain.
    assert "starling" in build.slug_variants("Starling Bank", ["starling"])
    assert "checkout" in build.slug_variants("Checkout.com", ["checkout", "checkoutcom"])
    assert "checkoutcom" in build.slug_variants("Checkout.com", ["checkout", "checkoutcom"])
    assert "monzo" in build.slug_variants("Monzo", [])


def test_slug_variants_handle_ampersand_and_aliases():
    slugs = build.slug_variants("Marks & Spencer", [])
    assert "marksspencer" in slugs
    assert "marksandspencer" in slugs         # "&" -> "and"
    assert "marks" in slugs                   # bare brand fallback
    assert "transferwise" in build.slug_variants("Wise", ["transferwise"])


def test_slug_variants_are_deduped_and_capped():
    slugs = build.slug_variants("Test Test Test", [])
    assert len(slugs) == len(set(slugs))
    assert len(slugs) <= 5, "each extra slug costs 5 more HTTP requests"


# --- identity: authoritative ATS board names -------------------------------
@pytest.mark.parametrize("candidate,board_name,expected", [
    ("Monzo", "Monzo", True),
    ("Tide", "Careers at Tide", True),          # real greenhouse board title
    ("Legal & General", "Legal & General", True),
    ("TP ICAP", "TP ICAP Group", True),
    ("Monzo", "Tide", False),                    # collision
    ("Wise", "Some Other Co", False),
    ("Monzo", None, None),                       # nothing authoritative to compare
    ("Zopa", None, None),
])
def test_names_agree(candidate, board_name, expected):
    assert build.names_agree(candidate, board_name) is expected


# --- identity: the text-evidence fallback (ashby/lever have no name API) ----
def _job(title="", location="", description=""):
    return {"title": title, "location": location, "description": description}


def test_multiword_brand_verifies_on_a_single_full_name_mention():
    stats = build.verify("Octopus Energy", [_job(description="Octopus Energy is a UK supplier.")])
    assert stats["identity_ok"] is True
    assert "multi-word" in stats["identity_why"]


def test_dotted_company_name_still_forms_a_strong_pattern():
    # "E.ON Next" naively splits into ["e", "on", "next"], both fragments are
    # dropped by the length filter, and only the everyday word "next" survives
    # as "evidence" — which matches "next steps" in any JD.
    stats = build.verify("E.ON Next", [_job(description="E.ON Next supplies power in the UK.")])
    assert stats["identity_ok"] is True
    assert "multi-word" in stats["identity_why"]


def test_single_everyday_word_name_is_never_verified_by_text():
    # "next steps" is in every JD; a lone "next" must not be treated as proof.
    stats = build.verify("E.ON Next", [_job(description="Requirements: Java. Next steps: apply now.")])
    assert stats["identity_ok"] is False
    assert "everyday words" in stats["identity_why"]


def test_wise_does_not_match_otherwise_by_substring():
    # The first version of this matcher used `token in blob`, so "wise" matched
    # "otherwise"/"likewise" and would have verified an unrelated board.
    stats = build.verify("Wise", [_job(description="The wise leader will otherwise succeed. Learning curve.")])
    assert stats["identity_ok"] is False


def test_single_word_brand_needs_corroboration_then_verifies():
    quiet = build.verify("Monzo", [_job(description="A fintech company.")])
    assert quiet["identity_ok"] is False, "one passing mention is not enough"
    loud = build.verify("Monzo", [
        _job(description="Monzo is a bank."),
        _job(title="Engineer at Monzo", description="Join Monzo."),
    ])
    assert loud["identity_ok"] is True


# --- the counts that decide whether a board is worth adding ----------------
def test_verify_counts_uk_london_and_gate_hits():
    jobs = [
        _job("Senior Test Automation Engineer", "London, UK", "Java and Cucumber."),
        _job("QA Automation Engineer", "Remote, United Kingdom", "Java."),
        _job("Senior Software Engineer", "Toronto, Canada", "C#."),
        _job("Marketing Manager", "London, UK", ""),
    ]
    stats = build.verify("Monzo", jobs)
    assert stats["total"] == 4
    assert stats["uk"] == 3
    assert stats["london"] == 2
    # Only the two UK QA roles pass title+location; the Toronto engineer fails
    # location, the marketing manager fails title.
    assert stats["gate"] == 2


def test_suffix_words_are_not_used_as_identity_evidence():
    # "bank" must not be what proves the board is Starling Bank.
    strong, weak = build._name_patterns("Starling Bank")
    assert "bank" not in weak
    assert "starling" in weak
