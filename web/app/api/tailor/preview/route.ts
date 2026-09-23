import { NextResponse } from "next/server";
import { getTailoring } from "@/lib/db";
import { runCvTool } from "@/lib/cvTool";

/**
 * POST /api/tailor/preview — draft and verify a tailored CV, writing no files.
 *
 * This is step one of two. The draft lands in the cv_tailorings table and comes
 * back to the browser for review; APPROVING it is what writes files and renders
 * a PDF. The split exists so the model's selection can be read before anything
 * is created — a tailored CV is a claim about a real person's experience, and
 * the one thing this pipeline must never do is produce a document nobody
 * checked.
 *
 * A MODEL CALL OF ~90s IS EXPECTED HERE. Measured: the local Qwen drafting
 * stage dominates (it writes a ~2 KB YAML document at ~35 tok/s), and the
 * DeepSeek verification adds ~5s. So maxDuration is set generously and the UI
 * shows a genuine in-progress state rather than a spinner that implies a
 * timeout is coming.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 660;

export async function POST(req: Request) {
  const body = await req.json().catch(() => null);
  const url = body?.url;
  if (!url || typeof url !== "string") {
    return NextResponse.json({ error: "Missing 'url' in request body" }, { status: 400 });
  }

  const args = ["preview", "--url", url];
  if (typeof body?.feedback === "string" && body.feedback.trim()) {
    args.push("--feedback", body.feedback);
  }
  if (body?.interview_prep === true) {
    args.push("--interview-prep");
  }

  const result = runCvTool(args);
  if (!result.ok) {
    // Nothing was written, so there is nothing to roll back — the record stays
    // as it was and the error is surfaced verbatim. The pipeline's errors are
    // written to be read by a person ("No local model server on ... Start it
    // with ..."), so they are passed through rather than summarised.
    return NextResponse.json({ error: result.error }, { status: 502 });
  }

  // Read the row back rather than trusting the process's own echo: the DB is
  // what `render` will read, so showing the browser anything else risks the
  // preview and the render disagreeing about what was drafted.
  const stored = getTailoring(url);
  return NextResponse.json({
    ...result.data,
    stored: stored
      ? {
          status: stored.status,
          day_dir: stored.day_dir,
          updated_at: stored.updated_at,
        }
      : null,
  });
}
