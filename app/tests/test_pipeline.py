"""Sanity test for filters.py + dedup.py against the LIVE filters.yaml.

This is the one test that deliberately reads the real config: it is the test OF
the filter policy, so its fixtures are Ed's actual market (London/UK, senior
SDET / test automation) rather than a pinned test config. The client tests are
the opposite — see app/tests/conftest.py for why they pin their own config.

Retuned 2026-09 alongside filters.yaml, which had shipped configured for the
original author (Canada/Alberta, "software engineer"), against which every one
of Ed's sixteen real postings was rejected on location.

Run with: python -m app.tests.test_pipeline
(pytest does not collect this file — the assertions live in main().)
"""
import os
from app import filters
from app import dedup

SAMPLE_JOBS = [
    # Should PASS: the archetypal target role — London hybrid Java SDET
    {"company": "Example FinTech Ltd", "title": "Senior Test Automation Engineer",
     "location": "London - hybrid, 2 days per week onsite",
     "url": "https://example.com/jobs/1001",
     "description": "Java, Cucumber, RestAssured, Selenium, Jenkins, Kubernetes and Testcontainers."},
    # Should PASS: UK-remote with an explicit UK signal in the location string
    {"company": "Xe", "title": "QA Automation Engineer",
     "location": "Remote, United Kingdom", "url": "https://example.com/jobs/1002",
     "description": "Java and k6 performance testing against microservices."},
    # Should PASS: title-only match, no JD text available
    {"company": "Resillion", "title": "Senior QA Engineer",
     "location": "London, UK", "url": "https://example.com/jobs/1003",
     "description": ""},
    # Should PASS: the "-ing" form. "Automation Testing Engineer" does NOT
    # contain the substring "test engineer" and is one of Ed's real postings.
    {"company": "Example Consultancy Ltd", "title": "Automation Testing Engineer",
     "location": "United Kingdom", "url": "https://example.com/jobs/1004",
     "description": "Ruby, Cucumber and Capybara automation."},
    # Should PASS: junior title still gets through, on purpose. The filters
    # judge TITLE SHAPE and STACK, not seniority — deciding that a Test Analyst
    # role is beneath a senior SDET is ai_evaluate.py's job, not this filter's.
    {"company": "Sogeti", "title": "Test Analyst",
     "location": "London, UK", "url": "https://example.com/jobs/1005",
     "description": ""},
    # Should DROP: stack dealbreaker — C#/.NET shop, no Java/Ruby/JS named
    # anywhere in the JD. Note this only fires because '\bc#(?![a-z0-9])' is a
    # WORKING pattern: the shipped '\bc#\b' could never match "C# and .NET".
    {"company": "BigCorp", "title": "Senior SDET",
     "location": "London, UK", "url": "https://example.com/jobs/2001",
     "description": "5+ years of C# and .NET required. SpecFlow a plus."},
    # Should DROP: same thing with the cross-language-tool trap. Selenium being
    # named must NOT rescue a C# role, which is why Selenium is not in stack_core.
    {"company": "AnotherCo", "title": "SDET",
     "location": "London, UK", "url": "https://example.com/jobs/2002",
     "description": "C# test automation with Selenium and Jenkins."},
    # Should DROP: excluded title keyword (hardware test engineering)
    {"company": "ARM", "title": "Hardware Test Engineer",
     "location": "Cambridge, United Kingdom", "url": "https://example.com/jobs/2003",
     "description": ""},
    # Should DROP: generic software engineering title. The shipped allowlist
    # carried "software engineer"/"backend"/"platform"; removed on purpose,
    # because Ed is not applying for those roles and they are pure AI spend.
    {"company": "Monzo", "title": "Senior Software Engineer, Backend",
     "location": "London, UK", "url": "https://example.com/jobs/2004",
     "description": "Java and Kubernetes."},
    # Should DROP: non-engineering title
    {"company": "Affirm", "title": "Marketing Operations Manager",
     "location": "London, UK", "url": "https://example.com/jobs/2005",
     "description": ""},
    # Should DROP: excluded keyword (nurse)
    {"company": "Some Co", "title": "Occupational Health Nurse",
     "location": "London, UK", "url": "https://example.com/jobs/2006",
     "description": ""},
    # Should DROP: location. "Remote Poland" was a real leak in the original
    # 2026-08-11 run and is still caught.
    {"company": "Affirm", "title": "Test Automation Engineer",
     "location": "Remote Poland", "url": "https://example.com/jobs/2007",
     "description": ""},
    # Should DROP: location — bare "Remote US" is not a UK signal
    {"company": "Affirm", "title": "Test Automation Engineer",
     "location": "Remote US", "url": "https://example.com/jobs/2008",
     "description": ""},
    # Should DROP: location — the real Trading 212 case. The advert states no
    # location and the benefits were the Bulgarian package; Sofia must not pass.
    {"company": "Trading 212", "title": "QA Automation Engineer",
     "location": "Sofia, Bulgaria", "url": "https://example.com/jobs/2009",
     "description": "Java and Selenium."},
    # Duplicate of the first PASS entry by URL -> dropped by dedup on 2nd pass
    {"company": "Example FinTech Ltd", "title": "Senior Test Automation Engineer",
     "location": "London - hybrid, 2 days per week onsite",
     "url": "https://example.com/jobs/1001",
     "description": "Java, Cucumber, RestAssured, Selenium, Jenkins, Kubernetes and Testcontainers."},
    # Repost under a new URL, same company+title+location -> caught by the
    # company+title dedup key. The location MUST match: dedup.make_company_
    # title_key() deliberately includes it, so that one role open in two
    # cities isn't collapsed into a single posting. The practical consequence
    # is worth knowing — the same role advertised twice with a reworded
    # location ("London" vs "London, UK") counts as two jobs, which is exactly
    # the duplicate-advert pattern that made four Harnham adverts one vacancy
    # in the CV repo.
    {"company": "Example FinTech Ltd", "title": "Senior Test Automation Engineer",
     "location": "London - hybrid, 2 days per week onsite",
     "url": "https://example.com/jobs/1001-repost",
     "description": "Java, Cucumber, RestAssured, Selenium, Jenkins, Kubernetes and Testcontainers."},
]

TEST_DB = "data/test_seen_jobs.sqlite3"


def main():
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    print("=== Filter results ===")
    filtered = []
    for job in SAMPLE_JOBS:
        ok = filters.passes_filters(job)
        print(f"{'PASS' if ok else 'DROP':5s} | {job['company']:20s} | {job['title'][:44]:44s} | {job['location']}")
        if ok:
            filtered.append(job)

    print(f"\n{len(filtered)}/{len(SAMPLE_JOBS)} passed title+location+stack filters\n")

    print("=== Dedup results (processing filtered jobs in order) ===")
    kept = []
    with dedup.connect(TEST_DB) as conn:
        for job in filtered:
            if dedup.is_new(conn, job):
                dedup.mark_seen(conn, job)
                kept.append(job)
                print(f"NEW  | {job['company']:20s} | {job['title'][:44]:44s} | {job['url']}")
            else:
                print(f"DUPE | {job['company']:20s} | {job['title'][:44]:44s} | {job['url']}")

    print(f"\nFinal candidates after filters+dedup: {len(kept)}")

    # Counts, derived from the fixtures above. 7 of the 16 rows pass the
    # filters (1001-1005 plus the two deliberate copies of 1001, which are
    # perfectly valid postings — filters judge each row, they don't dedup),
    # and dedup then collapses the 2 copies, leaving 5 unique candidates.
    assert len(filtered) == 7, f"expected 7 to pass all filters, got {len(filtered)}"
    assert len(kept) == 5, f"expected 5 unique candidates after dedup, got {len(kept)}"

    # --- Locations: the retune's core purpose -------------------------------
    assert filters.location_is_allowed("London, UK")
    assert filters.location_is_allowed("Greater London")
    assert filters.location_is_allowed("London - hybrid, 2 days per week onsite")
    assert filters.location_is_allowed("United Kingdom")
    assert filters.location_is_allowed("Remote, United Kingdom")
    assert filters.location_is_allowed("Remote (UK)")
    assert filters.location_is_allowed("UK Remote")
    # The shipped config kept none of the above, and kept all of these:
    assert not filters.location_is_allowed("Remote Poland")
    assert not filters.location_is_allowed("Remote Spain")
    assert not filters.location_is_allowed("Remote Australia")
    assert not filters.location_is_allowed("Remote US")
    assert not filters.location_is_allowed("Sofia, Bulgaria")
    assert not filters.location_is_allowed("Toronto, Canada")
    assert not filters.location_is_allowed("Calgary, AB")
    # A bare "Remote" stays disallowed: it is just as likely to mean "Remote US"
    assert not filters.location_is_allowed("Remote")

    # --- Titles -------------------------------------------------------------
    assert filters.title_is_relevant("Senior Test Automation Engineer")
    assert filters.title_is_relevant("Automation Testing Engineer")  # the -ing form
    assert filters.title_is_relevant("Senior SDET")
    assert filters.title_is_relevant("QA Automation Engineer")
    assert filters.title_is_relevant("Software Developer in Test")
    assert filters.title_is_relevant("Senior Test Automation Analyst")
    assert filters.title_is_relevant("Lead Test Engineer")
    # The compliance veto the retune removed on purpose: Ed's whole domain is
    # financial-crime compliance, so this must never be dropped.
    assert filters.title_is_relevant("QA Engineer, Compliance")
    assert filters.title_is_relevant("Test Engineer - Financial Crime")
    # Non-fits
    assert not filters.title_is_relevant("Marketing Operations Manager")
    assert not filters.title_is_relevant("Occupational Health Nurse")
    assert not filters.title_is_relevant("Sales Engineer")
    assert not filters.title_is_relevant("Hardware Test Engineer")
    assert not filters.title_is_relevant("Firmware Test Engineer")
    assert not filters.title_is_relevant("Manufacturing Quality Engineer")
    assert not filters.title_is_relevant("Games Tester")
    assert not filters.title_is_relevant("Penetration Tester")
    assert not filters.title_is_relevant("Senior Software Engineer, Backend")

    # --- Stack dealbreakers -------------------------------------------------
    assert filters.jd_stack_mismatch("5+ years of C# and .NET required.")
    assert filters.jd_stack_mismatch("C++ developer needed")
    assert filters.jd_stack_mismatch("Golang test automation with Kubernetes and Docker.")
    assert filters.jd_stack_mismatch("ASP.NET Core experience required")
    # Ed's own stack must never be a dealbreaker
    assert not filters.jd_stack_mismatch("Java, Spring Boot, Cucumber and Selenium.")
    assert not filters.jd_stack_mismatch("Ruby on Rails, Cucumber and Capybara.")
    assert not filters.jd_stack_mismatch("JavaScript and Node.js automation.")
    # False-positive protection: a C# posting that also names a core language
    assert not filters.jd_stack_mismatch("C# and .NET, with some Java services in the estate.")
    # Python IS now a dealbreaker (changed 2026-09 on request: "heavy on
    # python or playwright, which I don't have much of"). This assertion used
    # to say the opposite — that Python was simply not Ed's stack and the
    # TITLE filter was what decided those roles. That is no longer the policy:
    # a Python-only automation role is now rejected on language, with the
    # same escape hatch as every other dealbreaker.
    assert filters.jd_stack_mismatch("Python, Django and FastAPI.")
    assert filters.jd_stack_mismatch("Python and pytest test automation")
    assert filters.jd_stack_mismatch("Playwright end-to-end test suite")
    # ...but naming Java or Ruby still rescues it, which is the whole point.
    assert not filters.jd_stack_mismatch("Java and Python for test automation")
    assert not filters.jd_stack_mismatch("Ruby with some Playwright coverage")
    # A posting naming NO language must never be rejected on stack: 40 of the
    # 92 postings on the live board are in that state, and silence is not
    # evidence of a mismatch.
    assert not filters.jd_stack_mismatch("We need an experienced automation engineer.")
    # No JD text -> never reject on stack alone
    assert not filters.jd_stack_mismatch("")

    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()


# --- listing freshness --------------------------------------------------------
# Found because a tailored CV existed for a job the dashboard would not show.
# Trading 212's London QA role is advertised and still listed, but Ashby reports
# publishedAt 2023-06-28 — its five identical copies in other countries were
# re-published on 2026-08-26 and London's never was. The board's default 30-day
# recency window therefore hid a live, open role.
#
# The fix rests on a distinction worth keeping straight:
#   posted_at      the employer's CLAIM about when the advert went up. Can be
#                  years stale, and no amount of re-fetching corrects it — the
#                  only source for the date is the ATS, and it says 2023.
#   last_listed_at OBSERVED: the posting arrived in a board fetch just now.
# A posting that comes back with every fetch is live whatever its date says.

def _fresh_db(tmp_path):
    return dedup.connect(str(tmp_path / "listed.db"))


def test_mark_listed_stamps_only_the_given_url(tmp_path):
    with _fresh_db(tmp_path) as conn:
        for url in ("u1", "u2"):
            conn.execute("INSERT INTO job_details (url, company, title, passed_filters) "
                         "VALUES (?, 'Co', 'Tester', 1)", (url,))
        before = conn.execute("SELECT last_listed_at FROM job_details WHERE url='u2'").fetchone()[0]
        dedup.mark_listed(conn, "u1")

        seen = conn.execute("SELECT last_listed_at FROM job_details WHERE url='u1'").fetchone()[0]
        other = conn.execute("SELECT last_listed_at FROM job_details WHERE url='u2'").fetchone()[0]
        assert seen, "the fetched job must be stamped"
        assert other == before, "a job not in this fetch must be left alone"


def test_mark_listed_is_harmless_for_an_unknown_url(tmp_path):
    """An UPDATE matching nothing is not an error — aggregator rows and jobs
    deleted by hand must not blow up a pipeline run."""
    with _fresh_db(tmp_path) as conn:
        dedup.mark_listed(conn, "never-seen")


def test_a_stale_but_listed_posting_stays_on_the_board(tmp_path):
    """The end-to-end property the dashboard depends on: a row whose posted_at
    is years old but whose last_listed_at is today must survive a 30-day window."""
    with _fresh_db(tmp_path) as conn:
        conn.execute(
            "INSERT INTO job_details (url, company, title, posted_at, last_listed_at, passed_filters) "
            "VALUES ('u1', 'Trading 212', 'Senior Quality Assurance Engineer', "
            "        '2023-06-28T12:43:29+00:00', NULL, 1)")
        row = conn.execute("SELECT posted_at, last_listed_at FROM job_details WHERE url='u1'").fetchone()
        assert row[1] is None, "a row stored before this column existed has no freshness"

        dedup.mark_listed(conn, "u1")
        row = conn.execute("SELECT posted_at, last_listed_at FROM job_details WHERE url='u1'").fetchone()
        assert row[0].startswith("2023-06-28"), "posted_at is never rewritten — it is the employer's claim"
        assert row[1], "the sighting is what makes it visible"


def test_the_column_exists_after_migration_on_an_old_database(tmp_path):
    """An existing seen_jobs.sqlite3 predates this column, and _migrate's ALTER
    list is the only thing that adds it — a GET that SELECTs it would otherwise
    throw 'no such column' for every existing install."""
    path = str(tmp_path / "old.db")
    with dedup.connect(path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(job_details)")}
    assert "last_listed_at" in cols
