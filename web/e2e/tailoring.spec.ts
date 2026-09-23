import { expect, Page, Route, test } from "@playwright/test";
import { ROW, URLS, openBoard, openPanelFor, resetBoard } from "./fixtures";

/**
 * The tailored-CV overlay: a two-step flow where approving is what writes files.
 *
 * THE MODEL CALLS ARE STUBBED, AND THAT IS THE RIGHT CALL FOR A UI TEST
 * ---------------------------------------------------------------------
 * /api/tailor/preview and /api/tailor/render shell out to app/cv_tailor.py, which
 * makes a ~90-second local-LLM call and a DeepSeek verification call. Driving
 * that from a UI test would make the suite slow, non-deterministic, dependent on
 * model credentials, and would fail for reasons that have nothing to do with the
 * interface. So the two routes are intercepted with canned, schema-shaped
 * responses and the assertions are about the INTERFACE's contract:
 *
 *   * the right phase is shown at the right time (including the two long waits,
 *     which are ~90s in reality and must never look like a hang);
 *   * the YAML that goes back to /render is the text the user SAW and could have
 *     EDITED — the property the two-step design exists to guarantee, and the one
 *     a careless refactor would break by sending the original draft instead;
 *   * a failure leaves the overlay open with the pipeline's own message and
 *     claims nothing was written.
 *
 * What is deliberately NOT stubbed is /api/jobs and /api/jobs/tailoring, so the
 * panel's own state (a job with a stored draft, a job whose PDF is gone) is read
 * from a real database.
 */
const previewBody = (overrides: Record<string, unknown> = {}) => ({
  url: URLS.freshStart,
  status: "draft",
  day_dir: "01-01-01",
  company_slug: "new-beginnings-ltd",
  future_outdir: "/fixtures/cv_output/01-01-01/new-beginnings-ltd",
  tailored_yaml: "roles:\n  - ref: r1\n    emphasis: automation\nskills:\n  - ref: s1\n",
  report: "Led with the API automation work and cut the marketing background.",
  draft_model: "local:qwen3-coder-30b-a3b",
  verification: {
    verdict: "accept",
    verify_model: "deepseek:deepseek-flash",
    unsupported_refs: "none",
    fabricated_claims: "none",
    invented_metrics: "none",
    required_fixes: "none",
    missing_must_haves: "none",
    notes: "none",
  },
  attempts: [{ model: "local:qwen3-coder-30b-a3b", verdict: "accept", reason: null }],
  coverage_before: 64,
  coverage_after: 78,
  priority_gaps: 2,
  gap_report: "Two requirement-level terms cannot be evidenced.",
  interview_prep_md: null,
  posting_available: true,
  ...overrides,
});

const renderBody = (overrides: Record<string, unknown> = {}) => ({
  status: "rendered",
  outdir: "/fixtures/cv_output/01-01-01/new-beginnings-ltd",
  pdf_path: "/fixtures/cv_output/01-01-01/new-beginnings-ltd/Ed-Minshull-CV.pdf",
  docx_path: "/fixtures/cv_output/01-01-01/new-beginnings-ltd/Ed-Minshull-CV.docx",
  gap_report_path: null,
  interview_prep_path: null,
  coverage_before: 64,
  coverage_after: 78,
  priority_gaps: 2,
  warnings: [],
  ...overrides,
});

/** Intercept a route with an optional delay, so a long wait can be observed. */
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

test.describe("CV tailoring overlay", () => {
  test.beforeEach(() => resetBoard());

  test("P4 the journey from an untailored job: draft, review, approve, render", async ({ page }) => {
    // A ~700ms draft, so the in-progress phase is observable. In production this
    // is ~90 seconds and the UI shows a real elapsed clock, not a spinner.
    await stub(page, "**/api/tailor/preview", previewBody(), { delayMs: 700 });
    // Captured in an array rather than a `let`: TypeScript cannot see an
    // assignment made inside a route callback, so a `let` stays narrowed to
    // `null` at the assertions below.
    const renderPayloads: Record<string, unknown>[] = [];
    await page.route("**/api/tailor/render", async (route) => {
      renderPayloads.push(route.request().postDataJSON() as Record<string, unknown>);
      await route.fulfill({
        status: 200, contentType: "application/json",
        body: JSON.stringify(renderBody()),
      });
    });

    await openBoard(page);
    await openPanelFor(page, ROW.freshStart);

    // The button says "Tailor CV for this job" when nothing has been drafted —
    // the wording is the only signal that this job has no CV yet.
    const tailorButton = page.locator("#panel:not(.tailor-panel) button", { hasText: "Tailor CV for this job" });
    await expect(tailorButton).toBeVisible();
    await tailorButton.click();

    const overlay = page.locator(".tailor-panel");
    await expect(overlay).toBeVisible();

    // --- idle phase --------------------------------------------------------
    // Nothing is written until approval, and the copy has to say so: a tool that
    // silently produces files on a first click is one nobody dares click.
    await expect(overlay.locator("h3")).toContainText("Draft a CV for this posting");
    await expect(overlay).toContainText("Nothing is written to disk until you approve");
    await expect(overlay).toContainText("60–120 seconds");
    await expect(overlay.locator(".tailor-phases")).toContainText("1. Draft (local Qwen)");
    await expect(overlay.locator("button", { hasText: "Approve & render" })).toHaveCount(0);

    // The interview-prep checkbox is opt-in, and its cost is stated.
    await expect(overlay.locator(".tailor-check")).toContainText("Also generate interview prep");

    // --- drafting phase ----------------------------------------------------
    await overlay.locator("button", { hasText: "Draft CV" }).click();
    await expect(overlay.locator("h3")).toContainText("Drafting and verifying");
    // A long wait is explained rather than left as an anonymous spinner, and the
    // close button is disabled so the draft cannot be abandoned by a stray click.
    await expect(overlay.locator(".tailor-progress")).toContainText("elapsed");
    await expect(overlay.locator(".tailor-progress")).toContainText("local model is");
    await expect(overlay.locator("#panel-close")).toBeDisabled();

    // --- review phase ------------------------------------------------------
    await expect(overlay.locator("h3").first()).toContainText("Independent check");
    await expect(overlay.locator(".tailor-verdict")).toContainText("Accepted — every claim traces to the master CV");
    await expect(overlay.locator(".tailor-verdict")).toContainText("deepseek:deepseek-flash");
    // The verifier's verdict comes with a next step, so an accepted draft is not
    // a dead end.
    await expect(overlay.locator(".tailor-verdict-advice")).toContainText("approve it");

    // Coverage is shown as a before/after pair, because the two numbers are the
    // same measurement and only mean something compared.
    const coverage = overlay.locator(".tailor-cov-num");
    await expect(coverage).toHaveCount(2);
    expect(await coverage.allInnerTexts()).toEqual(["64%", "78%"]);
    // A gap the master cannot evidence is called out as a fact about the
    // candidate's history rather than a drafting mistake.
    await expect(overlay).toContainText("2 requirement-level terms in this posting cannot be evidenced");

    // The drafted YAML is EDITABLE. That is the whole point of the two steps.
    const yaml = overlay.locator("textarea.tailor-yaml");
    await expect(yaml).toHaveValue(/roles:/);
    await yaml.fill("roles:\n  - ref: r1\n    emphasis: api-and-performance\n");

    // --- approve and render ------------------------------------------------
    await overlay.locator("button", { hasText: "Approve & render" }).click();
    await expect(overlay.locator("h3", { hasText: "Written" })).toBeVisible();
    await expect(overlay.locator(".tailor-files")).toContainText("Open CV (PDF)");
    await expect(overlay.locator(".tailor-files")).toContainText("DOCX (to edit)");
    await expect(overlay).toContainText("Filed in");

    // THE assertion this whole test exists for: what was sent to /render is the
    // text the user just edited, not the draft that came back from the model.
    // The server re-verifies an edited draft precisely because an edit is a new
    // claim — so sending the original here would silently render something the
    // user never approved.
    expect(renderPayloads).toHaveLength(1);
    expect(renderPayloads[0].tailored_yaml)
      .toBe("roles:\n  - ref: r1\n    emphasis: api-and-performance\n");
    expect(renderPayloads[0].url).toBe(URLS.freshStart);

    // Once rendered, the approve button is gone — re-approving would archive the
    // version just written and produce a second one.
    await expect(overlay.locator("button", { hasText: "Approve & render" })).toHaveCount(0);

    // Closing reveals the board again, with the detail panel still open
    // underneath: the draft belongs to the URL, not to the overlay.
    await overlay.locator("#panel-close").click();
    await expect(page.locator(".tailor-panel")).toHaveCount(0);
    await expect(page.locator("#panel:not(.tailor-panel)")).toBeVisible();
  });

  test("B2 a stored draft opens straight into review, and a rendered record whose PDF is gone says so", async ({ page }) => {
    await openBoard(page);

    // --- the stored-draft boundary -----------------------------------------
    // Draft Partners has a cv_tailorings row with status "draft". Opening it must
    // go STRAIGHT to review, showing the text and verdict that were stored, with
    // NO model call — otherwise "open the CV" would cost 90 seconds and could
    // return a different draft than the one being reviewed.
    let previewCalls = 0;
    await page.route("**/api/tailor/preview", async (route) => {
      previewCalls += 1;
      await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "must not be called" }) });
    });

    await openPanelFor(page, ROW.draftPartners);
    // The panel already knows there is a draft, so the button is worded for
    // reopening rather than starting.
    const openCv = page.locator("#panel:not(.tailor-panel) button", { hasText: "Open tailored CV" });
    await expect(openCv).toBeVisible();
    // And it says what state the CV is in before you click: a draft that was
    // never approved is not the same as a finished one.
    await expect(page.locator("#panel:not(.tailor-panel) .chip.cv-warn")).toContainText("CV drafted, not rendered");
    await expect(page.locator("#panel:not(.tailor-panel)")).toContainText("64% (master)");
    await openCv.click();

    const overlay = page.locator(".tailor-panel");
    await expect(overlay).toBeVisible();
    // Straight into review: the "Draft a CV" idle screen is skipped entirely.
    await expect(overlay.locator("h3").first()).toContainText("Independent check");
    await expect(overlay.locator("button", { hasText: "Draft CV" })).toHaveCount(0);
    // The STORED verdict and findings are rendered — "revise", with the specific
    // claim the verifier flagged and the fix it asked for.
    await expect(overlay.locator(".tailor-verdict")).toContainText("Needs changes");
    await expect(overlay.locator(".tailor-finding.f-bad")).toContainText("Claims 'led a team of eight'");
    await expect(overlay.locator(".tailor-finding.f-warn")).toContainText("Reword the team-size claim");
    // A mild finding is shown too, in its own tone, rather than being hidden
    // because a worse one exists.
    await expect(overlay.locator(".tailor-finding.f-info").filter({ hasText: "Kafka" })).toContainText("No evidence of Kafka");

    // A field whose ANSWER is "none" is not a finding, however much explanation
    // follows it — and prose that merely starts with "No" still IS one. The two
    // directions together are what stop a clean check rendering as a red flag
    // (which it did) or a real problem being hidden (which would be worse).
    await expect(overlay).not.toContainText("No numbers appear anywhere");

    // The drafting provenance survives the reload. This record's local draft was
    // escalated to the cloud and the retry verified no better, so BOTH attempts
    // and the reason the second was set aside must be on screen. Without the
    // stored `attempts` column this block is absent, and a cloud call that was
    // really made becomes invisible — which is why a working fallback looked
    // broken. `draft_model` cannot carry it: it names only the surviving draft.
    const escalation = overlay.locator(".tailor-escalation .tailor-attempt");
    await expect(escalation).toHaveCount(2);
    await expect(escalation.nth(0)).toContainText("1. local");
    await expect(escalation.nth(1)).toContainText("2. deepseek");
    await expect(escalation.nth(1)).toContainText("verified no better");

    // The stored YAML is what is in the editor.
    await expect(overlay.locator("textarea.tailor-yaml")).toHaveValue(/api-testing/);
    await expect(overlay.locator("button", { hasText: "Approve & render" })).toBeVisible();
    expect(previewCalls).toBe(0);
    await overlay.locator("#panel-close").click();

    // --- the DB-and-disk-disagree boundary ---------------------------------
    // Ghost Files has status "rendered" and a pdf_path inside cv_output/ — but no
    // file. Found for real on 2026-09-23: two rows claimed rendered while
    // cv_output/<date>/ had gone, and the panel offered an "Open PDF" link to
    // nothing. The record and the filesystem can disagree, and when they do the
    // honest answer is "there is no CV here".
    await page.locator("#panel-close").click();
    await openPanelFor(page, ROW.ghostFiles);
    const panel = page.locator("#panel:not(.tailor-panel)");
    await expect(panel.locator(".chip.cv-bad")).toContainText("CV file missing — re-render");
    // Crucially, the board says so BEFORE the click. A dead link the user only
    // discovers by clicking is exactly the kind of half-truth this system exists
    // to avoid about facts — including its own file links.
    await expect(panel.locator('a:has-text("Open PDF")')).toHaveCount(0);
    await expect(panel.locator('a:has-text("DOCX")')).toHaveCount(0);
    await expect(panel).toContainText("the file is no longer in");
    await expect(panel).toContainText("does not need another model call");

    // The other half of the boundary: reopening a job that IS downloadable offers
    // the links, so the missing-link case above is a real distinction and not
    // simply "links are never rendered". Nothing in the fixture has a PDF on
    // disk, so this is asserted from the API's own view of the two rows.
    const api = (await (await page.request.get("/api/jobs")).json()) as
      { company: string; cv_status: string | null; cv_files_present: boolean }[];
    expect(api.find((j) => j.company === ROW.ghostFiles)?.cv_status).toBe("rendered");
    expect(api.find((j) => j.company === ROW.ghostFiles)?.cv_files_present).toBe(false);
    expect(api.find((j) => j.company === ROW.draftPartners)?.cv_status).toBe("draft");
    expect(api.find((j) => j.company === ROW.draftPartners)?.cv_files_present).toBe(false);
    // And a job that has never been tailored is distinguishable from either.
    expect(api.find((j) => j.company === ROW.freshStart)?.cv_status).toBe(null);
  });

  test("B3 an escalation that PROVED the drafter wrong says what it was taught", async ({ page }) => {
    // The lesson loop's only visible output. `learned` appears on an escalation
    // that verified strictly better — the one case that proves the first draft
    // was wrong — and its absence on a retry that fared no better is the gate
    // that keeps a verifier false positive out of every future prompt.
    await stub(page, "**/api/tailor/preview", previewBody({
      attempts: [
        { model: "local:qwen3-coder-30b-a3b", verdict: "revise", reason: null },
        {
          model: "deepseek:deepseek-flash",
          verdict: "accept",
          reason: "The local draft was flagged for changes by the verifier, so it was "
                + "re-drafted with deepseek using those corrections — now accepted",
          learned: ["invented_metric"],
        },
      ],
      verification: {
        verdict: "accept", verify_model: "deepseek:deepseek-flash",
        unsupported_refs: "none", fabricated_claims: "none", invented_metrics: "none",
        required_fixes: "none", missing_must_haves: "none", notes: "none",
      },
    }));

    await openBoard(page);
    await openPanelFor(page, ROW.freshStart);
    await page.locator("#panel:not(.tailor-panel) button", { hasText: "Tailor CV for this job" }).click();
    await page.locator(".tailor-panel button", { hasText: "Draft CV" }).click();

    const escalation = page.locator(".tailor-escalation .tailor-attempt");
    await expect(escalation).toHaveCount(2);
    // The mode is named in English, not as its internal key, and the effect is
    // stated — otherwise "learned: invented_metric" is a token, not a reason to
    // believe the cloud spend bought anything.
    await expect(escalation.nth(1)).toContainText("Taught the drafter");
    await expect(escalation.nth(1)).toContainText("invents numbers the master does not hold");
    await expect(escalation.nth(1)).toContainText("every future draft");
    // The attempt that taught nothing says nothing about lessons.
    await expect(escalation.nth(0)).not.toContainText("Taught the drafter");
  });
});
