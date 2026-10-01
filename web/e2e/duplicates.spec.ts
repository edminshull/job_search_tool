import { expect, test } from "@playwright/test";
import { openBoard, resetBoard, rows } from "./fixtures";

/**
 * The same job advertised twice, as the board presents it.
 *
 * The failure this exists to prevent is a wasted application: an agency
 * re-advertises the role it is still filling, under a new advert id with the
 * title and office address nudged, so both dedup keys miss it and the copy lands
 * on the board looking like fresh work. Before this, the only way to find out was
 * to open the advert on LinkedIn and read "Applied".
 *
 * The fixture reproduces the real pair exactly (see make-fixture-db.mjs):
 *
 *   Experis — "AI Automation Tester X2" @ Farringdon, Central London   applied
 *   Experis — "AI Automation Tester"    @ Fleet Street, Central London applied (inherited)
 *
 * Two things are asserted, and they are different claims:
 *   1. the re-advert is MARKED, so a scan of the board shows it;
 *   2. the inherited status is LABELLED as inherited, because "applied" you
 *      recorded and "applied" the tool derived are not the same claim, and
 *      presenting a derived value as the user's own decision is exactly the kind
 *      of quiet half-truth this system refuses elsewhere.
 */
test.describe("Re-adverts", () => {
  test.beforeEach(() => resetBoard());

  const ORIGINAL = "https://fixtures.test/jobs/experis-ai-automation-tester-x2";
  const READVERT = "https://fixtures.test/jobs/experis-ai-automation-tester";

  test("P1 a re-advert is marked on the board, and the original is not", async ({ page }) => {
    await openBoard(page);

    const readvertRow = rows(page).filter({ hasText: "AI Automation Tester" })
      .filter({ hasNotText: "X2" });
    const originalRow = rows(page).filter({ hasText: "X2" });

    await expect(readvertRow).toHaveCount(1);
    await expect(originalRow).toHaveCount(1);

    // The chip is on the copy, and only on the copy — the original is the row
    // everything else points at.
    await expect(readvertRow.locator(".dup-chip")).toHaveText("re-advert");
    await expect(originalRow.locator(".dup-chip")).toHaveCount(0);

    // The chip explains itself on hover, naming the advert it duplicates.
    await expect(readvertRow.locator(".dup-chip")).toHaveAttribute("title", /X2|5877123884|duplicate_of|already stored/i);
  });

  test("P2 the panel says where the inherited status came from, and links to it", async ({ page }) => {
    await openBoard(page);

    await rows(page).filter({ hasText: "AI Automation Tester" }).filter({ hasNotText: "X2" }).click();
    const panel = page.locator("#panel:not(.tailor-panel)");
    await expect(panel).toBeVisible();

    // The re-advert notice, naming the other advert.
    await expect(panel.locator(".dup-note")).toContainText("re-advert");
    await expect(panel.locator(".dup-note")).toContainText("Experis");

    // The provenance of the status: this must NOT read as a decision the user made.
    const inheritNote = panel.locator(".inherit-note");
    await expect(inheritNote).toBeVisible();
    await expect(inheritNote).toContainText("inherited");
    await expect(inheritNote).toContainText("not set here");

    // ...and it offers a way through to the advert that carries the decision.
    await inheritNote.locator("button.linklike").click();
    await expect(panel.locator("h2")).toHaveText("AI Automation Tester X2");
  });

  test("P3 the original advert shows no inherited-status note", async ({ page }) => {
    await openBoard(page);

    await rows(page).filter({ hasText: "X2" }).click();
    const panel = page.locator("#panel:not(.tailor-panel)");
    await expect(panel).toBeVisible();
    await expect(panel.locator("h2")).toHaveText("AI Automation Tester X2");

    // Its status is the user's own, so neither note applies.
    await expect(panel.locator(".dup-chip")).toHaveCount(0);
    await expect(panel.locator(".inherit-note")).toHaveCount(0);
  });

  test("P4 saving a status yourself clears the inherited label, and never overwrites the other advert's own", async ({ page }) => {
    await openBoard(page);

    await rows(page).filter({ hasText: "AI Automation Tester" }).filter({ hasNotText: "X2" }).click();
    const panel = page.locator("#panel:not(.tailor-panel)");
    await expect(panel.locator(".inherit-note")).toBeVisible();

    // Change it: from here on the value is the user's own decision, and the
    // provenance claim has to go with it.
    await panel.locator("select").first().selectOption("interview");
    await panel.locator("button", { hasText: "Save" }).click();
    await expect(panel.locator(".save-status")).toContainText("Saved");

    const res = await page.request.get("/api/jobs");
    const jobs = (await res.json()) as
      { url: string; my_status: string | null; status_inherited_from: string | null }[];

    const readvert = jobs.find((j) => j.url === READVERT);
    expect(readvert?.my_status).toBe("interview");
    expect(readvert?.status_inherited_from).toBeNull();

    // The other advert keeps ITS OWN "applied" — which the fixture gave it as the
    // user's decision, not an inherited value. Fanning "interview" across would
    // silently discard a real one, and that is the worst thing this feature could
    // do. Propagation deliberately only fills rows that have no status of their
    // own (asserted directly in lib/db.test.ts).
    const original = jobs.find((j) => j.url === ORIGINAL);
    expect(original?.my_status).toBe("applied");
    expect(original?.status_inherited_from).toBeNull();
  });

  test("P5 the re-advert still lists under a status filter", async ({ page }) => {
    // Marking a duplicate must not quietly hide it: it stays a row on the board,
    // and the filters see it like any other. A heuristic that silently removes a
    // posting would be a worse failure than the duplicate it fixed.
    await openBoard(page);

    await page.locator('.toolbar label:has-text("My status") select').selectOption("applied");
    await expect(rows(page)).toHaveCount(3);
    await expect(rows(page).filter({ hasText: "AI Automation Tester" })).toHaveCount(2);
  });
});
