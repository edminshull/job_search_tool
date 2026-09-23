import { NextResponse } from "next/server";
import { getTailoring, getJobs } from "@/lib/db";

/**
 * GET /api/jobs/tailoring?url=... — the stored CV state for one posting.
 *
 * Read when a panel opens, so an already-tailored job shows its file links and
 * its previous draft without re-running a model. `getJobs()` carries only the
 * summary columns (path, status, coverage); the YAML and the verifier's findings
 * are several KB per job and would be waste on a 300-row board payload.
 *
 * `master` reports the fact base's size and whether it is still placeholder
 * data — a placeholder master means every CV built from it is fictional, which
 * is worth knowing before reading a word of the draft.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url).searchParams.get("url");
  if (!url) {
    return NextResponse.json({ error: "Missing 'url' query parameter" }, { status: 400 });
  }
  try {
    const known = getJobs().some((j) => j.url === url);
    if (!known) {
      return NextResponse.json({ error: `No known job for url '${url}'` }, { status: 404 });
    }
    return NextResponse.json(
      { tailoring: getTailoring(url) },
      { headers: { "Cache-Control": "no-store, must-revalidate" } }
    );
  } catch (err: any) {
    return NextResponse.json({ error: `Failed to read from SQLite: ${err.message}` }, { status: 500 });
  }
}
