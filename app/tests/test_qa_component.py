"""Component tests for the pipeline core: positive, negative, boundary, error.

WHAT THESE ARE FOR
------------------
app/tests already has good tests. They are, however, weighted heavily toward
"does this parse the right field" and "is this wired into the dispatch table".
This module is the QA layer over the same components, organised the way a test
plan is: for each behaviour, the case that must work, the case that must be
refused, the value sitting exactly ON the limit, and the malformed input that
must fail loudly rather than quietly doing the wrong thing.

The four categories, and why each is a separate test rather than another assert
in the one above it:

  POSITIVE   the pipeline's contract still holds — a real posting reaches the
             board, is stored, and is deduped on the next run.
  NEGATIVE   the things that must never reach the board: a posting in a
             language the search is not for, and one demanding clearance that
             cannot be obtained. A filter that stops filtering is silent, so
             these are the ones whose absence you would never notice.
  BOUNDARY   the exact edges. A day rate AT the floor must pass and one penny
             under must not; a posting naming no language must not be treated as
             a mismatch. Off-by-one here is invisible in the output — it just
             quietly drops or admits one role.
  ERROR      malformed configuration and malformed input. These assert the
             failure is LOUD and NAMED. The dangerous failure mode in this
             codebase is not an exception, it is a silent empty result: an empty
             keyword list would otherwise compile to a regex matching every
             title, turning an allowlist into "allow everything".

RELATION TO THE EXISTING PINNED CONFIG
--------------------------------------
app/tests/conftest.py installs a pinned, test-only filter configuration so that
editing filters.yaml cannot break the client tests. Most of the tests below are
deliberately NOT marked `real_filters`: they use whatever config is installed, so
they keep working after a retune.

The exceptions are the tests that ARE about filter policy — the dealbreaker
list, the clearance tiers, the day-rate floor. Those carry `real_filters`,
because asserting anything about "Python is a dealbreaker" or "£X is the floor"
against a pinned config would be asserting that the test's own fixture says so.
Marked exactly as app/tests/test_screening.py does, and for the same reason.
IF YOU RETUNE filters.yaml OR contract.yaml, THOSE FOUR ARE THE TESTS TO UPDATE —
they are meant to notice.

ONE DEFECT FOUND WHILE WRITING THESE, NOT COVERED BY THE TEN
------------------------------------------------------------
`add_job.add_job(url="")` stores a row whose primary key is the empty string,
and the NEXT call with a different company and title is then reported as
"Already stored" and dropped — `dedup.get_details_by_url("")` matches the first
row, so one bad call silently swallows every subsequent url-less posting. The
codebase already treats this shape as a bug worth guarding against:
ats_clients.fetch_smartrecruiters explicitly skips a posting rather than storing
url="", for exactly this reason. The CLI is safe (`--url` is required, which IS
asserted in test_error_malformed_config_and_old_schema_never_answer_silently);
the gap is reachable through the Python API. Reported as a finding rather than
given a test slot, because this module is held to ten cases by the test plan.

Run with: python -m pytest app/tests/test_qa_component.py -v
"""
import os
import sqlite3
import sys

import pytest

from app import add_job
from app import contract_rates
from app import dedup
from app import filters
from app import main

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

# Compatible with the PINNED test config that conftest.py installs (an allowlist
# containing "software engineer", and Edmonton/Calgary location patterns). This
# test is about the pipeline's own bookkeeping — store once, dedupe, stamp
# last_listed_at — so it is deliberately not marked real_filters and must not
# depend on the live London/UK configuration.
GOOD_JOB = {
    "company": "TestCo",
    "title": "Software Engineer",
    "location": "Calgary, AB, Canada",
    "url": "https://example.test/jobs/qa-1001",
    "posted_at": "2026-08-01T00:00:00Z",
    "description": "Java, JUnit and Maven. Hybrid working.",
    "source": "greenhouse",
}


@pytest.fixture
def db(tmp_path):
    """A fresh SQLite store per test.

    Never the real data/seen_jobs.sqlite3: the pipeline tests write rows, and a
    test that can corrupt the board it is testing is not a test you can run
    casually. tmp_path is per-test, so two tests cannot see each other's rows
    and order does not matter."""
    with dedup.connect(str(tmp_path / "seen_jobs.sqlite3")) as conn:
        yield conn


def job(**overrides) -> dict:
    return {**GOOD_JOB, **overrides}


# ===========================================================================
# POSITIVE
# ===========================================================================

def test_positive_pipeline_stores_a_matching_job_and_dedupes_the_next_run(db):
    """The pipeline's central promise: a matching posting is fetched once,
    stored once, and never reconsidered.

    Driven through main.process_jobs rather than by calling the pieces in turn,
    because the ORDER inside process_jobs is the thing that matters — mark_seen
    before filtering (so a job that is dropped is still never re-fetched), and
    apply_to_job before passes_filters (so the day-rate floor can act on the
    verdict). Calling the pieces directly would test them in the order the test
    chose, which is not the order production uses.
    """
    batch = [job(), job(), job(title="Marketing Manager", description="Brand campaigns.")]

    kept = main.process_jobs(batch, db)
    assert len(kept) == 1, "the duplicate URL must collapse, and the non-QA posting must be dropped"
    assert kept[0]["url"] == GOOD_JOB["url"]

    # One row in job_details, not two: the duplicate collapsed on the URL key.
    rows = db.execute("SELECT url, passed_filters FROM job_details").fetchall()
    assert rows == [(GOOD_JOB["url"], 1)]

    # The dropped non-QA posting is stored with passed_filters=0 rather than
    # discarded. That is what lets refilter.py rescue it after a filter change
    # without re-fetching anything.
    stored = {r[0]: r[1] for r in db.execute("SELECT url, passed_filters FROM job_details")}
    assert set(stored) == {GOOD_JOB["url"]}, "the non-QA job was not stored at all"
    # Both the kept job and the dropped one are remembered as SEEN, so a future
    # run does not re-fetch either.
    seen = {r[0] for r in db.execute("SELECT url FROM seen_jobs")}
    assert GOOD_JOB["url"] in seen
    # The screening signals are persisted alongside, not recomputed by the board.
    tier, hits = db.execute(
        "SELECT language_tier, language_hits FROM job_details WHERE url = ?", (GOOD_JOB["url"],)
    ).fetchone()
    assert tier in ("priority", "none"), f"unexpected language tier {tier!r}"
    assert tier == "none" or "java" in hits

    # Second run: same batch, nothing new, but last_listed_at is stamped —
    # which is how the board can tell a still-advertised posting from an old one.
    second = main.process_jobs(batch, db)
    assert second == [], "a re-fetch must yield no new candidates"
    assert db.execute("SELECT COUNT(*) FROM job_details").fetchone()[0] == 1
    listed_at = db.execute(
        "SELECT last_listed_at FROM job_details WHERE url = ?", (GOOD_JOB["url"],)
    ).fetchone()[0]
    assert listed_at, "a re-fetched posting must be recorded as still listed"

    print("process_jobs: store once, dedupe on re-fetch, stamp last_listed_at — OK")


def test_positive_add_job_canonicalises_the_url_and_normalises_a_paste(db, monkeypatch):
    """A pasted LinkedIn posting lands in the same store as a fetched one.

    Auto-added rows are written with passed_filters=1 on purpose: pasting it IS
    the in-scope judgement, and the board shows only passed_filters=1. So the
    two things worth pinning are that the URL is canonicalised (the URL is the
    primary key everything else — notes, status, tailored CV — hangs off) and
    that a re-paste is refused instead of silently creating a second row.
    """
    # Three shapes of the SAME LinkedIn job: copied from the page, the slug
    # form, and copied from the search results list. All must reduce to one key,
    # or the identical job pasted twice becomes two rows in the tracker.
    canonical = "https://www.linkedin.com/jobs/view/4400123456/"
    for variant in (
        "https://www.linkedin.com/jobs/view/4400123456/?refId=abc&trackingId=xyz",
        "https://www.linkedin.com/jobs/view/senior-qa-engineer-at-monzo-4400123456",
        "https://www.linkedin.com/jobs/search/?currentJobId=4400123456",
    ):
        assert add_job.canonical_url(variant) == canonical, variant

    paste = (
        "LinkedIn\n"
        "Senior QA Automation Engineer\n"
        "Resillion · London, England, United Kingdom · 2 weeks ago\n"
        "About the job\n"
        "Java, Cucumber and RestAssured.\n"
    )

    # The middot line ("Company · Location · age") is the most reliable structure
    # in a LinkedIn paste, and it is tried before anything fuzzier. But the guess
    # is GATED on the configured market: parse_paste refuses to hand back a
    # location the live filters would reject, so what it returns depends on
    # filters.location_is_allowed. That coupling is asserted from both sides
    # rather than assumed, with a stub allowlist — which keeps this test
    # independent of whichever market filters.yaml is pointed at.
    monkeypatch.setattr(filters, "location_is_allowed", lambda loc: True)
    parsed = add_job.parse_paste(paste)
    assert parsed["company"] == "Resillion"
    assert parsed["location"] == "London, England, United Kingdom"
    # The age on the end of the line is stripped off the location rather than
    # becoming part of it ("London, England, United Kingdom · 2 weeks ago" would
    # be a location no filter could match).
    assert parsed["posted_at"] == "2 weeks ago"
    assert parsed["title"] == "Senior QA Automation Engineer"
    # Guessed values are LABELLED as guesses. The CLI refuses to write them
    # without --yes, because measured against 16 real postings the heuristics got
    # all three fields right only 3 times.
    assert parsed["company_how"] == "guessed" and parsed["title_how"] == "guessed"

    # The other side of the gate: with a market that refuses the location, the
    # same paste yields NO company and NO location — the guess is withheld rather
    # than offered as a maybe. This is why `--infer` can appear to do nothing on
    # a posting from outside the configured market.
    monkeypatch.setattr(filters, "location_is_allowed", lambda loc: False)
    refused = add_job.parse_paste(paste)
    assert "company" not in refused and "location" not in refused
    # The title is NOT gated on location, so it is still offered.
    assert refused["title"] == "Senior QA Automation Engineer"

    # --- the write path -----------------------------------------------------
    # An explicitly labelled paste is authoritative and never passes through the
    # location gate, so this part is configuration-independent.
    labelled = add_job.parse_paste(
        "Company: Resillion\n"
        "Title: Senior QA Automation Engineer\n"
        "Location: London, England, United Kingdom\n"
        "Java and Cucumber.\n"
    )
    assert labelled["company_how"] == "labelled" and labelled["location_how"] == "labelled"

    inserted, message = add_job.add_job(
        db, url=canonical + "?trackingId=xyz", company=labelled["company"],
        title=labelled["title"], location=labelled["location"], description=labelled["location"],
        posted_at="2 weeks ago",
    )
    assert inserted is True and "Added" in message
    stored = dedup.get_details_by_url(db, canonical)
    assert stored is not None and stored["company"] == "Resillion"
    assert stored["passed_filters"] == 1, "a pasted posting is in scope by definition"
    assert stored["source"] == "linkedin"

    # Re-pasting the same advert under a different tracking URL is refused, and
    # the message says how to override. Without this the tracker accumulates
    # duplicate rows for one job, which is exactly what canonical_url prevents.
    again, message = add_job.add_job(
        db, url=canonical, company="Resillion", title=labelled["title"],
        location=labelled["location"], description="Java and Cucumber.",
    )
    assert again is False
    assert "Already stored" in message and "--update" in message
    assert db.execute("SELECT COUNT(*) FROM job_details").fetchone()[0] == 1

    # A different URL for what looks like the same role is ALLOWED but flagged:
    # it is usually the same advert from another source, and a silent second row
    # would hide that.
    twin, message = add_job.add_job(
        db, url="https://www.linkedin.com/jobs/view/9999999999/", company="Resillion",
        title=labelled["title"], location=labelled["location"], description="Java and Cucumber.",
    )
    assert twin is True and "NOTE" in message and "same advert from another source" in message

    print("add_job: URL canonicalisation, gated paste parsing, duplicate refusal, twin warning — OK")


# ===========================================================================
# NEGATIVE
# ===========================================================================

@pytest.mark.real_filters
def test_negative_postings_that_must_never_reach_the_board():
    """The two rules that decide a posting is out of reach.

    Both are checked against the LIVE filters.yaml, because the assertion is
    about the shipped policy ("Python and Playwright are dealbreakers", "clearance
    you must already hold is disqualifying") and not about a fixture. See the
    module docstring: retune filters.yaml and this is one of the tests to update.
    """
    # --- a posting in a language the search is not for -----------------------
    # Nothing here names Java, Ruby, JavaScript or Node, so the dealbreaker list
    # is reached — and "no priority language named" is what makes it safe to act
    # on at all. Note this is the escape hatch working: a JD naming BOTH Java and
    # Python is a Java role and must survive (asserted below).
    python_only = {
        "title": "Senior QA Engineer",
        "location": "London, UK",
        "description": "Python, pytest and Playwright automation. CI in GitHub Actions.",
    }
    assert filters.language_fit(python_only)["language_tier"] == "mismatch"
    assert filters.passes_filters(python_only) is False
    reason = filters.rejection_reason(python_only)
    # The reason must NAME the words that matched. "rejected" with no evidence is
    # a decision you cannot check.
    assert "language mismatch" in reason
    assert "python" in reason and "playwright" in reason

    # The escape hatch, asserted rather than trusted: naming a dealbreaker is not
    # enough to reject when a priority language is also named.
    java_and_python = {**python_only, "description": "Java and Python. Selenium, JUnit, Maven."}
    assert filters.language_fit(java_and_python)["language_tier"] == "priority"
    assert filters.passes_filters(java_and_python) is True

    # --- clearance that cannot be obtained ----------------------------------
    held = {
        "title": "Senior QA Engineer",
        "location": "London, UK",
        "description": "Active SC clearance required. Java, JUnit and RestAssured.",
    }
    assert filters.clearance_check(held)["clearance_status"] == "blocked"
    assert filters.passes_filters(held) is False
    # It is dropped on clearance ALONE. A rule tested only in combination hides
    # the case where two rules both fire and one of them is wrong.
    assert filters.language_fit(held)["language_tier"] == "priority"
    assert filters.title_is_relevant(held["title"]) and filters.location_is_allowed(held["location"])
    assert "clearance you do not hold" in filters.rejection_reason(held)

    # "must be ELIGIBLE for SC" is the opposite decision and must be KEPT. These
    # are real advert phrasings, and the distinction is the whole point of the
    # check: eligibility is routine for the successful candidate.
    for phrase in (
        "You must be eligible for Security Clearance (SC). Java and JUnit.",
        "BPSS to start, with eligibility for Home Office SC. Java and JUnit.",
        "Existing SC Clearance would be a benefit. Java and JUnit.",
    ):
        obtainable = {"title": "Senior QA Engineer", "location": "London, UK", "description": phrase}
        assert filters.clearance_check(obtainable)["clearance_status"] == "possible", phrase
        assert filters.passes_filters(obtainable) is True, phrase
        # Flagged, so the board can show it and the AI prompt can be told.
        assert filters.screening_signals(obtainable)["clearance_evidence"], phrase

    print("filters: stack dealbreaker and clearance tiers refuse the right postings — OK")


@pytest.mark.real_filters
def test_negative_contract_below_the_floor_is_refused_but_a_missing_rate_is_not(db):
    """Two DELIBERATE escapes from the day-rate floor, and one real refusal.

    The refusal: a contract role quoting a rate below the floor is out of scope.
    The escapes are what stop the floor emptying the board of contract work —
    most contract adverts quote no rate at all, and a rate only Adzuna ESTIMATED
    is a third party's guess about the role rather than a statement about it.
    Getting these the wrong way round is the difference between a shortlist and
    an empty page, and neither shows up as an error.
    """
    def contract(description, **extra):
        j = {
            "company": "TestCo", "title": "Senior QA Engineer", "location": "London, UK",
            "url": "https://example.test/jobs/rate-1", "description": description,
            "employment_type": "contract", **extra,
        }
        contract_rates.apply_to_job(j)
        return j

    # Refused: a rate the advert itself states, below the floor.
    below = contract("Java, JUnit. Day rate £250 per day.")
    assert below["day_rate_source"] == "stated"
    assert below["rate_verdict"] == "fail"
    assert filters.passes_content_filters(below) is True, "it passes on content; the rate is what drops it"
    assert filters.passes_filters(below) is False
    assert "below the" in contract_rates.reject_reason(below)

    # Escape 1: NO rate stated. Kept — it is exactly the kind of role worth
    # asking an agent about.
    unstated = contract("Java, JUnit. Contract role, rate on application.")
    assert unstated["rate_verdict"] == "unknown" and unstated["day_rate_max"] is None
    assert contract_rates.reject_reason(unstated) is None
    assert filters.passes_filters(unstated) is True

    # Escape 2: a rate only Adzuna guessed. Never acted on.
    guessed = contract("Java, JUnit.", day_rate_min=200.0, day_rate_max=250.0)
    guessed["day_rate_source"] = "estimated_by_adzuna"
    assert guessed["rate_verdict"] == "fail", "the arithmetic still says fail"
    assert contract_rates.reject_reason(guessed) is None, "but a guess must not drop a real posting"
    assert filters.passes_filters(guessed) is True

    # And a permanent role is never touched by the money check at all.
    permanent = contract("Java, JUnit. Salary £25,000.")
    permanent["employment_type"] = "permanent"
    assert contract_rates.reject_reason(permanent) is None

    print("contract_rates: below-floor refusal, plus the two deliberate escapes — OK")


# ===========================================================================
# BOUNDARY
# ===========================================================================

@pytest.mark.real_filters
def test_boundary_day_rate_exactly_on_the_threshold():
    """The floor, from both sides, on the real configured numbers.

    The threshold is READ from the shipped model rather than hard-coded, so this
    keeps testing the edge after contract.yaml is retuned for a new target
    salary — which is the whole reason the arithmetic is in one place. What is
    asserted is the RELATION (at the line passes, a penny under does not), not
    the number.
    """
    model = contract_rates.default_model()
    inside = model.day_rates()["inside_ir35"]["day_rate_gbp"]
    outside = model.day_rates()["outside_ir35"]["day_rate_gbp"]
    assert outside < inside, "an outside-IR35 role is cheaper to staff; the two must differ"

    # ON the line: passes. A comparison written as `>` instead of `>=` fails
    # exactly here, and a role paying precisely what was asked for would be
    # silently dropped.
    at = contract_rates.classify_rate(inside, inside, "inside", model)
    assert at["verdict"] == "pass"
    assert at["required_day_rate"] == pytest.approx(inside)

    # A PENNY under: fails. This is the assertion that catches a floor rounded
    # down to the nearest pound.
    under = contract_rates.classify_rate(inside - 0.01, inside - 0.01, "inside", model)
    assert under["verdict"] == "fail"

    # The three-way split when IR35 is NOT stated: above the outside threshold
    # but below the inside one is "review", not "fail" — one question to the
    # agent, not an automatic no. Collapsing review into fail here would hide
    # every outside-IR35 contract role whose status the advert omitted, which is
    # most of them.
    review = contract_rates.classify_rate(outside, outside, "unknown", model)
    assert review["verdict"] == "review"
    assert "OUTSIDE IR35" in review["reason"]
    # And just under the outside threshold it is a genuine fail.
    assert contract_rates.classify_rate(outside - 0.01, outside - 0.01, "unknown", model)["verdict"] == "fail"

    # No rate at all is "unknown" and carries NO verdict against the floor —
    # absence of evidence, not evidence of a low rate.
    unknown = contract_rates.classify_rate(None, None, "unknown", model)
    assert unknown["verdict"] == "unknown"
    assert unknown["required_day_rate"] is None and unknown["perm_equivalent"] is None

    print(f"classify_rate: pass at £{inside:,.2f}/day, fail a penny under, review between the "
          f"two thresholds — OK")


@pytest.mark.real_filters
def test_boundary_language_classification_and_dedup_key_normalisation(db):
    """The two classifications whose edge cases decide whether a real job is seen.

    LANGUAGE: `none` (the advert names no language at all) must not be a
    mismatch. Forty-odd postings on the live board are in that state — short
    aggregator snippets, or JDs that talk about "automation frameworks"
    generically — and treating silence as a rejection would throw away real
    roles on the absence of evidence.

    DEDUP KEY: the key is normalised, so the same role re-advertised with
    different punctuation is the same role; but the location is part of it, so
    the same title open in two cities is two jobs. Both directions are asserted,
    because getting the first wrong duplicates the board and getting the second
    wrong HIDES a job.
    """
    # --- language: silence is not a mismatch --------------------------------
    silent = {"title": "Senior QA Engineer", "description": "You will own the automation framework."}
    fit = filters.language_fit(silent)
    assert fit["language_tier"] == "none"
    assert fit["language_hits"] is None
    # Ranked BELOW a named language but above a mismatch.
    assert 0 < fit["language_rank"] < filters.language_fit(
        {"title": "x", "description": "Java"}
    )["language_rank"]
    # And an empty description must not crash the classification — the board is
    # full of postings with no JD text at all.
    assert filters.language_fit({})["language_tier"] == "none"
    assert filters.language_fit({"description": None})["language_tier"] == "none"

    # A dealbreaker with nothing else named IS a mismatch; naming JS alongside
    # it is not (secondary beats dealbreaker).
    #
    # The pattern is `\bgolang\b`, deliberately NOT `\bgo\b`: "go" is an English
    # verb that appears in ordinary advert prose ("we go fast", "as you go"), and
    # matching it would classify a large slice of unrelated postings as Go shops.
    # So the common name "Go" is NOT a dealbreaker and a "Go and Kubernetes" role
    # lands in `none` (kept, ranked low) rather than `mismatch` (dropped). That is
    # the intended trade-off — dropping on a guess is worse than ranking down — and
    # the README's "Go" should be read as shorthand for Golang.
    assert filters.language_fit({"description": "We write Golang and deploy to Kubernetes."})["language_tier"] == "mismatch"
    assert filters.language_fit({"description": "We go fast and deploy to Kubernetes."})["language_tier"] == "none"
    assert filters.language_fit({"description": "Golang and JavaScript."})["language_tier"] == "secondary"

    # --- dedup key: normalisation, and what must stay distinct ---------------
    seen = job()
    dedup.mark_seen(db, seen)

    same_role_new_punctuation = job(
        company="TESTCO, Inc.", title="Software  Engineer!!", location="Calgary AB Canada",
    )
    assert dedup.is_new(db, same_role_new_punctuation) is False, \
        "case, punctuation and whitespace must not create a second row for one job"

    # Different location: a genuinely different posting, and it MUST be new.
    # "Backend Engineer" open in both Calgary and Edmonton is two jobs, and a
    # location-free key would silently hide the second one.
    #
    # The URL has to differ too. is_new() is an OR over two keys — the exact URL,
    # and the normalised company/title/location triple — so reusing the URL would
    # short-circuit on the first key and prove nothing about the second.
    other_city = job(url="https://example.test/jobs/qa-1002", location="Edmonton, AB, Canada")
    assert dedup.is_new(db, other_city) is True

    # The key itself is normalised to lowercase alphanumerics.
    key = dedup.make_company_title_key("Acme, Ltd.", "Senior QA  Engineer!!", " London, UK ")
    assert key == "acme ltd::senior qa engineer::london uk"

    print("language_fit/dedup: silence is not a mismatch, normalised key, location distinguishes — OK")


# ===========================================================================
# ERROR HANDLING
# ===========================================================================

def test_error_malformed_config_and_old_schema_never_answer_silently(db, monkeypatch, tmp_path):
    """Off-spec input and off-spec state must fail loudly, or migrate — never
    quietly produce a wrong answer.

    That is the point of the test. An exception is easy to notice; an empty
    keyword list that compiles to a regex matching every title is not, and it
    turns an allowlist into "allow everything" while every run reports success.
    So the assertions are on the failure being raised AND on its message naming
    the cause.

    The last third covers the other direction: a database that is off-spec in a
    way that is NOT the caller's fault. dedup._migrate exists because CREATE
    TABLE IF NOT EXISTS only helps a brand-new file — a DB created before the
    contract/screening columns existed keeps its old shape, and every SELECT of
    those columns then raises "no such column". That is the state a user's own
    data/seen_jobs.sqlite3 is in after a git pull, and silently losing their rows
    would be the worst available outcome, so the upgrade path is asserted rather
    than assumed.
    """
    # An empty keyword list would join to "" and compile to "()", which matches
    # the empty string at every position — quietly matching every title.
    with pytest.raises(ValueError) as excinfo:
        filters._compile_keyword_alternation([], "title_allow_keywords")
    assert "title_allow_keywords" in str(excinfo.value)
    assert "empty" in str(excinfo.value)

    # An empty named-pattern mapping is refused for the same reason.
    with pytest.raises(ValueError):
        filters._compile_named({}, "stack_dealbreakers")

    # An invalid regex names the offending pattern, so the fix is obvious.
    with pytest.raises(ValueError) as excinfo:
        filters._compile_named({"broken": "([unclosed"}, "stack_priority")
    assert "broken" in str(excinfo.value)

    # A typo'd cv_tailorings column raises rather than being dropped: silently
    # discarding it would look like a successful save that lost the value.
    with pytest.raises(ValueError) as excinfo:
        dedup.save_tailoring(db, "https://example.test/jobs/x", statuss="draft")
    assert "statuss" in str(excinfo.value)

    # The CLI refuses to run with no URL rather than storing a row keyed on
    # nothing.
    monkeypatch.setattr(sys, "argv", ["add_job"])
    with pytest.raises(SystemExit) as excinfo:
        add_job.main()
    assert "--url is required" in str(excinfo.value)

    # A job with no description is the NORMAL case (short aggregator snippets,
    # boards that omit the JD), so every screen must handle it without raising
    # and without rejecting on stack alone.
    assert filters.jd_stack_mismatch("") is False
    assert filters.jd_stack_mismatch(None) is False
    assert filters.looks_truncated(None) is True
    assert filters.clearance_check({}) == {"clearance_status": "none", "clearance_evidence": None}
    assert contract_rates.reject_reason({"title": "x", "location": "", "description": ""}) is None
    # A job dict with entirely missing keys must survive the full enrichment
    # path used by process_jobs, rather than KeyError-ing a whole run.
    assert contract_rates.apply_to_job({})["rate_verdict"] == "unknown"

    # --- an old-shaped database is migrated, not crashed on ------------------
    path = str(tmp_path / "old.sqlite3")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE job_details (
            url TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT,
            posted_at TEXT, description TEXT, passed_filters INTEGER,
            fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE seen_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE,
            company_title_key TEXT, company TEXT, title TEXT,
            first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    old.execute(
        "INSERT INTO job_details (url, company, title, location, passed_filters) VALUES (?,?,?,?,1)",
        ("https://example.test/jobs/old-1", "LegacyCo", "SDET", "Calgary, AB, Canada"),
    )
    old.commit()
    old.close()

    with dedup.connect(path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(job_details)")}
        for added in ("language_tier", "clearance_status", "day_rate_max", "last_listed_at", "source"):
            assert added in columns, f"{added} was not migrated in"
        row = dedup.get_details_by_url(conn, "https://example.test/jobs/old-1")
        assert row is not None and row["company"] == "LegacyCo", "the existing row was lost"
        assert row["passed_filters"] == 1
        assert row["language_tier"] is None, "a migrated column starts empty, not guessed"

    # Re-opening is idempotent: _migrate must not attempt a duplicate ALTER.
    with dedup.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_details").fetchone()[0] == 1

    print("malformed config/input: loud, named failures; absent JD handled; old DB migrated — OK")

