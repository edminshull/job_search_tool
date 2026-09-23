"""Offline tests for app/add_job.py — manually adding a LinkedIn posting.

These matter more than they look. The metadata stored here goes straight into
the AI prompt and onto the board, so a wrong company/title is not cosmetic: it
corrupts the fit scoring and the job's identity for dedup.

The paste parser was measured against the 16 real postings in the CV repo. On a
genuine LinkedIn clipboard paste it recovers all four fields; on those curated
multi-page documents it gets the title right about half the time, because the
title lives in their job.yaml rather than the pasted text. Hence the rule these
tests enforce: a GUESSED value is never written without --yes, while a LABELLED
one ("Company: Monzo") is trusted.

Marked real_filters because the location plausibility check consults the live
London/UK allowlist (the rest of the suite pins a Canada/Alberta test config).
"""
import os
import subprocess
import sys

import pytest

from app import add_job, dedup

pytestmark = pytest.mark.real_filters

TEST_DB = "data/test_add_job.sqlite3"

RAW_PASTE = """Senior Test Automation Engineer
Resillion · London, England, United Kingdom · 2 weeks ago
Easy Apply
About the job
We are looking for an experienced automation engineer.
Applied
1st
Some Recruiter
Talent Partner at Resillion"""


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "add_job.sqlite3")


# --- the real workflow: a raw clipboard paste ------------------------------
def test_raw_clipboard_paste_recovers_all_four_fields():
    got = add_job.parse_paste(RAW_PASTE)
    assert got["company"] == "Resillion"
    assert got["title"] == "Senior Test Automation Engineer"
    assert got["location"] == "London, England, United Kingdom"
    assert got["posted_at"] == "2 weeks ago"
    # Guessed, not authoritative — the CLI requires --yes for these.
    assert {got["company_how"], got["title_how"], got["location_how"]} == {"guessed"}


def test_labelled_fields_are_authoritative():
    got = add_job.parse_paste("QA Automation Engineer\nCompany: Monzo\nLocation: London, UK")
    assert got["company"] == "Monzo" and got["company_how"] == "labelled"
    assert got["location"] == "London, UK" and got["location_how"] == "labelled"


# --- the mis-parses that were actually observed ----------------------------
def test_does_not_invent_a_company_from_a_descriptor_line():
    # Real mis-parse: "Senior Level | Financial Services | Agile Quality
    # Engineering" produced company="Senior Level", location="Financial Services".
    got = add_job.parse_paste(
        "Lead Test Engineer\n"
        "Senior Level | Financial Services | Agile Quality Engineering\n"
        "We are looking for an experienced Lead Test Engineer."
    )
    assert got.get("company") is None
    assert got.get("location") is None
    assert got["title"] == "Lead Test Engineer"


def test_never_treats_a_salary_as_a_location():
    got = add_job.parse_paste("QA Automation Engineer\nLondon (Hybrid)\nGBP75,000 + Benefits")
    assert got.get("location") != "GBP75,000 + Benefits"


def test_markdown_chrome_is_not_a_title():
    # The CV repo's pastes carry a header comment; it was being stored as the
    # job title in 8 of 16 files before the chrome filter.
    got = add_job.parse_paste(
        "# Job posting — captured verbatim from LinkedIn paste\n"
        "> **Formatting note:** bullets were reconstructed.\n"
        "Senior Test Automation Analyst\n"
    )
    assert got["title"] == "Senior Test Automation Analyst"


def test_labelled_meta_lines_are_not_titles():
    got = add_job.parse_paste("Work Arrangement: Hybrid (3 days per week onsite)\nQA Engineer")
    assert got.get("title") != "Work Arrangement: Hybrid (3 days per week onsite)"


# --- storage behaviour ----------------------------------------------------
def test_added_job_is_visible_to_the_board_and_the_ai_queue(db):
    with dedup.connect(db) as conn:
        inserted, _ = add_job.add_job(
            conn, url="https://www.linkedin.com/jobs/view/123", company="Resillion",
            title="Senior Test Automation Analyst", location="London (hybrid)",
            description="Java and Cucumber.", source="linkedin",
        )
        assert inserted

        details = dedup.get_details_by_url(conn, "https://www.linkedin.com/jobs/view/123")
        assert details["company"] == "Resillion"
        assert details["source"] == "linkedin"
        # The board only lists passed_filters = 1 rows.
        board = conn.execute(
            "SELECT company FROM job_details WHERE passed_filters = 1").fetchall()
        assert board == [("Resillion",)]
        # And the AI step's queue picks it up, unprompted.
        queue = dedup.get_unevaluated_candidates(conn)
        assert [q["title"] for q in queue] == ["Senior Test Automation Analyst"]
        # It is recorded as seen, so a later pipeline run will not duplicate it.
        assert conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0] == 1


def test_re_adding_the_same_url_is_refused_unless_updating(db):
    url = "https://www.linkedin.com/jobs/view/999"
    with dedup.connect(db) as conn:
        assert add_job.add_job(conn, url=url, company="A", title="Test Engineer",
                               location="London", description="x")[0]
        inserted, message = add_job.add_job(conn, url=url, company="A", title="Test Engineer",
                                           location="London", description="x")
        assert not inserted and "Already stored" in message
        assert add_job.add_job(conn, url=url, company="A", title="Senior Test Engineer",
                               location="London", description="y", update=True)[0]
        assert dedup.get_details_by_url(conn, url)["title"] == "Senior Test Engineer"


# --- URL canonicalisation: the same job must never become two rows ---------
def test_linkedin_url_forms_collapse_to_one_key():
    """LinkedIn appends tracking params, and /jobs/view/<id> also comes in a
    slug form and as a search URL carrying currentJobId. All three are the same
    job, and the URL is the primary key everything hangs off — without this the
    same posting pasted twice became two tracker rows (verified: 3 rows)."""
    expected = "https://www.linkedin.com/jobs/view/4400123456/"
    for url in [
        "https://www.linkedin.com/jobs/view/4400123456/?refId=abc&trackingId=xyz",
        "https://www.linkedin.com/jobs/view/senior-qa-engineer-at-monzo-4400123456?refId=x",
        "https://www.linkedin.com/jobs/search/?currentJobId=4400123456&keywords=qa",
        "https://www.linkedin.com/jobs/view/4400123456/",
        "  https://www.linkedin.com/jobs/view/4400123456  ",
    ]:
        assert add_job.canonical_url(url) == expected, url


def test_non_linkedin_urls_are_left_alone():
    url = "https://boards.greenhouse.io/monzo/jobs/123"
    assert add_job.canonical_url(url) == url


def test_same_job_pasted_three_ways_is_stored_once(db):
    with dedup.connect(db) as conn:
        urls = [
            "https://www.linkedin.com/jobs/view/4400123456/?refId=abc&trackingId=xyz",
            "https://www.linkedin.com/jobs/view/senior-qa-engineer-at-monzo-4400123456?refId=x",
            "https://www.linkedin.com/jobs/view/4400123456/",
        ]
        results = [add_job.add_job(conn, url=u, company="Monzo", title="Senior QA Engineer",
                                   location="London, UK", description="x")[0] for u in urls]
        assert results == [True, False, False]
        assert conn.execute("SELECT COUNT(*) FROM job_details").fetchone()[0] == 1


def test_flags_a_likely_duplicate_from_another_source(db):
    """Same company/title/location under a different URL is allowed — it is
    usually the same advert syndicated through an agency — but the user should
    be told rather than silently tracking it twice."""
    with dedup.connect(db) as conn:
        add_job.add_job(conn, url="https://www.linkedin.com/jobs/view/111111111/",
                        company="Resillion", title="Senior Test Automation Analyst",
                        location="London (hybrid)", description="x", source="linkedin")
        _ok, message = add_job.add_job(conn, url="https://www.adzuna.co.uk/details/999",
                                       company="Resillion", title="Senior Test Automation Analyst",
                                       location="London (hybrid)", description="y", source="adzuna")
        assert "NOTE" in message and "another source" in message


# --- the CLI end to end (separate process: real filters.yaml, real argparse) --
def test_cli_ingests_a_paste_and_reports_it(db, tmp_path):
    paste = tmp_path / "posting.txt"
    paste.write_text(RAW_PASTE)
    url = "https://www.linkedin.com/jobs/view/4242"

    result = subprocess.run(
        [sys.executable, "-m", "app.add_job", "--db", db, "--url", url,
         "--company", "Resillion", "--title", "Senior Test Automation Engineer",
         "--location", "London (hybrid)", "--file", str(paste)],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert result.returncode == 0, result.stderr
    assert "Result       : Added" in result.stdout
    assert "http://localhost:3000" in result.stdout

    listing = subprocess.run(
        [sys.executable, "-m", "app.add_job", "--db", db, "--list"],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert "Resillion" in listing.stdout and "unscored" in listing.stdout

    removed = subprocess.run(
        [sys.executable, "-m", "app.add_job", "--db", db, "--remove", url],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    assert removed.returncode == 0 and "Removed" in removed.stdout


def test_cli_refuses_guessed_values_without_yes(db, tmp_path):
    """The safety property: --infer alone must not write a guessed company."""
    paste = tmp_path / "posting.txt"
    paste.write_text(RAW_PASTE)
    result = subprocess.run(
        [sys.executable, "-m", "app.add_job", "--db", db, "--url",
         "https://www.linkedin.com/jobs/view/1", "--infer", "--file", str(paste)],
        capture_output=True, text=True, cwd=os.getcwd(),
    )
    # The paste yields everything, but all of it is guessed, so nothing is
    # written and the missing-field error stands.
    assert result.returncode != 0
    assert "NOT used" in result.stdout
    with dedup.connect(db) as conn:
        assert dedup.get_details_by_url(conn, "https://www.linkedin.com/jobs/view/1") is None


# --- contract roles --------------------------------------------------------
CONTRACT_PASTE = """Senior QA Automation Engineer
Monzo · London, United Kingdom · 3 days ago

6 month contract, Outside IR35 determination.
Rate: £450 - £500 per day.
Java, Cucumber, RestAssured, Testcontainers."""


def test_contract_flag_stores_the_day_rate_and_ir35_verdict(db):
    """A manually-added contract role has to arrive on the board with the same
    numbers a fetched one would. This matters more here than for the pipeline:
    a LinkedIn advert is often the only place the day rate was ever stated."""
    with dedup.connect(db) as conn:
        inserted, _ = add_job.add_job(
            conn, url="https://www.linkedin.com/jobs/view/4400123456/",
            company="Monzo", title="Senior QA Automation Engineer",
            location="London, United Kingdom", description=CONTRACT_PASTE,
            posted_at="3 days ago", employment_type="contract")
        assert inserted
        got = dedup.get_details_by_url(conn, "https://www.linkedin.com/jobs/view/4400123456/")

    assert got["employment_type"] == "contract"
    assert got["ir35_status"] == "outside"
    assert (got["day_rate_min"], got["day_rate_max"]) == (450.0, 500.0)
    assert got["day_rate_source"] == "stated"
    assert got["rate_verdict"] == "pass"
    # £500/day outside IR35 is worth well over the salary floor.
    assert got["perm_equivalent"] > 90000
    # posted_at stays the free text the advert gave; lib/dates.ts parses it.
    assert got["posted_at"] == "3 days ago"


def test_contract_details_are_recorded_even_without_the_flag(db):
    """The rate and IR35 status are read from the pasted text regardless — the
    flag only sets the employment type, which an advert may not state plainly.
    So a user who forgets --contract still gets the analysis, they just do not
    get the Contract chip or the type filter."""
    with dedup.connect(db) as conn:
        add_job.add_job(
            conn, url="https://www.linkedin.com/jobs/view/4400999999/",
            company="Monzo", title="Senior QA Automation Engineer",
            location="London, United Kingdom", description=CONTRACT_PASTE)
        got = dedup.get_details_by_url(conn, "https://www.linkedin.com/jobs/view/4400999999/")

    assert got["employment_type"] == "unknown"
    assert got["ir35_status"] == "outside"
    assert got["day_rate_max"] == 500.0
    assert got["rate_verdict"] == "pass"


def test_add_job_never_rejects_on_day_rate(db):
    """Pasting a job IS the in-scope judgement (the same rule the filter
    verdict follows), so a contract below the floor is stored and flagged
    rather than refused."""
    with dedup.connect(db) as conn:
        inserted, _ = add_job.add_job(
            conn, url="https://www.linkedin.com/jobs/view/4400111111/",
            company="Acme", title="Senior QA Engineer", location="London, UK",
            description="Contract, inside IR35. Rate: £200 per day.",
            employment_type="contract")
        assert inserted
        got = dedup.get_details_by_url(conn, "https://www.linkedin.com/jobs/view/4400111111/")

    assert got["passed_filters"] == 1
    assert got["rate_verdict"] == "fail"
    assert got["rate_required"] > 300
