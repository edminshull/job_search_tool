/** @type {import('next').NextConfig} */
const nextConfig = {
  // better-sqlite3 is a native module — don't let webpack try to bundle it.
  serverExternalPackages: ["better-sqlite3"],

  // Build output directory, overridable so a SECOND dev server can run
  // alongside the one you already have open.
  //
  // Next 16 takes an exclusive on-disk lock at `<distDir>/lock` and refuses to
  // start a second `next dev` for the same project ("Another next dev server is
  // already running"), which is right for a human but blocks E2E tests from
  // serving their own fixture database without stopping the dev server you are
  // working in. Giving the test server its own distDir gives it its own lock,
  // so the two run concurrently and neither touches the other's build output.
  //
  // Default is unchanged: only the E2E harness sets NEXT_DIST_DIR.
  distDir: process.env.NEXT_DIST_DIR || ".next",
};

module.exports = nextConfig;
