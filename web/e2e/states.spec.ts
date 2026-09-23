import { Route, test, expect } from "@playwright/test";
import { ROW, openBoard, openPanelFor, resetBoard } from "./fixtures";

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
 *   slow overlay the stored-CV lookup is delayed, so the overlay shows that it is
 *                loading rather than rendering "never tailored" for a job that has
 *                a CV. Showing the wrong state and then correcting it is worse
 *                than a moment of honesty.
 *   refused write a render that fails must leave the overlay open, quote the
 *                pipeline's own message, and state that nothing was written.
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
    await expect(page.locator("table tbody tr")).toHaveCount(10);
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

    // --- 3. the tailoring overlay's own loading state ----------------------
    // The stored-CV record is fetched separately from the board payload when the
    // overlay opens, because the drafted YAML is several KB per job. That second
    // request can be slow, and until it lands the board genuinely does not know
    // whether this job has a CV.
    await page.route("**/api/jobs/tailoring*", async (route: Route) => {
      await new Promise((r) => setTimeout(r, 900));
      await route.continue();
    });
    await openBoard(page);
    await openPanelFor(page, ROW.freshStart);
    await page.locator("#panel:not(.tailor-panel) button", { hasText: "Tailor CV for this job" }).click();
    const overlay = page.locator(".tailor-panel");
    await expect(overlay.locator(".loading-state")).toHaveText("Loading the stored CV…");
    // The overlay is up but has NOT yet decided which phase to show — so the idle
    // screen must not have appeared. This is the assertion that catches mounting
    // the overlay before the record arrives, which would flash "Draft a CV" at
    // someone who already has one.
    await expect(overlay.locator("button", { hasText: "Draft CV" })).toHaveCount(0);
    // It resolves into the correct phase: Fresh Start has no CV, so idle.
    await expect(overlay.locator("h3")).toContainText("Draft a CV for this posting");

    // --- 4. a REFUSED render ----------------------------------------------
    // The renderer refuses to build a CV whose claims cv/master.yaml cannot
    // support. That is the whole reason the output can be trusted, and it is a
    // user-actionable outcome (edit or regenerate), not a crash — so it must be
    // reported as such and must write nothing.
    await page.route("**/api/tailor/preview", async (route: Route) => {
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify({
          url: "https://fixtures.test/jobs/fresh-start-automation",
          status: "draft", day_dir: "01-01-01", company_slug: "fresh-start-ltd",
          future_outdir: "/fixtures/cv_output/01-01-01/fresh-start-ltd",
          tailored_yaml: "roles:\n  - ref: not-in-the-master\n",
          report: "Draft.", draft_model: "local:qwen3-coder-30b-a3b",
          verification: { verdict: "reject", verify_model: "deepseek:deepseek-flash",
            fabricated_claims: "Lists a skill the master cannot evidence." },
          coverage_before: 64, coverage_after: 70, priority_gaps: 1,
          gap_report: "", interview_prep_md: null, posting_available: true,
        }),
      });
    });

    // The unsupported-claim branch first: 422, and the copy is specific about
    // what happened and what to do.
    await page.route("**/api/tailor/render", async (route: Route) => {
      await route.fulfill({
        status: 422,
        contentType: "application/json",
        body: JSON.stringify({
          error: "Refusing to render: skill 'Playwright' (ref s9) is not supported by cv/master.yaml.",
          kind: "unsupported_claim",
        }),
      });
    });

    await overlay.locator("button", { hasText: "Draft CV" }).click();
    await expect(overlay.locator(".tailor-verdict")).toContainText("Rejected");
    await overlay.locator("button", { hasText: "Approve & render" }).click();

    await expect(overlay.locator("h3").first()).toHaveText("Nothing was rendered — unsupported claim");
    // The pipeline's own message, verbatim — it names the skill and the ref, which
    // is what makes the fix possible.
    await expect(overlay.locator("pre.tailor-error")).toContainText("is not supported by cv/master.yaml");
    await expect(overlay).toContainText("refuses to build a CV whose skills the master cannot support");
    // The overlay stays OPEN: the draft is still there to be fixed, and closing it
    // would throw away the work.
    await expect(overlay).toBeVisible();
    await expect(overlay.locator("h3", { hasText: "Written" })).toHaveCount(0);
    await expect(overlay.locator("button", { hasText: "Back to the draft" })).toBeVisible();

    // The other branch: a machinery failure rather than a bad claim. 502, and the
    // generic "No files were written." note instead of the fabrication advice.
    await page.unroute("**/api/tailor/render");
    await page.route("**/api/tailor/render", async (route: Route) => {
      await route.fulfill({
        status: 502,
        contentType: "application/json",
        body: JSON.stringify({
          error: "No Python at /repo/.venv/bin/python. The CV engine needs its own virtualenv — create it once with: python3 -m venv .venv",
          kind: "pipeline_error",
        }),
      });
    });
    await overlay.locator("button", { hasText: "Back to the draft" }).click();
    await expect(overlay.locator("button", { hasText: "Approve & render" })).toBeVisible();
    await overlay.locator("button", { hasText: "Approve & render" }).click();

    await expect(overlay.locator("h3").first()).toHaveText("Failed");
    // The instruction to create the venv survives intact, because the pipeline
    // writes its errors to be read by a person. Summarising it here would remove
    // the one line that says how to fix it.
    await expect(overlay.locator("pre.tailor-error")).toContainText("python3 -m venv .venv");
    await expect(overlay).toContainText("No files were written.");
    // Still open, still offering the draft back and a clean restart.
    await expect(overlay.locator("button", { hasText: "Back to the draft" })).toBeVisible();
    await expect(overlay.locator("button", { hasText: "Start over" })).toBeVisible();

    // Closing at this point is allowed (the failure is not a busy state) and the
    // board underneath is still usable — no orphaned overlay, no stuck modal.
    await overlay.locator("#panel-close").click();
    await expect(page.locator(".tailor-panel")).toHaveCount(0);
    await expect(page.locator("#panel:not(.tailor-panel)")).toBeVisible();
    await expect(page.locator("table tbody tr")).toHaveCount(10);
  });
});
