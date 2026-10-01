import { expect, Page, Route, test } from "@playwright/test";
import {
  ROW,
  SCAN,
  URLS,
  detailPanel,
  openBoard,
  openPanelFor,
  openPanelForTitle,
  resetBoard,
  scanOverlay,
  scanPanel,
} from "./fixtures";

/**
 * The ATS-scan overlay: "would the CV I already have pass this posting, and what
 * real experience is it not showing?"
 *
 * THE SCAN ROUTE IS STUBBED, AND THAT IS THE RIGHT CALL FOR A UI TEST
 * -----------------------------------------------------------------
 * POST /api/cv/scan shells out to `python -m app.cv_tailor scan`, which reads the
 * master CV from Google Docs, parses `cv/master.yaml` and writes a `cv_scans`
 * row. Driving that from a UI test would make the suite depend on a network, a
 * Doc's share settings and the python virtualenv, and it would fail for reasons
 * that have nothing to do with the interface. So the route is intercepted with
 * canned, schema-shaped payloads and the assertions are about the INTERFACE's
 * contract:
 *
 *   * the right phase at the right time (idle -> scanning -> result/error), and
 *     that opening the overlay RUNS NOTHING — the stored record is read from
 *     /api/jobs/scan, which is NOT stubbed;
 *   * "cannot tell" is never dressed as a verdict. This is the single most
 *     important thing on this screen: a green tick on an unread advert would send
 *     someone to an interview believing they passed a filter that never ran;
 *   * a suggestion is quoted VERBATIM from the master, and the add/reword
 *     distinction is visible in the copy, not buried in a class name;
 *   * a failure leaves the overlay open, quoting the pipeline's own message, and
 *     claims nothing was written.
 *
 * What is deliberately NOT stubbed is /api/jobs and /api/jobs/scan, so the
 * panel's own state — a job with a stored scan, a job whose stored description is
 * a snippet, a job with no scan at all — is read from a real database written by
 * e2e/scripts/make-fixture-db.mjs.
 */

/** Intercept the scan route with an optional delay, so the in-progress phase is
 *  observable. Only POST is answered: a GET to this path is not a thing the app
 *  does, and silently fulfilling one would hide that. */
async function stub(
  page: Page,
  pattern: string,
  body: unknown,
  { status = 200, delayMs = 0 } = {}
): Promise<void> {
  await page.route(pattern, async (route: Route) => {
    if (route.request().method() !== "POST") return route.continue();
    if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
    await route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });
  });
}

/** A posting term, in the shape app/ats_scan._term_row emits. */
const term = (name: string, weight = 3, section = "requirements", skill = true) =>
  ({ term: name, weight, section, skill });

/** The `ats` block, with the fields a scan always carries. */
function ats(overrides: Record<string, unknown> = {}) {
  return {
    verdict: "pass",
    reason: "Every requirement-level hard skill is covered and priority-term coverage is 91%.",
    reliable: true,
    unreliable_reason: null,
    sections_found: ["duties", "requirements"],
    terms_found: 38,
    posting_chars: 3_140,
    priority_coverage: 91,
    priority_evidenced: 29,
    priority_total: 32,
    all_term_coverage: 74,
    thresholds: { pass_coverage: 85 },
    hard_gaps: [],
    priority_gaps: [],
    evidenced_priorities: [],
    ...overrides,
  };
}

/** A whole scan payload, Fresh Start's by default. */
function scanBody(overrides: Record<string, unknown> = {}) {
  return {
    url: URLS.freshStart,
    company: "Fresh Start Ltd",
    title: "Automation Test Engineer",
    cv_source: {
      kind: "service_account",
      cached_from: null,
      doc_id: "1AbC-fixture-master-cv",
      fetched_at: "2026-10-01T06:00:00.000Z",
      stale: false,
      age_hours: 4,
      reason: null,
      chars: 6_180,
    },
    ats: ats(),
    suggestions: [],
    unclosable: [],
    roles_missing_from_cv: [],
    projection: {
      coverage: 91,
      verdict: null,
      closes_terms: [],
      still_open: [],
      note: "No master experience would close any of this posting's gaps.",
    },
    ...overrides,
  };
}

/** A master achievement quoted verbatim, in the shape _suggestions emits. */
function suggestion(kind: "add_achievement" | "reword_achievement", text: string,
                    closes: string[], overrides: Record<string, unknown> = {}) {
  return {
    kind,
    on_cv_overlap: kind === "add_achievement" ? 0.1 : 0.82,
    role_id: kind === "add_achievement" ? "fintech" : "legacyco",
    role_title: kind === "add_achievement" ? "Senior QA Engineer" : "Test Consultant",
    company: kind === "add_achievement" ? "Example FinTech Ltd" : "Example Consultancy Ltd",
    achievement_id: kind === "add_achievement" ? "fintech-7" : "legacyco-2",
    text,
    tags: ["api", "automation"],
    closes_terms: closes.map((c) => term(c)),
    closes_count: closes.length,
    weight_closed: closes.length * 3,
    ...overrides,
  };
}

const ADD_TEXT =
  "Rebuilt the regression suite as a Playwright project, cutting the nightly run from 90 minutes to 12.";
const REWORD_TEXT =
  "Owned the Java API test framework that four squads wrote their service tests against.";

/** How many times the scan route was POSTed to, and with what. */
function watchScanPosts(page: Page) {
  const posts: Record<string, unknown>[] = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && new URL(req.url()).pathname === "/api/cv/scan") {
      posts.push(req.postDataJSON() as Record<string, unknown>);
    }
  });
  return posts;
}

/** Every /api/jobs/scan read the page made, by the url it asked for. */
function watchStoredReads(page: Page) {
  const reads: string[] = [];
  page.on("request", (req) => {
    const url = new URL(req.url());
    if (req.method() === "GET" && url.pathname === "/api/jobs/scan") {
      reads.push(url.searchParams.get("url") ?? "");
    }
  });
  return reads;
}

/** Open Fresh Start (no scan stored) and start the overlay. */
async function openScanner(page: Page) {
  await openBoard(page);
  await openPanelFor(page, ROW.freshStart);
  await detailPanel(page).locator("button", { hasText: "Scan CV for this job" }).click();
  const overlay = scanPanel(page);
  await expect(overlay).toBeVisible();
  return overlay;
}

test.describe("CV scan overlay", () => {
  test.beforeEach(() => resetBoard());

  test("S1 the idle screen explains what a scan is, and running it issues exactly ONE POST", async ({ page }) => {
    const posts = watchScanPosts(page);
    // A real wait, so the in-progress phase is observable. In production a scan
    // is keyword arithmetic and finishes in well under a second; the only slow
    // part is a live fetch of the Doc, which is what the copy says.
    await stub(page, "**/api/cv/scan", scanBody(), { delayMs: 700 });

    await openBoard(page);
    await openPanelFor(page, ROW.freshStart);

    // Nothing has been scanned, so the detail panel offers a scan rather than
    // the scan — and says what one is before the click is spent on it.
    const button = detailPanel(page).locator("button", { hasText: "Scan CV for this job" });
    await expect(button).toBeVisible();
    await expect(detailPanel(page)).toContainText(
      "Scans the master CV against this posting with no model call"
    );

    await button.click();
    const overlay = scanPanel(page);
    await expect(overlay).toBeVisible();

    // --- idle phase --------------------------------------------------------
    // Opening the overlay only READS the stored scan. Nothing is run here: a
    // page load that fired a Google Docs fetch per row, or a button that scanned
    // before the user had read what a scan does, would both be wrong.
    expect(posts).toHaveLength(0);
    await expect(overlay.locator("h3")).toHaveText("Does my CV pass this posting?");
    await expect(overlay).toContainText("No model call, no cost, and nothing is written to the CV");
    // The provenance line, present on every phase: the scan reads the master CV,
    // so which copy was read is part of what a result would mean.
    await expect(overlay.locator(".scan-source")).toContainText("Master CV:");
    // No verdict of any kind before a scan has run.
    await expect(overlay.locator(".scan-verdict")).toHaveCount(0);

    // --- scanning phase ----------------------------------------------------
    await overlay.locator("button", { hasText: "Run the scan" }).click();
    await expect(overlay.locator("h3")).toHaveText("Scanning…");
    // The wait is explained rather than left as an anonymous spinner.
    await expect(overlay.locator(".tailor-progress")).toContainText("reading the master CV and this posting");
    await expect(overlay.locator(".tailor-progress")).toContainText("keyword arithmetic, not a model call");
    // And the overlay cannot be dismissed mid-run, so a stray click cannot
    // abandon a scan whose result is about to arrive.
    await expect(overlay.locator("#panel-close")).toBeDisabled();

    // --- result, and the POST that produced it ------------------------------
    await expect(overlay.locator(".scan-verdict-label")).toHaveText("Would pass");
    // THE assertion of this test: one click, one request, with the body the
    // route expects. `refresh_master: false` is the important half — the cache
    // exists so a scan does not depend on a network call it does not need.
    expect(posts).toHaveLength(1);
    expect(posts[0]).toEqual({ url: URLS.freshStart, refresh_master: false });

    // The results phase offers the one action that needs the Doc re-read, and it
    // asks for it BY NAME: leaving the flag off would silently re-use the cached
    // copy and show the user their pre-edit CV.
    await expect(overlay.locator("#panel-close")).toBeEnabled();
    await overlay.locator("button", { hasText: "Re-read the Doc and scan again" }).click();
    await expect(overlay.locator(".scan-verdict-label")).toHaveText("Would pass");
    expect(posts).toHaveLength(2);
    expect(posts[1]).toEqual({ url: URLS.freshStart, refresh_master: true });
  });

  test("S2 a pass shows the verdict, the coverage figure and verbatim suggestions with a copy button", async ({ page }) => {
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await stub(page, "**/api/cv/scan", scanBody({
      ats: ats({
        priority_gaps: [term("Kubernetes", 2, "duties", false), term("mentoring", 2, "duties", false)],
        evidenced_priorities: [term("Java"), term("JUnit"), term("Docker")],
      }),
      suggestions: [
        suggestion("add_achievement", ADD_TEXT, ["Playwright", "regression"]),
        suggestion("reword_achievement", REWORD_TEXT, ["API testing"]),
      ],
      projection: {
        coverage: 96, verdict: "pass", closes_terms: ["Playwright", "API testing"],
        still_open: [term("Kubernetes", 2, "duties", false)],
        note: "If every suggestion above were added to the CV.",
      },
    }));

    const overlay = await openScanner(page);
    await overlay.locator("button", { hasText: "Run the scan" }).click();

    // --- the verdict band --------------------------------------------------
    const band = overlay.locator(".scan-verdict");
    await expect(band).toHaveClass(/\bv-pass\b/);
    await expect(band.locator(".scan-verdict-label")).toHaveText("Would pass");
    // The coverage number, given as both a percentage and its fraction. A
    // percentage with no denominator is how a 3-of-3 posting came to read as a
    // confident 100% on an advert that had not been read.
    await expect(band.locator(".scan-verdict-cov")).toContainText("91% of requirement-level terms");
    await expect(band.locator(".scan-verdict-cov")).toContainText("(29/32)");
    await expect(band.locator(".scan-verdict-reason").first()).toContainText("priority-term coverage is 91%");

    // The comparable figure, restated with its arithmetic underneath so the
    // number can be checked rather than believed.
    const coverage = overlay.locator(".scan-coverage");
    await expect(coverage.locator(".scan-num")).toHaveText("91%");
    await expect(coverage).toContainText("29 of 32");
    await expect(overlay).toContainText("This posting yielded 38 terms, of which 32 are requirement-level");

    // --- the suggestion cards ---------------------------------------------
    const cards = overlay.locator(".scan-suggestion");
    await expect(cards).toHaveCount(2);
    const add = cards.filter({ hasText: ADD_TEXT });
    // VERBATIM. The whole safety argument for this feature is that a suggestion
    // is a line that already exists in cv/master.yaml, so anything the panel
    // wrapped, truncated or re-punctuated would be the tool writing the CV.
    await expect(add.locator(".scan-suggestion-text")).toHaveText(ADD_TEXT);
    await expect(add.locator(".scan-suggestion-head")).toContainText("Example FinTech Ltd");
    await expect(add.locator(".scan-suggestion-head")).toContainText("fintech-7");
    // The terms it would close are named, so the claim is traceable to the ad.
    await expect(add.locator(".scan-suggestion-why")).toContainText("Names: Playwright, regression");

    // --- the copy button ---------------------------------------------------
    const copy = add.locator("button.scan-copy");
    await expect(copy).toHaveText("Copy");
    await copy.click();
    await expect(copy).toHaveText("Copied");
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(ADD_TEXT);
    // It resets, so the button is usable a second time rather than reporting a
    // stale state forever.
    await expect(copy).toHaveText("Copy", { timeout: 5_000 });

    // --- the projection ----------------------------------------------------
    // What the suggestions are FOR: the block says whether acting on them
    // changes the verdict, or is busywork.
    const projection = overlay.locator(".scan-projection");
    await expect(projection).toContainText("coverage goes 91% → 96%");
    await expect(projection).toContainText("the verdict does not change");
    await expect(projection).toContainText("If every suggestion above were added to the CV.");
  });

  test("S3 an addition and a rewording are visibly different advice, not just different classes", async ({ page }) => {
    await stub(page, "**/api/cv/scan", scanBody({
      suggestions: [
        suggestion("add_achievement", ADD_TEXT, ["Playwright"]),
        suggestion("reword_achievement", REWORD_TEXT, ["API testing"]),
      ],
      projection: {
        coverage: 97, verdict: "pass", closes_terms: [], still_open: [],
        note: "If every suggestion above were added to the CV.",
      },
    }));

    const overlay = await openScanner(page);
    await overlay.locator("button", { hasText: "Run the scan" }).click();
    await expect(overlay.locator(".scan-suggestion")).toHaveCount(2);

    // Two headings, each stating what the reader is being asked to DO. Telling
    // someone to add a bullet they already have is a small lie about their own
    // CV, and it is the failure the add/reword split exists to prevent.
    await expect(overlay.locator("h3", { hasText: "Add to the CV (1)" })).toBeVisible();
    await expect(overlay.locator("h3", { hasText: "Already on the CV — name it in their words (1)" }))
      .toBeVisible();

    // The addition: not on the CV at all, so adding it is the fix.
    const add = overlay.locator(".scan-suggestion.is-add");
    await expect(add).toHaveCount(1);
    await expect(add.locator(".scan-suggestion-text")).toHaveText(ADD_TEXT);
    await expect(add.locator(".scan-suggestion-why"))
      .toContainText("Not on the CV at all — this is the one to add.");

    // The rewording: the fact IS on the CV, and the posting uses other words for
    // it — a different instruction, a different tone, and a different card.
    const reword = overlay.locator(".scan-suggestion.is-reword");
    await expect(reword).toHaveCount(1);
    await expect(reword.locator(".scan-suggestion-text")).toHaveText(REWORD_TEXT);
    await expect(reword.locator(".scan-suggestion-why"))
      .toContainText("The fact is already on the CV, but not in this posting's words.");
    await expect(add.locator(".scan-suggestion-why"))
      .not.toContainText("The fact is already on the CV");
  });

  test("S4 a fail names the hard gaps, the ones no edit can close, and what closing the rest would buy", async ({ page }) => {
    await stub(page, "**/api/cv/scan", scanBody({
      ats: ats({
        verdict: "fail",
        reason: "3 requirement-level hard skills the CV does not mention: Kubernetes, Terraform, Go.",
        priority_coverage: 50,
        priority_evidenced: 12,
        priority_total: 24,
        all_term_coverage: 38,
        hard_gaps: [term("Kubernetes"), term("Terraform"), term("Go", 2.5)],
        priority_gaps: [term("Kubernetes"), term("Terraform"), term("Go", 2.5),
                        term("on-call", 2, "duties", false)],
        evidenced_priorities: [term("Java"), term("CI/CD", 2, "duties", false)],
      }),
      // Go IS closable (the master holds it), the other two are not — which is
      // the real shape of a fail, and why "hard gaps" and "not closable" are two
      // lists and not one.
      suggestions: [suggestion("add_achievement", ADD_TEXT, ["Go"])],
      unclosable: [term("Kubernetes"), term("Terraform")],
      roles_missing_from_cv: [{
        id: "legacyco", company: "Example Consultancy Ltd", title: "Test Consultant",
        start: "2014-03", end: "2016-08", achievements: 4,
      }],
      projection: {
        coverage: 62.5, verdict: "fail", closes_terms: ["Go"],
        still_open: [term("Kubernetes"), term("Terraform")],
        note: "If every suggestion above were added to the CV.",
      },
    }));

    const overlay = await openScanner(page);
    await overlay.locator("button", { hasText: "Run the scan" }).click();

    // --- the verdict band --------------------------------------------------
    const band = overlay.locator(".scan-verdict");
    await expect(band).toHaveClass(/\bv-fail\b/);
    await expect(band.locator(".scan-verdict-label")).toHaveText("Would be filtered out");
    await expect(band.locator(".scan-verdict-reason").first())
      .toContainText("3 requirement-level hard skills the CV does not mention: Kubernetes, Terraform, Go.");
    // The advice must say what a fail MEANS, not merely that it happened: a
    // keyword filter is a fact about the reading of the CV, not a judgement on it.
    await expect(band.locator(".scan-verdict-reason").last())
      .toContainText("A keyword filter looking for these terms would not find them.");

    // --- the hard-skill gap chips -----------------------------------------
    await expect(overlay.locator("h3", { hasText: "Requirement-level hard skills the CV does not mention" }))
      .toBeVisible();
    const hardChips = overlay.locator(".scan-term.is-hard");
    expect(await hardChips.allInnerTexts()).toEqual(["Kubernetes", "Terraform", "Go"]);
    // Each chip carries its evidence: which part of the advert demanded it, and
    // whether it is a recognised skill or a phrase the advert happened to use.
    await expect(hardChips.first()).toHaveAttribute("title", /requirements · recognised skill · weight 3/);

    // --- the gaps no edit can close ---------------------------------------
    await expect(overlay.locator("h3", { hasText: "Not closable — you do not have these" })).toBeVisible();
    const realGaps = overlay.locator(".scan-term.is-real-gap");
    expect(await realGaps.allInnerTexts()).toEqual(["Kubernetes", "Terraform"]);
    await expect(overlay).toContainText("no edit to the CV would honestly fix it");

    // --- a whole role missing ---------------------------------------------
    const role = overlay.locator(".scan-role");
    await expect(role).toContainText("Example Consultancy Ltd");
    await expect(role).toContainText("Test Consultant");
    await expect(role).toContainText("4 achievements in the master");

    // --- the projection ----------------------------------------------------
    // The honest half of the advice: adding everything still leaves the
    // requirement the candidate does not hold, so the verdict does NOT change —
    // and the panel says so rather than letting a rising percentage imply a win.
    const projection = overlay.locator(".scan-projection");
    await expect(projection).toContainText("coverage goes 50% → 62.5%");
    await expect(projection).toContainText("the verdict does not change");

    // --- the collapsed lists ----------------------------------------------
    await expect(overlay.locator("summary", { hasText: "All requirement-level gaps (4)" })).toBeVisible();
    await expect(overlay.locator("summary", { hasText: "What the CV already evidences (2)" })).toBeVisible();
  });

  test("S5 an unreadable posting says 'Cannot tell', never a verdict — and offers the fuller copy", async ({ page }) => {
    const reads = watchStoredReads(page);
    const posts = watchScanPosts(page);
    await stub(page, "**/api/cv/scan", scanBody({
      url: URLS.experisReadvert,
      company: "Experis",
      title: "AI Automation Tester",
      ats: ats({
        verdict: "unknown",
        reliable: false,
        unreliable_reason: "snippet",
        sections_found: [],
        terms_found: 3,
        // 59 characters: the fixture row's stored text really is that short —
        // "AI Automation Tester\n\nJava, Cucumber, performance testing.\n" — which
        // is EXACTLY the state this branch exists for.
        posting_chars: 59,
        priority_coverage: 100,
        priority_evidenced: 0,
        priority_total: 0,
        all_term_coverage: 0,
        reason: "The stored description is a search-result snippet rather than the advert, so "
              + "there is not enough of the posting to judge anything against. This is NOT a "
              + "pass and NOT a fail. Scanning the employer's own copy of this job, if the "
              + "board has one, is the fix.",
      }),
      // The other advert of the SAME job, with a usable description — the fix
      // the reason above points at. The pair is real: app/duplicates.py links
      // these two fixture rows.
      better_source: {
        url: URLS.experisOriginal,
        company: "Experis",
        title: "AI Automation Tester X2",
        source: "adzuna",
        chars: 121,
      },
    }));

    // The re-advert — addressed by exact title, because the fixture stores the
    // same job twice under one employer.
    await openBoard(page);
    await openPanelForTitle(page, "AI Automation Tester");
    await detailPanel(page).locator("button", { hasText: "Scan CV for this job" }).click();
    const overlay = scanPanel(page);
    await expect(overlay).toBeVisible();
    await overlay.locator("button", { hasText: "Run the scan" }).click();

    // --- the verdict band --------------------------------------------------
    const band = overlay.locator(".scan-verdict");
    await expect(band).toHaveClass(/\bv-unknown\b/);
    await expect(band.locator(".scan-verdict-label")).toHaveText("Cannot tell");
    // THE assertion this whole branch exists for. A green (or amber, or red)
    // band on a posting that could not be read would read as a judgement about
    // the CV, when it is a fact about the stored advert.
    await expect(overlay.locator(".scan-verdict.v-pass")).toHaveCount(0);
    await expect(overlay.locator(".scan-verdict.v-borderline")).toHaveCount(0);
    await expect(overlay.locator(".scan-verdict.v-fail")).toHaveCount(0);
    // The reason, in the pipeline's own words: it names the problem AND the fix.
    await expect(band.locator(".scan-verdict-reason").first())
      .toContainText("This is NOT a pass and NOT a fail.");

    // The char count, because it is the evidence for the refusal: a real advert
    // is several thousand characters, and this one is 59.
    await expect(band).toContainText("The stored description for this advert is 59 characters long");
    await expect(band).toContainText("A real advert is several thousand");

    // Nothing downstream of a verdict is shown: no coverage figure, no
    // suggestion, no projection. Each of those would be computed from a posting
    // the scan has just said it could not read.
    await expect(overlay.locator(".scan-coverage")).toHaveCount(0);
    await expect(overlay.locator("h3", { hasText: "Coverage" })).toHaveCount(0);
    await expect(overlay.locator(".scan-suggestion")).toHaveCount(0);
    await expect(overlay.locator(".scan-projection")).toHaveCount(0);

    // --- the way out -------------------------------------------------------
    // "Cannot tell" must not be a dead end: the board holds the same job twice
    // and the employer's own copy is the fuller one.
    const fuller = band.locator("button", { hasText: "Scan the fuller copy from adzuna (121 chars)" });
    await expect(fuller).toBeVisible();
    await fuller.click();

    // Switching reads the STORED scan for the other advert — it does not run a
    // scan of its own, because the user asked to LOOK at that posting's copy,
    // not to spend a Doc fetch on it.
    await expect.poll(() => reads.filter((u) => u === URLS.experisOriginal).length).toBeGreaterThan(0);
    expect(posts).toHaveLength(1);

    // And the panel is now showing THAT advert — the previous result is gone
    // rather than left on screen explaining a posting the user has navigated away
    // from. The X2 advert has no stored scan in the fixture, so the overlay is
    // back to its idle screen for the new job.
    await expect(scanPanel(page).locator(".panel-meta").first())
      .toContainText("AI Automation Tester X2");
    await expect(scanPanel(page).locator("h3")).toHaveText("Does my CV pass this posting?");
    await expect(scanPanel(page).locator(".scan-verdict")).toHaveCount(0);
  });

  test("S6 a stored scan reopens straight into the result, with ZERO scans run", async ({ page }) => {
    const posts = watchScanPosts(page);
    await openBoard(page);

    // The board already knows the verdict, so the chip is on the row's panel
    // BEFORE the click — and the button is worded for reopening rather than for
    // starting, which is the only signal that this job has been scanned.
    await openPanelFor(page, SCAN.pass.company);
    const panel = detailPanel(page);
    const openScan = panel.locator("button", { hasText: "Open CV scan" });
    await expect(openScan).toBeVisible();
    await expect(panel.locator(`.chip.cv-${SCAN.pass.chip.tone}`)).toHaveText(SCAN.pass.chip.label);
    await expect(panel).toContainText(`Priority-term coverage ${SCAN.pass.coverage}%`);
    await expect(panel).not.toContainText("Scan CV for this job");
    await openScan.click();

    // --- straight into the result ------------------------------------------
    const overlay = scanPanel(page);
    await expect(overlay).toBeVisible();
    await expect(overlay.locator(".scan-verdict-label")).toHaveText("Would pass");
    await expect(overlay.locator(".scan-verdict-cov"))
      .toContainText(`${SCAN.pass.coverage}% of requirement-level terms`);
    await expect(overlay.locator(".scan-verdict-cov"))
      .toContainText(`(${SCAN.pass.evidenced}/${SCAN.pass.total})`);
    // The idle screen is skipped entirely: being asked "does my CV pass this
    // posting?" about a posting that was already judged would be a step backwards.
    await expect(overlay.locator("button", { hasText: "Run the scan" })).toHaveCount(0);
    // The provenance came back with it: which copy of the Doc was read.
    await expect(overlay.locator(".scan-source")).toContainText("Master CV: read from the Google Doc");
    await expect(overlay.locator(".scan-source")).not.toHaveClass(/is-warn/);
    // And the footer says how old the stored result is, with the one action that
    // would make it current.
    await expect(overlay).toContainText("Stored scan last updated");
    await expect(overlay.locator("button", { hasText: "Re-read the Doc and scan again" })).toBeVisible();

    // THE assertion of this test: reopening a job runs NOTHING. A scan costs a
    // Google Docs read and writes a row, and "open the CV scan" must be free.
    expect(posts).toHaveLength(0);
  });

  test("S7 the board's chip tells the three verdict bands apart, and a failed one apart from a pass", async ({ page }) => {
    await openBoard(page);

    // A pass, a fail and an uncountable posting are three different things, and
    // the chip must not flatten them: "inconclusive" dressed as a warning beside
    // a real fail would train the reader to ignore both.
    await openPanelFor(page, SCAN.pass.company);
    await expect(detailPanel(page).locator(`.chip.cv-${SCAN.pass.chip.tone}`))
      .toHaveText(SCAN.pass.chip.label);
    await page.locator("#panel-close").click();

    await openPanelFor(page, SCAN.fail.company);
    await expect(detailPanel(page).locator(`.chip.cv-${SCAN.fail.chip.tone}`))
      .toHaveText(SCAN.fail.chip.label);
    // The summary beside it names what is wrong, so the chip is not the only
    // signal: how many hard skills the CV misses and what adding them would buy.
    await expect(detailPanel(page)).toContainText(
      `${SCAN.fail.hardGaps} requirement-level hard skills the CV does not mention`
    );
    await expect(detailPanel(page)).toContainText(
      `${SCAN.fail.suggestions} piece of master experience worth adding`
    );
    await expect(detailPanel(page)).toContainText(
      `Adding all of them would take it to ${SCAN.fail.projected}%`
    );
    await page.locator("#panel-close").click();

    await openPanelFor(page, SCAN.inconclusive.company);
    await expect(detailPanel(page).locator(`.chip.cv-${SCAN.inconclusive.chip.tone}`))
      .toHaveText(SCAN.inconclusive.chip.label);
    // The chip is its own tone, not a warning: see atsStateLabel.
    await expect(detailPanel(page).locator(".chip.cv-warn")).toHaveCount(0);
    await page.locator("#panel-close").click();

    // A job with no scan at all offers the scan and carries no chip, so "never
    // scanned" is distinguishable from every verdict above.
    await openPanelFor(page, ROW.freshStart);
    await expect(detailPanel(page).locator("button", { hasText: "Scan CV for this job" })).toBeVisible();
    await expect(detailPanel(page).locator(".chip.cv-ok, .chip.cv-warn, .chip.cv-bad, .chip.cv-unev"))
      .toHaveCount(0);
  });

  test("S8 a refused scan keeps the overlay open and quotes the pipeline's own message", async ({ page }) => {
    // The venv message, verbatim: it is the one line that says how to fix a
    // first-run installation, and summarising it here would remove exactly that.
    const PIPELINE_MESSAGE =
      "No Python at /repo/.venv/bin/python. The CV engine needs its own virtualenv — "
      + "create it once with: python3 -m venv .venv && ./.venv/bin/python -m pip install "
      + "python-docx fpdf2 PyYAML pypdf httpx python-dotenv";
    await stub(page, "**/api/cv/scan", { error: PIPELINE_MESSAGE }, { status: 502 });

    const overlay = await openScanner(page);
    await overlay.locator("button", { hasText: "Run the scan" }).click();

    await expect(overlay.locator("h3")).toHaveText("Could not scan");
    await expect(overlay.locator("pre.tailor-error")).toHaveText(PIPELINE_MESSAGE);
    // A scan writes nothing to the CV at any point, and the failure path has to
    // say so rather than leaving the user to wonder what state they are in.
    await expect(overlay).toContainText("Nothing was written.");
    await expect(overlay).toContainText("MASTER_CV_DOC_URL");
    // No result is claimed: the phase is the error phase, not a partial result.
    await expect(overlay.locator(".scan-verdict")).toHaveCount(0);

    // Still open, and offering both a retry and a way out.
    await expect(overlay).toBeVisible();
    await expect(overlay.locator("button", { hasText: "Try again" })).toBeVisible();
    await expect(overlay.locator("button", { hasText: "Close" })).toBeVisible();

    // Closing at this point is allowed (a failure is not a busy state) and the
    // detail panel underneath is still usable.
    await overlay.locator("#panel-close").click();
    await expect(scanOverlay(page)).toHaveCount(0);
    await expect(detailPanel(page)).toBeVisible();
  });
});
