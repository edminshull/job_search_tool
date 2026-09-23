"""SQLite-backed local datastore: dedup tracking + full job details.

Two dedup keys, matching the approach from the original post:
  1. exact job URL
  2. normalized (company, title) pair — catches reposts under a new URL

A job is only "new" if neither key has been seen before. Seen jobs are
recorded regardless of whether they passed the content filters, so we
never re-fetch/re-consider the same posting on a future run.

`job_details` holds the full record (including the JD description text)
for every job we've ever seen, keyed by URL — this is where descriptions
live instead of in candidates.csv. Rationale (2026-08-11): a full JD is
one to several KB of HTML/text; dumping that into a CSV column makes the
file unreadable in Excel/Sheets and defeats the point of the CSV being a
quick human-scannable list. Query this table directly (see
`get_details_by_url` / `iter_all_details`) when you want to see why a job
was included or excluded, or once ai_evaluate.py exists, to feed the JD
into the Haiku prompt without re-fetching it.
"""
import os
import re
import sqlite3
from contextlib import contextmanager

DB_PATH = "data/seen_jobs.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    company_title_key TEXT,
    company TEXT,
    title TEXT,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_company_title_key ON seen_jobs(company_title_key);

CREATE TABLE IF NOT EXISTS job_details (
    url TEXT PRIMARY KEY,
    company TEXT,
    title TEXT,
    location TEXT,
    posted_at TEXT,
    description TEXT,
    passed_filters INTEGER,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    source TEXT,
    -- Contract-role fields, filled in by contract_rates.apply_to_job as each
    -- job enters the pipeline. Kept on job_details rather than a side table
    -- because they are properties of the posting, derived from its own text.
    employment_type TEXT,     -- permanent | contract | unknown
    ir35_status TEXT,         -- inside | outside | unknown
    ir35_evidence TEXT,       -- the phrase the status was read from
    day_rate_min REAL,
    day_rate_max REAL,
    day_rate_source TEXT,     -- stated | derived_from_advertised_salary | estimated_by_adzuna
    rate_verdict TEXT,        -- pass | review | fail | unknown
    rate_required REAL,       -- the £/day threshold it was judged against
    perm_equivalent REAL,     -- what the day rate is worth as a salary
    rate_reason TEXT,
    -- Deterministic screening signals from filters.py, filled in by
    -- filters.screening_signals() as each job enters the pipeline.
    language_tier TEXT,       -- priority | secondary | none | mismatch
    language_rank INTEGER,    -- 3 | 2 | 1 | 0, for sorting
    language_hits TEXT,       -- which words matched, e.g. "java, junit, maven"
    clearance_status TEXT,    -- blocked | possible | none
    clearance_evidence TEXT,
    -- When this posting was last seen ON ITS BOARD. Distinct from posted_at,
    -- which is what the employer CLAIMS about when the advert went up, and the
    -- two disagree in a way that matters:
    --
    -- Trading 212's London QA role is still listed and still advertised, but
    -- Ashby reports publishedAt 2023-06-28 — its five identical copies in other
    -- countries were re-published on 2026-08-26 and London's never was. So a
    -- 30-day recency window hides a live, open role, and the CV was tailored for
    -- a job the board was concealing.
    --
    -- A `posted_at` cannot tell you this. Being seen in a board fetch can: a
    -- posting that arrives with every fetch is live whatever its date says.
    last_listed_at TEXT
);

CREATE TABLE IF NOT EXISTS ai_evaluations (
    url TEXT PRIMARY KEY,
    match_score INTEGER,
    recommendation TEXT,
    genuine_gaps TEXT,
    transferable_strengths TEXT,
    risk_factors TEXT,
    model TEXT,
    evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (url) REFERENCES job_details(url)
);

-- Your own tracking, separate from the AI's recommendation. The AI's
-- apply/consider/skip is a suggestion made before applying; my_status is
-- what actually happened (applied/interview/rejected/skipped/silence) —
-- deliberately a different vocabulary so the two never collide in the UI.
CREATE TABLE IF NOT EXISTS user_status (
    url TEXT PRIMARY KEY,
    my_status TEXT,
    notes TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (url) REFERENCES job_details(url)
);

-- One row per job posting whose CV has been tailored, keyed by the posting
-- URL so re-tailoring the same job updates its record instead of creating a
-- second application folder. Written by app/cv_tailor.py, read by the web
-- app's job panel.
--
-- The drafted YAML is stored HERE rather than only on disk, and that is what
-- makes the two-step UI honest: the preview you read and the file that gets
-- rendered are the same text, so approving cannot render something you never
-- saw. It also means a preview that is never approved leaves no files in
-- cv_output/ at all.
CREATE TABLE IF NOT EXISTS cv_tailorings (
    url TEXT PRIMARY KEY,
    status TEXT,              -- draft | rendered | failed
    day_dir TEXT,             -- DD-MM-YY; fixed when the application is first prepared
    company_slug TEXT,        -- slugified employer; the folder inside day_dir
    tailored_yaml TEXT,       -- the full YAML, exactly as previewed
    report TEXT,              -- the model's own account of what it cut and reworded
    interview_prep_md TEXT,   -- generated separately, stored once written
    outdir TEXT,              -- absolute path of the application folder
    pdf_path TEXT,
    docx_path TEXT,
    gap_report_path TEXT,
    interview_prep_path TEXT,
    draft_model TEXT,         -- e.g. "local:qwen3-coder-30b-a3b"
    verify_model TEXT,        -- e.g. "deepseek:deepseek-flash"
    verify_verdict TEXT,      -- accept | revise | reject
    verify_notes TEXT,        -- the verifier's findings, verbatim
    attempts TEXT,            -- JSON: one entry per drafting attempt, in order
    master_coverage REAL,     -- priority-term %, before tailoring
    tailored_coverage REAL,   -- priority-term %, after tailoring
    error TEXT,               -- the failure that stopped the last attempt
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (url) REFERENCES job_details(url)
);
"""

MY_STATUS_VALUES = ["applied", "interview", "rejected", "skipped", "silence"]

# ---------------------------------------------------------------------------
# drafting_lessons — what the drafter has demonstrably got wrong before
# ---------------------------------------------------------------------------
# One row per FAILURE MODE (see cv_tailor.FAILURE_MODES), counted every time the
# escalation PROVES the drafter made that mistake. The mode names are a fixed,
# bounded set defined in code, not free text: a table a model can write arbitrary
# rows into is a table that can teach the prompt anything, including something
# false. `last_evidence` is the verifier's own wording, kept ONLY so a human can
# see what produced a count — it is never a source of prompt text.
LESSON_THRESHOLD = 2

LESSON_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafting_lessons (
    mode TEXT PRIMARY KEY,
    hits INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    active_since TEXT,        -- set when hits reach the threshold; NULL means "not yet taught"
    last_evidence TEXT        -- human audit only. NEVER injected into a prompt.
);
"""


def _normalize(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def make_company_title_key(company: str, title: str, location: str = "") -> str:
    # location is part of the key so two genuinely distinct postings with
    # the same title at the same company (e.g. "Backend Engineer" open in
    # both Toronto and Vancouver) aren't treated as a repost of each other.
    return f"{_normalize(company)}::{_normalize(title)}::{_normalize(location)}"


def _migrate(conn) -> None:
    """SCHEMA's CREATE TABLE IF NOT EXISTS only handles brand-new DBs —
    columns added later need an explicit ALTER TABLE for a DB file that
    already exists (e.g. `source`, added 2026-08-12, and the ten contract
    columns added 2026-09-21). Declared as a list of (name, type) so adding
    the next column is one line and cannot silently drift from SCHEMA.

    Both tables are covered: `job_details` and `cv_tailorings` each carry their
    own column list, because a column added to one of them is invisible to the
    other's migration."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(job_details)")}
    for column, decl in (
        ("source", "TEXT"),
        ("employment_type", "TEXT"),
        ("ir35_status", "TEXT"),
        ("ir35_evidence", "TEXT"),
        ("day_rate_min", "REAL"),
        ("day_rate_max", "REAL"),
        ("day_rate_source", "TEXT"),
        ("rate_verdict", "TEXT"),
        ("rate_required", "REAL"),
        ("perm_equivalent", "REAL"),
        ("rate_reason", "TEXT"),
        ("language_tier", "TEXT"),
        ("language_rank", "INTEGER"),
        ("language_hits", "TEXT"),
        ("clearance_status", "TEXT"),
        ("clearance_evidence", "TEXT"),
        ("last_listed_at", "TEXT"),
    ):
        if column not in existing:
            conn.execute(f"ALTER TABLE job_details ADD COLUMN {column} {decl}")

    # cv_tailorings needs the same treatment for the same reason. `attempts` is
    # the drafting provenance — which model drafted, in what order, and why an
    # earlier attempt was set aside. It is stored rather than derived because the
    # derivation is not reversible: `draft_model` records only the draft that
    # SURVIVED, so a record whose local draft was escalated to the cloud and then
    # lost the comparison is indistinguishable from one that was never escalated.
    # The user is left looking at a local draft and a verdict with no way to tell
    # that a cloud retry happened at all.
    tailoring = {row[1] for row in conn.execute("PRAGMA table_info(cv_tailorings)")}
    for column, decl in (("attempts", "TEXT"),):
        if column not in tailoring:
            conn.execute(f"ALTER TABLE cv_tailorings ADD COLUMN {column} {decl}")


@contextmanager
def connect(db_path: str = DB_PATH):
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    # A separate CREATE TABLE IF NOT EXISTS rather than a line inside SCHEMA: the
    # lessons table is new, so an existing database simply gains it, which is the
    # one case _migrate's ALTER list neither covers nor needs to.
    conn.executescript(LESSON_SCHEMA)
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def is_new(conn, job: dict) -> bool:
    key = make_company_title_key(job["company"], job["title"], job.get("location", ""))
    cur = conn.execute(
        "SELECT 1 FROM seen_jobs WHERE url = ? OR company_title_key = ? LIMIT 1",
        (job["url"], key),
    )
    return cur.fetchone() is None


def mark_seen(conn, job: dict) -> None:
    key = make_company_title_key(job["company"], job["title"], job.get("location", ""))
    conn.execute(
        "INSERT OR IGNORE INTO seen_jobs (url, company_title_key, company, title) "
        "VALUES (?, ?, ?, ?)",
        (job["url"], key, job["company"], job["title"]),
    )


def save_details(conn, job: dict, passed_filters: bool) -> None:
    """Store the full job record (incl. description) keyed by URL. Called
    for every job we ever see — pass or fail — so you can audit filter
    decisions later without re-fetching anything."""
    conn.execute(
        "INSERT OR REPLACE INTO job_details "
        "(url, company, title, location, posted_at, description, passed_filters, source, "
        " employment_type, ir35_status, ir35_evidence, day_rate_min, day_rate_max, "
        " day_rate_source, rate_verdict, rate_required, perm_equivalent, rate_reason, "
        " language_tier, language_rank, language_hits, clearance_status, clearance_evidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job.get("url", ""),
            job.get("company", ""),
            job.get("title", ""),
            job.get("location", ""),
            job.get("posted_at"),
            job.get("description", ""),
            1 if passed_filters else 0,
            job.get("source", ""),
            job.get("employment_type"),
            job.get("ir35_status"),
            job.get("ir35_evidence"),
            job.get("day_rate_min"),
            job.get("day_rate_max"),
            job.get("day_rate_source"),
            job.get("rate_verdict"),
            job.get("rate_required"),
            job.get("perm_equivalent"),
            job.get("rate_reason"),
            job.get("language_tier"),
            job.get("language_rank"),
            job.get("language_hits"),
            job.get("clearance_status"),
            job.get("clearance_evidence"),
        ),
    )


# Every column of job_details that consumers may want, in one place, so the
# three iter_* helpers below cannot drift apart from each other or from the
# table definition.
JOB_DETAIL_COLUMNS = [
    "url", "company", "title", "location", "posted_at", "description",
    "passed_filters", "fetched_at", "source", "employment_type", "ir35_status",
    "ir35_evidence", "day_rate_min", "day_rate_max", "day_rate_source",
    "rate_verdict", "rate_required", "perm_equivalent", "rate_reason",
    "language_tier", "language_rank", "language_hits",
    "clearance_status", "clearance_evidence",
]


def _row_to_dict(row) -> dict:
    return dict(zip(JOB_DETAIL_COLUMNS, row))


def _select_job_details(where: str = "", params: tuple = ()) -> str:
    return f"SELECT {', '.join(JOB_DETAIL_COLUMNS)} FROM job_details {where}"


def get_details_by_url(conn, url: str) -> dict | None:
    cur = conn.execute(_select_job_details("WHERE url = ?"), (url,))
    row = cur.fetchone()
    return None if row is None else _row_to_dict(row)


def distinct_companies(conn) -> list[str]:
    """Every distinct company name we've ever seen in job_details — the
    input for try_companies_across_ats.py, which checks which of these
    aren't in companies.yaml yet and tries to guess their ATS slug
    directly, since the Adzuna redirect_url path (discover_companies.py)
    turned out to almost never leave adzuna.* for real queries (see
    diagnose_adzuna_redirects.py's 2026-08-12 finding: 25/25 samples
    stayed on Adzuna)."""
    cur = conn.execute("SELECT DISTINCT company FROM job_details WHERE company != '' ORDER BY company")
    return [row[0] for row in cur.fetchall()]


def iter_filtered_out(conn):
    """Every job we've ever stored that didn't pass the filters at fetch
    time — the input for refilter.py, which re-runs the CURRENT filters.py
    against them without re-hitting the ATS/aggregator APIs."""
    cur = conn.execute(_select_job_details("WHERE passed_filters = 0"))
    for row in cur.fetchall():
        yield _row_to_dict(row)


def iter_passed(conn):
    """Every job we've ever stored that DID pass the filters at fetch time
    — the other input for refilter.py, so a TIGHTENED filter can demote
    jobs that no longer pass, not just rescue ones that now do."""
    cur = conn.execute(_select_job_details("WHERE passed_filters = 1"))
    for row in cur.fetchall():
        yield _row_to_dict(row)


def set_passed_filters(conn, url: str, passed: bool) -> None:
    """Flip a stored job's passed_filters flag in place — used by
    refilter.py when a filter change rescues (or newly drops) a job that
    was already fetched, without touching seen_jobs (still deduped) or
    forcing a re-fetch."""
    conn.execute(
        "UPDATE job_details SET passed_filters = ? WHERE url = ?",
        (1 if passed else 0, url),
    )


def save_derived_fields(conn, job: dict) -> None:
    """Update ONLY the DERIVED columns of an already-stored job — the contract
    verdict and the screening signals — leaving the description and everything
    else alone. Used by refilter.py and the backfill script, which re-evaluate
    a job against changed config: re-running save_details there would work
    too, but it would rewrite several KB of description per job to change a
    handful of scalars."""
    conn.execute(
        "UPDATE job_details SET employment_type=?, ir35_status=?, ir35_evidence=?, "
        "day_rate_min=?, day_rate_max=?, day_rate_source=?, rate_verdict=?, "
        "rate_required=?, perm_equivalent=?, rate_reason=?, "
        "language_tier=?, language_rank=?, language_hits=?, "
        "clearance_status=?, clearance_evidence=? WHERE url=?",
        (
            job.get("employment_type"), job.get("ir35_status"), job.get("ir35_evidence"),
            job.get("day_rate_min"), job.get("day_rate_max"), job.get("day_rate_source"),
            job.get("rate_verdict"), job.get("rate_required"), job.get("perm_equivalent"),
            job.get("rate_reason"),
            job.get("language_tier"), job.get("language_rank"), job.get("language_hits"),
            job.get("clearance_status"), job.get("clearance_evidence"),
            job["url"],
        ),
    )


def save_description(conn, url: str, description: str) -> None:
    """Replace only the stored description for a job. Used by the backfill when
    it recovers the real advert out of a stored Adzuna listing page — writing
    the whole row back would be wasteful when one column changed."""
    conn.execute("UPDATE job_details SET description = ? WHERE url = ?", (description, url))


def _same_number(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 0.01


def refresh_contract_fields(conn, job: dict) -> bool:
    """Re-derive the contract columns for an already-seen job from the copy
    just fetched. Returns True if anything actually changed.

    Why this exists: dedup means an already-seen job is never re-inserted,
    so a posting first stored before the contract columns existed would keep
    a NULL employment_type forever — it would show a day rate on the board
    but no "Contract" chip, and would be missing from the Contract filter.
    Re-running the evaluation on the copy we just fetched is what heals
    those rows without a re-fetch.

    Refuses to overwrite a known employment type with "unknown", because the
    ATS fetchers do not report one at all: a Greenhouse refresh of a job
    Adzuna had already told us is contract would otherwise erase that."""
    row = conn.execute(
        "SELECT employment_type, ir35_status, day_rate_max, rate_verdict, "
        "       language_tier, clearance_status "
        "FROM job_details WHERE url = ?",
        (job["url"],),
    ).fetchone()
    if row is None:
        return False
    (stored_type, stored_ir35, stored_rate, stored_verdict,
     stored_language, stored_clearance) = row

    new_type = job.get("employment_type")
    if new_type == "unknown" and stored_type in ("permanent", "contract"):
        return False

    if (stored_type == new_type
            and stored_ir35 == job.get("ir35_status")
            and _same_number(stored_rate, job.get("day_rate_max"))
            and stored_verdict == job.get("rate_verdict")
            and stored_language == job.get("language_tier")
            and stored_clearance == job.get("clearance_status")):
        return False

    save_derived_fields(conn, job)
    return True


def mark_listed(conn, url: str) -> None:
    """Record that this posting was present in a board fetch just now.

    The one signal that distinguishes a LIVE posting from an old one. `posted_at`
    is the employer's claim about when the advert went up and it can be years
    stale on a role that is still open — Trading 212's London QA role is listed
    and advertised with a 2023-06-28 date, while its five identical copies in
    other countries were re-published in August 2026. Re-fetching cannot recover
    from that: the ATS is the only source for the date, and it says 2023.

    Being SEEN, though, is observed rather than claimed. A posting that arrives
    with every board fetch is live whatever its date says, so the board can say
    "still listed" instead of hiding it behind a recency window.

    Called only for jobs a fetch actually returned, so a run that skips a source
    leaves the other sources' rows alone rather than marking them stale."""
    conn.execute(
        "UPDATE job_details SET last_listed_at = CURRENT_TIMESTAMP WHERE url = ?",
        (url,),
    )


def get_unevaluated_candidates(conn, include_evaluated: bool = False) -> list[dict]:
    """Jobs that passed the deterministic filters — the input queue for
    ai_evaluate.py.

    By default only the ones that have NOT been scored yet. Pass
    include_evaluated=True to get the whole board instead, which is what
    ai_evaluate.py's --rescore needs: a score is only comparable to another
    score made with the same prompt and the same underlying text, and both
    change (the prompt when the screening signals or profile change, the text
    when a description is re-fetched). Without this there was no way to refresh
    a stale score short of deleting rows by hand."""
    cur = conn.execute(
        "SELECT jd.url, jd.company, jd.title, jd.location, jd.posted_at, jd.description, "
        "       jd.employment_type, jd.ir35_status, jd.day_rate_min, jd.day_rate_max, "
        "       jd.rate_verdict, jd.perm_equivalent, "
        "       jd.language_tier, jd.language_hits, jd.clearance_status, jd.clearance_evidence "
        "FROM job_details jd "
        "LEFT JOIN ai_evaluations ae ON jd.url = ae.url "
        "WHERE jd.passed_filters = 1"
        + ("" if include_evaluated else " AND ae.url IS NULL")
    )
    keys = ["url", "company", "title", "location", "posted_at", "description",
            "employment_type", "ir35_status", "day_rate_min", "day_rate_max",
            "rate_verdict", "perm_equivalent",
            "language_tier", "language_hits", "clearance_status", "clearance_evidence"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def save_evaluation(conn, url: str, evaluation: dict, model: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO ai_evaluations "
        "(url, match_score, recommendation, genuine_gaps, transferable_strengths, risk_factors, model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            url,
            evaluation.get("match_score"),
            evaluation.get("recommendation"),
            evaluation.get("genuine_gaps"),
            evaluation.get("transferable_strengths"),
            evaluation.get("risk_factors"),
            model,
        ),
    )


def save_user_status(conn, url: str, my_status: str | None, notes: str | None) -> None:
    """my_status should be one of MY_STATUS_VALUES or None/'' to clear it.
    Not validated strictly here (the web UI constrains it via a <select>)
    so a hand-edited import file with a typo doesn't hard-fail the import."""
    conn.execute(
        "INSERT INTO user_status (url, my_status, notes, updated_at) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(url) DO UPDATE SET my_status=excluded.my_status, "
        "notes=excluded.notes, updated_at=CURRENT_TIMESTAMP",
        (url, my_status or None, notes or None),
    )


def get_user_status(conn, url: str) -> dict | None:
    cur = conn.execute("SELECT url, my_status, notes, updated_at FROM user_status WHERE url = ?", (url,))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(["url", "my_status", "notes", "updated_at"], row))


# Columns of cv_tailorings, in table order. Named once so the upsert below and
# every reader cannot drift apart, and so adding a column is a one-line change
# here plus one in SCHEMA (the same discipline _migrate's column list uses).
CV_TAILORING_FIELDS = [
    "status", "day_dir", "company_slug", "tailored_yaml", "report",
    "interview_prep_md", "outdir", "pdf_path", "docx_path", "gap_report_path",
    "interview_prep_path", "draft_model", "verify_model", "verify_verdict",
    "verify_notes", "attempts", "master_coverage", "tailored_coverage", "error",
]


def save_tailoring(conn, url: str, **fields) -> None:
    """Upsert a cv_tailorings row. Only the keys passed are written, so a
    preview can store the draft without clearing paths a previous render set,
    and a render can store its paths without rewriting the YAML.

    An unknown key is a programming error and raises, rather than being
    silently dropped — a typo'd column name would otherwise look like a
    successful save that quietly lost the value."""
    unknown = set(fields) - set(CV_TAILORING_FIELDS)
    if unknown:
        raise ValueError(f"Unknown cv_tailorings field(s): {sorted(unknown)}")
    if not fields:
        return
    existing = conn.execute("SELECT 1 FROM cv_tailorings WHERE url = ?", (url,)).fetchone()

    if existing is None:
        # created_at is left to its DEFAULT; day_dir and company_slug are the
        # only columns the first write must supply, and a partial first write
        # is still valid (a failed preview records error and nothing else).
        cols = ["url"] + list(fields)
        conn.execute(
            f"INSERT INTO cv_tailorings ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' * len(cols))})",
            [url] + [fields[c] for c in fields],
        )
    else:
        assignments = ", ".join(f"{c}=?" for c in fields)
        conn.execute(
            f"UPDATE cv_tailorings SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE url = ?",
            [fields[c] for c in fields] + [url],
        )


def get_tailoring(conn, url: str) -> dict | None:
    cols = ["url"] + CV_TAILORING_FIELDS + ["created_at", "updated_at"]
    cur = conn.execute(f"SELECT {', '.join(cols)} FROM cv_tailorings WHERE url = ?", (url,))
    row = cur.fetchone()
    return dict(zip(cols, row)) if row else None


def iter_tailorings(conn) -> list[dict]:
    cols = ["url"] + CV_TAILORING_FIELDS + ["created_at", "updated_at"]
    cur = conn.execute(f"SELECT {', '.join(cols)} FROM cv_tailorings")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# --- drafting lessons ------------------------------------------------------
LESSON_FIELDS = ("mode", "hits", "first_seen", "last_seen", "active_since",
                 "last_evidence")


def record_lesson(conn, mode: str, evidence: str = "",
                  threshold: int = LESSON_THRESHOLD) -> dict:
    """Count one PROVEN instance of `mode`, and start teaching it at the threshold.

    `mode` must come from cv_tailor.FAILURE_MODES; passing free text is refused
    rather than stored, because the whole safety property of this table is that
    its keys are a closed set — a row a model could name is a row that could
    teach the prompt something false.

    Returns the row as it now stands, so the caller can say whether this instance
    tipped the mode into being taught."""
    from app import cv_tailor  # local import: cv_tailor imports this module
    if mode not in cv_tailor.FAILURE_MODES:
        raise ValueError(f"Unknown drafting failure mode: {mode!r}")
    conn.execute(
        "INSERT INTO drafting_lessons (mode, hits, last_evidence) VALUES (?, 1, ?) "
        "ON CONFLICT(mode) DO UPDATE SET hits = hits + 1, "
        "last_seen = CURRENT_TIMESTAMP, last_evidence = excluded.last_evidence",
        (mode, (evidence or "")[:400]),
    )
    # Promotion is a separate statement so it can be an explicit comparison
    # rather than a WHERE on the just-written row: a mode becomes taught the
    # moment it RECURS, and never on a single sighting, which may be one
    # posting's quirk or one noisy verdict.
    conn.execute(
        "UPDATE drafting_lessons SET active_since = CURRENT_TIMESTAMP "
        "WHERE mode = ? AND active_since IS NULL AND hits >= ?",
        (mode, threshold),
    )
    return get_lesson(conn, mode)


def get_lesson(conn, mode: str) -> dict | None:
    cur = conn.execute(
        f"SELECT {', '.join(LESSON_FIELDS)} FROM drafting_lessons WHERE mode = ?", (mode,))
    row = cur.fetchone()
    return dict(zip(LESSON_FIELDS, row)) if row else None


def all_lessons(conn) -> list[dict]:
    """Every recorded mode, most-proven first. `active_since` NULL means counted
    but not yet taught."""
    cur = conn.execute(
        f"SELECT {', '.join(LESSON_FIELDS)} FROM drafting_lessons "
        "ORDER BY hits DESC, mode")
    return [dict(zip(LESSON_FIELDS, row)) for row in cur.fetchall()]


def active_lessons(conn) -> list[dict]:
    """The modes that have recurred enough to be worth stating to the drafter."""
    return [r for r in all_lessons(conn) if r["active_since"]]


def clear_lessons(conn, mode: str | None = None) -> int:
    """Forget one mode, or all of them. The user's veto: a lesson that is wrong,
    or that has stopped being true as the draft prompt changes, must be
    removable without editing a database by hand."""
    if mode is None:
        cur = conn.execute("DELETE FROM drafting_lessons")
    else:
        cur = conn.execute("DELETE FROM drafting_lessons WHERE mode = ?", (mode,))
    return cur.rowcount


def iter_scored_candidates(conn):
    """All evaluated candidates, best match first, joined with job_details
    for display — used to write the scored CSV.

    Includes rows that were scored and have since been filtered out (a rule
    tightened, or a posting's day rate turned out to be below the floor), so
    the count here can exceed the number of rows on the board. `passed_filters`
    is part of the output for exactly that reason: without it the export looks
    like a shortlist that silently disagrees with the board."""
    cur = conn.execute(
        "SELECT jd.company, jd.title, jd.location, jd.url, jd.posted_at, "
        "       jd.employment_type, jd.ir35_status, jd.day_rate_min, jd.day_rate_max, "
        "       jd.rate_verdict, jd.perm_equivalent, jd.language_tier, jd.clearance_status, "
        "       jd.passed_filters, "
        "       ae.match_score, ae.recommendation, ae.genuine_gaps, "
        "       ae.transferable_strengths, ae.risk_factors "
        "FROM ai_evaluations ae "
        "JOIN job_details jd ON jd.url = ae.url "
        "ORDER BY ae.match_score DESC"
    )
    keys = ["company", "title", "location", "url", "posted_at",
            "employment_type", "ir35_status", "day_rate_min", "day_rate_max",
            "rate_verdict", "perm_equivalent", "language_tier", "clearance_status",
            "passed_filters", "match_score", "recommendation", "genuine_gaps", "transferable_strengths", "risk_factors"]
    for row in cur.fetchall():
        yield dict(zip(keys, row))