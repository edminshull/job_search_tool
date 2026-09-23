// Tests for the tailored-CV state that lib/db.ts derives for the board.
// Run with: npm test
//
// The one that matters here is cv_files_present. It exists because the
// database and the filesystem can disagree: cv_tailorings said `rendered` for
// two jobs while cv_output/<date>/ was gone, and the panel offered an "Open PDF"
// link that 404'd. A row claiming a CV it cannot produce is the same class of
// half-truth this system refuses to tell about a candidate's experience, so it
// must not tell it about its own files either.
//
// The database and the output root are both redirected into a temp directory
// BEFORE lib/db.ts is imported, because both are module-level constants.
import {test} from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import {mkdtempSync, mkdirSync, writeFileSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import Database from "better-sqlite3";

const FIXTURE = mkdtempSync(path.join(tmpdir(), "db-cv-state-"));
const DB_PATH = path.join(FIXTURE, "test.sqlite3");
const OUTPUT_ROOT = path.join(FIXTURE, "cv_output");
mkdirSync(OUTPUT_ROOT, {recursive: true});

process.env.DB_PATH = DB_PATH;
process.env.CV_OUTPUT_ROOT = OUTPUT_ROOT;

const {getJobs, getTailoring} = await import("./db.ts");

// --- fixture: one job whose CV file exists, one whose file does not ---------
const presentDir = path.join(OUTPUT_ROOT, "23-09-26", "acme");
mkdirSync(presentDir, {recursive: true});
const presentPdf = path.join(presentDir, "Ed-Minshull-CV.pdf");
writeFileSync(presentPdf, "%PDF-1.4 fixture");
const missingPdf = path.join(OUTPUT_ROOT, "23-09-26", "ghost", "Ed-Minshull-CV.pdf");

{
  const db = new Database(DB_PATH);
  // Columns mirrored from dedup.py's SCHEMA, which is the real source of truth.
  // A fixture that omits them fails with "no such column" rather than testing
  // anything — the board SELECTs the full contract/clearance/screening set.
  db.exec(`
    CREATE TABLE job_details (url TEXT PRIMARY KEY, company TEXT, title TEXT,
      location TEXT, posted_at TEXT, description TEXT, passed_filters INTEGER,
      source TEXT, employment_type TEXT, ir35_status TEXT, ir35_evidence TEXT,
      day_rate_min REAL, day_rate_max REAL, day_rate_source TEXT,
      rate_verdict TEXT, rate_required REAL, perm_equivalent REAL, rate_reason TEXT,
      language_tier TEXT, language_rank INTEGER, language_hits TEXT,
      clearance_status TEXT, clearance_evidence TEXT);
    CREATE TABLE ai_evaluations (url TEXT PRIMARY KEY, match_score INTEGER,
      recommendation TEXT, genuine_gaps TEXT, transferable_strengths TEXT,
      risk_factors TEXT);
    CREATE TABLE user_status (url TEXT PRIMARY KEY, my_status TEXT, notes TEXT);
    CREATE TABLE cv_tailorings (url TEXT PRIMARY KEY, status TEXT, day_dir TEXT,
      company_slug TEXT, tailored_yaml TEXT, pdf_path TEXT, docx_path TEXT,
      master_coverage REAL, tailored_coverage REAL, verify_verdict TEXT,
      verify_notes TEXT, updated_at TEXT);
  `);
  const addJob = db.prepare(
    "INSERT INTO job_details (url, company, title, passed_filters) VALUES (?, ?, ?, 1)");
  addJob.run("u-present", "Acme", "SDET");
  addJob.run("u-missing", "Ghost Ltd", "QA Engineer");
  // A job with no tailoring at all, to prove the join does not invent a state.
  addJob.run("u-untailored", "Never Tailored", "Tester");

  const addTailoring = db.prepare(
    `INSERT INTO cv_tailorings (url, status, day_dir, company_slug, pdf_path,
       master_coverage, tailored_coverage, verify_verdict, updated_at)
     VALUES (?, 'rendered', '23-09-26', ?, ?, 82.0, 74.0, 'accept', CURRENT_TIMESTAMP)`);
  addTailoring.run("u-present", "acme", presentPdf);
  // The bug: status says rendered, the file is not there.
  addTailoring.run("u-missing", "ghost", missingPdf);
  db.close();
}

const byUrl = () => new Map(getJobs().map((j) => [j.url, j]));

test("a rendered row whose file exists reports files present", () => {
  const job = byUrl().get("u-present");
  assert.equal(job?.cv_status, "rendered");
  assert.equal(job?.cv_files_present, true);
  assert.equal(job?.cv_pdf_path, presentPdf);
});

test("a rendered row whose file is gone does NOT report files present", () => {
  // The regression this file exists for. Without it the panel renders an
  // "Open PDF" link to a path that is not on disk.
  const job = byUrl().get("u-missing");
  assert.equal(job?.cv_status, "rendered", "status still says rendered — that is the whole problem");
  assert.equal(job?.cv_files_present, false, "must not claim a file that does not exist");
  assert.equal(job?.cv_pdf_path, missingPdf, "the stale path is still recorded, so it can be re-rendered");
});

test("a job that was never tailored has no CV state at all", () => {
  const job = byUrl().get("u-untailored");
  assert.equal(job?.cv_status, null);
  assert.equal(job?.cv_files_present, false);
  assert.equal(job?.cv_pdf_path, null);
});

test("a draft with no pdf_path is not reported as having files", () => {
  // `draft` rows have no pdf_path by design (the preview writes nothing), and
  // that must not read as a missing file — it is simply not rendered yet.
  const db = new Database(DB_PATH);
  db.prepare(`INSERT INTO cv_tailorings (url, status, tailored_yaml) VALUES (?, 'draft', 'summary: x')`)
    .run("u-untailored");
  db.close();
  const job = byUrl().get("u-untailored");
  assert.equal(job?.cv_status, "draft");
  assert.equal(job?.cv_files_present, false);
});

test("the full tailoring record round-trips for the panel", () => {
  const stored = getTailoring("u-present");
  assert.equal(stored?.status, "rendered");
  assert.equal(stored?.master_coverage, 82);
  assert.equal(stored?.tailored_coverage, 74);
  assert.equal(stored?.verify_verdict, "accept");
  assert.equal(getTailoring("nobody"), null);
});

test.after(() => {
  assert.ok(FIXTURE.startsWith(tmpdir()), "refusing to clean up outside the temp dir");
  rmSync(FIXTURE, {recursive: true, force: true});
});
