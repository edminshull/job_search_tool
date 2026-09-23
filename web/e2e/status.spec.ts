import { expect, test } from "@playwright/test";
import { ROW, URLS, openBoard, resetBoard, rows } from "./fixtures";

/**
 * "My status" — the user's own tracking, which is separate from the AI's
 * recommendation and is the only thing on the board a human writes.
 *
 * It matters more than a UI nicety suggests: this column is the record of what
 * actually happened to an application, and it is written into the same SQLite
 * file the pipeline reads. So the tests here are about the WRITE being real and
 * the failure being visible, not about the select rendering.
 *
 * The AI's vocabulary (apply/consider/skip) and the user's
 * (applied/interview/rejected/skipped/silence) are deliberately different — see
 * the comment on the user_status table — so the two can never be confused for one
 * another in the UI. The invalid-value test below pins the API side of that.
 */
test.describe("My status", () => {
  test.beforeEach(() => resetBoard());

  test("P3 the inline row status persists, does not open the panel, and survives a refresh", async ({ page }) => {
    await openBoard(page);

    // Coastal Systems starts with no status at all. A row that already had one
    // would not prove the select writes anything.
    const row = rows(page).filter({ hasText: ROW.coastal });
    await expect(row).toHaveCount(1);
    const select = row.locator("select.my-status-select");
    await expect(select).toHaveValue("");

    // The select sits inside the row, and the row itself opens the detail panel
    // on click. Changing the status must NOT also open the panel — the handler
    // calls stopPropagation precisely for that, and losing it would make the
    // quick status a two-step interaction on every use.
    await select.selectOption("interview");
    await expect(page.locator("#panel")).toHaveCount(0);

    // The write must actually land, read back through the API the board reads.
    const res = await page.request.get("/api/jobs");
    const jobs = (await res.json()) as
      { url: string; company: string; my_status: string | null; notes: string | null }[];
    expect(jobs.find((j) => j.company === ROW.coastal)?.my_status).toBe("interview");
    // And it landed on ONE row. Bluepeak's existing status is the control: a
    // write that keyed on the wrong column would change it.
    expect(jobs.find((j) => j.company === ROW.bluepeak)?.my_status).toBe("applied");

    // The select shows the new value without a reload — the page updates its own
    // state rather than waiting for the next fetch.
    await expect(select).toHaveValue("interview");

    // It survives a Refresh, which re-reads from SQLite. This is the assertion
    // that distinguishes "persisted" from "optimistic in the browser": a
    // response that was never written would be lost here.
    await page.locator(".toolbar button", { hasText: "Refresh" }).click();
    await expect(rows(page).filter({ hasText: ROW.coastal }).locator("select.my-status-select"))
      .toHaveValue("interview");

    // And the toolbar filter now finds it under the new status, which is the
    // point of setting it.
    await page.locator('.toolbar label:has-text("My status") select').selectOption("interview");
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(ROW.coastal);

    // Clearing it is a real operation too, not a no-op: back to "no status".
    await rows(page).first().locator("select.my-status-select").selectOption("");
    await page.locator('.toolbar label:has-text("My status") select').selectOption("none");
    await expect(rows(page).filter({ hasText: ROW.coastal })).toHaveCount(1);
    const cleared = (await (await page.request.get("/api/jobs")).json()) as
      { company: string; my_status: string | null }[];
    expect(cleared.find((j) => j.company === ROW.coastal)?.my_status).toBe(null);
  });

  test("N2 a status the database does not know is refused, and the UI does not claim success", async ({ page }) => {
    await openBoard(page);

    // Bluepeak is the one row that starts with a status, so "did it change?"
    // has an unambiguous answer.
    const row = rows(page).filter({ hasText: ROW.bluepeak });
    await expect(row.locator("select.my-status-select")).toHaveValue("applied");

    // --- the API refuses an unknown status ---------------------------------
    // The <select> constrains what a person can pick, but the route is a public
    // POST and is the real boundary. An unknown value must be a 400, not a
    // silently-stored typo — a bad status would show up as a row that disappears
    // from every filter.
    for (const bad of ["offered", "APPLIED", "pending", "applied "]) {
      const res = await page.request.post("/api/status", {
        data: { url: URLS.bluepeak, my_status: bad, notes: null },
      });
      expect(res.status(), `my_status=${JSON.stringify(bad)} must be rejected`).toBe(400);
      const body = (await res.json()) as { error: string };
      // The message names the offending value AND the accepted set, so the fix
      // does not require reading the source.
      expect(body.error).toContain(bad);
      expect(body.error).toContain("applied, interview, rejected, skipped, silence");
    }

    // Nothing was written by any of those attempts.
    const afterBad = (await (await page.request.get("/api/jobs")).json()) as
      { company: string; my_status: string | null }[];
    expect(afterBad.find((j) => j.company === ROW.bluepeak)?.my_status).toBe("applied");

    // --- a missing url, and an unknown one ---------------------------------
    // Both are refused for different reasons, and both would otherwise create a
    // user_status row keyed on nothing or on a job that does not exist.
    const noUrl = await page.request.post("/api/status", { data: { my_status: "applied" } });
    expect(noUrl.status()).toBe(400);
    expect(((await noUrl.json()) as { error: string }).error).toContain("url");

    const unknownJob = await page.request.post("/api/status", {
      data: { url: "https://fixtures.test/jobs/does-not-exist", my_status: "applied" },
    });
    expect(unknownJob.status()).toBe(404);
    expect(((await unknownJob.json()) as { error: string }).error).toContain("No known job");

    // --- the UI surfaces a failed save rather than pretending --------------
    // A 500 is forced on the route, and the row's own status select is used, so
    // the assertion covers the browser-side path rather than the API in isolation.
    // The failure must be reported: `saveStatus` alerts when a quick change
    // fails, because a select that silently reverts looks like a mis-click.
    await page.route("**/api/status", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({ error: "Failed to write to SQLite: database is locked" }),
        });
        return;
      }
      await route.continue();
    });

    let alerted = "";
    page.on("dialog", async (dialog) => {
      alerted = dialog.message();
      await dialog.dismiss();
    });

    await row.locator("select.my-status-select").selectOption("rejected");
    await expect.poll(() => alerted).toContain("Failed to save status");
    // The message carries the server's own reason, so "database is locked" is
    // visible rather than being flattened into "something went wrong".
    expect(alerted).toContain("database is locked");

    // And the refusal is NOT persisted: after a reload the row is back to the
    // stored value, because nothing was written.
    await page.unroute("**/api/status");
    await page.reload();
    await expect(
      rows(page).filter({ hasText: ROW.bluepeak }).locator("select.my-status-select")
    ).toHaveValue("applied");
  });
});
