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
 *   12 stored (all passed_filters = 1)
 *   10 visible under the default 30-day window, 2 hidden by it
 *    8 AI-evaluated → 4 apply, 3 consider, 1 skip, 4 not evaluated
 *    1 contract, 2 permanent, 9 with no employment type declared
 *    4 language priority, 1 secondary, 7 naming no language
 *    1 row with a My status ("applied"), 2 rows with CV state
 */
// Playwright transpiles these spec files to CommonJS (package.json has no
// "type": "module"), so `__dirname` is the correct way to locate a sibling file
// here — `import.meta.url` throws "Cannot use 'import.meta' outside a module" at
// collection time, which looks like a broken test file rather than a module
// format mismatch.
const FIXTURE_SCRIPT = path.join(__dirname, "scripts", "make-fixture-db.mjs");

export const BOARD = {
  /** Every row in job_details with passed_filters = 1 — the board's denominator. */
  total: 12,
  /** Rows the default 30-day window shows. */
  visible: 10,
  /** Rows the default 30-day window hides (31 days, and 200 days, never re-listed). */
  hiddenByDate: 2,
} as const;

/** Counts per toolbar filter, under the DEFAULT date window unless noted. */
export const FILTER_EXPECTATIONS = {
  typeContract: 1,       // Northwind Digital
  typePermanent: 9,      // permanent (2) + unknown (9) − the contract row, of 10 visible
  languagePriority: 4,   // Northwind, Trading House, Ghost Files, Defence Systems
  languagePriorityPlus: 5, // + Bluepeak (JS/Node secondary)
  languageKnown: 5,      // same as priority+ : "no language named" is excluded
  aiApply: 4,
  aiConsider: 3,
  aiSkip: 1,
  aiNotEvaluated: 2,     // of 4; the other two are hidden by the date window
  myStatusApplied: 1,    // Bluepeak — the only row with a My status
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
  ghostFiles: "https://fixtures.test/jobs/ghost-files-senior-qa",
  draftPartners: "https://fixtures.test/jobs/draft-partners-qa",
  freshStart: "https://fixtures.test/jobs/fresh-start-automation",
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
 * `#panel:not(.tailor-panel)` rather than `#panel`, because the tailoring
 * overlay reuses the `#panel` id and both can be mounted at once — the detail
 * panel stays open underneath while a CV is drafted. An unscoped `#panel`
 * locator would match two elements and fail in strict mode the moment a
 * tailoring overlay opens. */
export async function openPanelFor(page: Page, company: string): Promise<void> {
  await rows(page).filter({ hasText: company }).first().click();
  await expect(page.locator("#panel:not(.tailor-panel)")).toBeVisible();
}

/** The detail panel (never the tailoring overlay). */
export function detailPanel(page: Page) {
  return page.locator("#panel:not(.tailor-panel)");
}

/** The tailoring overlay's panel. */
export function tailorPanel(page: Page) {
  return page.locator(".tailor-panel");
}

/** The toolbar control, addressed by its visible label. */
export function toolbarSelect(page: Page, label: string) {
  return page.locator(`.toolbar label:has-text("${label}") select`);
}

export function toolbarSearch(page: Page) {
  return page.locator('.toolbar input[type="search"]');
}
