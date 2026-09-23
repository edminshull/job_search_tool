import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright configuration for the job-search board's UI tests.
 *
 * SELF-CONTAINED: one command starts everything
 * ---------------------------------------------
 * `webServer` runs e2e/scripts/serve-fixture-board.mjs, which resets its own
 * fixture SQLite file, starts `next dev` against it on its own port, waits for
 * readiness and warms the routes. So `npm run test:ui` works from a cold machine
 * with no setup and no manual server — and it does NOT disturb the board you may
 * already have open on :3000. See that script for why it needs its own
 * NEXT_DIST_DIR (Next 16 refuses a second dev server for the same directory).
 *
 * `reuseExistingServer: false` deliberately: if something is already listening
 * on the test port, fail loudly. Reusing it would silently run the suite against
 * a server pointed at the REAL database, and the status tests would write into a
 * live job tracker.
 *
 * CHROME, HEADLESS OR NOT
 * -----------------------
 * Two projects, same assertions:
 *
 *   chrome-headless   the default. What CI and `npm run test:ui` run.
 *   chrome-headed     a real visible window, for watching a failure happen or
 *                     for demoing a journey to someone.
 *
 *     npm run test:ui                  # headless
 *     npm run test:ui:headed           # visible Chrome
 *     npm run test:ui -- --ui          # Playwright's interactive UI mode
 *
 * Both use Playwright's `channel: "chrome"`, i.e. the Google Chrome already
 * installed on the machine, rather than a downloaded Chromium build. That is the
 * browser the board is actually used in, and it avoids a ~150 MB download on
 * first run. If Chrome is not installed, switch the channel to `chromium` and
 * run `npx playwright install chromium`.
 *
 * WHY workers: 1
 * --------------
 * All ten tests share one board, and two of them WRITE to it (the inline status
 * change and its negative case). Serial execution plus the per-spec `resetBoard()`
 * hook makes each test's starting state a fact rather than a race, which matters
 * far more here than the few seconds saved by parallelising ten tests.
 */
const PORT = Number(process.env.QA_BOARD_PORT || 3100);
const HOST = process.env.QA_BOARD_HOST || "127.0.0.1";
const BASE_URL = `http://${HOST}:${PORT}`;

export default defineConfig({
  testDir: "./e2e",
  // Nothing in e2e/scripts is a test; only *.spec.ts files are collected.
  testMatch: /.*\.spec\.ts/,
  // Puts the auto-generated next-env.d.ts back to the ".next" build directory,
  // which the E2E server's own distDir otherwise rewrites. See the file.
  globalTeardown: "./e2e/global-teardown.ts",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],

  // Generous on purpose: the board is served by `next dev`, so a cold route
  // compile lands in the first navigation. The wrapper warms the routes to keep
  // this headroom unused, but a slow machine should not produce a false failure.
  timeout: 60_000,
  expect: { timeout: 15_000 },

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    // The board is a desktop table UI; a phone viewport would be testing a
    // layout that does not exist. 1440x900 keeps all nine columns visible.
    viewport: { width: 1440, height: 900 },
  },

  projects: [
    {
      name: "chrome-headless",
      use: { ...devices["Desktop Chrome"], channel: "chrome", headless: true },
    },
    {
      name: "chrome-headed",
      use: {
        ...devices["Desktop Chrome"],
        channel: "chrome",
        headless: false,
        // A headed run is for watching, so slow every action down enough to see
        // what happened. Harmless when the project is not selected.
        launchOptions: { slowMo: process.env.QA_SLOWMO ? Number(process.env.QA_SLOWMO) : 0 },
      },
    },
  ],

  webServer: {
    command: "node e2e/scripts/serve-fixture-board.mjs",
    // Must be a route that answers GET with a 2xx. /api/status is POST-only and
    // returns 405 — Playwright would poll it until the startup timeout expired,
    // reporting "timed out waiting for webServer" while the board was in fact
    // serving perfectly well.
    url: `${BASE_URL}/api/jobs`,
    reuseExistingServer: false,
    timeout: 240_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
