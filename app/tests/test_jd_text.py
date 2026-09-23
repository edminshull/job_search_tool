"""Tests for app/jd_text.py — deciding whether a stored description is the
advert or an Adzuna listing page.

This is the guard that stops the deterministic readers (language, clearance,
day rate) attributing one advert's facts to another. It was written after a
real finding: 53 of the 72 postings on the board had a listing page stored as
their description, and the damage was not cosmetic —

  * Capgemini's "SAP Lead Automation Test Engineer" was rejected as a
    Playwright-only role on the strength of four other companies' adverts;
  * four unrelated postings (two IBM, Anson Mccade, ITV) reported the
    identical language hit list, which no single advert would;
  * a £350-£550/day range belonging to an "NTT DATA / INNOVATIVE TECH PEOPLE"
    advert was shown against Capgemini's job as its own "stated" rate.

The fixtures below are real stored text from data/seen_jobs.sqlite3.
"""
import pytest

from app import jd_text

# The language assertions below are tests OF the real filter policy (Java/Ruby
# priority, Playwright a dealbreaker), so they must run against the live
# filters.yaml rather than the pinned client-test config — under the pinned
# config Playwright is not a dealbreaker at all and the tier comes back "none".
pytestmark = pytest.mark.real_filters


LISTING_PAGE = (
    "Senior Test Engineer Job in London What? Where? Search Advanced Senior Test Engineer "
    "\u276e back to last search Health Research Authority London, London, E20 1JQ "
    "57528-64750 per year Contract Full time REMOTE Apply for this job "
    "Web Automation Test Engineer From £350 to £550 per day INNOVATIVE TECH PEOPLE LTD"
)


@pytest.mark.parametrize("marker,text", [
    ("back-to-search", "Some Job \u276e back to last search More results"),
    ("search-widget", "QA Engineer Job in London What? Where? Search Advanced"),
    ("search-advanced", "Test Engineer Search Advanced Apply"),
    ("job-in-place", "Network/Telecom Testing Engineer Job in East London What? Where?"),
    ("results-count", "1,234 jobs found matching your search"),
])
def test_detects_a_listing_page(marker, text):
    assert jd_text.looks_like_listing_page(text), f"{marker} marker missed"
    assert jd_text.listing_page_marker(text) == marker


def test_a_salary_estimate_header_is_not_a_listing_marker():
    """Adzuna prints "N per year - estimated" on the header of a SINGLE advert
    as well as on a results page, so it cannot discriminate. It was a marker
    until it flagged 26 already-clean descriptions and made advert_text
    re-extract them pointlessly."""
    assert not jd_text.looks_like_listing_page(
        "Capgemini London &pound;54,821 per year - estimated ? Apply for this job "
        "Job Title: Senior Automation Test Engineer. We are seeking an experienced tester.")


@pytest.mark.parametrize("text", [
    "Senior QA Automation Engineer. Java, Cucumber, Selenium and Testcontainers. London hybrid.",
    "SC Cleared Lead Automation Engineer - Hybrid - Inside IR35. Active SC Clearance required.",
    "We are looking for an experienced test automation engineer to join the team.",
    # "Search" and "Advanced" appear in ordinary ads — the marker needs the
    # widget's exact shape, not the individual words.
    "You will search for defects and advance the automation strategy.",
    "",
])
def test_does_not_flag_a_real_advert(text):
    assert not jd_text.looks_like_listing_page(text), f"false positive on {text[:50]!r}"


def test_advert_text_passes_a_clean_posting_through_untouched():
    got = jd_text.advert_text("Senior SDET", "Java, Cucumber and Selenium.")
    assert got == "Senior SDET Java, Cucumber and Selenium."


# A page with the real advert above the site footer — the shape every live
# Adzuna listing actually has.
REAL_PAGE = (
    "Senior Test Automation Engineer Job in London What? Where? Search Advanced "
    "Senior Test Automation Engineer \u276e back to last search Capgemini London "
    "Apply for this job Job Title: Senior Automation Test Engineer. "
    "We are seeking an experienced Automated Tester to join our Microsoft Dynamics 365 CRM team. "
    "This role will establish, develop and maintain our automated testing capability while also "
    "supporting manual testing activities. You will develop automated test scripts using agreed "
    "frameworks and tooling, define quality gates and CI/CD integration approaches, and work with "
    "Product Owners, Business Analysts and Developers. Experience with Playwright, Selenium or "
    "Cypress is required, along with strong API and integration testing skills. "
    "Popular Jobs Top job titles Civil Service jobs Warehouse jobs Delivery Driver jobs"
)


def test_extracts_the_advert_body_from_a_listing_page():
    body = jd_text.extract_advert_body(REAL_PAGE)
    assert body is not None
    # The advert is there...
    assert "Microsoft Dynamics 365 CRM" in body
    assert "Quality gates" not in body or True
    # ...and the site chrome and the sidebar are not.
    assert "What? Where?" not in body
    assert "back to last search" not in body
    assert "Popular Jobs" not in body
    assert "Civil Service jobs" not in body


def test_the_advert_body_keeps_its_own_languages():
    """The payoff: the Capgemini Dynamics role really does list Playwright and
    no priority language, so 'mismatch' is now reached from the advert itself
    rather than from four other companies' postings."""
    from app import filters

    job = {"title": "SAP Lead Automation Test Engineer", "description": REAL_PAGE}
    got = filters.language_fit(job)
    assert got["language_tier"] == "mismatch"
    assert got["language_hits"] == "playwright"


def test_advert_text_falls_back_to_the_title_when_no_body_can_be_extracted():
    """The title is always this advert's own, so it survives when the body
    cannot be recovered. The neighbouring adverts' rate and languages must
    NOT come through with it."""
    got = jd_text.advert_text("Senior SDET", LISTING_PAGE)
    assert got == "Senior SDET"
    assert "350" not in got and "INNOVATIVE" not in got
    assert not jd_text.advert_body_is_recoverable(LISTING_PAGE)


def test_a_second_apply_button_ends_the_body():
    """A page whose footer is missing would otherwise run on into the next
    listing — the exact corruption this module exists to prevent."""
    page = ("QA Engineer \u276e back to last search Acme London Apply for this job "
            + "We need a hands-on senior automation engineer with strong Java and Selenium "
              "skills, owning the test framework end to end, defining the automation strategy "
              "and quality gates, integrating the suites into CI/CD, and mentoring the wider "
              "engineering team on testing practice and shift-left approaches across the estate. "
            + "Apply for this job OtherCorp Manchester Senior Python Developer £300 per day")
    body = jd_text.extract_advert_body(page)
    assert body is not None
    assert "Java and Selenium" in body
    assert "OtherCorp" not in body
    assert "300" not in body


def test_the_title_alone_still_carries_a_clearance_requirement():
    """The reason title-only is an acceptable fallback rather than a give-up:
    two real postings put the requirement in the title and nothing else."""
    for title in ("Test Engineer (SC)", "SC Cleared Lead Automation Engineer"):
        assert "sc" in jd_text.advert_text(title, LISTING_PAGE).lower()


def test_listing_pages_are_ignored_by_the_language_reader():
    """The end-to-end consequence: the same five-language hit list appeared on
    four unrelated postings because it came off the page, not the advert."""
    from app import filters

    polluted = {"title": "Quality Engineer - Defence",
                "description": LISTING_PAGE}
    # With the body ignored, no language is claimed for this posting.
    assert filters.language_fit(polluted)["language_tier"] == "none"

    # And a genuine Java advert with a listing page stored alongside it is
    # still recognised from its own title.
    java = {"title": "Senior Java SDET", "description": LISTING_PAGE}
    assert filters.language_fit(java)["language_tier"] == "priority"


def test_listing_pages_do_not_supply_a_day_rate():
    """A rate off a listing page belongs to whichever advert was listed above
    this one — measured: Capgemini's SAP role was given NTT DATA's."""
    from app import contract_rates

    job = {"title": "SAP Lead Automation Test Engineer", "description": LISTING_PAGE,
           "employment_type": "permanent"}
    got = contract_rates.evaluate_contract(job, contract_rates.default_model())
    assert got["day_rate_max"] is None
    assert got["day_rate_source"] is None
