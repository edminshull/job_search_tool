import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

/**
 * Put `next-env.d.ts` back the way the normal dev server expects it.
 *
 * WHY THIS IS NEEDED
 * ------------------
 * `next-env.d.ts` is AUTO-GENERATED, and what it generates depends on `distDir`:
 *
 *   next dev  (distDir ".next")     -> import "./.next/types/routes.d.ts"
 *   the E2E server (".next-e2e")    -> import "./.next-e2e/dev/types/routes.d.ts"
 *
 * So running the UI suite rewrites a tracked file, and afterwards `git status`
 * shows a change nobody made and `tsc --noEmit` points into a build directory
 * that a later `rm -rf .next-e2e` removes — at which point typecheck fails with
 * "Cannot find module './.next-e2e/dev/types/routes.d.ts'", which reads like a
 * broken project rather than a stale generated file.
 *
 * Restoring it here is the right place: the file's own header says it should not
 * be edited, so the only sensible way to keep it in step is programmatically.
 * This runs as Playwright's `globalTeardown`.
 *
 * It rewrites the paths rather than restoring a saved copy, so it is idempotent
 * and does nothing at all when the file was not touched.
 *
 * Written as TypeScript (not .mjs) so Playwright's commonjs loader provides
 * `__dirname`; `import.meta.url` is unavailable in that loader.
 */
const NEXT_ENV = path.join(__dirname, "..", "next-env.d.ts");

export default function globalTeardown(): void {
  let before: string;
  try {
    before = readFileSync(NEXT_ENV, "utf8");
  } catch {
    return; // nothing to restore
  }

  // Both shapes Next can emit while pointing at the test server's build dir.
  const restored = before
    .replaceAll(".next-e2e/dev/types/", ".next/types/")
    .replaceAll(".next-e2e/types/", ".next/types/");

  if (restored !== before) {
    writeFileSync(NEXT_ENV, restored, "utf8");
    console.log("[teardown] restored next-env.d.ts to the .next build directory");
  }
}
