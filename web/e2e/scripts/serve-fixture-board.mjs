/**
 * Serve the fixture board for the Playwright suite, on its own port.
 *
 * WHY THIS WRAPPER EXISTS
 * -----------------------
 * Three problems, one command:
 *
 * 1. THE FIXTURE MUST EXIST BEFORE THE SERVER STARTS. `next dev` reads DB_PATH
 *    once, at module load, and caches the handle. So the database is created here
 *    first, not in a Playwright `globalSetup` — whose position relative to
 *    `webServer` is an implementation detail nobody should have to rely on.
 *
 * 2. NEXT 16 REFUSES A SECOND `next dev` FOR THE SAME DIRECTORY. It takes an
 *    exclusive lock at `<distDir>/lock` and exits with "Another next dev server
 *    is already running" — which is correct behaviour for a human, and blocks a
 *    test server entirely when you already have the board open on :3000. Giving
 *    the test server its own NEXT_DIST_DIR gives it its own lock, so both run at
 *    once and neither touches the other's build output. (See the comment in
 *    next.config.js.) That is why this runs `next dev` directly rather than
 *    `npm run dev`, so the env is unambiguous.
 *
 * 3. THE FIRST PAGE LOAD COMPILES THE APP. A cold Turbopack compile can take
 *    longer than a test's navigation timeout, which shows up as one flaky first
 *    test. So this waits for the server, then warms `/` and `/api/jobs` before
 *    Playwright is told the server is ready.
 *
 * It also forwards SIGTERM/SIGINT to the child, so Playwright's shutdown of the
 * webServer does not leave an orphaned `next dev` holding the port.
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { buildFixtureDb } from "./make-fixture-db.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.join(here, "..", "..");

const PORT = Number(process.env.QA_BOARD_PORT || 3100);
const HOST = process.env.QA_BOARD_HOST || "127.0.0.1";
const BASE = `http://${HOST}:${PORT}`;

// 1. The fixture, reset on every run so a previous run's status writes are gone.
const { path: dbPath } = buildFixtureDb();

// 2. The server, with its own build output directory and the fixture database.
const child = spawn(
  process.execPath,
  [path.join(webRoot, "node_modules", "next", "dist", "bin", "next"), "dev",
   "--port", String(PORT), "--hostname", HOST],
  {
    cwd: webRoot,
    env: { ...process.env, NEXT_DIST_DIR: ".next-e2e", DB_PATH: dbPath },
    stdio: ["ignore", "inherit", "inherit"],
  }
);

child.on("exit", (code) => process.exit(code ?? 0));
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.on(signal, () => child.kill(signal));
}

/** Poll a URL until it answers with any HTTP status (a 404 still proves the
 *  server is up, and some routes legitimately 404). */
async function waitForServer(url, { attempts = 90, delayMs = 1000 } = {}) {
  for (let i = 0; i < attempts; i++) {
    try {
      const res = await fetch(url, { redirect: "manual" });
      if (res.status < 500) return true;
    } catch {
      // not listening yet
    }
    await new Promise((r) => setTimeout(r, delayMs));
  }
  throw new Error(`the board never answered on ${url} — see the next dev output above`);
}

// 3. Wait, then warm the two routes the tests hit first, so the compile cost is
//    paid here rather than inside a test's timeout.
await waitForServer(`${BASE}/api/status`, { attempts: 90, delayMs: 500 });
for (const route of ["/", "/api/jobs"]) {
  try {
    const res = await fetch(`${BASE}${route}`);
    await res.text(); // drain, so the route is fully rendered
    console.log(`[board] warmed ${route} (${res.status})`);
  } catch (err) {
    console.warn(`[board] warm-up of ${route} failed: ${err.message}`);
  }
}
console.log(`[board] ready on ${BASE} using ${dbPath}`);

// Keep this process alive alongside the child; the exit handler above ends it.
await new Promise(() => {});
