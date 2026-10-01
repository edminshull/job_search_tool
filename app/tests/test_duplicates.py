"""Offline tests for app/duplicates.py and app/find_duplicates.py.

These guard the two things that decide whether this feature helps or hurts:

  * PRECISION. Every match is a claim that two adverts are the one job, and the
    consequence of a wrong claim is that a real job is marked "applied" and never
    looked at again. So the negative cases here matter as much as the positive
    ones — Capco's genuinely different Principal/Senior SDET pair must NOT match,
    and neither must the same title in two different cities.
  * NEVER OVERWRITING A DECISION. Copying a derived status over one you set
    yourself would silently discard real work. That rule is asserted directly.

Every fixture is synthetic and local (tmp_path); no network, no real store. Run
with: python -m pytest app/tests/test_duplicates.py
"""
import pytest

from app import dedup
from app import duplicates
from app import find_duplicates


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "duplicates.sqlite3")


# --- the shape of a match -------------------------------------------------
def test_company_key_ignores_legal_suffixes_and_punctuation():
    assert duplicates.company_key("Hackajob Ltd") == duplicates.company_key("Hackajob")
    assert duplicates.company_key("Acme, Inc.") == "acme"
    assert duplicates.company_key("VIQU IT Recruitment") == "viqu it recruitment"
    assert duplicates.company_key("") == ""


def test_title_tokens_drop_the_filler_a_readvert_churns():
    """Every dropped word is a headcount, engagement or work-arrangement marker:
    none of them says WHICH role this is, which is the only question here."""
    base = duplicates.title_tokens("AI Automation Tester")
    for variant in [
        "AI Automation Tester X2",
        "AI Automation Tester (Contract)",
        "AI Automation Tester - Remote - ASAP",
        "AI Automation Tester, 6 month contract",
    ]:
        assert duplicates.title_tokens(variant) == base, variant


def test_title_tokens_keep_what_identifies_the_role():
    assert duplicates.title_tokens("Senior QA Engineer") != duplicates.title_tokens("QA Engineer")
    assert "sdet" in duplicates.title_tokens("Senior SDET")


def test_london_sub_areas_are_the_same_place():
    """The Experis pair, in one assertion: the agency moved its office address
    between two adverts for the same job."""
    assert duplicates.locations_compatible("Farringdon, Central London",
                                          "Fleet Street, Central London")
    assert duplicates.locations_compatible("London, UK", "London ")
    assert duplicates.locations_compatible("The City, Central London", "Aldgate, Central London")


def test_genuinely_different_cities_are_not_the_same_place():
    """Two adverts for one title at one employer in two cities are two jobs, and
    this is exactly the case the exact company::title::location key was built to
    keep apart (see dedup.make_company_title_key)."""
    assert not duplicates.locations_compatible("London, UK", "Bristol, UK")
    assert not duplicates.locations_compatible("Solihull, England, United Kingdom",
                                               "London, UK")


def test_a_vague_location_matches_a_specific_one():
    """The Experis contract is "Remote-first with occasional travel to London", so
    its next repost could carry a bare "UK". Refusing that match would defeat the
    rule exactly where it is needed."""
    assert duplicates.locations_compatible("UK", "London, UK")
    assert duplicates.locations_compatible("Remote", "Farringdon, Central London")
    assert duplicates.locations_compatible("Homeworker - UK", "London, UK")


def test_places_abroad_are_not_merged_by_this_module_s_ignorance():
    """The regression that shaped this rule: an earlier version treated "names no
    city I recognise" as "names no place", which merged 33 SumUp "Field Sales
    Executive" rows from 33 different countries into one cluster. Vagueness has to
    be a property of the string, not of the list."""
    assert not duplicates.locations_compatible("Belfast, Northern Ireland", "Berlin")
    assert not duplicates.locations_compatible("Romania", "Turkey")
    # ...while an identical unknown place is still the same place.
    assert duplicates.locations_compatible("Romania", "Romania")


# --- detection ------------------------------------------------------------
def _job(url, company, title, location="London, UK", fetched_at=None):
    return {"url": url, "company": company, "title": title, "location": location,
            "fetched_at": fetched_at}


def _save(conn, job, fetched_at=None, passed=True):
    """Store a row and pin its fetched_at.

    dedup.save_details deliberately does NOT take fetched_at — it is SQLite's
    CURRENT_TIMESTAMP, because "when this repo first stored the row" is not a
    property of the posting. Ordering is the whole subject of this module, so the
    tests set it explicitly here rather than pretending the job dict carries it.
    The dict is kept in step with the row, so a test that passes a stored row back
    into detect() is describing the row as it actually is."""
    dedup.save_details(conn, job, passed_filters=passed)
    if fetched_at:
        conn.execute("UPDATE job_details SET fetched_at = ? WHERE url = ?",
                     (fetched_at, job["url"]))
        job["fetched_at"] = fetched_at


def test_the_reported_experis_case_is_caught():
    """The case that prompted this module: same agency, same contract, new advert
    id, title nudged, office address moved. Both existing dedup checks miss it."""
    stored = _job("https://adzuna/5896693403", "Experis", "AI Automation Tester",
                  "Fleet Street, Central London", "2026-09-25 08:12:06")
    index = duplicates.DuplicateIndex([
        _job("https://adzuna/5877123884", "Experis", "AI Automation Tester X2",
             "Farringdon, Central London", "2026-09-21 11:48:07"),
        stored,
    ])
    original = index.original_for(stored)
    assert original is not None
    assert original["url"] == "https://adzuna/5877123884"


def test_the_exact_dedup_key_would_have_missed_it():
    """Proof that this is not duplicating work dedup.is_new already does."""
    old = dedup.make_company_title_key("Experis", "AI Automation Tester X2",
                                       "Farringdon, Central London")
    new = dedup.make_company_title_key("Experis", "AI Automation Tester",
                                       "Fleet Street, Central London")
    assert old != new


def test_a_different_seniority_level_is_a_different_job():
    """Capco's Principal SDET and Senior SDET are two adverts for two roles —
    measured on the real board they share 0.75 of their title words. Requiring an
    identical token set, rather than a similarity threshold, is what keeps them
    apart; a threshold anywhere below 1.0 would merge them."""
    principal = _job("u-principal", "Capco", "Principal Software Development Engineer in Test (SDET)")
    senior = _job("u-senior", "Capco", "Senior Software Development Engineer in Test (SDET)")
    index = duplicates.DuplicateIndex([principal, senior])
    assert index.original_for(senior) is None
    assert index.clusters() == []


def test_the_same_title_in_two_cities_is_two_jobs():
    index = duplicates.DuplicateIndex([
        _job("u-london", "Acme", "Backend Engineer", "London, UK"),
        _job("u-bristol", "Acme", "Backend Engineer", "Bristol, UK"),
    ])
    assert index.clusters() == []


def test_the_same_title_at_two_employers_is_two_jobs():
    index = duplicates.DuplicateIndex([
        _job("u-a", "Acme", "QA Engineer"),
        _job("u-b", "Globex", "QA Engineer"),
    ])
    assert index.clusters() == []


def test_a_job_with_no_title_or_company_is_never_matched():
    """Guessing would be worse than missing it: an empty title matches every
    other empty title at the same employer."""
    index = duplicates.DuplicateIndex([
        _job("u-a", "Acme", ""),
        _job("u-b", "Acme", ""),
        _job("u-c", "", "QA Engineer"),
        _job("u-d", "", "QA Engineer"),
    ])
    assert index.clusters() == []
    assert duplicates.cluster_key(_job("u-e", "Acme", "")) is None


def test_a_row_never_matches_itself():
    """Re-running the audit over an already-linked store must be a no-op, not a
    self-reference."""
    row = _job("u-a", "Acme", "QA Engineer")
    assert duplicates.DuplicateIndex([row]).original_for(row) is None


def test_the_earliest_row_is_the_original():
    """Oldest-first by fetched_at, which — unlike posted_at, which arrives as
    epoch millis, ISO strings and free text — is always comparable."""
    index = duplicates.DuplicateIndex([
        _job("u-late", "Acme", "QA Engineer", fetched_at="2026-09-20 10:00:00"),
        _job("u-early", "Acme", "QA Engineer", fetched_at="2026-09-01 10:00:00"),
    ])
    clusters = index.clusters()
    assert len(clusters) == 1
    assert clusters[0]["original"]["url"] == "u-early"
    assert [d["url"] for d in clusters[0]["duplicates"]] == ["u-late"]


def test_clusters_do_not_chain_through_a_vague_location():
    """"London" ~ "UK" ~ "Bristol" individually, but London and Bristol are
    different jobs. Groups are anchored to their ORIGINAL rather than to the last
    row added, so the non-transitivity cannot merge them.

    Note a lone row is not reported as a cluster at all — clusters() returns only
    groups that actually contain a re-advert, so Bristol's absence here is the
    assertion that matters."""
    index = duplicates.DuplicateIndex([
        _job("u-london", "Acme", "QA Engineer", "London, UK", "2026-09-01 10:00:00"),
        _job("u-uk", "Acme", "QA Engineer", "UK", "2026-09-02 10:00:00"),
        _job("u-bristol", "Acme", "QA Engineer", "Bristol, UK", "2026-09-03 10:00:00"),
    ])
    clusters = index.clusters()
    assert len(clusters) == 1
    assert clusters[0]["original"]["url"] == "u-london"
    assert [d["url"] for d in clusters[0]["duplicates"]] == ["u-uk"]
    assert all("u-bristol" not in (c["original"]["url"], *[d["url"] for d in c["duplicates"]])
               for c in clusters), "Bristol must not be merged into the London cluster"


def test_the_index_learns_rows_added_during_a_run():
    """The pipeline builds the index once per batch. A duplicate pair inside one
    batch would be missed if the second job were only matched against the
    snapshot taken before the first was stored."""
    index = duplicates.DuplicateIndex()
    first = _job("u-new-1", "Acme", "QA Engineer", fetched_at="2026-09-20 10:00:00")
    index.add(first)
    second = _job("u-new-2", "Acme", "QA Engineer X2", fetched_at="2026-09-20 10:00:01")
    assert index.original_for(second)["url"] == "u-new-1"


def test_a_just_stored_row_sorts_last_so_it_cannot_become_the_original():
    """index_row stamps `now`. Giving it an empty fetched_at instead would sort it
    first and let a job become the original of the very row it was detected
    against."""
    row = duplicates.index_row(_job("u-now", "Acme", "QA Engineer"))
    assert row["fetched_at"] > "2030-01-01 00:00:00" or row["fetched_at"] >= "2026-"
    existing = _job("u-old", "Acme", "QA Engineer", fetched_at="2020-01-01 00:00:00")
    index = duplicates.DuplicateIndex([existing])
    index.add(row)
    assert index.original_for(row)["url"] == "u-old"


# --- detection against a real store --------------------------------------
def test_detect_marks_the_job_but_writes_nothing(db):
    """The link travels with the row through one save, so a job that fails later
    in the pipeline is still linked — and nothing is written before it exists."""
    with dedup.connect(db) as conn:
        _save(conn, _job("u-old", "Experis", "AI Automation Tester X2",
                         "Farringdon, Central London"))
        index = duplicates.DuplicateIndex.from_db(conn)
        new = _job("u-new", "Experis", "AI Automation Tester", "Fleet Street, Central London")
        original = duplicates.detect(new, index)

        assert original["url"] == "u-old"
        assert new["duplicate_of"] == "u-old"
        # Not stored yet, and no status invented for a row that does not exist.
        assert dedup.get_details_by_url(conn, "u-new") is None
        assert dedup.get_user_status(conn, "u-new") is None


def test_inherit_status_copies_the_decision_from_the_earliest_actioned_advert(db):
    """Red Badger's real shape: the ORIGINAL carries no decision and the "skipped"
    sits on the newer advert. Looking only at the original would leave the copy you
    have not judged still looking like fresh work — the whole problem."""
    with dedup.connect(db) as conn:
        old = _job("u-old", "Red Badger", "Senior QA Engineer", "London, UK")
        new = _job("u-new", "Red Badger", "Senior QA Engineer", "London ")
        _save(conn, old, "2026-09-21 11:00:00")
        _save(conn, new, "2026-09-23 12:00:00")
        dedup.save_user_status(conn, "u-new", "skipped", None)

        index = duplicates.DuplicateIndex.from_db(conn)
        assert duplicates.detect(old, index) is None      # old is the original
        original = duplicates.detect(new, index)
        assert original["url"] == "u-old"
        # detect() only sets new["duplicate_of"] — storing it is the caller's job
        # (the pipeline does it through save_details, the audit through
        # set_duplicate_of). inherit_status resolves the cluster through that link,
        # so the store has to have it.
        dedup.set_duplicate_of(conn, "u-new", original["url"])

        assert duplicates.inherit_status(conn, "u-old") is True
        status = dedup.get_user_status(conn, "u-old")
        assert status["my_status"] == "skipped"
        assert status["inherited_from"] == "u-new"
        # ...and the row you actually decided keeps its own, un-inherited status.
        assert dedup.get_user_status(conn, "u-new")["inherited_from"] is None


def test_inherit_status_never_overwrites_a_status_you_set(db):
    with dedup.connect(db) as conn:
        _save(conn, _job("u-old", "Acme", "QA Engineer"))
        _save(conn, _job("u-new", "Acme", "QA Engineer"))
        dedup.set_duplicate_of(conn, "u-new", "u-old")
        dedup.save_user_status(conn, "u-old", "applied", "referred by Sam")
        dedup.save_user_status(conn, "u-new", "rejected", "not a fit")

        assert duplicates.inherit_status(conn, "u-new") is False
        status = dedup.get_user_status(conn, "u-new")
        assert status["my_status"] == "rejected"
        assert status["notes"] == "not a fit"
        assert status["inherited_from"] is None


def test_inherit_status_does_nothing_when_no_advert_carries_a_decision(db):
    with dedup.connect(db) as conn:
        _save(conn, _job("u-old", "Acme", "QA Engineer"))
        _save(conn, _job("u-new", "Acme", "QA Engineer"))
        dedup.set_duplicate_of(conn, "u-new", "u-old")
        assert duplicates.inherit_status(conn, "u-new") is False
        assert dedup.get_user_status(conn, "u-new") is None


# --- setting a status fans out to the other adverts -----------------------
def test_setting_a_status_reaches_the_other_adverts_of_the_job(db):
    """Forward propagation: applying to the newer copy must stop the older one
    sitting on the board as untouched work."""
    with dedup.connect(db) as conn:
        _save(conn, _job("u-old", "Acme", "QA Engineer"))
        _save(conn, _job("u-new", "Acme", "QA Engineer"))
        dedup.set_duplicate_of(conn, "u-new", "u-old")

        dedup.save_user_status(conn, "u-new", "applied", None)

        assert dedup.get_user_status(conn, "u-old")["my_status"] == "applied"
        assert dedup.get_user_status(conn, "u-old")["inherited_from"] == "u-new"
        # The row you acted on is yours, not inherited.
        assert dedup.get_user_status(conn, "u-new")["inherited_from"] is None


def test_propagation_leaves_your_own_decision_on_a_sibling_alone(db):
    with dedup.connect(db) as conn:
        for url in ("u-old", "u-new", "u-third"):
            _save(conn, _job(url, "Acme", "QA Engineer"))
        # Only the re-adverts point at the original; the original points at nothing.
        for url in ("u-new", "u-third"):
            dedup.set_duplicate_of(conn, url, "u-old")
        dedup.save_user_status(conn, "u-third", "skipped", "keep mine")

        dedup.save_user_status(conn, "u-old", "applied", None)

        assert dedup.get_user_status(conn, "u-third")["my_status"] == "skipped"
        assert dedup.get_user_status(conn, "u-third")["notes"] == "keep mine"
        assert dedup.get_user_status(conn, "u-new")["my_status"] == "applied"


def test_propagation_does_not_reach_an_unrelated_job(db):
    with dedup.connect(db) as conn:
        _save(conn, _job("u-a", "Acme", "QA Engineer"))
        _save(conn, _job("u-b", "Globex", "QA Engineer"))
        dedup.save_user_status(conn, "u-a", "applied", None)
        assert dedup.get_user_status(conn, "u-b") is None


def test_an_inherited_status_can_be_refreshed_but_your_own_cannot(db):
    """The distinction `inherited_from` exists to make.

    An inherited value must stay correctable — treating it as sacred made the first
    inheritance permanent, so whichever advert you happened to click first decided
    every other one forever. A value YOU set is the opposite: never overwritten."""
    with dedup.connect(db) as conn:
        for url in ("u-old", "u-new", "u-mine"):
            _save(conn, _job(url, "Acme", "QA Engineer"))
        for url in ("u-new", "u-mine"):
            dedup.set_duplicate_of(conn, url, "u-old")

        # u-new picks up an inherited value first...
        assert dedup.save_inherited_status(conn, "u-new", "skipped", "u-mine") is True
        assert dedup.get_user_status(conn, "u-new")["my_status"] == "skipped"
        # ...and a later, better-informed decision on another advert replaces it.
        assert dedup.save_inherited_status(conn, "u-new", "applied", "u-old") is True
        assert dedup.get_user_status(conn, "u-new")["my_status"] == "applied"
        assert dedup.get_user_status(conn, "u-new")["inherited_from"] == "u-old"

        # A status you set yourself is refused, whatever the source.
        dedup.save_user_status(conn, "u-mine", "rejected", "my own call")
        assert dedup.save_inherited_status(conn, "u-mine", "applied", "u-old") is False
        assert dedup.get_user_status(conn, "u-mine")["my_status"] == "rejected"
        assert dedup.get_user_status(conn, "u-mine")["inherited_from"] is None


def test_clearing_a_status_does_not_fan_out(db):
    """Clearing is the absence of a decision, not a decision to propagate."""
    with dedup.connect(db) as conn:
        _save(conn, _job("u-old", "Acme", "QA Engineer"))
        _save(conn, _job("u-new", "Acme", "QA Engineer"))
        dedup.set_duplicate_of(conn, "u-new", "u-old")
        dedup.save_user_status(conn, "u-old", "applied", None)
        dedup.save_user_status(conn, "u-old", None, None)
        assert dedup.get_user_status(conn, "u-new")["my_status"] == "applied"


def test_setting_a_status_yourself_clears_the_inherited_marker(db):
    with dedup.connect(db) as conn:
        _save(conn, _job("u-old", "Acme", "QA Engineer"))
        _save(conn, _job("u-new", "Acme", "QA Engineer"))
        dedup.set_duplicate_of(conn, "u-new", "u-old")
        dedup.save_inherited_status(conn, "u-new", "applied", "u-old")
        assert dedup.get_user_status(conn, "u-new")["inherited_from"] == "u-old"

        dedup.save_user_status(conn, "u-new", "interview", "call booked")
        status = dedup.get_user_status(conn, "u-new")
        assert status["my_status"] == "interview"
        assert status["inherited_from"] is None


# --- the audit CLI --------------------------------------------------------
def _seed_experis(conn):
    """The real shape of the reported case: the APPLIED advert is the older one and
    the fresh, un-actioned copy arrived 15 days later."""
    _save(conn, _job("u-old", "Experis", "AI Automation Tester X2",
                     "Farringdon, Central London"), "2026-09-09 10:00:00")
    _save(conn, _job("u-new", "Experis", "AI Automation Tester",
                     "Fleet Street, Central London"), "2026-09-24 10:00:00")
    dedup.save_user_status(conn, "u-old", "applied", None)


def test_audit_reports_and_writes_nothing_by_default(db, capsys):
    """The "check" half. A user asking "is there any way to check duplicates" must
    get an answer that cannot change anything."""
    with dedup.connect(db) as conn:
        _seed_experis(conn)

    assert find_duplicates.main(["--db", db]) == 0
    out = capsys.readouterr().out
    assert "1 duplicate cluster(s)" in out
    assert "would inherit: applied" in out
    assert "Nothing written" in out

    with dedup.connect(db) as conn:
        assert dedup.get_details_by_url(conn, "u-new")["duplicate_of"] is None
        assert dedup.get_user_status(conn, "u-new") is None


def test_audit_apply_links_and_inherits_then_is_idempotent(db, capsys):
    """The "stop" half. Running it twice must change nothing the second time, so
    it is safe to re-run after you apply to something."""
    with dedup.connect(db) as conn:
        _seed_experis(conn)

    assert find_duplicates.main(["--db", db, "--apply"]) == 0
    assert "Linked 1 row(s)" in capsys.readouterr().out
    with dedup.connect(db) as conn:
        assert dedup.get_details_by_url(conn, "u-new")["duplicate_of"] == "u-old"
        status = dedup.get_user_status(conn, "u-new")
        assert status["my_status"] == "applied"
        assert status["inherited_from"] == "u-old"

    assert find_duplicates.main(["--db", db, "--apply"]) == 0
    assert "Linked 0 row(s) to their original; inherited 0 status(es)." in capsys.readouterr().out


def test_audit_on_a_store_with_no_duplicates_says_so(db, capsys):
    with dedup.connect(db) as conn:
        _save(conn, _job("u-a", "Acme", "QA Engineer"))
    assert find_duplicates.main(["--db", db]) == 0
    assert "No duplicates found" in capsys.readouterr().out


def test_audit_ignores_filtered_out_rows_unless_asked(db, capsys):
    """The board's scope. `--all` exists for a store-wide audit, but a duplicate
    among rows the filters dropped cannot reach the board and would only add
    noise to the default report."""
    with dedup.connect(db) as conn:
        _save(conn, _job("u-a", "Acme", "QA Engineer"), passed=False)
        _save(conn, _job("u-b", "Acme", "QA Engineer"), passed=False)

    assert find_duplicates.main(["--db", db]) == 0
    assert "No duplicates found" in capsys.readouterr().out

    assert find_duplicates.main(["--db", db, "--all"]) == 0
    assert "1 duplicate cluster(s)" in capsys.readouterr().out
