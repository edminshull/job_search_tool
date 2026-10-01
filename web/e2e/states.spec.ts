import { Route, test, expect } from "@playwright/test";
import { BOARD, ROW, openBoard, openPanelFor, resetBoard } from "./fixtures";

/**
 * The states the board shows when data is SLOW, unreachable, or REFUSED.
 *
 * This is one test rather than four because these are the same concern seen from
 * four angles, and they have to be exercised in sequence anyway: a failing board
 * cannot also be a board you are mid-journey on. What each section protects:
 *
 *   loading      the fetch takes ~800ms, so "Loading..." is on screen. Without
 *                it the board is briefly a blank table, which reads as "no jobs"
 *                — the same thing an empty database looks like.
 *   read failure /api/jobs returns 500. The board must say it cannot read the
 *                database and name the reason, NOT render an empty board. A
 *                "0 jobs" board and a broken one must never look alike, because
 *                the entire purpose of this tool is deciding what to apply to.
 *   slow overlay the stored-SCAN lookup is delayed, so the overlay shows that it
 *                is loading rather than rendering the idle screen for a job that
 *                has a scan. Showing the wrong state and then correcting it is
 *                worse than a moment of honesty.
 *   refused scan the scan route refuses (502, the subprocess could not run) and
 *                the overlay must stay open, quote the pipeline's own message,
 *                and state that nothing was written.
 */
test.describe("Loading and error states", () => {
  test.beforeEach(() => resetBoard());

  test("E1 slow, unreadable and refused: the board says what is wrong instead of looking empty", async ({ page }) => {
    // --- 1. a slow board shows a loading state -----------------------------
    await page.route("**/api/jobs", async (route: Route) => {
      await new Promise((r) => setTimeout(r, 800));
      await route.continue();
    });
    await page.goto("/");
    await expect(page.locator(".loading-state")).toHaveText("Loading...");
    await expect(page.locator("table")).toHaveCount(0);
    // ...and then real data arrives, so the loading state is a phase and not a
    // permanent condition.
    await expect(page.locator("table tbody tr")).toHaveCount(BOARD.visible);
    await expect(page.locator(".loading-state")).toHaveCount(0);
    await page.unroute("**/api/jobs");

    // --- 2. an unreadable board says so ------------------------------------
    await page.route("**/api/jobs", async (route: Route) => {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ error: "Failed to read from SQLite: unable to open database file" }),
      });
    });
    await page.reload();
    const error = page.locator(".error-state");
    await expect(error).toBeVisible();
    // The server's own reason is carried through, so the operator sees what
    // actually happened rather than a generic "something went wrong".
    await expect(error).toContainText("Can't read db:");
    await expect(error).toContainText("unable to open database file");
    // And the likely fix, because the overwhelmingly common cause is a first run
    // that has never populated the database.
    await expect(error).toContainText("make run");
    // Critically: no table and no empty state. An empty board would be
    // indistinguishable from having no jobs, which is the one thing this screen
    // must not imply.
    await expect(page.locator("table")).toHaveCount(0);
    await expect(page.locator(".empty-state")).toHaveCount(0);
    // The toolbar is still rendered, but the count badge cannot claim a count it
    // does not have.
    await expect(page.locator(".count-badge")).toHaveText("0 / 0");

    await page.unroute("**/api/jobs");

    // --- 3. the scan overlay's own loading state ---------------------------
    // The stored-scan record is fetched separately from the board payload when
    // the overlay opens, because the full scan is several KB per job. That second
    // request can be slow, and until it lands the board genuinely does not know
    // whether this job has a scan.
    await page.route("**/api/jobs/scan*", async (route: Route) => {
      await new Promise((r) => setTimeout(r, 900));
      await route.continue();
    });
    await openBoard(page);
    await openPanelFor(page, ROW.freshStart);
    await page.locator("#panel:not(.tailor-panel) button", { hasText: "Scan CV for this job" }).click();
    const overlay = page.locator(".tailor-overlay");
    await expect(overlay.locator(".loading-state")).toHaveText("Loading the stored scan…");
    // The overlay is up but has NOT yet decided which phase to show — so the idle
    // screen must not have appeared. This is the assertion that catches mounting
    // the panel before the record arrives, which would flash "does my CV pass this
    // posting?" at someone whose scan is already stored and about to be shown.
    await expect(overlay.locator("button", { hasText: "Run the scan" })).toHaveCount(0);
    await expect(overlay.locator(".scan-verdict")).toHaveCount(0);
    // It resolves into the correct phase: Fresh Start has no stored scan, so idle.
    await expect(overlay.locator("h3")).toHaveText("Does my CV pass this posting?");

    // --- 4. a REFUSED scan --------------------------------------------------
    // POST /api/cv/scan shells out to the Python pipeline, and a 502 is the
    // "it never ran" case: no virtualenv, no venv python, a wedged subprocess. It
    // is a user-actionable outcome (install the venv, try again), not a crash — so
    // it must be reported as such, with the pipeline's own words intact.
    const PIPELINE_MESSAGE =
      "No Python at /repo/.venv/bin/python. The CV engine needs its own virtualenv — "
      + "create it once with: python3 -m venv .venv";
    await page.route("**/api/cv/scan", async (route: Route) => {
      await route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({ error: PIPELINE_MESSAGE }),
      });
    });

    await overlay.locator("button", { hasText: "Run the scan" }).click();

    await expect(overlay.locator("h3")).toHaveText("Could not scan");
    // The instruction to create the venv survives intact, because the pipeline
    // writes its errors to be read by a person. Summarising it here would remove
    // the one line that says how to fix it.
    await expect(overlay.locator("pre.tailor-error")).toHaveText(PIPELINE_MESSAGE);
    await expect(overlay).toContainText("Nothing was written.");
    // Still open, still offering a retry and a clean way out.
    await expect(overlay.locator("button", { hasText: "Try again" })).toBeVisible();
    await expect(overlay.locator("button", { hasText: "Close" })).toBeVisible();

    // Closing at this point is allowed (the failure is not a busy state) and the
    // board underneath is still usable — no orphaned overlay, no stuck modal.
    await overlay.locator("#panel-close").click();
    await expect(page.locator(".tailor-overlay")).toHaveCount(0);
    await expect(page.locator("#panel:not(.tailor-panel)")).toBeVisible();
    await expect(page.locator("table tbody tr")).toHaveCount(BOARD.visible);
  });
});
