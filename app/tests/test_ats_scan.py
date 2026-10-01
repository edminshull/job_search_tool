"""Tests for the ATS scan — the deterministic half of the "Scan CV" button.

Everything here runs offline: `app/ats_scan.py` makes no model call, no network
call and no database call, which is the whole reason it was built as a pure
function over three strings. The fixtures are small synthetic postings rather
than a real advert, so each test states one rule and can be read as that rule.

The behaviour with the most tests is the one that would do real damage if it
regressed: the scan claiming a verdict it has no evidence for. A green tick on a
posting that was never really read sends someone to an interview believing they
passed a filter that was never applied, so "cannot tell" is covered from several
directions on purpose.
"""
from __future__ import annotations

from app import ats_scan


# A posting with the structure a real advert has: a requirements section, a
# duties section and a nice-to-have. Heading *isolation* (blank lines around the
# heading) is what `keywords.split_sections` keys on, so the blank lines below
# are load-bearing, not formatting.
#
# It is deliberately LONG. `ats_scan.SNIPPET_CHARS` treats anything under 1,000
# characters as a search-result teaser rather than an advert, and reports
# "unknown" instead of a verdict — so a short fixture would silently test the
# snippet path in every single case here.
POSTING = """Senior Quality Engineer

About us

We are a payments company building reliable systems for merchants across
Europe, processing millions of transactions a day. Our platform is built on
microservices, and we care a great deal about the quality of what we ship.
We are looking for a senior engineer to join the quality team and help us keep
that standard as the platform grows. You will work alongside developers,
product managers and platform engineers, and you will have real ownership of
how quality is done in your area rather than being handed a list of test cases
to execute. We invest in tooling, we expect people to automate the boring
parts, and we would rather prevent a defect than find one late in the cycle.

Requirements

Essential experience with Java, Cucumber and Selenium WebDriver.
Experience with Kubernetes and continuous integration.
Experience building and maintaining test automation frameworks from scratch.
A track record of working with Appium for mobile test automation.

What you'll be doing

You will build and extend test automation frameworks for our payments platform.
You will own the quality of the services your team ships to production.
You will work with Grafana dashboards to track suite health over time.

Nice to have

Experience with performance testing tools would be an advantage.
"""

# A CV that covers the language and framework requirements but NOT Kubernetes,
# Grafana or Appium.
CV = """Ed Minshull
Senior SDET | Test Automation & Quality Engineering

CORE SKILLS
Test Automation & Frameworks: Java, Cucumber, Selenium WebDriver, RestAssured
CI/CD, Cloud & Infrastructure: Jenkins, Docker, Maven

PROFESSIONAL EXPERIENCE

Senior SDET - Acme Payments        Jan 2020 - present
Payments. Microservices and event-driven architecture.
- Built BDD test automation frameworks in Java and Cucumber for 5 services.
- Ran continuous integration pipelines in Jenkins across the whole estate.
"""

def _master_data() -> dict:
    """The fact bank. Two roles, and the shape of the middle achievement is the
    interesting part:
      * acme-1's TEXT is on the CV verbatim, but its TAGS claim Kubernetes —
        the real-world case where a term reads as
        missing while the bullet carrying it is already printed, so the right
        advice is "reword", not "add".
      * acme-2 is not on the CV at all, so the right advice is "add".
      * oldco shares no vocabulary with the CV, so it is the missing role.
    """
    return {
        "experience": [
            {
                "id": "acme",
                "company": "Acme Payments",
                "title": "Senior SDET",
                "start": "2020-01",
                "end": "present",
                "achievements": [
                    {"id": "acme-1",
                     "text": "Built BDD test automation frameworks in Java and Cucumber for 5 services.",
                     "tags": ["java", "cucumber", "bdd", "kubernetes"]},
                    {"id": "acme-2",
                     "text": "Managed Grafana dashboards and traced defects across 20 services.",
                     "tags": ["grafana", "observability"]},
                ],
            },
            {
                "id": "oldco",
                "company": "OldCo Consulting",
                "title": "Graduate Testing Consultant",
                "start": "2015-11",
                "end": "2016-05",
                "achievements": [
                    {"id": "oldco-1",
                     "text": "Delivered functional test automation using a legacy GUI tool for an ERP rollout.",
                     "tags": ["legacy gui automation", "erp"]},
                ],
            },
        ]
    }


def _master_text() -> str:
    parts = []
    for role in _master_data()["experience"]:
        parts.extend([role["company"], role["title"]])
        for ach in role["achievements"]:
            parts.append(ach["text"])
            parts.extend(ach["tags"])
    return "\n".join(parts)


def _scan(posting: str = POSTING, cv: str = CV, **kwargs) -> dict:
    return ats_scan.scan(
        job={"url": "u1", "company": "Acme", "title": "Senior Quality Engineer"},
        posting_text=posting,
        cv_text=cv,
        master_text=_master_text(),
        master_data=_master_data(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The reliability gate
# ---------------------------------------------------------------------------

def test_a_structured_posting_is_assessable():
    result = _scan()
    assert result["ats"]["reliable"] is True
    assert result["ats"]["unreliable_reason"] is None
    assert "requirements" in result["ats"]["sections_found"]


def test_a_snippet_is_never_given_a_verdict():
    """The failure this exists to prevent, measured on the real board.

    Lendable's "Senior Quality Engineer - AI" is on the board twice: once from
    Ashby with the full 8,680-character advert, once from Adzuna with a
    500-character scrape that has no headings at all. The unheaded copy produced
    "PASS — priority-term coverage 100% (4/4)" — a confident green tick from four
    terms, on a posting the scan had not actually read. A false pass is worse
    than no answer."""
    snippet = "Lendable London, London 47,204 per year - estimated Full time CLOSING SOON Apply for this job"
    result = _scan(posting=snippet)
    assert result["ats"]["verdict"] == "unknown"
    assert result["ats"]["reliable"] is False
    assert result["ats"]["unreliable_reason"] == "snippet"
    # And the reason has to name the fix, not just the symptom.
    assert "NOT a pass and NOT a fail" in result["ats"]["reason"]


def test_the_truncation_flag_is_honoured_even_when_the_text_is_long():
    """`filters.looks_truncated` is the pipeline's own snippet test and is passed
    in, because a description can be long and still be a cut-off fragment. The
    scan must not overrule a signal it cannot see from here."""
    padded = POSTING + (" filler" * 400)
    assert _scan(posting=padded)["ats"]["reliable"] is True
    flagged = _scan(posting=padded, posting_truncated=True)
    assert flagged["ats"]["verdict"] == "unknown"
    assert flagged["ats"]["unreliable_reason"] == "snippet"


def test_a_full_posting_with_no_headings_says_so_distinctly():
    """The two ways to be unassessable have different fixes — re-fetch the advert
    vs read the employer's own board — so they must not share a message."""
    unheaded = ("We are looking for a senior quality engineer. Your Mission: "
                "perform control testing across our cloud infrastructure and SaaS landscape. "
                "Partner with key stakeholders across finance, engineering and risk. " * 12)
    result = _scan(posting=unheaded)
    assert result["ats"]["verdict"] == "unknown"
    assert result["ats"]["unreliable_reason"] == "no_headings"
    assert "headings" in result["ats"]["reason"]
    assert "snippet" not in result["ats"]["reason"]


# ---------------------------------------------------------------------------
# The verdict rule
# ---------------------------------------------------------------------------

def test_a_missing_requirement_level_hard_skill_is_a_fail():
    result = _scan()
    gaps = [g["term"] for g in result["ats"]["hard_gaps"]]
    assert "kubernetes" in gaps, gaps
    assert result["ats"]["verdict"] == "fail"
    assert "kubernetes" in result["ats"]["reason"]


def test_hard_gaps_are_requirement_level_skills_only():
    """A term mentioned once under "nice to have" is not a filter, and treating
    it as one would fail CVs for not listing things nobody asked for."""
    for gap in _scan()["ats"]["hard_gaps"]:
        assert gap["section"] == "requirements", gap
        assert gap["skill"] is True, gap


def test_a_covered_requirement_set_passes():
    cv = CV + "\nKubernetes, Grafana, Appium\n"
    result = _scan(cv=cv)
    assert result["ats"]["hard_gaps"] == []
    assert result["ats"]["verdict"] in ("pass", "borderline")


def test_coverage_is_a_percentage_of_requirement_level_terms():
    ats = _scan()["ats"]
    assert ats["priority_total"] == ats["priority_evidenced"] + len(ats["priority_gaps"])
    assert 0.0 <= ats["priority_coverage"] <= 100.0


def test_the_verdict_states_its_thresholds_rather_than_hiding_them():
    """A rule that cannot be argued with is a rule nobody can check. The scan
    returns the number its borderline band is drawn at."""
    assert _scan()["ats"]["thresholds"]["pass_coverage"] == ats_scan.PASS_COVERAGE


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

def test_a_gap_the_master_can_close_produces_a_suggestion_quoted_verbatim():
    result = _scan()
    # Grafana is a requirement-level gap the master covers, in prose, so it must
    # produce a suggestion — and that suggestion must be the master's own line.
    assert any("Grafana" in s["text"] for s in result["suggestions"]), result["suggestions"]
    master_lines = [a["text"].strip() for r in _master_data()["experience"] for a in r["achievements"]]
    for text in [s["text"] for s in result["suggestions"]]:
        # THE central guarantee: a suggestion is never generated prose. It is a
        # line from the fact bank, and if it is not in the bank it cannot appear.
        assert text in master_lines, text


def test_a_gap_the_master_cannot_close_is_reported_as_unclosable():
    """The two gap lists answer different questions and must not be merged:
    "look here in your own record" versus "you do not have this". Appium is in
    the requirements and nowhere in the master, so it is the second kind."""
    result = _scan()
    unclosable = [t["term"] for t in result["unclosable"]]
    assert "appium" in unclosable, unclosable
    closable = {t["term"] for s in result["suggestions"] for t in s["closes_terms"]}
    assert "appium" not in closable
    # And nothing appears in both — the lists are a partition, not two views.
    assert not (set(unclosable) & closable)


def test_unclosable_is_priority_scoped():
    """A word the advert used is not a thing anyone can have. Before this was
    scoped, the list filled with 'products', "you'll" and 'keep'."""
    for term in _scan()["unclosable"]:
        assert term["section"] in ("requirements", "duties", "nice-to-have"), term


def test_an_achievement_already_on_the_cv_is_a_reword_not_an_addition():
    """The scan computes "in the master, missing from the CV" from the master's
    TAGS as well as its prose, and a tag is not printed on a CV. So a term can
    read as missing while the bullet carrying it is already there. Telling
    someone to add a bullet they already have is a lie about their own CV, and a
    detectable one — hence the two kinds."""
    result = _scan()
    kinds = {s["achievement_id"]: s["kind"] for s in result["suggestions"]}
    # acme-1's wording is on the CV; only its Kubernetes TAG is not.
    assert kinds.get("acme-1") == "reword_achievement", kinds
    # acme-2 appears nowhere on the CV, so it is a genuine addition.
    assert kinds.get("acme-2") == "add_achievement", kinds


def test_suggestions_are_ranked_by_how_much_of_the_posting_they_answer():
    """Ranked by `weight_closed`, not by how many terms a line happens to
    collide with: one requirement-level hit is worth more than five passing
    mentions, and `weight` is what encodes that. Kind is only a tie-break."""
    weighted = [s["weight_closed"] for s in _scan()["suggestions"]]
    assert weighted == sorted(weighted, reverse=True), weighted


def test_overlap_is_measured_per_line_not_across_the_whole_document():
    """A document-wide word set scored an absent role's bullet at 0.8, because
    "test", "performance" and "software" appear in every QA bullet there is. The
    check has to find one line that matches, not a scattering of shared words."""
    lines = ats_scan._cv_lines("Built test automation. Ran performance suites. Software delivery.")
    scatter = "Gained grounding in test methodologies, the software lifecycle and performance testing"
    # Every word is present in the document, none of them together on one line.
    assert ats_scan._text_overlap(scatter, lines) < ats_scan.ON_CV_OVERLAP
    exact = ats_scan._cv_lines("Gained grounding in test methodologies, the software lifecycle and performance testing")
    assert ats_scan._text_overlap(scatter, exact) == 1.0


def test_suggestions_are_driven_by_priority_gaps_only():
    """Offering a bullet because it contains a word the advert used once in
    passing is how a CV grows without getting better. The first version of this
    proposed four edits to a CV it had just scored 100%."""
    result = _scan(cv=CV + "\nKubernetes Grafana Appium\n")
    if result["ats"]["priority_gaps"] == []:
        assert result["suggestions"] == []


# ---------------------------------------------------------------------------
# Roles missing from the CV
# ---------------------------------------------------------------------------

def test_a_role_absent_from_the_cv_is_reported_by_employer_name():
    missing = _scan()["roles_missing_from_cv"]
    assert [r["id"] for r in missing] == ["oldco"]


def test_a_shared_job_title_does_not_count_as_the_role_being_present():
    """The first version also accepted the job TITLE as evidence, which made the
    check useless rather than lenient: the omitted role's title shares a word
    ("consultant") with a different job that IS on the CV, so the role was
    reported present by a word it had in common with an unrelated one. The
    employer's distinctive name is now the only signal."""
    cv = CV + "\nTesting Consultant - Somewhere Else\n"
    assert [r["id"] for r in _scan(cv=cv)["roles_missing_from_cv"]] == ["oldco"]


def test_a_role_present_by_a_shortened_employer_name_is_not_reported():
    cv = CV + "\nGraduate Testing Consultant, OldCo\n"
    assert _scan(cv=cv)["roles_missing_from_cv"] == []


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------

def test_the_projection_reports_what_adding_everything_would_do():
    result = _scan()
    projection = result["projection"]
    assert projection["coverage"] >= result["ats"]["priority_coverage"]
    assert projection["verdict"] in ("pass", "borderline", "fail", "unknown")
    assert projection["closes_terms"], projection


def test_a_projection_is_never_offered_for_an_unassessable_posting():
    snippet = "too short to be an advert"
    result = _scan(posting=snippet)
    assert result["suggestions"] == []
    assert result["projection"]["verdict"] is None


# ---------------------------------------------------------------------------
# The payload the web app consumes
# ---------------------------------------------------------------------------

def test_the_payload_carries_everything_the_panel_reads():
    result = _scan()
    assert set(result) >= {"url", "company", "title", "cv_source", "ats",
                           "suggestions", "unclosable", "roles_missing_from_cv", "projection"}
    assert set(result["ats"]) >= {
        "verdict", "reason", "reliable", "unreliable_reason", "sections_found",
        "terms_found", "posting_chars", "priority_coverage", "priority_evidenced",
        "priority_total", "hard_gaps", "priority_gaps", "evidenced_priorities", "thresholds",
    }


def test_the_scan_is_deterministic():
    """Same inputs, same output. The whole reason `verdict_for` is a rule and not
    a score: two runs of the same scan must not disagree, or the number is not
    evidence of anything."""
    assert _scan() == _scan()
