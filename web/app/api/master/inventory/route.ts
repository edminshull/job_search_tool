import { NextResponse } from "next/server";
import { runCvTool } from "@/lib/cvTool";

/**
 * POST /api/master/inventory — re-render the master full inventory CV.
 *
 * ON DEMAND ONLY, and that is the whole point of it being a separate endpoint.
 * Ed's instruction (2026-09-23): the master inventory is updated when a new
 * skill or achievement is genuinely added — "when I have a new skill or
 * something comes up from the tailored cv for a particular role", which is a
 * human judgement about whether a fact is real. Regenerating it inside every
 * tailoring run would make the inventory look current whenever it merely looked
 * recent, and would quietly turn a deliberate act into a side effect.
 *
 * So nothing calls this automatically. The tailored-CV preview surfaces the
 * posting terms that cv/master.yaml cannot evidence, which is the evidence a
 * person needs to decide — and then they edit cv/master.yaml and press this.
 * Wiring the verifier's suggestions straight into the master is exactly the
 * failure the fabrication guard exists to prevent.
 */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 120;

export async function POST() {
  const result = runCvTool(["master-inventory"]);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 502 });
  }
  return NextResponse.json(result.data);
}

/** GET reports the master's size and placeholder state without rendering. */
export async function GET() {
  const result = runCvTool(["master-status"]);
  if (!result.ok) {
    return NextResponse.json({ error: result.error }, { status: 502 });
  }
  return NextResponse.json(result.data, {
    headers: { "Cache-Control": "no-store, must-revalidate" },
  });
}
