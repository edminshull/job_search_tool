import { spawnSync } from "node:child_process";
import path from "node:path";
import { realpathSync, statSync } from "node:fs";

/**
 * Runs app/cv_tailor.py and returns whatever it emitted.
 *
 * The Python module is the whole pipeline, and the web app is a caller of it
 * rather than a reimplementation — same relationship the board already has with
 * app/ai_evaluate.py and the SQLite file. It is a SUBPROCESS rather than an
 * import because the tailoring engine is Python (the vendored renderer uses
 * fpdf2/python-docx), and because a 90-second model call that ends in an exit
 * code is a far better boundary than one that ends in a stack trace.
 *
 * `./.venv/bin/python` explicitly: python-docx and fpdf2 are not installed
 * system-wide, and a missing-venv error should say which venv is missing
 * rather than surface an ImportError from three frames deep.
 *
 * One JSON object on stdout is the protocol (`cv_tailor._emit`). Anything the
 * vendored scripts print goes to stderr, so a stray print cannot corrupt it.
 */
export const CV_TOOL = path.join(process.cwd(), "..", "app", "cv_tailor.py");
export const CV_ROOT = path.join(process.cwd(), "..");
export const VENV_PYTHON = path.join(CV_ROOT, ".venv", "bin", "python");

// A tailoring run is ~90s in practice (measured: Qwen drafting a ~2 KB YAML at
// ~34 tok/s dominates; the DeepSeek verification is ~5s). This ceiling exists
// only so a wedged process cannot hang a request forever.
//
// It MUST stay comfortably ABOVE the model call's own timeout
// (LOCAL_LLM_TIMEOUT, default 600s). If it were shorter, this would kill the
// subprocess while the local model carried on generating into a request nobody
// is reading — the caller sees a timeout, the GPU stays busy for another minute,
// and the run is lost either way. Enforced by a test, because the two numbers
// live in different languages and nothing else would catch them diverging.
const TIMEOUT_MS = 640_000;

/** The subprocess ceiling, exported so a test can assert it stays above the
 *  model call's own timeout (LOCAL_LLM_TIMEOUT in app/llm_providers.py). */
export const CV_TOOL_TIMEOUT_MS = TIMEOUT_MS;

export type CvToolResult =
  | { ok: true; data: Record<string, unknown> }
  | { ok: false; error: string };

export function runCvTool(args: string[]): CvToolResult {
  const proc = spawnSync(VENV_PYTHON, ["-m", "app.cv_tailor", ...args], {
    cwd: CV_ROOT,
    encoding: "utf8",
    timeout: TIMEOUT_MS,
    maxBuffer: 32 * 1024 * 1024,
    env: { ...process.env },
  });

  if (proc.error) {
    const e = proc.error as NodeJS.ErrnoException;
    if (e.code === "ENOENT") {
      return {
        ok: false,
        error:
          `No Python at ${VENV_PYTHON}. The CV engine needs its own virtualenv — ` +
          `create it once with: python3 -m venv .venv && ./.venv/bin/python -m pip install ` +
          `python-docx fpdf2 PyYAML pypdf httpx python-dotenv`,
      };
    }
    return { ok: false, error: `Could not run the CV pipeline: ${e.message}` };
  }

  if (proc.status === null) {
    return {
      ok: false,
      error: `The CV pipeline did not finish within ${Math.round(TIMEOUT_MS / 1000)}s and was killed.`,
    };
  }

  // Parse stdout even on a non-zero exit: cv_tailor.py reports its OWN errors
  // as {"ok": false, "error": ...} with exit 1, so the useful message is in
  // the JSON, not the status. Only a truly empty/invalid stdout falls back to
  // the raw streams.
  const stdout = (proc.stdout || "").trim();
  if (stdout) {
    try {
      const parsed = JSON.parse(stdout);
      if (parsed && typeof parsed === "object" && "ok" in parsed) {
        if (parsed.ok) return { ok: true, data: parsed };
        return { ok: false, error: String(parsed.error || "The CV pipeline failed.") };
      }
    } catch {
      // fall through to the raw-stream report below
    }
  }

  const detail = (proc.stderr || stdout || "").trim().slice(0, 1500);
  return {
    ok: false,
    error: detail
      ? `The CV pipeline exited ${proc.status} without a usable result:\n${detail}`
      : `The CV pipeline exited ${proc.status} with no output.`,
  };
}

/** Where tailored CVs are filed. Mirrors cv_tailor.OUTPUT_ROOT.
 *
 *  `CV_OUTPUT_ROOT` is overridable so a test can point the guard at a
 *  throwaway directory. That is not a convenience: these tests CREATE files to
 *  prove what the guard refuses, and an earlier version of them did that inside
 *  the real cv_output/ — then deleted the directory it had created, which on
 *  this project meant deleting a colleague's rendered application. A test that
 *  writes into the live output tree is a test that can destroy the one artefact
 *  this system exists to protect. */
export const CV_OUTPUT_ROOT = process.env.CV_OUTPUT_ROOT || path.join(CV_ROOT, "cv_output");

/**
 * Resolves a requested file path, refusing anything outside the CV output tree.
 *
 * This guards a route that takes a path from the browser, so it is worth being
 * explicit about what it does and does not do:
 *
 *   * It resolves the path FIRST and then checks containment, rather than
 *     rejecting strings containing "..". String-matching dot-segments is
 *     defeated by symlinks, by absolute paths, and by encodings — the check has
 *     to be on the resolved real path or it is not a check.
 *   * BOTH sides are canonicalised with `realpathSync` before comparing. That
 *     is what makes the check correct in both directions: a symlink INSIDE the
 *     tree pointing OUT of it resolves to its real target and is refused, while
 *     a tree that is itself reached through a symlink (macOS gives every
 *     temporary directory this shape, /tmp → /private/tmp) still matches
 *     instead of rejecting every legitimate file.
 *   * `path.relative` is used rather than `startsWith`, because "/a/cv_output-x"
 *     starts with "/a/cv_output" and would pass a prefix test while sitting
 *     outside the directory entirely.
 *   * Rejecting anything outside cv_output/ is the intent, not an accident: the
 *     only files the UI ever links to are a rendered CV, its DOCX, its gap
 *     report and its interview prep, and every one of those lives under
 *     cv_output/. So there is no need for a wider root — and a wider root would
 *     expose cv/master.yaml, which is real personal data, plus the whole
 *     checkout.
 *
 * Returns null when the path is not servable. Non-existent and out-of-tree are
 * deliberately the same answer, so the route cannot be used to probe which
 * paths exist on the machine.
 */
export function resolveServableFile(requested: string): string | null {
  if (!requested || requested.includes("\0")) return null;
  let resolved: string;
  try {
    resolved = realpathSync(path.resolve(requested));
  } catch {
    // realpathSync throws when the path does not exist — which is also the
    // answer we want, so it is not distinguished from a refusal.
    return null;
  }

  let root: string;
  try {
    // The turbopackIgnore comment is not cosmetic. `CV_OUTPUT_ROOT` is resolved
    // at runtime (and is overridable, so tests can point this at a throwaway
    // tree), which Turbopack cannot scope statically — so it conservatively
    // traces the WHOLE project into the server bundle and warns on every build.
    // The path is confined to cv_output/ by the check immediately below, so
    // there is nothing here for the tracer to protect. Turbopack documents this
    // comment as the opt-out.
    root = realpathSync(/* turbopackIgnore: true */ CV_OUTPUT_ROOT);
  } catch {
    // The output tree has not been created yet: nothing can be served from it.
    return null;
  }

  const rel = path.relative(root, resolved);
  if (rel === "" || rel.startsWith("..") || path.isAbsolute(rel)) return null;

  try {
    if (!statSync(resolved).isFile()) return null;
  } catch {
    return null;
  }
  return resolved;
}

const CONTENT_TYPES: Record<string, string> = {
  ".pdf": "application/pdf",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".md": "text/markdown; charset=utf-8",
  ".yaml": "text/yaml; charset=utf-8",
  ".yml": "text/yaml; charset=utf-8",
  ".json": "application/json; charset=utf-8",
};

export function contentTypeFor(filePath: string): string {
  return CONTENT_TYPES[path.extname(filePath).toLowerCase()] ?? "application/octet-stream";
}
