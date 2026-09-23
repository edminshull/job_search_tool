// Tests for the CV artifact file guard. Run with: npm test
//
// This guards a route that takes a filesystem path from the browser, so the
// cases that matter are the ones that must be REFUSED. cv/master.yaml holds a
// real name, email, phone number and employment history, and it lives one
// directory above the tree this route is allowed to serve — so a guard that
// only looks plausible is the difference between a private CV and a public one.
//
// Containment is checked on the RESOLVED path rather than by matching ".." in
// the input string, which is why these tests use traversal, absolute paths and
// symlinks rather than the obvious "../../etc/passwd" alone: a prefix or
// substring check passes the last two.
//
// THE WHOLE FIXTURE LIVES IN A TEMPORARY TREE, pointed at by CV_OUTPUT_ROOT
// before cvTool.ts is imported. This is load-bearing, not tidiness. The first
// version of this file built its fixture inside the real cv_output/ and then
// removed it in a cleanup hook — which destroyed a rendered application CV.
// A test must never create, or delete, anything under the live output tree.
import {test} from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import {mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync, readdirSync, realpathSync,
        existsSync, readFileSync} from "node:fs";
import {tmpdir} from "node:os";

const FIXTURE_ROOT = mkdtempSync(path.join(tmpdir(), "cv-guard-test-"));
// Set BEFORE the module under test is imported: CV_OUTPUT_ROOT is a module-level
// const, so it is read once at import time.
process.env.CV_OUTPUT_ROOT = path.join(FIXTURE_ROOT, "cv_output");

const {CV_OUTPUT_ROOT, CV_ROOT, CV_TOOL_TIMEOUT_MS, contentTypeFor, resolveServableFile} =
  await import("./cvTool.ts");

const inside = path.join(CV_OUTPUT_ROOT, "23-09-26", "acme");
mkdirSync(inside, {recursive: true});
const realCv = path.join(inside, "Ed-Minshull-CV.pdf");
writeFileSync(realCv, "%PDF-1.4 fixture");

// A directory OUTSIDE the output tree but with a name that shares a prefix with
// it. `startsWith(CV_OUTPUT_ROOT)` would accept this one; path.relative does not.
const siblingDir = path.join(path.dirname(CV_OUTPUT_ROOT), "cv_output-x");
mkdirSync(siblingDir, {recursive: true});
const siblingFile = path.join(siblingDir, "secret.pdf");
writeFileSync(siblingFile, "not yours");

// A symlink inside the allowed tree pointing at something outside it. Nothing
// about the requested path string is suspicious; only the resolved target is.
// The target is a real file that exists but must never be served.
const outsideTarget = path.join(FIXTURE_ROOT, "outside-target.yaml");
writeFileSync(outsideTarget, "private: true");
const linkPath = path.join(CV_OUTPUT_ROOT, "escape-link");
let linkMade = false;
try {
  symlinkSync(outsideTarget, linkPath);
  linkMade = true;
} catch {
  // Symlinks are unavailable under some sandboxes; the other cases still hold.
}

test("the subprocess ceiling stays above the model call's own timeout", () => {
  // These two numbers live in different languages — CV_TOOL_TIMEOUT_MS here,
  // LOCAL_LLM_TIMEOUT in app/llm_providers.py — and nothing but this test would
  // notice them crossing. If the outer one is shorter, killing the subprocess
  // does NOT stop the local model: it generates on into a request nobody reads,
  // the GPU stays busy, and the run is lost anyway. Real failure, 2026-09-23: a
  // draft met the model's then-120s ceiling and aborted a legitimate long
  // generation, because that value had been tuned for cloud calls.
  const providers = path.join(CV_ROOT, "app", "llm_providers.py");
  if (!existsSync(providers)) return; // not running from a full checkout
  const source = readFileSync(providers, "utf8");

  const localMatch = source.match(/LOCAL_LLM_TIMEOUT[^)]*"(\d+)"/);
  assert.ok(localMatch, "could not find LOCAL_LLM_TIMEOUT's default in llm_providers.py");
  const modelTimeoutMs = Number(localMatch![1]) * 1000;
  assert.ok(
    CV_TOOL_TIMEOUT_MS > modelTimeoutMs,
    `the subprocess timeout (${CV_TOOL_TIMEOUT_MS}ms) must exceed the local model's ` +
      `(${modelTimeoutMs}ms), or a slow draft is killed while still generating`,
  );

  // The cloud ceiling must also be covered, since a run may call either.
  const deepseekMatch = source.match(/^TIMEOUT = ([\d.]+)/m);
  assert.ok(deepseekMatch, "could not find the DeepSeek TIMEOUT in llm_providers.py");
  assert.ok(CV_TOOL_TIMEOUT_MS > Number(deepseekMatch![1]) * 1000,
    "the subprocess timeout must also exceed the DeepSeek request timeout");
});

test("the fixture is isolated from the real CV output tree", () => {
  // A guard against exactly the bug that motivated this file: if the module
  // under test ever ignores CV_OUTPUT_ROOT again, this fails loudly instead of
  // silently letting the fixture into the real output directory.
  const realRoot = path.join(CV_ROOT, "cv_output");
  assert.notEqual(CV_OUTPUT_ROOT, realRoot, "tests must not use the real cv_output/");
  assert.ok(CV_OUTPUT_ROOT.startsWith(FIXTURE_ROOT), "fixture must live in a temp directory");
  assert.ok(!realRoot.startsWith(FIXTURE_ROOT), "the real tree must be outside the fixture");
});

test("serves a file inside the CV output tree", () => {
  // Compared via realpath: the guard returns the canonical path, and on macOS
  // a temp directory is reached through /var -> /private/var, so the raw
  // fixture path would differ from the answer for a reason that has nothing to
  // do with the guard.
  assert.equal(resolveServableFile(realCv), realpathSync(realCv));
});

test("refuses the master CV, which sits outside the output tree", () => {
  // The whole reason this guard exists. This path exists and is readable.
  const master = path.join(CV_ROOT, "cv", "master.yaml");
  assert.equal(resolveServableFile(master), null);
});
test("refuses traversal out of the tree", () => {
  const escape = path.join(CV_OUTPUT_ROOT, "..", "cv", "master.yaml");
  assert.equal(resolveServableFile(escape), null);
  assert.equal(resolveServableFile(path.join(CV_OUTPUT_ROOT, "..", "..", "etc", "hosts")), null);
});

test("refuses a sibling directory sharing a name prefix with the output tree", () => {
  // "cv_output-x" starts with "cv_output". A prefix test would serve this.
  assert.equal(resolveServableFile(siblingFile), null);
});

test("refuses an absolute path outside the tree", () => {
  assert.equal(resolveServableFile("/etc/hosts"), null);
});

test("refuses the output root itself", () => {
  // A directory is not a CV, and serving one is not a case worth supporting.
  assert.equal(resolveServableFile(CV_OUTPUT_ROOT), null);
});

test("refuses a symlink that resolves outside the tree", {skip: !linkMade}, () => {
  assert.equal(resolveServableFile(linkPath), null);
});

test("refuses a path that does not exist, identically to one out of tree", () => {
  // Same answer for both, so the route cannot be used to probe what exists.
  const missing = path.join(CV_OUTPUT_ROOT, "23-09-26", "acme", "nope.pdf");
  assert.equal(resolveServableFile(missing), null);
  assert.equal(resolveServableFile(""), null);
  assert.equal(resolveServableFile("\0"), null);
});

test("maps extensions to content types the browser can act on", () => {
  assert.equal(contentTypeFor("/x/a.pdf"), "application/pdf");
  assert.equal(contentTypeFor("/x/a.docx"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document");
  assert.equal(contentTypeFor("/x/a.md"), "text/markdown; charset=utf-8");
  // An unknown extension must not be guessed at as text/html — that would make
  // an uploaded file scriptable in the origin.
  assert.equal(contentTypeFor("/x/a.bin"), "application/octet-stream");
});

test.after(() => {
  // The ENTIRE fixture goes, and nothing else. Everything this file created
  // lives under FIXTURE_ROOT, so this cannot reach the real project tree — the
  // bug this cleanup is written around is that the previous version deleted a
  // directory inside the live cv_output/.
  assert.ok(FIXTURE_ROOT.startsWith(tmpdir()), "refusing to clean up outside the temp dir");
  rmSync(FIXTURE_ROOT, {recursive: true, force: true});

  // And prove the live tree was never touched. If a future edit reintroduces
  // the isolation bug, the fixture would have to appear here first.
  const realRoot = path.join(CV_ROOT, "cv_output");
  let leaked: string[] = [];
  try {
    leaked = readdirSync(realRoot).filter((n) => n === "escape-link" || n === "cv_output-x");
  } catch {
    // No output tree at all is also fine — it means nothing was created.
  }
  assert.deepEqual(leaked, [], `test artefacts leaked into the real cv_output/: ${leaked}`);
});
