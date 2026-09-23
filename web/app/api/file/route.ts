import { NextResponse } from "next/server";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { contentTypeFor, resolveServableFile } from "@/lib/cvTool";

/**
 * GET /api/file?path=... — serve a rendered CV artifact to the browser.
 *
 * The tailored CV, its DOCX, the gap report and the interview prep all live
 * under cv_output/ on disk, and the panel links straight to them. Serving them
 * through this route rather than exposing the tree statically is what keeps
 * cv/master.yaml — which holds a real name, email, phone number and employment
 * history — out of the browser's reach: `resolveServableFile` refuses anything
 * outside cv_output/, and refuses it before touching the filesystem.
 *
 * `Content-Disposition: inline` so a PDF opens in the browser's viewer; the
 * DOCX has no inline viewer and will download, which is what it is for.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const requested = new URL(req.url).searchParams.get("path");
  if (!requested) {
    return NextResponse.json({ error: "Missing 'path' query parameter" }, { status: 400 });
  }

  const safe = resolveServableFile(requested);
  if (!safe) {
    // One answer for "outside the output tree" and "does not exist", so this
    // route cannot be used to discover what is on the machine.
    return NextResponse.json(
      { error: "No such file in the CV output folder." },
      { status: 404 }
    );
  }

  try {
    const body = await readFile(safe);
    return new NextResponse(new Uint8Array(body), {
      headers: {
        "Content-Type": contentTypeFor(safe),
        "Content-Length": String(body.byteLength),
        "Content-Disposition": `inline; filename="${path.basename(safe)}"`,
        "Cache-Control": "no-store",
      },
    });
  } catch (err: any) {
    return NextResponse.json({ error: `Could not read the file: ${err.message}` }, { status: 500 });
  }
}
