import Database from "better-sqlite3";
import path from "node:path";
import { existsSync } from "node:fs";

// Points at the SAME sqlite file the Python pipeline (dedup.py) writes to
// — this app is a read/write viewer on top of it, not a separate data
// store. Override with DB_PATH if your job_search_pipeline checkout lives
// somewhere other than the parent of this web/ folder.
const DB_PATH = process.env.DB_PATH || path.join(process.cwd(), "..", "data", "seen_jobs.sqlite3");

export const MY_STATUS_VALUES = ["applied", "interview", "rejected", "skipped", "silence"] as const;
export type MyStatus = (typeof MY_STATUS_VALUES)[number];

export type JobRow = {
  url: string;
  company: string;
  title: string;
  location: string;
  posted_at: string | null;
  description: string;
  passed_filters: number;
  source: string | null;
  // Contract-role fields, written by app/contract_rates.py
  employment_type: string | null;      // permanent | contract | unknown
  ir35_status: string | null;          // inside | outside | unknown
  ir35_evidence: string | null;
  day_rate_min: number | null;
  day_rate_max: number | null;
  day_rate_source: string | null;      // stated | derived_from_advertised_salary | estimated_by_adzuna
  rate_verdict: string | null;         // pass | review | fail | unknown
  rate_required: number | null;        // the £/day threshold it was judged against
  perm_equivalent: number | null;      // what the rate is worth as a salary
  rate_reason: string | null;
  // Deterministic screening signals, written by app/filters.py
  language_tier: string | null;        // priority | secondary | none | mismatch
  language_rank: number | null;        // 3 | 2 | 1 | 0, for sorting
  language_hits: string | null;        // e.g. "java, junit, maven"
  clearance_status: string | null;     // blocked | possible | none
  clearance_evidence: string | null;
  /** When this posting was last seen in a board fetch. Distinct from posted_at:
   *  that is the employer's claim about when the advert went up and can be years
   *  stale on a role that is still open. Null for aggregator-sourced rows, which
   *  are not re-fetched per board. */
  last_listed_at: string | null;
  /** The url of the advert this row is a RE-ADVERT of, or null when this row is
   *  the original. Written by app/duplicates.py, which decides that two adverts
   *  are the same job (same employer, identical title words, compatible
   *  locations) — the case the url and company_title_key both miss, because an
   *  agency reposts under a new advert id with the title and address nudged. */
  duplicate_of: string | null;
  match_score: number | null;
  recommendation: string | null;
  genuine_gaps: string | null;
  transferable_strengths: string | null;
  risk_factors: string | null;
  my_status: MyStatus | null;
  notes: string | null;
  /** Set only when my_status was COPIED from another advert of the same job that
   *  you had already decided, and it names that advert. "Applied" you recorded
   *  and "applied" a re-advert inherited are not the same claim, so the UI says
   *  which one it is rather than presenting a derived value as your own. */
  status_inherited_from: string | null;
  // Tailored-CV state, from cv_tailorings (written by app/cv_tailor.py).
  // Present on every row so the table can show a "CV ready" marker without
  // a second query per job.
  cv_status: string | null;             // draft | rendered | failed
  cv_pdf_path: string | null;
  cv_docx_path: string | null;
  cv_coverage_master: number | null;    // priority-term %, before tailoring
  cv_coverage_tailored: number | null;  // priority-term %, after tailoring
  cv_verify_verdict: string | null;     // accept | revise | reject | unknown
  cv_updated_at: string | null;
  /** True only when status is "rendered" AND the PDF is really on disk. The
   *  DB and the filesystem can disagree; the UI must not offer a link to a file
   *  that is not there. */
  cv_files_present: boolean;
  // ATS-scan state, from cv_scans (written by app/ats_scan.py via
  // `cv_tailor scan`). This is what the "Generate CV" button produces now:
  // whether the master CV would pass this posting's screening, and what real
  // experience is missing from it. Kept as summary columns on every row so the
  // table can badge a verdict without a query per job; the full payload (gap
  // lists, suggestions, evidence) is several KB and is read only when a panel
  // is open — same split as cv_tailorings above.
  cv_ats_verdict: string | null;        // pass | borderline | fail | unknown
  cv_ats_coverage: number | null;       // priority-term %, the CV as it stands
  cv_ats_projected: number | null;      // priority-term %, if every suggestion were added
  cv_ats_hard_gaps: number | null;      // requirement-level hard skills the CV misses
  cv_ats_suggestions: number | null;    // master achievements worth adding
  cv_ats_updated_at: string | null;
  // computed
  status: string;
};

let db: Database.Database | null = null;

// Columns added to job_details after the table first shipped. Kept in step
// with dedup.py's _migrate(), which declares the same set for the Python
// side. If the Next.js app is started against a DB file the pipeline has
// not touched yet, SELECTing these would throw "no such column" — so this
// app migrates them too rather than assuming the pipeline ran first.
const ADDED_COLUMNS: [string, string][] = [
  ["source", "TEXT"],
  ["employment_type", "TEXT"],
  ["ir35_status", "TEXT"],
  ["ir35_evidence", "TEXT"],
  ["day_rate_min", "REAL"],
  ["day_rate_max", "REAL"],
  ["day_rate_source", "TEXT"],
  ["rate_verdict", "TEXT"],
  ["rate_required", "REAL"],
  ["perm_equivalent", "REAL"],
  ["rate_reason", "TEXT"],
  ["language_tier", "TEXT"],
  ["language_rank", "INTEGER"],
  ["language_hits", "TEXT"],
  ["clearance_status", "TEXT"],
  ["clearance_evidence", "TEXT"],
  ["last_listed_at", "TEXT"],
  ["duplicate_of", "TEXT"],
];

function getDb(): Database.Database {
  if (db) return db;
  db = new Database(DB_PATH);
  // Mirrors dedup.py's SCHEMA for the tables this app touches — if the
  // Python pipeline hasn't run yet, `npm run dev` still starts cleanly
  // instead of erroring on a missing table.
  db.exec(`
    CREATE TABLE IF NOT EXISTS job_details (
      url TEXT PRIMARY KEY,
      company TEXT,
      title TEXT,
      location TEXT,
      posted_at TEXT,
      description TEXT,
      passed_filters INTEGER,
      fetched_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS ai_evaluations (
      url TEXT PRIMARY KEY,
      match_score INTEGER,
      recommendation TEXT,
      genuine_gaps TEXT,
      transferable_strengths TEXT,
      risk_factors TEXT,
      model TEXT,
      evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS user_status (
      url TEXT PRIMARY KEY,
      my_status TEXT,
      notes TEXT,
      updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
      inherited_from TEXT
    );
    CREATE TABLE IF NOT EXISTS cv_tailorings (
      url TEXT PRIMARY KEY,
      status TEXT,
      day_dir TEXT,
      company_slug TEXT,
      tailored_yaml TEXT,
      report TEXT,
      interview_prep_md TEXT,
      outdir TEXT,
      pdf_path TEXT,
      docx_path TEXT,
      gap_report_path TEXT,
      interview_prep_path TEXT,
      draft_model TEXT,
      verify_model TEXT,
      verify_verdict TEXT,
      verify_notes TEXT,
      attempts TEXT,
      readded TEXT,
      master_coverage REAL,
      tailored_coverage REAL,
      error TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    -- Mirrors app/dedup.py's SCAN_SCHEMA, for the same reason the block above
    -- mirrors its cv_tailorings DDL: this app must start against a database the
    -- pipeline has not touched yet, rather than erroring on a missing table.
    CREATE TABLE IF NOT EXISTS cv_scans (
      url TEXT PRIMARY KEY,
      verdict TEXT,
      coverage REAL,
      projected_coverage REAL,
      hard_gaps INTEGER,
      suggestion_count INTEGER,
      cv_source TEXT,
      cv_fetched_at TEXT,
      cv_stale INTEGER,
      scan TEXT,
      error TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP,
      updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
  `);

  const existing = new Set(
    (db.prepare("PRAGMA table_info(job_details)").all() as { name: string }[]).map((c) => c.name)
  );
  for (const [column, decl] of ADDED_COLUMNS) {
    if (!existing.has(column)) db.exec(`ALTER TABLE job_details ADD COLUMN ${column} ${decl}`);
  }
  // user_status needs its own check for the same reason dedup.py's _migrate keeps
  // a separate list per table: a column added to job_details is invisible here.
  const statusCols = new Set(
    (db.prepare("PRAGMA table_info(user_status)").all() as { name: string }[]).map((c) => c.name)
  );
  if (!statusCols.has("inherited_from")) {
    db.exec("ALTER TABLE user_status ADD COLUMN inherited_from TEXT");
  }
  return db;
}

export function getJobs(): JobRow[] {
  const rows = getDb()
    .prepare(
      `
      SELECT jd.url, jd.company, jd.title, jd.location, jd.posted_at,
             jd.description, jd.passed_filters, jd.source,
             jd.employment_type, jd.ir35_status, jd.ir35_evidence,
             jd.day_rate_min, jd.day_rate_max, jd.day_rate_source,
             jd.rate_verdict, jd.rate_required, jd.perm_equivalent, jd.rate_reason,
             jd.language_tier, jd.language_rank, jd.language_hits,
             jd.clearance_status, jd.clearance_evidence, jd.last_listed_at,
             jd.duplicate_of,
             ae.match_score, ae.recommendation, ae.genuine_gaps,
             ae.transferable_strengths, ae.risk_factors,
             us.my_status, us.notes, us.inherited_from AS status_inherited_from,
             cv.status AS cv_status, cv.pdf_path AS cv_pdf_path,
             cv.docx_path AS cv_docx_path, cv.master_coverage AS cv_coverage_master,
             cv.tailored_coverage AS cv_coverage_tailored,
             cv.verify_verdict AS cv_verify_verdict, cv.updated_at AS cv_updated_at,
             cs.verdict AS cv_ats_verdict, cs.coverage AS cv_ats_coverage,
             cs.projected_coverage AS cv_ats_projected, cs.hard_gaps AS cv_ats_hard_gaps,
             cs.suggestion_count AS cv_ats_suggestions, cs.updated_at AS cv_ats_updated_at
      FROM job_details jd
      LEFT JOIN ai_evaluations ae ON jd.url = ae.url
      LEFT JOIN user_status us ON jd.url = us.url
      LEFT JOIN cv_tailorings cv ON jd.url = cv.url
      LEFT JOIN cv_scans cs ON jd.url = cs.url
      WHERE jd.passed_filters = 1
      ORDER BY ae.match_score DESC
      `
    )
    .all() as Omit<JobRow, "status" | "cv_files_present">[];

  return rows.map((r) => ({
    ...r,
    // A rendered row whose PDF is no longer on disk. The database and the
    // filesystem can disagree — the file can be deleted, moved, or lost — and
    // when they do the honest answer is "there is no CV here", not a link that
    // 404s. This was found for real (2026-09-23): two rows claimed `rendered`
    // while cv_output/<date>/ had gone, and the panel happily offered an
    // "Open PDF" link to nothing.
    //
    // Checked here rather than in the file route alone, so the board can say so
    // BEFORE the click — a broken link the user only discovers by clicking is
    // exactly the kind of half-truth this system exists to avoid about facts,
    // and it applies just as much to its own file links.
    cv_files_present:
      r.cv_status === "rendered" && r.cv_pdf_path ? existsSync(r.cv_pdf_path) : false,
    status: r.recommendation ?? "not_evaluated", // apply | consider | skip | not_evaluated
  }));
}

export function jobExists(url: string): boolean {
  return getDb().prepare("SELECT 1 FROM job_details WHERE url = ?").get(url) !== undefined;
}

export function saveUserStatus(url: string, myStatus: string | null, notes: string | null): void {
  const db = getDb();
  db.prepare(
    `
    INSERT INTO user_status (url, my_status, notes, updated_at, inherited_from)
    VALUES (?, ?, ?, CURRENT_TIMESTAMP, NULL)
    ON CONFLICT(url) DO UPDATE SET
      my_status = excluded.my_status,
      notes = excluded.notes,
      updated_at = CURRENT_TIMESTAMP,
      inherited_from = NULL
    `
  ).run(url, myStatus || null, notes || null);

  // Writing a status HERE means you decided it, so any status this row had
  // inherited stops being inherited — without clearing it the row would keep
  // claiming the value came from another advert.
  //
  // Then carry the decision across to the other adverts of the SAME job, so
  // applying to the newer copy stops the older one sitting on the board as
  // untouched work. This is the mirror of app/duplicates.py's backward
  // inheritance and of dedup.propagate_status on the Python side; all three work
  // off `duplicate_of` alone, so the rules for what a duplicate IS stay in
  // app/duplicates.py and are never re-implemented here.
  //
  // Only rows with no status of their own are touched: a decision you made about
  // one advert is never overwritten by a decision you made about another.
  if (!myStatus) return;
  const root = db
    .prepare(`SELECT COALESCE(duplicate_of, url) AS root FROM job_details WHERE url = ?`)
    .get(url) as { root: string } | undefined;
  if (!root) return;
  db.prepare(
    `
    INSERT INTO user_status (url, my_status, inherited_from, updated_at)
    SELECT url, ?, ?, CURRENT_TIMESTAMP
    FROM job_details
    WHERE (url = ? OR duplicate_of = ?) AND url <> ?
    ON CONFLICT(url) DO UPDATE SET
      my_status = excluded.my_status,
      inherited_from = excluded.inherited_from,
      updated_at = CURRENT_TIMESTAMP
      WHERE user_status.my_status IS NULL
    `
  ).run(myStatus, url, root.root, root.root, url);
}

/**
 * The full tailoring record for one posting, including the drafted YAML.
 *
 * Kept separate from getJobs() because it is large: the YAML and the verifier's
 * findings are several KB per job, and the board renders 300+ rows. This is
 * read only when a panel is open, and it is how "Regenerate" can show the
 * previous draft instead of starting from nothing.
 */
export type TailoringRow = {
  url: string;
  status: string | null;
  day_dir: string | null;
  company_slug: string | null;
  tailored_yaml: string | null;
  report: string | null;
  interview_prep_md: string | null;
  outdir: string | null;
  pdf_path: string | null;
  docx_path: string | null;
  gap_report_path: string | null;
  interview_prep_path: string | null;
  draft_model: string | null;
  verify_model: string | null;
  verify_verdict: string | null;
  verify_notes: string | null;
  /** JSON array of CvAttempt, written by cv_tailor.preview so the drafting
   *  provenance — including a cloud re-draft that lost the comparison —
   *  survives a reload instead of being re-derived from `draft_model`. */
  attempts: string | null;
  master_coverage: number | null;
  tailored_coverage: number | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export function getTailoring(url: string): TailoringRow | null {
  const row = getDb()
    .prepare(`SELECT * FROM cv_tailorings WHERE url = ?`)
    .get(url) as TailoringRow | undefined;
  return row ?? null;
}

/**
 * The stored ATS scan for one posting.
 *
 * `scan` is the whole JSON payload the pipeline emitted — gap lists, the
 * suggestions with the master achievement each one came from, and the evidence
 * behind the verdict. Parsed here rather than in the component so that one
 * malformed payload cannot crash a render: a scan that will not parse is
 * reported as no scan, which is a state this UI already handles.
 */
export type ScanRow = {
  url: string;
  verdict: string | null;
  coverage: number | null;
  projected_coverage: number | null;
  hard_gaps: number | null;
  suggestion_count: number | null;
  cv_source: string | null;
  cv_fetched_at: string | null;
  cv_stale: number | null;
  scan: string | null;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
};

export type ScanPayload = {
  row: ScanRow;
  /** The decoded `scan` JSON, or null when it is absent/unparseable. */
  result: Record<string, unknown> | null;
};

export function getScan(url: string): ScanPayload | null {
  const row = getDb()
    .prepare(`SELECT * FROM cv_scans WHERE url = ?`)
    .get(url) as ScanRow | undefined;
  if (!row) return null;
  let result: Record<string, unknown> | null = null;
  if (row.scan) {
    try {
      const parsed = JSON.parse(row.scan);
      if (parsed && typeof parsed === "object") result = parsed as Record<string, unknown>;
    } catch {
      result = null;
    }
  }
  return { row, result };
}

/** Where tailored CVs are filed, for the messages the UI shows. Derived from
 *  the same env var the Python side uses, so the two cannot disagree. */
export const CV_OUTPUT_ROOT =
  process.env.CV_OUTPUT_ROOT || path.join(process.cwd(), "..", "cv_output");
