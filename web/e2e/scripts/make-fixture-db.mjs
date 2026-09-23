/**
 * Build (or reset) the deterministic board the Playwright suite runs against.
 *
 * WHY A FIXTURE RATHER THAN THE REAL DATABASE
 * -------------------------------------------
 * data/seen_jobs.sqlite3 holds 16,000+ postings and the user's own application
 * statuses and notes. Testing the board against it would make every assertion
 * depend on content that changes on every pipeline run, and the status tests
 * would write into a live tracker. So the suite gets its own database, seeded
 * here, on a fixed cast of postings chosen so that each test's expectation is an
 * exact number rather than a range.
 *
 * RESET IS IN-PLACE SQL, NOT A NEW FILE
 * ------------------------------------
 * `next dev` holds a better-sqlite3 handle open on this path for the life of the
 * server. Deleting and recreating the file underneath it would leave that handle
 * pointing at an unlinked inode, so a per-test reset would appear to do nothing.
 * DELETE + INSERT against the same file is visible to the running server
 * immediately, which is what makes `resetBoard()` in the specs work.
 *
 * THE CAST, AND WHY EACH ROW EXISTS
 * ---------------------------------
 * Dates are relative to "today" so the date-window boundaries stay exactly on
 * their boundary whenever the suite runs.
 *
 *   company           title                         why it is here
 *   ------------------------------------------------------------------------
 *   Northwind Digital Senior Test Automation Eng.   top of the default-sort
 *                                                   board; contract + IR35 +
 *                                                   day rate; JAVA priority;
 *                                                   newest posting; HTML JD
 *   Bluepeak Software QA Automation Engineer        JS/Node secondary; the only
 *                                                   row with a My status set
 *   Coastal Systems   Test Analyst                  no language named; AI=skip
 *   Trading House Ltd Automation Engineer           posted 2 YEARS ago but
 *                                                   listed today: the
 *                                                   "still listed" case, and
 *                                                   the HIGHEST score, so the
 *                                                   sort test can show it
 *                                                   dropping down the board
 *   Boundary Analytics SDET                         posted EXACTLY 30 days ago
 *                                                   (the window's closed edge)
 *   Fresh Start Ltd   Automation Test Engineer      never AI-scored; never
 *                                                   tailored — the job the
 *                                                   tailoring journey starts on
 *   Ghost Files Ltd   Senior QA Engineer            record says "rendered" but
 *                                                   the PDF is NOT on disk
 *   Draft Partners    QA Engineer                   has a STORED draft with the
 *                                                   verifier's findings
 *   Defence Systems   Test Engineer                 clearance mentioned but
 *                                                   obtainable; JAVA priority
 *   Legacy Corp       QA Lead                       NO posted date at all
 *   Just Past Ltd     Performance Test Engineer     posted 31 days ago, never
 *                                                   re-listed: hidden by the
 *                                                   default window
 *   Antique Systems   QA Engineer                   posted 200 days ago, never
 *                                                   re-listed: also hidden
 *
 * Totals with the default 30-day window: 12 stored, 10 visible, 2 hidden.
 *
 * A deliberate mix of date FORMATS is used (ISO with Z, a local datetime string,
 * a relative phrase, and epoch millis) because app/lib/dates.ts exists precisely
 * because the sources disagree, and a fixture with one format would never
 * exercise the other three.
 *
 * ONLY Trading House carries `last_listed_at`, and that is faithful modelling
 * rather than convenience. The column is written when a board fetch returns the
 * posting again, and aggregator-sourced rows are never re-fetched — so for them
 * `posted_at` is the only evidence there is, which is what the code says. It also
 * keeps the date window testable: a "listed today" stamp on every row would make
 * all of them immune to the window, and its boundaries could not be exercised at
 * all. Trading House is the one row where the two signals disagree, which is
 * exactly the case the feature exists for.
 *
 * Usage:
 *   node e2e/scripts/make-fixture-db.mjs            # create or reset
 *   node e2e/scripts/make-fixture-db.mjs --quiet     # no output (used per test)
 */
import Database from "better-sqlite3";
import { existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
export const FIXTURE_DB_PATH =
  process.env.QA_BOARD_DB || path.join(here, "..", ".fixtures", "board.sqlite3");

/** The repo root (web/..). Used only to build a realistic cv_output path that
 *  deliberately does NOT exist — see the Ghost Files row below. */
const REPO_ROOT = path.join(here, "..", "..", "..");
const GHOST_OUTDIR = path.join(REPO_ROOT, "cv_output", "01-01-01", "ghost-files-ltd");

// ---------------------------------------------------------------------------
// Dates, anchored to today so the window boundaries are exact whenever this runs
// ---------------------------------------------------------------------------

const DAY_MS = 86_400_000;

/** Local midnight, `days` days ago. Written in SQLite's CURRENT_TIMESTAMP shape
 *  ("YYYY-MM-DD HH:MM:SS"), which dates.ts parses as LOCAL time — matching how
 *  the real columns (written by SQLite on this machine) behave. */
function localMidnightDaysAgo(days) {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - days);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** An ISO timestamp with Z, the shape Adzuna and Greenhouse send. */
function isoDaysAgo(days) {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  d.setDate(d.getDate() - days);
  return d.toISOString();
}

/** Epoch millis, the shape SmartRecruiters sends.
 *
 *  Returned as a DIGIT STRING, not a number, and that is not cosmetic. This
 *  column has TEXT affinity, and better-sqlite3 binds a JS number as a double, so
 *  a raw number is stored as "1772884800000.0" — which app/lib/dates.ts's
 *  `^\d{10,13}$` check rejects, after which Date.parse fails too and the posting
 *  is reported as "no date". Undated rows are ALWAYS kept by the date window, so
 *  that mistake silently makes an old posting permanently visible.
 *
 *  The digit-string form is also the shape that actually occurs: posted_at is
 *  written by app/dedup.py from Python, where an int binds as an INTEGER and TEXT
 *  affinity renders it as clean digits with no fractional part. dates.ts handles
 *  BOTH that and a genuine JS number; web/lib/dates.test.ts covers the numeric
 *  branch, so this fixture covers the string one. See QA_REPORT.md for the gap
 *  this exposed. */
function epochDaysAgo(days) {
  const d = new Date();
  d.setHours(12, 0, 0, 0);
  d.setDate(d.getDate() - days);
  return String(d.getTime());
}

// ---------------------------------------------------------------------------
// The cast
// ---------------------------------------------------------------------------

const JAVA_JD_HTML =
  "<p>We are looking for a Senior Test Automation Engineer to own our Java test " +
  "framework.</p><ul><li>Java 17, JUnit 5, Maven</li><li>Cucumber and RestAssured " +
  "for API coverage</li><li>Jenkins pipelines, Docker</li></ul>" +
  "<p>Hybrid working: two days per week in the London office.</p>";

const JOBS = [
  {
    url: "https://fixtures.test/jobs/northwind-senior-test-automation",
    company: "Northwind Digital",
    title: "Senior Test Automation Engineer",
    location: "London (hybrid)",
    posted_at: isoDaysAgo(3),
    description: JAVA_JD_HTML,
    source: "greenhouse",
    employment_type: "contract",
    ir35_status: "outside",
    ir35_evidence: "This role is Outside IR35",
    day_rate_min: 450,
    day_rate_max: 500,
    day_rate_source: "stated",
    rate_verdict: "pass",
    rate_required: 375.21,
    perm_equivalent: 92500,
    rate_reason: "£500/day clears the £375/day needed for outside IR35.",
    language_tier: "priority",
    language_rank: 3,
    language_hits: "java, junit, maven",
    clearance_status: "none",
    clearance_evidence: null,
    last_listed_at: null,
    match_score: 81,
    recommendation: "apply",
    genuine_gaps: "No production Kotlin experience.",
    transferable_strengths: "Ten years of Java SDET work transfers directly.",
    risk_factors: "Contract role; confirm the day rate is outside IR35 in writing.",
    my_status: null,
    notes: null,
  },
  {
    url: "https://fixtures.test/jobs/bluepeak-qa-automation",
    company: "Bluepeak Software",
    title: "QA Automation Engineer",
    location: "Manchester, UK",
    // A relative phrase, which is what a pasted LinkedIn advert gives us.
    posted_at: "10 days ago",
    // Plain text: the Ashby shape, and the branch of formatDescription that must
    // render as text rather than through dangerouslySetInnerHTML.
    description: "QA Automation Engineer\n\nYou will maintain our JavaScript and " +
                 "Node test suites.\nPlaywright experience welcome.\n",
    source: "ashby",
    employment_type: "permanent",
    language_tier: "secondary",
    language_rank: 2,
    language_hits: "javascript, node",
    clearance_status: "none",
    last_listed_at: null,
    match_score: 74,
    recommendation: "consider",
    genuine_gaps: "The stack is Node-first rather than Java.",
    transferable_strengths: "Automation architecture transfers across languages.",
    risk_factors: "Permanent salary may be below the target.",
    // The ONLY row with a My status, so the "My status" filter and the
    // invalid-status test both have a stable subject.
    my_status: "applied",
    notes: "Applied via the careers page on Monday.",
  },
  {
    url: "https://fixtures.test/jobs/coastal-test-analyst",
    company: "Coastal Systems",
    title: "Test Analyst",
    location: "Leeds, UK",
    posted_at: localMidnightDaysAgo(20),
    description: "Manual and exploratory testing of our internal tooling.",
    source: "lever",
    employment_type: null,
    language_tier: "none",
    language_rank: 1,
    language_hits: null,
    clearance_status: "none",
    last_listed_at: null,
    match_score: 60,
    recommendation: "skip",
    genuine_gaps: "No automation in the role at all.",
    transferable_strengths: "Exploratory testing experience.",
    risk_factors: "A step away from automation.",
    my_status: null,
    notes: null,
  },
  {
    url: "https://fixtures.test/jobs/trading-house-automation",
    company: "Trading House Ltd",
    title: "Automation Engineer",
    location: "London, UK",
    // Two years old, and STILL LISTED. This is the case that proves posted_at is
    // the employer's claim rather than the truth: without last_listed_at the
    // default 30-day window would hide a live, open role.
    posted_at: "2023-06-28T00:00:00.000Z",
    description: "Java and Cucumber automation for a trading platform.",
    source: "ashby",
    employment_type: "permanent",
    language_tier: "priority",
    language_rank: 3,
    language_hits: "java, cucumber",
    clearance_status: "none",
    last_listed_at: localMidnightDaysAgo(0),
    // Highest score on the board, deliberately: the Posted sort test asserts
    // that this row stops being first, which only proves anything if it starts
    // first under the default Score sort.
    match_score: 92,
    recommendation: "apply",
    genuine_gaps: "No trading-domain experience.",
    transferable_strengths: "Java automation at exactly this level.",
    risk_factors: "Financial-services domain.",
    my_status: null,
    notes: null,
  },
  {
    url: "https://fixtures.test/jobs/boundary-sdet",
    company: "Boundary Analytics",
    title: "SDET",
    location: "Bristol, UK",
    // EXACTLY on the window's closed edge: withinWindow is `days <= window`, so
    // this must be visible at the default 30-day setting. An off-by-one here
    // hides a real posting and looks like nothing at all.
    posted_at: localMidnightDaysAgo(30),
    description: "Java SDET role, hybrid in Bristol.",
    source: "greenhouse",
    language_tier: "none",
    language_rank: 1,
    language_hits: null,
    clearance_status: "none",
    last_listed_at: null,
    match_score: 65,
    recommendation: "consider",
    genuine_gaps: "Bristol-based, so relocation or travel is needed.",
    transferable_strengths: "Java and JUnit.",
    risk_factors: "Location.",
    my_status: null,
    notes: null,
  },
  {
    url: "https://fixtures.test/jobs/fresh-start-automation",
    company: "Fresh Start Ltd",
    title: "Automation Test Engineer",
    location: "London, UK",
    posted_at: isoDaysAgo(5),
    description: "A brand new automation role with no AI evaluation yet.",
    source: "lever",
    language_tier: "none",
    language_rank: 1,
    language_hits: null,
    clearance_status: "none",
    last_listed_at: null,
    // No ai_evaluations row and no cv_tailorings row: the subject of the
    // "Not evaluated" filter and of the tailoring journey from a standing start.
    match_score: null,
    recommendation: null,
    genuine_gaps: null,
    transferable_strengths: null,
    risk_factors: null,
    my_status: null,
    notes: null,
  },
  {
    url: "https://fixtures.test/jobs/ghost-files-senior-qa",
    company: "Ghost Files Ltd",
    title: "Senior QA Engineer",
    location: "London, UK",
    posted_at: isoDaysAgo(4),
    // HTML that was ENTITY-ESCAPED upstream — the Greenhouse shape that
    // formatDescription has to decode before it can be rendered.
    description: "&lt;p&gt;Java and Selenium. &amp;quot;Quality first&amp;quot; culture.&lt;/p&gt;",
    source: "greenhouse",
    language_tier: "priority",
    language_rank: 3,
    language_hits: "java, selenium",
    clearance_status: "none",
    last_listed_at: null,
    match_score: 70,
    recommendation: "apply",
    genuine_gaps: "Selenium-only, no Playwright.",
    transferable_strengths: "Java automation.",
    risk_factors: "None identified.",
    my_status: null,
    notes: null,
    // The DB says rendered; the filesystem says otherwise. This is the state the
    // board must report honestly rather than offering a link that 404s.
    cv: {
      status: "rendered",
      day_dir: "23-09-26",
      company_slug: "ghost-files-ltd",
      tailored_yaml: "roles:\n  - ref: r1\n",
      report: "Led with the Java API testing work.",
      verify_verdict: "accept",
      verify_notes: JSON.stringify({
        verdict: "accept", verify_model: "deepseek:deepseek-flash",
        unsupported_refs: "none", fabricated_claims: "none", invented_metrics: "none",
        required_fixes: "none", missing_must_haves: "none", notes: "none",
      }),
      outdir: GHOST_OUTDIR,
      // Inside the REAL cv_output tree, at a deliberately fake day/company pair
      // so it can never collide with a genuine application folder — but with no
      // file actually there. This reproduces the state found for real on
      // 2026-09-23: two rows claimed `rendered` while cv_output/<date>/ had gone,
      // so the panel offered an "Open PDF" link to nothing.
      pdf_path: path.join(GHOST_OUTDIR, "Ed-Minshull-CV.pdf"),
      docx_path: path.join(GHOST_OUTDIR, "Ed-Minshull-CV.docx"),
      gap_report_path: null,
      interview_prep_path: null,
      draft_model: "local:qwen3-coder-30b-a3b",
      verify_model: "deepseek:deepseek-flash",
      master_coverage: 58,
      tailored_coverage: 66,
    },
  },
  {
    url: "https://fixtures.test/jobs/draft-partners-qa",
    company: "Draft Partners",
    title: "QA Engineer",
    location: "London, UK",
    posted_at: "6 days ago",
    description: "Java automation with a focus on API testing.",
    source: "lever",
    language_tier: "none",
    language_rank: 1,
    language_hits: null,
    clearance_status: "none",
    last_listed_at: null,
    match_score: 68,
    recommendation: "consider",
    genuine_gaps: "No performance-testing evidence.",
    transferable_strengths: "API automation.",
    risk_factors: "None identified.",
    my_status: null,
    notes: null,
    // A draft that was written but never approved. The overlay must open
    // straight into review showing THIS text, without another model call — and
    // with a verdict of "revise", so its findings are rendered too.
    cv: {
      status: "draft",
      day_dir: "23-09-26",
      company_slug: "draft-partners",
      tailored_yaml: "roles:\n  - ref: r1\n    emphasis: api-testing\nskills:\n  - ref: s1\n",
      report: "Cut the marketing background and led with API testing.",
      verify_verdict: "revise",
      verify_notes: JSON.stringify({
        verdict: "revise",
        verify_model: "deepseek:deepseek-flash",
        unsupported_refs: "none",
        fabricated_claims: "Claims 'led a team of eight' — the master says 'mentored two'.",
        // "none" PLUS an explanation, which is how the verifier really writes a
        // clean field. The old full-string match read this as a finding and
        // rendered a clean check as a red "Numbers not in the master" flag.
        invented_metrics: "none. No numbers appear anywhere in the draft body.",
        required_fixes: "Reword the team-size claim to match the master.",
        missing_must_haves: "No evidence of Kafka.",
        notes: "Otherwise a faithful selection.",
      }),
      outdir: null, pdf_path: null, docx_path: null,
      gap_report_path: null, interview_prep_path: null,
      draft_model: "local:qwen3-coder-30b-a3b",
      verify_model: "deepseek:deepseek-flash",
      // The provenance, including the escalation that lost. `draft_model` alone
      // cannot express this: it names only the draft that SURVIVED, so without
      // this column a reopened record looks like no cloud retry ever happened —
      // which is precisely how a working fallback came to look broken.
      attempts: JSON.stringify([
        { model: "local:qwen3-coder-30b-a3b", verdict: "revise", reason: null },
        { model: "deepseek:deepseek-flash", verdict: "revise",
          reason: "Escalated to deepseek, but its draft verified no better "
                  + "(revise vs revise), so the original is shown" },
      ]),
      master_coverage: 64,
      tailored_coverage: 71,
    },
  },
  {
    url: "https://fixtures.test/jobs/defence-test-engineer",
    company: "Defence Systems Ltd",
    title: "Test Engineer",
    location: "London, UK",
    posted_at: isoDaysAgo(8),
    description: "Java and Selenium. You must be eligible for SC clearance.",
    source: "workable",
    language_tier: "priority",
    language_rank: 3,
    language_hits: "java, selenium",
    // Mentioned but obtainable: kept and FLAGGED. A "blocked" posting never
    // reaches the board at all, so "possible" is the only value the UI can show.
    clearance_status: "possible",
    clearance_evidence: "eligible: \u201cmust be eligible for SC\u201d",
    last_listed_at: null,
    match_score: 88,
    recommendation: "apply",
    genuine_gaps: "No defence-sector experience.",
    transferable_strengths: "Java automation and UK-based.",
    risk_factors: "Clearance process adds lead time.",
    my_status: null,
    notes: null,
  },
  {
    url: "https://fixtures.test/jobs/legacy-qa-lead",
    company: "Legacy Corp",
    title: "QA Lead",
    location: "Cardiff, UK",
    // No date at all. Undated postings are ALWAYS kept — we cannot prove they are
    // stale, and hiding a job because its advert omitted a date is worse than
    // showing it marked "no date".
    posted_at: null,
    description: "Lead the QA function. No posting date was provided.",
    source: "lever",
    language_tier: "none",
    language_rank: 1,
    language_hits: null,
    clearance_status: "none",
    last_listed_at: null,
    match_score: null,
    recommendation: null,
    genuine_gaps: null, transferable_strengths: null, risk_factors: null,
    my_status: null,
    notes: null,
  },
  {
    url: "https://fixtures.test/jobs/just-past-performance",
    company: "Just Past Ltd",
    title: "Performance Test Engineer",
    location: "Glasgow, UK",
    // 31 days: one day past the closed edge, so the default window hides it.
    // last_listed_at is NULL on purpose — otherwise "still listed" would keep it
    // visible and the boundary would not be tested at all.
    posted_at: localMidnightDaysAgo(31),
    description: "JMeter and Gatling performance testing.",
    source: "workable",
    language_tier: "none",
    language_rank: 1,
    language_hits: null,
    clearance_status: "none",
    last_listed_at: null,
    match_score: null,
    recommendation: null,
    genuine_gaps: null, transferable_strengths: null, risk_factors: null,
    my_status: null,
    notes: null,
  },
  {
    url: "https://fixtures.test/jobs/antique-qa",
    company: "Antique Systems",
    title: "QA Engineer",
    location: "London, UK",
    // The fourth date SHAPE: epoch milliseconds, as SmartRecruiters sends them.
    posted_at: epochDaysAgo(200),
    description: "An old posting, kept in the store but well outside the window.",
    source: "smartrecruiters",
    language_tier: "none",
    language_rank: 1,
    language_hits: null,
    clearance_status: "none",
    last_listed_at: null,
    match_score: null,
    recommendation: null,
    genuine_gaps: null, transferable_strengths: null, risk_factors: null,
    my_status: null,
    notes: null,
  },
];

// ---------------------------------------------------------------------------
// Schema — mirrors app/dedup.py's SCHEMA for the tables the board reads, so this
// script does not need the Python side to have run first.
// ---------------------------------------------------------------------------

const SCHEMA = `
CREATE TABLE IF NOT EXISTS job_details (
  url TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT, posted_at TEXT,
  description TEXT, passed_filters INTEGER, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
  source TEXT, employment_type TEXT, ir35_status TEXT, ir35_evidence TEXT,
  day_rate_min REAL, day_rate_max REAL, day_rate_source TEXT, rate_verdict TEXT,
  rate_required REAL, perm_equivalent REAL, rate_reason TEXT,
  language_tier TEXT, language_rank INTEGER, language_hits TEXT,
  clearance_status TEXT, clearance_evidence TEXT, last_listed_at TEXT
);
CREATE TABLE IF NOT EXISTS seen_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT UNIQUE, company_title_key TEXT,
  company TEXT, title TEXT, first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS ai_evaluations (
  url TEXT PRIMARY KEY, match_score INTEGER, recommendation TEXT, genuine_gaps TEXT,
  transferable_strengths TEXT, risk_factors TEXT, model TEXT,
  evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user_status (
  url TEXT PRIMARY KEY, my_status TEXT, notes TEXT,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS cv_tailorings (
  url TEXT PRIMARY KEY, status TEXT, day_dir TEXT, company_slug TEXT,
  tailored_yaml TEXT, report TEXT, interview_prep_md TEXT, outdir TEXT,
  pdf_path TEXT, docx_path TEXT, gap_report_path TEXT, interview_prep_path TEXT,
  draft_model TEXT, verify_model TEXT, verify_verdict TEXT, verify_notes TEXT,
  attempts TEXT,
  master_coverage REAL, tailored_coverage REAL, error TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
`;

const CV_FIELDS = [
  "status", "day_dir", "company_slug", "tailored_yaml", "report", "interview_prep_md",
  "outdir", "pdf_path", "docx_path", "gap_report_path", "interview_prep_path",
  "draft_model", "verify_model", "verify_verdict", "verify_notes", "attempts",
  "master_coverage", "tailored_coverage", "error",
];

const JOB_FIELDS = [
  "url", "company", "title", "location", "posted_at", "description", "source",
  "employment_type", "ir35_status", "ir35_evidence", "day_rate_min", "day_rate_max",
  "day_rate_source", "rate_verdict", "rate_required", "perm_equivalent", "rate_reason",
  "language_tier", "language_rank", "language_hits", "clearance_status",
  "clearance_evidence", "last_listed_at",
];

function normaliseKey(company, title, location) {
  const norm = (t) => String(t || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
  return `${norm(company)}::${norm(title)}::${norm(location)}`;
}

export function buildFixtureDb({ quiet = false } = {}) {
  mkdirSync(path.dirname(FIXTURE_DB_PATH), { recursive: true });
  const db = new Database(FIXTURE_DB_PATH);
  // WAL so the running Next server can read while this script writes. Without it
  // a reset during a test could hit "database is locked".
  db.pragma("journal_mode = WAL");
  db.exec(SCHEMA);

  // A `.fixtures/` database built by an earlier run has the OLD cv_tailorings
  // shape, and CREATE TABLE IF NOT EXISTS will not add a column to a table that
  // already exists — so the INSERT below would fail with "no column named
  // attempts" for anyone who has run the suite before. The fixture is generated
  // and gitignored, so it is never rebuilt by a checkout, which is exactly why
  // this has to heal itself rather than assume a fresh file.
  //
  // Same discipline as app/dedup.py's _migrate, for the same reason: adding a
  // column to the schema is not enough when something already wrote the old one.
  const tailoringColumns = new Set(
    db.prepare("PRAGMA table_info(cv_tailorings)").all().map((c) => c.name)
  );
  if (!tailoringColumns.has("attempts")) {
    db.exec("ALTER TABLE cv_tailorings ADD COLUMN attempts TEXT");
  }

  // Reset IN PLACE. Order matters only for readability; there are no FK
  // constraints declared between these tables.
  db.exec("DELETE FROM cv_tailorings; DELETE FROM user_status; DELETE FROM ai_evaluations; DELETE FROM job_details; DELETE FROM seen_jobs;");

  const insertJob = db.prepare(
    `INSERT INTO job_details (${JOB_FIELDS.join(", ")}, passed_filters)
     VALUES (${JOB_FIELDS.map(() => "?").join(", ")}, 1)`
  );
  const insertSeen = db.prepare(
    "INSERT OR IGNORE INTO seen_jobs (url, company_title_key, company, title) VALUES (?, ?, ?, ?)"
  );
  const insertEval = db.prepare(
    `INSERT OR REPLACE INTO ai_evaluations
       (url, match_score, recommendation, genuine_gaps, transferable_strengths, risk_factors, model)
     VALUES (?, ?, ?, ?, ?, ?, 'fixture')`
  );
  const insertStatus = db.prepare(
    "INSERT OR REPLACE INTO user_status (url, my_status, notes) VALUES (?, ?, ?)"
  );
  const insertCv = db.prepare(
    `INSERT OR REPLACE INTO cv_tailorings (url, ${CV_FIELDS.join(", ")})
     VALUES (?, ${CV_FIELDS.map(() => "?").join(", ")})`
  );

  const seed = db.transaction(() => {
    for (const job of JOBS) {
      // Every fixture row is passed_filters=1: the board shows only those, and
      // the filter tests are about the UI's own filtering, not the pipeline's.
      insertJob.run(...JOB_FIELDS.map((f) => (job[f] === undefined ? null : job[f])));
      insertSeen.run(job.url, normaliseKey(job.company, job.title, job.location), job.company, job.title);

      if (job.match_score !== null) {
        insertEval.run(job.url, job.match_score, job.recommendation,
                       job.genuine_gaps, job.transferable_strengths, job.risk_factors);
      }
      if (job.my_status || job.notes) {
        insertStatus.run(job.url, job.my_status ?? null, job.notes ?? null);
      }
      if (job.cv) {
        insertCv.run(job.url, ...CV_FIELDS.map((f) => (job.cv[f] === undefined ? null : job.cv[f])));
      }
    }
  });
  seed();

  const counts = {
    stored: db.prepare("SELECT COUNT(*) AS n FROM job_details WHERE passed_filters = 1").get().n,
    evaluated: db.prepare("SELECT COUNT(*) AS n FROM ai_evaluations").get().n,
    tailored: db.prepare("SELECT COUNT(*) AS n FROM cv_tailorings").get().n,
  };
  db.close();

  if (!quiet) {
    console.log(`[fixture] ${FIXTURE_DB_PATH} — ${counts.stored} on the board, ` +
                `${counts.evaluated} evaluated, ${counts.tailored} with CV state`);
  }
  return { path: FIXTURE_DB_PATH, ...counts };
}

// Run directly (`node make-fixture-db.mjs`), but stay importable from the specs.
if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  buildFixtureDb({ quiet: process.argv.includes("--quiet") });
  if (!existsSync(FIXTURE_DB_PATH)) process.exit(1);
}
