import { expect, test } from "@playwright/test";
import {
  BOARD,
  FILTER_EXPECTATIONS,
  ROW,
  SORTED_BY_POSTED_DESC,
  SORTED_BY_SCORE_DESC,
  badgeCounts,
  columnValues,
  openBoard,
  resetBoard,
  rows,
  toolbarSearch,
  toolbarSelect,
} from "./fixtures";

/**
 * The board itself: what it shows, what the toolbar narrows it to, how it sorts,
 * and where the date window's edges are.
 *
 * Every count asserted here comes from e2e/fixtures.ts, whose header lists the
 * cast and why each row exists.
 */
test.describe("Job board", () => {
  test.beforeEach(() => resetBoard());

  test("P1 loads the board and the toolbar filters narrow it correctly", async ({ page }) => {
    await openBoard(page);

    // The fixture's whole board, unfiltered. If this number is wrong, every
    // other assertion in the suite is built on sand.
    await expect(rows(page)).toHaveCount(BOARD.visible);
    await expect(page.locator(".count-badge")).toHaveText(`${BOARD.visible} / ${BOARD.total}`);
    // The header says which window is active, so a user cannot mistake a windowed
    // board for a complete one.
    // The label is lower-cased in the sentence ("Showing the last month by
    // default"), and marked up as a <strong> so the active window is scannable.
    await expect(page.locator("header .subtitle")).toContainText("Showing the last month by default");
    await expect(page.locator("header .subtitle strong")).toHaveText("last month");

    // --- Type -------------------------------------------------------------
    // CONTRACT is the exact direction and must give exactly the one contract row.
    await toolbarSelect(page, "Type").selectOption("contract");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.typeContract);
    await expect(rows(page).first()).toContainText(ROW.northwind);
    // The contract chip comes from employment_type, and it carries the IR35
    // status, which is the thing a contract reader looks for first.
    await expect(rows(page).first().locator(".contract-chip")).toContainText("Outside IR35");

    // PERMANENT is the loose direction, and that is deliberate: ATS boards rarely
    // declare an employment type, so filtering to "permanent" must keep the
    // unknown rows rather than hiding most of the board.
    await toolbarSelect(page, "Type").selectOption("permanent");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.typePermanent);
    await expect(rows(page).filter({ hasText: ROW.northwind })).toHaveCount(0);

    await toolbarSelect(page, "Type").selectOption("all");
    await expect(rows(page)).toHaveCount(BOARD.visible);

    // --- Language ---------------------------------------------------------
    await toolbarSelect(page, "Language").selectOption("priority");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.languagePriority);
    await expect(rows(page).first().locator(".lang-cell .chip")).toHaveText("Java/Ruby");

    // priority+ adds the JS/Node rows but not the silent ones.
    await toolbarSelect(page, "Language").selectOption("priority+");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.languagePriorityPlus);

    // "Any language named" excludes rows that name NONE. This is the assertion
    // that matters most in this block: `none` is not evidence of a mismatch, but
    // it is also not evidence of a match, so it must not be swept into either.
    await toolbarSelect(page, "Language").selectOption("known");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.languageKnown);
    await expect(rows(page).filter({ hasText: ROW.coastal })).toHaveCount(0);

    await toolbarSelect(page, "Language").selectOption("all");

    // --- AI status --------------------------------------------------------
    await toolbarSelect(page, "AI status").selectOption("apply");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.aiApply);
    await expect(rows(page).first().locator(".badge")).toHaveText("Apply");

    await toolbarSelect(page, "AI status").selectOption("consider");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.aiConsider);

    await toolbarSelect(page, "AI status").selectOption("skip");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.aiSkip);

    // "Not evaluated" is 4 rows stored, 2 of them hidden by the date window —
    // the only filter whose visible count differs from its stored count, and so
    // the one that would expose a filter applied AFTER the window instead of
    // before it.
    await toolbarSelect(page, "AI status").selectOption("not_evaluated");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.aiNotEvaluated);
    await expect(rows(page).first().locator(".badge")).toHaveText("Not evaluated");

    // --- My status --------------------------------------------------------
    await toolbarSelect(page, "AI status").selectOption("all");
    await toolbarSelect(page, "My status").selectOption("applied");
    await expect(rows(page)).toHaveCount(FILTER_EXPECTATIONS.myStatusApplied);
    await expect(rows(page).first()).toContainText(ROW.bluepeak);

    // "No status" is the complement, and must include every untouched row —
    // not only the ones with an empty string.
    await toolbarSelect(page, "My status").selectOption("none");
    await expect(rows(page)).toHaveCount(BOARD.visible - FILTER_EXPECTATIONS.myStatusApplied);

    // --- filters COMBINE ---------------------------------------------------
    // Individually-correct filters that fail to compose are a classic board bug,
    // so one combination is asserted: contract AND Java/Ruby AND Apply is the
    // single row the whole search is for.
    await toolbarSelect(page, "My status").selectOption("all");
    await toolbarSelect(page, "Type").selectOption("contract");
    await toolbarSelect(page, "Language").selectOption("priority");
    await toolbarSelect(page, "AI status").selectOption("apply");
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(ROW.northwind);

    // A combination that matches nothing says so rather than showing a blank
    // table, which would look like a load failure. The one contract row is an
    // "Apply", never a "Skip", so narrowing AI status to Skip empties the board.
    await toolbarSelect(page, "AI status").selectOption("skip");
    await expect(page.locator(".empty-state")).toBeVisible();
    await expect(page.locator(".empty-state")).toContainText("Nothing matches the current filter/search.");
    await expect(rows(page)).toHaveCount(0);
    await expect(page.locator(".count-badge")).toHaveText(`0 / ${BOARD.total}`);
  });

  test("P2 search narrows on company or title, and the sortable columns reorder", async ({ page }) => {
    await openBoard(page);

    // --- default sort: Score, descending ------------------------------------
    let companies = await columnValues(page, ".company-cell");
    expect(companies[0]).toBe(SORTED_BY_SCORE_DESC.first);
    // The unscored rows sink, and there are exactly two of them visible. Their
    // order relative to each other is not contractual, so only the tail count is
    // asserted — pinning which of the two is last would be asserting SQLite's
    // tie order, not the board's behaviour.
    let scoreCells = (await columnValues(page, ".score-cell")).map((s) => s.trim());
    expect(scoreCells.slice(0, BOARD.visible - 2).every((s) => s !== "—")).toBe(true);
    expect(scoreCells.slice(BOARD.visible - 2)).toEqual(["—", "—"]);
    // The badge marks the active column, so the user can see why the order is
    // what it is.
    await expect(page.locator('th.active-sort')).toHaveText("Score");

    // --- search matches the TITLE as well as the company -------------------
    await toolbarSearch(page).fill("Northwind");
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(ROW.northwind);

    // A TITLE-only match. "Engineer" appears in 9 stored titles and in no
    // company name, and 7 of those are visible (the 31-day and 200-day rows are
    // outside the window), so this proves the haystack includes the title and
    // that the window is applied on top of it.
    await toolbarSearch(page).fill("Engineer");
    await expect(rows(page)).toHaveCount(7);
    const titles = await columnValues(page, ".title-text");
    expect(titles.every((t) => t.includes("Engineer"))).toBe(true);

    // Search is case-insensitive, so a title pasted as copied still matches.
    await toolbarSearch(page).fill("engineer");
    await expect(rows(page)).toHaveCount(7);
    await toolbarSearch(page).fill("ENGINEER");
    await expect(rows(page)).toHaveCount(7);

    // A multi-word substring match, which is what a half-remembered title looks
    // like: 3 rows stored, 2 visible.
    await toolbarSearch(page).fill("QA Engineer");
    await expect(rows(page)).toHaveCount(2);

    // A single character still narrows rather than being ignored.
    await toolbarSearch(page).fill("SDET");
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(ROW.boundary);

    await toolbarSearch(page).fill("");

    // --- sorting: Score, toggled to ascending ------------------------------
    // The click only sets React state; reading the DOM immediately afterwards
    // races the re-render and reads the PREVIOUS order. So each sort asserts on
    // the expected first row first — a retrying expectation — and only then reads
    // the whole column.
    await page.locator('th:has-text("Score")').click();
    await expect(page.locator('th.active-sort')).toHaveText("Score");
    await expect(rows(page).first()).toContainText("Coastal Systems"); // score 60, the lowest
    const scores = await columnValues(page, ".score-cell");
    // Ascending, and the unknown ("—") scores still sink to the bottom. Unknowns
    // always sink whichever direction is active: an unscored job is not a low
    // score, and floating them to the top on a reverse sort is a real bug this
    // assertion exists to catch.
    const numeric = scores.filter((s) => s !== "—").map(Number);
    expect(numeric).toEqual([...numeric].sort((a, b) => a - b));
    expect(numeric).toHaveLength(8); // 10 visible rows, 2 of them unscored
    // The unknowns are all at the END, whichever direction is active. Asserted as
    // an exact tail rather than a "contains", so a dash floating to the top fails.
    expect(scores.slice(numeric.length)).toEqual(["—", "—"]);

    // --- sorting: Posted, newest first -------------------------------------
    // Clicking Posted switches column AND picks descending, because an ascending
    // posting-date sort would put the oldest advert at the top, which is never
    // what was meant.
    await page.locator('th:has-text("Posted")').click();
    await expect(page.locator('th.active-sort')).toHaveText("Posted");
    await expect(rows(page).first()).toContainText(SORTED_BY_POSTED_DESC.first);
    companies = await columnValues(page, ".company-cell");
    expect(companies[0]).toBe(SORTED_BY_POSTED_DESC.first);
    // The highest-scoring row has a two-year-old date, so it must no longer be
    // first. This is the assertion that proves the two sorts are genuinely
    // different orders rather than both defaulting to the same thing.
    expect(companies[0]).not.toBe(SORTED_BY_SCORE_DESC.first);
    expect(companies).toContain(SORTED_BY_SCORE_DESC.first);

    // Toggling the same column reverses it, and must land on the OLDEST advert —
    // Trading House's two-year-old date. Ascending is the direction nobody wants
    // as a default and the one that is easy to get wrong.
    await page.locator('th:has-text("Posted")').click();
    await expect(rows(page).first()).toContainText(SORTED_BY_SCORE_DESC.first);
    companies = await columnValues(page, ".company-cell");
    expect(companies[0]).toBe(SORTED_BY_SCORE_DESC.first);
    expect(companies[0]).not.toBe(SORTED_BY_POSTED_DESC.first);
  });

  test("N1 a no-match search shows the empty state, and the date window says when IT is the cause", async ({ page }) => {
    await openBoard(page);

    // --- nothing matches the search ----------------------------------------
    await toolbarSearch(page).fill("zzzz-no-such-role");
    await expect(rows(page)).toHaveCount(0);
    await expect(page.locator(".empty-state")).toContainText("Nothing matches the current filter/search.");
    await expect(page.locator(".count-badge")).toHaveText(`0 / ${BOARD.total}`);
    // Nothing is hidden by the WINDOW here, so the board must not offer the
    // "show all dates" escape — offering it would imply a fix that does nothing.
    await expect(page.locator(".empty-state")).not.toContainText("hidden by the date window");
    await expect(page.locator(".window-note")).toHaveCount(0);

    // --- every match exists but the WINDOW hides it ------------------------
    // "Applied" is the 10-day-old Bluepeak row, which is stale enough to fall
    // outside a 7-day window and has no last_listed_at to rescue it. So the board
    // is empty even though the posting is there — the one case where an empty
    // board is the window's fault, and the case the escape hatch exists for.
    await toolbarSearch(page).fill("");
    await toolbarSelect(page, "My status").selectOption("applied");
    await toolbarSelect(page, "Posted within").selectOption("7");
    await expect(rows(page)).toHaveCount(0);
    const empty = page.locator(".empty-state");
    await expect(empty).toContainText("hidden by the date window");
    await expect(empty).toContainText("1 posting hidden");

    // The offered fix must actually work: one click and the row is back.
    await empty.locator("button.link-btn").click();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first()).toContainText(ROW.bluepeak);
    // And the window select reflects the change rather than silently disagreeing
    // with the table.
    await expect(toolbarSelect(page, "Posted within")).toHaveValue("all");
  });

  test("B1 the date window's closed edge: 30 days shows, 31 hides, undated always shows", async ({ page }) => {
    await openBoard(page);

    // Default window: 30 days. The window is a DEFAULT, not a limit, so when it
    // is hiding anything the board must say so and offer the way out. Silently
    // dropping postings is indistinguishable from having no postings.
    await expect(page.locator(".window-note")).toContainText("Hiding");
    await expect(page.locator(".window-note")).toContainText(`${BOARD.hiddenByDate} postings older than`);
    await expect(page.locator(".window-note")).toContainText("last month");

    // Boundary Analytics is posted EXACTLY 30 days ago and must be VISIBLE: the
    // comparison is `days <= window`, so an off-by-one here silently hides a real
    // posting and looks like nothing at all.
    await expect(rows(page).filter({ hasText: ROW.boundary })).toHaveCount(1);

    // Just Past Ltd is 31 days — one day past the edge — and must be hidden.
    await expect(rows(page).filter({ hasText: ROW.justPast })).toHaveCount(0);
    // As is the 200-day-old row.
    await expect(rows(page).filter({ hasText: ROW.antique })).toHaveCount(0);

    // An UNDATED posting is always kept: we cannot prove it is stale, and hiding
    // a job because its advert omitted a date is worse than showing it marked.
    await expect(rows(page).filter({ hasText: ROW.legacy })).toHaveCount(1);
    await expect(
      rows(page).filter({ hasText: ROW.legacy }).locator(".date-cell")
    ).toContainText("no date");

    // Tightening the window to 7 days hides the 30-day row and the 8/10/20-day
    // rows, and the count in the note must track it.
    await toolbarSelect(page, "Posted within").selectOption("7");
    await expect(rows(page).filter({ hasText: ROW.boundary })).toHaveCount(0);
    await expect(rows(page)).toHaveCount(6);
    await expect(page.locator(".window-note")).toContainText("6 postings older than");

    // "Show all dates" is the one-click way out, and must reveal both hidden rows
    // plus everything the tighter window hid.
    await page.locator(".window-note button.link-btn").click();
    await expect(rows(page)).toHaveCount(BOARD.total);
    await expect(rows(page).filter({ hasText: ROW.justPast })).toHaveCount(1);
    await expect(rows(page).filter({ hasText: ROW.antique })).toHaveCount(1);
    // Nothing is hidden, so the note and the announcement both disappear.
    await expect(page.locator(".window-note")).toHaveCount(0);
    await expect(page.locator("header .subtitle")).not.toContainText("Showing the");

    // A wider window is sticky across a Refresh — the toolbar is user state, not
    // something a data reload should reset.
    await page.locator(".toolbar button", { hasText: "Refresh" }).click();
    await expect(rows(page)).toHaveCount(BOARD.total);
    await expect(toolbarSelect(page, "Posted within")).toHaveValue("all");
  });

  test("B2 a stale posting that is still listed stays visible, and says so", async ({ page }) => {
    await openBoard(page);

    // Trading House Ltd claims a 2023 posted date and is STILL ADVERTISED — the
    // real case this feature exists for (a live London QA role carrying a
    // two-year-old date while its identical copies in other countries were
    // re-published). A 30-day window on posted_at alone would hide a job that is
    // open, and the tailored CV would then point at a posting the board refused
    // to show.
    const listed = rows(page).filter({ hasText: ROW.tradingHouse });
    await expect(listed).toHaveCount(1);
    await expect(listed.locator(".date-cell")).toContainText("still listed");
    // The date cell must ALSO admit the advertised date is stale, rather than
    // showing "still listed" alone and implying the advert is fresh.
    await expect(listed.locator(".date-cell")).toContainText("y ago");

    // The tooltip explains BOTH dates, since "2y ago" and "still listed" is an
    // otherwise confusing pairing to meet in a table.
    const title = await listed.locator(".date-cell").getAttribute("title");
    expect(title).toContain("Last seen on the board");

    // The stale date is dimmed, so it is obvious at a glance even with the window
    // widened.
    await expect(listed.locator(".date-cell")).toHaveClass(/age-stale/);

    // The OTHER direction of the boundary: a row whose posted_at is old AND whose
    // last_listed_at is also outside the window gets NO rescue. Trading House was
    // listed today so it survives even a 7-day window, while the 30-day and
    // 31-day rows that were never re-listed do not. If the chip were keyed off
    // posted_at, or if last_listed_at were ignored, this pairing would be
    // impossible to produce.
    await toolbarSelect(page, "Posted within").selectOption("7");
    await expect(rows(page).filter({ hasText: ROW.tradingHouse })).toHaveCount(1);
    await expect(rows(page).filter({ hasText: ROW.boundary })).toHaveCount(0);

    // The chip is a statement about the POSTING, not about the window: the advert
    // is old and the role is still open, which stays true however the board is
    // filtered. So it must still be there under "All dates" — unlike the
    // ".window-note" announcement, which is about the window and disappears.
    await toolbarSelect(page, "Posted within").selectOption("all");
    await expect(rows(page).filter({ hasText: ROW.tradingHouse }).locator(".listed-chip")).toHaveCount(1);
    await expect(page.locator(".window-note")).toHaveCount(0);
    // ...and the stale dimming stays too: the posted date is still two years old.
    await expect(
      rows(page).filter({ hasText: ROW.tradingHouse }).locator(".date-cell")
    ).toHaveClass(/age-stale/);

    // The two never-re-listed old rows carry NO chip even under "All dates",
    // which is what stops "still listed" from being decoration on every stale row.
    await expect(rows(page).filter({ hasText: ROW.justPast }).locator(".listed-chip")).toHaveCount(0);
    await expect(rows(page).filter({ hasText: ROW.antique }).locator(".listed-chip")).toHaveCount(0);

    const [shown, total] = await badgeCounts(page);
    expect(shown).toBe(BOARD.total);
    expect(total).toBe(BOARD.total);
  });
});
