import { NextResponse } from "next/server";
import { writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { getTailoring } from "@/lib/db";
import { runCvTool } from "@/lib/cvTool";

/**
 * POST /api/tailor/render — write the approved CV to disk and render it.
 *
 * Step two of two. The YAML that comes back here is the text the user approved
 * and may have edited in the preview; the Python side re-verifies an edited
 * draft, because an edit is a new claim and the previous verdict was about
 * different text.
 *
 * If the YAML is a hand edit it is passed via a TEMPORARY FILE rather than as a
 * command-line argument. A tailored CV is several KB and can contain anything a
 * person types, including characters a shell would reinterpret; an argv entry
 * also lands in the process table and in any error message that echoes the
 * command. The temp file is removed in a finally block whether the render
 * succeeded or not.
 *
 * A FABRICATION CHECK FAILURE IS EXPECTED TO BE POSSIBLE HERE and returns 422,
 * not 500: it means the drafted CV claimed something cv/master.yaml cannot
 * support, the renderer wrote nothing, and the fix is to edit or regenerate the
 * draft. That is a user-actionable outcome, not a server fault.
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

  const editedYaml = typeof body?.tailored_yaml === "string" ? body.tailored_yaml : null;
  if (editedYaml !== null && !editedYaml.trim()) {
    return NextResponse.json({ error: "The approved YAML is empty." }, { status: 400 });
  }

  const stored = getTailoring(url);
  const isEdit =
    editedYaml !== null && editedYaml.trim() !== (stored?.tailored_yaml ?? "").trim();
  if (!stored && editedYaml === null) {
    return NextResponse.json(
      { error: "Nothing has been previewed for this job yet — draft a CV first." },
      { status: 409 }
    );
  }

  const args = ["render", "--url", url];
  if (body?.interview_prep === true) args.push("--interview-prep");

  let tmpDir: string | null = null;
  try {
    if (isEdit) {
      tmpDir = mkdtempSync(path.join(tmpdir(), "cv-approve-"));
      const yamlPath = path.join(tmpDir, "tailored.yaml");
      writeFileSync(yamlPath, editedYaml as string, "utf8");
      args.push("--yaml-file", yamlPath);
    }

    const result = runCvTool(args);
    if (!result.ok) {
      // Distinguish "you edited it into an unsupported claim" from "the
      // machinery broke". Both are the pipeline's own message, but only the
      // first is something the user can act on by editing the text.
      const unsupported =
        /fabrication|cannot support|is not a/.test(result.error) &&
        /ref|master/.test(result.error);
      return NextResponse.json(
        { error: result.error, kind: unsupported ? "unsupported_claim" : "pipeline_error" },
        { status: unsupported ? 422 : 502 }
      );
    }
    return NextResponse.json(result.data);
  } finally {
    if (tmpDir) rmSync(tmpDir, { recursive: true, force: true });
  }
}
