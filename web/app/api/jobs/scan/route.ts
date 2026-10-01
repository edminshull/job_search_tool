import { NextResponse } from "next/server";
import { unstable_noStore as noStore } from "next/cache";
import { getScan, jobExists } from "@/lib/db";

// Reads cv_scans, which `cv_tailor scan` writes. Same cache discipline as
// /api/jobs/tailoring: never let Next.js or any proxy serve a stale row for a
// scan the user just ran.
export const dynamic = "force-dynamic";
export const revalidate = 0;

/**
 * The STORED scan for one posting — no subprocess, no work.
 *
 * Separate from POST /api/cv/scan because opening the panel must not run
 * anything. A stored scan is shown as-is, and a scan is only ever run when the
 * user asks for one; that is what makes reopening a job instant, and what stops
 * a page load from firing a Google Docs fetch per row.
 */
export async function GET(req: Request) {
  noStore();
  const url = new URL(req.url).searchParams.get("url") || "";
  if (!url) {
    return NextResponse.json({ error: "Missing 'url' query parameter" }, { status: 400 });
  }
  if (!jobExists(url)) {
    return NextResponse.json({ error: `No known job for url '${url}'` }, { status: 404 });
  }
  try {
    return NextResponse.json(
      { scan: getScan(url) },
      { headers: { "Cache-Control": "no-store, must-revalidate" } }
    );
  } catch (err: any) {
    return NextResponse.json(
      { error: `Failed to read from SQLite: ${err.message}` },
      { status: 500 }
    );
  }
}
