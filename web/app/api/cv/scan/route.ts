import { NextResponse } from "next/server";
import { runCvTool, scanArgs } from "@/lib/cvTool";
import { getScan, jobExists } from "@/lib/db";

// Scanning is deterministic and makes NO model call, so unlike
// /api/tailor/preview it finishes in well under a second. `maxDuration` is
// still set because the one slow thing it can do is a live fetch of the master
// CV from Google Docs, and a route that is killed mid-request reports nothing
// useful about why.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 120;

/**
 * Run the ATS scan for one posting.
 *
 * This is what the "Scan CV" button calls. It is the successor to
 * /api/tailor/preview, which drafted and verified a rewritten CV per posting;
 * the CV that gets sent is now the single master document, so the questions
 * worth asking per posting are "would it pass" and "what real experience is
 * missing from it" — see app/ats_scan.py.
 *
 * The scan is stored by the Python side as part of running it, so a successful
 * response does not need a second write here. The response carries the stored
 * row back anyway, because the board's summary columns come from it and the
 * panel must not have to guess what was persisted.
 */
export async function POST(req: Request) {
  let body: { url?: unknown; refresh_master?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Expected a JSON body." }, { status: 400 });
  }

  const url = typeof body.url === "string" ? body.url.trim() : "";
  if (!url) {
    return NextResponse.json({ error: "Missing 'url' in request body" }, { status: 400 });
  }
  // Checked against the board first, so an unknown url is a 404 naming the
  // problem rather than a subprocess that starts Python to discover it.
  if (!jobExists(url)) {
    return NextResponse.json({ error: `No job on the board with url '${url}'` }, { status: 404 });
  }

  const result = runCvTool(scanArgs({ url, refreshMaster: body.refresh_master === true }));
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 502 });
  }

  // Re-read rather than echoing what the subprocess returned. The subprocess's
  // payload is what the panel shows; the ROW is what the board will show on the
  // next load, and returning the row that is actually in the database is what
  // keeps those two from being able to disagree.
  const stored = getScan(url);
  return NextResponse.json({ ...result.data, stored });
}
