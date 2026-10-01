import { expect, Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import path from "node:path";

/**
 * Shared helpers and the fixture's expected numbers, in ONE place.
 *
 * Every count asserted by more than one spec lives here rather than being
 * written out per test, so a change to the fixture cast cannot leave one spec
 * asserting yesterday's arithmetic while another is updated. The comments say
 * what each number IS, because a bare `expect(count).toBe(10)` in a test is a
 * fact with no reason attached.
 *
 * The board (see e2e/scripts/make-fixture-db.mjs for the full cast):
 *
 *   14 stored (all passed_filters = 1), and all 14 shown by DEFAULT — the
 *      date window is now "All dates" (see lib/dates.ts's DEFAULT_DATE_WINDOW),
 *      so nothing is hidden until the user narrows it
 *   12 visible under "Last month" (30 days), 2 hidden by it
 *   10 AI-evaluated → 4 apply, 5 consider, 1 skip, 4 not evaluated
 *    3 contract, 11 kept by "permanent" (permanent + unknown)
 *    6 language priority, 7 priority+, 7 naming a language at all
 *    3 rows with a My status ("applied"), 2 rows with CV state
 *    3 rows with a stored ATS scan: pass (Northwind), fail (Bluepeak),
 *      inconclusive (Coastal — its stored text is a snippet)
 *    2 rows of one re-advert pair (see duplicates.spec.ts)
 */
// Playwright transpiles these spec files to CommonJS (package.json has no
// "type": "module"), so `__dirname` is the correct way to locate a sibling file
// here — `import.meta.url` throws "Cannot use 'import.meta' outside a module" at
// collection time, which looks like a broken test file rather than a module
// format mismatch.
const FIXTURE_SCRIPT = path.join(__dirname, "scripts", "make-fixture-db.mjs");

export const BOARD = {
  /** Every row in job_details with passed_filters = 1 — the board's denominator. */
  total: 14,
  /** Rows the DEFAULT window shows. It is "All dates", so this is every row:
   *  the recency window is an opt-in filter, not a hidden default limit. */
  visible: 14,
  /** Rows "Last month" (30 days) shows — the boundary test's narrowed board. */
  visibleLastMonth: 12,
  /** Rows "Last month" (30 days) hides: the 31-day and the 200-day posting,
   *  neither of them re-listed. The undated row is ALWAYS kept. */
  hiddenByLastMonth: 2,
  /** Rows with no AI score, which the sort tests expect to sink to the bottom. */
  unscored: 4,
} as const;

/** Counts per toolbar filter, under the DEFAULT "All dates" window. */
export const FILTER_EXPECTATIONS = {
  typeContract: 3,       // Northwind Digital + both Experis adverts of one job
  typePermanent: 11,     // permanent (2) + unknown (9) − the contract rows
  languagePriority: 6,   // Northwind, Trading House, Ghost Files, Defence + both Experis
  languagePriorityPlus: 7, // + Bluepeak (JS/Node secondary)
  languageKnown: 7,      // same as priority+ : "no language named" is excluded
  aiApply: 4,
  aiConsider: 5,         // includes both Experis adverts of one job
  aiSkip: 1,
  aiNotEvaluated: 4,     // Fresh Start, Legacy (undated), Just Past, Antique
  myStatusApplied: 3,    // Bluepeak + both Experis adverts (one of them inherited)
} as const;

/** The stored ATS scans, addressed by the row they belong to. The numbers are
 *  the fixture's own `cv_scans` columns (joined onto the board as the `cv_ats_*`
 *  fields), so a spec asserting a board chip and a spec asserting the open panel
 *  are asserting the same scan — and a change to the fixture cannot leave one
 *  asserting yesterday's number. */
export const SCAN = {
  /** Northwind Digital — pass, 88% priority-term coverage (22 of 25), no gaps. */
  pass: {
    company: "Northwind Digital", verdict: "pass", coverage: 88,
    evidenced: 22, total: 25,
    chip: { label: "CV would pass", tone: "ok" },
  },
  /** Bluepeak Software — fail: 2 hard gaps, 1 closable suggestion, 1 unclosable
   *  gap, a projection that still fails. */
  fail: {
    company: "Bluepeak Software", verdict: "fail", coverage: 54.5, projected: 63.6,
    hardGaps: 2, suggestions: 1,
    chip: { label: "CV would be filtered", tone: "bad" },
  },
  /** Coastal Systems — cannot tell: the stored text is a 52-char snippet, so the
   *  scan refuses a verdict rather than inventing one. */
  inconclusive: {
    company: "Coastal Systems", verdict: "unknown", coverage: 100,
    chip: { label: "CV scan inconclusive", tone: "unev" },
  },
} as const;

/** The rows the default sort (Score, descending) puts first. */
export const SORTED_BY_SCORE_DESC = {
  /** 92 — the highest score on the board, and a stale posted_at rescued by
   *  last_listed_at, so the Posted sort must move it off the top. */
  first: "Trading House Ltd",
} as const;

/** The rows the newest-first Posted sort puts first and last. */
export const SORTED_BY_POSTED_DESC = {
  first: "Northwind Digital",   // 3 days ago
} as const;

/** Titles/companies used to address individual rows. */
export const ROW = {
  northwind: "Northwind Digital",
  bluepeak: "Bluepeak Software",
  coastal: "Coastal Systems",
  tradingHouse: "Trading House Ltd",
  boundary: "Boundary Analytics",
  freshStart: "Fresh Start Ltd",
  ghostFiles: "Ghost Files Ltd",
  draftPartners: "Draft Partners",
  defence: "Defence Systems Ltd",
  legacy: "Legacy Corp",
  justPast: "Just Past Ltd",
  antique: "Antique Systems",
} as const;

/** Job URLs — the primary key every API in the board is addressed by. */
export const URLS = {
  northwind: "https://fixtures.test/jobs/northwind-senior-test-automation",
  bluepeak: "https://fixtures.test/jobs/bluepeak-qa-automation",
  coastal: "https://fixtures.test/jobs/coastal-test-analyst",
  ghostFiles: "https://fixtures.test/jobs/ghost-files-senior-qa",
  draftPartners: "https://fixtures.test/jobs/draft-partners-qa",
  freshStart: "https://fixtures.test/jobs/fresh-start-automation",
  /** The re-advert pair: the same job advertised twice, the shorter stored text
   *  being the re-advert. app/duplicates.py links them, which is what makes the
   *  scan panel able to offer "scan the fuller copy" at all. */
  experisReadvert: "https://fixtures.test/jobs/experis-ai-automation-tester",
  experisOriginal: "https://fixtures.test/jobs/experis-ai-automation-tester-x2",
} as const;

/**
 * Reset the fixture database IN PLACE, between tests.
 *
 * The two status tests write rows; without a reset, whether they pass would
 * depend on the order they happened to run in. In place, not by deleting the
 * file, because `next dev` holds an open handle on it — see the note at the top
 * of make-fixture-db.mjs.
 */
export function resetBoard(): void {
  execFileSync(process.execPath, [FIXTURE_SCRIPT, "--quiet"], { stdio: "pipe" });
}

/** Load the board and wait for the table to be rendered and populated. */
export async function openBoard(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.locator("header h1")).toHaveText("Job Search Board");
  // The count badge is rendered as soon as data arrives, and it is the one
  // element that proves BOTH that the fetch finished and what it contained.
  await expect(page.locator(".count-badge")).toHaveText(
    new RegExp(`^\\d+ / ${BOARD.total}$`)
  );
}

/** The board's rows, excluding the header row. */
export function rows(page: Page) {
  return page.locator("table tbody tr");
}

/**
 * The rendering order of a column, for sort assertions.
 *
 * Reads the actual cells rather than trusting a class, so a sort that reorders
 * nothing cannot pass.
 */
export async function columnValues(page: Page, cellSelector: string): Promise<string[]> {
  return page.locator(`table tbody tr ${cellSelector}`).allInnerTexts();
}

/** The count badge, parsed as [shown, total]. */
export async function badgeCounts(page: Page): Promise<[number, number]> {
  const text = (await page.locator(".count-badge").innerText()).trim();
  const [shown, total] = text.split("/").map((part) => Number(part.trim()));
  return [shown, total];
}

/** Open the detail panel for the row belonging to `company`.
 *
 * `#panel:not(.tailor-panel)` rather than `#panel`, because the CV-scan overlay
 * reuses the `#panel` id and both can be mounted at once — the detail panel stays
 * open underneath while a scan is shown. An unscoped `#panel` locator would match
 * two elements and fail in strict mode the moment the scan overlay opens. */
export async function openPanelFor(page: Page, company: string): Promise<void> {
  await rows(page).filter({ hasText: company }).first().click();
  await expect(page.locator("#panel:not(.tailor-panel)")).toBeVisible();
}

/** Open the detail panel for the row whose TITLE cell matches exactly.
 *
 * Needed because the fixture deliberately stores the same job twice under one
 * employer (the Experis re-advert pair), so `openPanelFor`'s company match is
 * ambiguous between them and `.first()` would silently address whichever of the
 * two the current sort happened to put first. */
export async function openPanelForTitle(page: Page, title: string): Promise<void> {
  await rows(page)
    .filter({has: page.locator(".title-text", {hasText: new RegExp(`^${title}$`)})})
    .first()
    .click();
  await expect(page.locator("#panel:not(.tailor-panel)")).toBeVisible();
}

/** The detail panel (never the scan overlay). */
export function detailPanel(page: Page) {
  return page.locator("#panel:not(.tailor-panel)");
}

/** The scan overlay's wrapper — present for both its loading state and the panel. */
export function scanOverlay(page: Page) {
  return page.locator(".tailor-overlay");
}

/** The scan overlay's own panel, once the stored scan has arrived.
 *
 * `#panel.scan-panel` rather than `.tailor-panel` alone: the loading state that
 * precedes it renders a `#panel.tailor-panel` with no `scan-panel` class, and a
 * spec that matched both would be asserting against the wrong phase. */
export function scanPanel(page: Page) {
  return page.locator("#panel.scan-panel");
}

/** The toolbar control, addressed by its visible label. */
export function toolbarSelect(page: Page, label: string) {
  return page.locator(`.toolbar label:has-text("${label}") select`);
}

export function toolbarSearch(page: Page) {
  return page.locator('.toolbar input[type="search"]');
}
