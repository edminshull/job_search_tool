"""Tests for the language-fit and security-clearance screening in
app/filters.py.

Both of these decide what Ed never sees, so the tests are as much about what
must NOT be rejected as about what must be:

  * a posting that names no language at all is NOT a mismatch — 40 of the 92
    postings on the live board are in that state, and rejecting silence would
    throw away real roles;
  * "must be ELIGIBLE for SC" is not "must already hold SC" — the second is
    unreachable, the first is routine for the successful candidate.

Every clearance fixture below is real advert text, taken from
data/seen_jobs.sqlite3. The patterns in filters.yaml were derived from those
examples and are pinned here so a future edit cannot quietly turn an
"eligible" posting into a dropped one.

Marked real_filters: these ARE tests of the live filter policy, so they must
run against the real filters.yaml rather than the pinned client-test config.
"""
import pytest

from app import filters

pytestmark = pytest.mark.real_filters


def job(title, description, location="London, UK"):
    return {"title": title, "description": description, "location": location}


# ---------------------------------------------------------------------------
# Language fit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Java and Selenium test automation", "priority"),
    ("Strong Ruby on Rails and RSpec experience", "priority"),
    ("Groovy and Spock based automation framework", "priority"),
    ("JUnit and Maven, working on a Spring Boot service", "priority"),
    # JavaScript is acceptable but not the target, so it is a tier below.
    ("Automation with JavaScript and Node.js", "secondary"),
    # A dealbreaker with nothing from the priority/secondary tiers.
    ("Python and pytest test automation", "mismatch"),
    ("Playwright end-to-end tests", "mismatch"),
    ("C# and .NET test automation", "mismatch"),
    # Silence is NOT a mismatch — the single most important assertion here.
    ("We need an experienced test automation engineer", "none"),
    ("Automation frameworks, tooling standards, quality gates", "none"),
])
def test_language_tier(text, expected):
    assert filters.language_fit(job("SDET", text))["language_tier"] == expected


def test_a_java_role_mentioning_python_is_still_a_java_role():
    """The whole reason dealbreakers are conditional. Real postings say
    "Java, Python or JavaScript" and must not be rejected for the Python."""
    got = filters.language_fit(job("SDET", "Java, Python and Selenium for test automation"))
    assert got["language_tier"] == "priority"
    assert "java" in got["language_hits"]
    assert not filters.jd_stack_mismatch("Java, Python and Selenium for test automation")


def test_javascript_is_kept_off_the_java_match():
    """'\\bjava\\b' must not fire inside 'JavaScript', or every front-end role
    would be reported as a Java role.

    Asserted against the parsed hit LIST, not a substring test: "java" IS a
    substring of "javascript", so `"java" not in hits` would pass or fail for
    the wrong reason depending on which other words matched."""
    assert filters.language_fit(job("SDET", "JavaScript and Node.js"))["language_tier"] == "secondary"
    hits = filters.language_fit(job("SDET", "JavaScript"))["language_hits"].split(", ")
    assert hits == ["javascript"]
    assert "java" not in hits


def test_priority_rank_sorts_above_the_rest():
    ranks = {t: filters.language_fit(job("SDET", d))["language_rank"] for t, d in (
        ("priority", "Java"), ("secondary", "JavaScript"), ("none", "no stack named"),
        ("mismatch", "Python"))}
    assert ranks["priority"] > ranks["secondary"] > ranks["none"] > ranks["mismatch"]


def test_hits_name_the_words_so_the_board_can_explain_itself():
    got = filters.language_fit(job("SDET", "Java, JUnit and Maven automation"))
    assert set(got["language_hits"].split(", ")) == {"java", "junit", "maven"}


def test_the_reported_language_survives_a_combined_priority_and_dealbreaker():
    """Both lists match, so the evidence has to name both — otherwise the
    board would show "java" and hide why the posting was still a concern."""
    got = filters.language_fit(job("SDET", "Java and Python, using Playwright"))
    assert got["language_tier"] == "priority"
    assert "java" in got["language_hits"] and "python" in got["language_hits"]


# ---------------------------------------------------------------------------
# Security clearance — blockers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,description", [
    # Real: VIQU IT Recruitment. Clearance is in the TITLE here, which is why
    # the check reads title and description together.
    ("SC Cleared Lead Automation Engineer", "Hybrid, inside IR35, hands-on automation."),
    ("Test Engineer (SC)", "Essential Requirements Active SC Clearance."),
    ("Automation Test Engineer", "£560 per day Inside IR35 - Umbrella only Active SC clearance required"),
    ("Senior QA / Test Engineer", "Due to the nature of the programme, candidates must hold current SC Clearance"),
    ("Senior Automation Test Engineer", "Senior Automation Test Engineer - eSC or eDV Clearance Required"),
    ("Test Analyst", "Clearance: Active SC Clearance Required"),
    ("QA Tester", "QA Tester - Must have Active SC Cleared for this role"),
    ("Automation Engineer", "candidates who have held Security Clereance in the past or hold an active SC status"),
    ("Test Automation Engineer", "Test Automation Engineer (DV Cleared) - London"),
])
def test_postings_requiring_held_clearance_are_blocked(title, description):
    got = filters.clearance_check(job(title, description))
    assert got["clearance_status"] == "blocked", f"expected blocked for {title!r}"
    assert filters.clearance_reject_reason(job(title, description))
    assert not filters.passes_content_filters(job(title, description))


# ---------------------------------------------------------------------------
# Security clearance — NOT blockers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title,description,why", [
    # Real: Capgemini. They SPONSOR the clearance; the requirement is
    # residency, which is a different question from holding one today.
    ("SAP Lead Automation Test Engineer",
     "Once successfully appointed to this role, it is a requirement to obtain Security Check (SC) clearance. "
     "To obtain SC clearance, the successful applicant must have resided continuously within the United Kingdom.",
     "they obtain it for you"),
    # Real: RedTech. The clearance is a nice-to-have, not a requirement.
    ("QA Automation Engineer",
     "You must be eligible to obtain UK SC Security Clearance due to the nature of some projects. "
     "Existing SC Clearance would be a benefit.",
     "beneficial, not required"),
    # Real: Anson Mccade. BPSS is baseline pre-employment screening.
    ("Automation Tester",
     "Clearance: BPSS to start, with eligibility for Home Office SC",
     "BPSS is screening, not clearance"),
    # Real: NTT. Explicitly not mandated.
    ("Senior QE Test Engineer",
     "BPSS minimum, SC not mandated but strong likelihood may be required",
     "explicitly not mandated"),
    # Real: Genesis. Offers a non-SC alternative.
    ("Test Engineer", "Location: London Clearance: SC-cleared / Non-SC Start: Immediate",
     "a non-SC route exists"),
    # Real: Appvia.
    ("Software Developer in Test",
     "Important: You must be eligible for UK Government Security Clearance (SC): British Passport or ILR",
     "eligibility, not possession"),
])
def test_obtainable_clearance_is_flagged_but_kept(title, description, why):
    got = filters.clearance_check(job(title, description))
    assert got["clearance_status"] == "possible", f"{title!r}: expected possible ({why})"
    assert got["clearance_evidence"]
    # Kept, not dropped.
    assert filters.clearance_reject_reason(job(title, description)) is None
    assert filters.passes_content_filters(job(title, description))


def test_a_posting_that_never_mentions_clearance_is_none():
    got = filters.clearance_check(job("Senior QA Engineer", "Java, Selenium, Cucumber. London hybrid."))
    assert got["clearance_status"] == "none"
    assert got["clearance_evidence"] is None


@pytest.mark.parametrize("text", [
    # The regression that made this gate necessary: ordinary JD boilerplate
    # was classifying postings as clearance-related. Measured on the live
    # corpus before the mention gate existed, this flagged 85 postings
    # including "Freelance Copywriter" and "Remote Office Assistant".
    "Ability to work independently and manage your own workload",
    "We offer a bonus scheme and flexible working",
    "Excellent communication skills, self-motivated, able to prioritise",
    "This is a preferred qualification but not essential",
])
def test_generic_boilerplate_is_not_clearance(text):
    assert filters.clearance_check(job("QA Engineer", text))["clearance_status"] == "none"


def test_clearance_rejection_can_be_turned_off(monkeypatch):
    """The rule is a config flag, not hardcoded, so a posting Ed could
    actually take can always be brought back."""
    monkeypatch.setattr(filters, "CLEARANCE_REJECT_BLOCKED", False)
    j = job("Test Engineer (SC)", "Active SC Clearance required")
    assert filters.clearance_check(j)["clearance_status"] == "blocked"
    assert filters.clearance_reject_reason(j) is None
    assert filters.passes_content_filters(j)


# ---------------------------------------------------------------------------
# The one place that names the cause
# ---------------------------------------------------------------------------

def test_rejection_reason_distinguishes_the_causes():
    """The run report used to call every failure a "day rate" failure. This
    is what stops a clearance drop being reported as a money drop."""
    assert "title" in filters.rejection_reason(job("Marketing Manager", "Java"))
    assert "location" in filters.rejection_reason(job("SDET", "Java", location="Toronto, Canada"))
    assert "language" in filters.rejection_reason(job("SDET", "Python and pytest automation"))
    assert "clearance" in filters.rejection_reason(
        job("Test Engineer (SC)", "Active SC Clearance required, Java"))
    assert filters.rejection_reason(job("SDET", "Java, Selenium", location="London, UK")) is None


def test_screening_signals_bundles_both_checks():
    got = filters.screening_signals(job("SDET", "Java and Selenium", location="London, UK"))
    assert got["language_tier"] == "priority"
    assert got["clearance_status"] == "none"
    assert "language_rank" in got and "clearance_evidence" in got
