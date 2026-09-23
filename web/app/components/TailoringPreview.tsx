"use client";

import {useEffect, useMemo, useRef, useState} from "react";
import {CvAttempt, CvPreview, CvRender, CvVerification, DatedJob, StoredTailoring, TailorPhase} from "@/app/types";

type TailoringPreviewProps = {
  job: DatedJob;
  /** The stored record, if this job has been tailored before. */
  stored: StoredTailoring | null;
  onClose: () => void;
  /** Called after a successful render so the board can refresh its row. */
  onRendered: () => void;
};

const VERDICT_LABEL: Record<string, string> = {
  accept: "Accepted — every claim traces to the master CV",
  revise: "Needs changes — see what the verifier flagged",
  reject: "Rejected — something in it is not in the master CV",
  unknown: "Not verified — the check returned nothing, so this draft is unchecked",
};

/** What the verdict means you should DO. A verdict with no next step reads as a
 *  dead end, which is how a rejected draft was first reported: the findings were
 *  all there, but the headline said "Rejected" and nothing said the draft was
 *  still editable. */
const VERDICT_ADVICE: Record<string, string> = {
  accept: "Nothing flagged. Read it once, then approve it.",
  revise:
    "Fix the flagged points below and approve, or press Regenerate to retry. The YAML is editable.",
  reject:
    "Do NOT send this as-is. The flagged claims would not survive an interview. Press Regenerate, or edit the flagged wording out of the YAML below and approve that.",
  unknown:
    "This draft has not been independently checked, so read the YAML critically before approving.",
};

/** Past-tense forms for the "drafted, then ..." line, written out rather than
 *  built by appending "ed": that produced "reviseed" for the most common
 *  verdict of all. The Python side keeps the same map for the same reason. */
const PAST_VERDICT: Record<string, string> = {
  accept: "accepted",
  revise: "flagged for changes",
  reject: "rejected",
  unknown: "unverified",
};

/** Human names for the drafting failure modes (cv_tailor.FAILURE_MODES). The
 *  keys are the closed set the lesson store accepts; an unknown key falls back
 *  to the raw mode rather than being hidden, because a mode the panel cannot
 *  name is still a thing the drafter was taught. */
const LEARNED_LABEL: Record<string, string> = {
  unsupported_ref: "cites references that are not in the master CV",
  invented_metric: "invents numbers the master does not hold",
  fabricated_claim: "rewrites a bullet into something the master does not say",
};

/** The verifier's fields, in the order they should be read. `verdict` and the
 *  model name are handled separately, so they are not listed here. */
const FINDING_FIELDS: {key: keyof CvVerification; label: string; tone: "bad" | "warn" | "info"}[] = [
  {key: "unsupported_refs", label: "References not in the master CV", tone: "bad"},
  {key: "fabricated_claims", label: "Claims the master CV does not support", tone: "bad"},
  {key: "invented_metrics", label: "Numbers not in the master", tone: "bad"},
  {key: "required_fixes", label: "Required fixes", tone: "warn"},
  {key: "missing_must_haves", label: "Requirements it cannot evidence", tone: "info"},
  {key: "notes", label: "Other notes", tone: "info"},
];

/** A finding is only worth rendering if it says something. The schema asks the
 *  model to write "none" when a check is clean, and a wall of "none" buries the
 *  one line that matters.
 *
 *  The model writes the verdict word first and THEN explains it — a real clean
 *  field read "none. No numbers appear anywhere in the draft body...". The old
 *  pattern anchored a full-string match, so that explanation made a clean field
 *  render as a red "Numbers not in the master" flag. The marker now counts only
 *  when it is a complete answer: it ends the string, or is followed by
 *  sentence-ending punctuation. Kept deliberately tight — "No Azure DevOps claim
 *  was made" and "None of the refs are valid" must stay visible, or a real
 *  problem would be hidden rather than shown.
 *
 *  Mirrors cv_tailor._NONE_IS_THE_ANSWER; the two must agree, or the panel and
 *  the escalation critique disagree about what the verifier found. */
function isRealFinding(text: string | undefined): boolean {
  if (!text) return false;
  const t = text.trim();
  if (!t) return false;
  return !/^(?:none|n\/a|no|nothing)\s*[.!]?\s*$|^(?:none|n\/a|nothing)\s*[.!:]\s/i.test(t);
}

function Phase({phase}: {phase: TailorPhase}) {
  const steps: {key: TailorPhase[]; label: string}[] = [
    {key: ["drafting"], label: "1. Draft (local Qwen)"},
    {key: ["review", "rendering", "rendered"], label: "2. Verify (DeepSeek)"},
    {key: ["rendered"], label: "3. Render PDF + DOCX"},
  ];
  return (
    <div className="tailor-phases">
      {steps.map((s) => {
        const done = phase === "rendered" || (s.label.startsWith("1.") && phase !== "drafting");
        const active = s.key.includes(phase);
        return (
          <span key={s.label} className={`tailor-phase${done ? " done" : ""}${active && !done ? " active" : ""}`}>
            {done ? "✓ " : active ? "◐ " : "○ "}{s.label}
          </span>
        );
      })}
    </div>
  );
}

export default function TailoringPreview({job, stored, onClose, onRendered}: TailoringPreviewProps) {
  const [phase, setPhase] = useState<TailorPhase>(stored ? "review" : "idle");
  const [error, setError] = useState<string | null>(null);
  const [errorKind, setErrorKind] = useState<string | null>(null);
  const [preview, setPreview] = useState<CvPreview | null>(null);
  const [rendered, setRendered] = useState<CvRender | null>(null);
  const [yamlText, setYamlText] = useState<string>(stored?.tailored_yaml ?? "");
  const [showReport, setShowReport] = useState(false);
  const [showGap, setShowGap] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [wantInterviewPrep, setWantInterviewPrep] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const startedAt = useRef<number | null>(null);

  const busy = phase === "drafting" || phase === "rendering";

  // A ~90s wait needs a clock, not a spinner. The drafting stage is the local
  // model writing a few KB of YAML at roughly 35 tokens/sec, so the wait is
  // long and entirely normal — an unexplained spinner reads as a hang.
  useEffect(() => {
    if (!busy) return;
    startedAt.current = Date.now();
    setElapsed(0);
    const id = setInterval(() => setElapsed(Math.round((Date.now() - (startedAt.current ?? Date.now())) / 1000)), 1000);
    return () => clearInterval(id);
  }, [busy]);

  const storedVerification = useMemo<CvVerification | null>(() => {
    if (!stored?.verify_notes) return null;
    try {
      const parsed = JSON.parse(stored.verify_notes);
      return {verdict: stored.verify_verdict ?? "unknown", ...parsed};
    } catch {
      return {verdict: stored.verify_verdict ?? "unknown", notes: stored.verify_notes};
    }
  }, [stored]);

  const verification = preview?.verification ?? storedVerification;
  // Drafting provenance: which model drafted, in what order, and why an earlier
  // attempt was set aside. A fresh preview reports it directly, and a stored
  // record now carries what that preview wrote — so reopening a job still shows
  // the cloud re-draft that was made and lost. Deriving it from `draft_model`
  // instead was the bug: that column records only the draft that SURVIVED, so an
  // escalated-then-discarded attempt looked exactly like one that never
  // happened, and the local draft's verdict read as if no fallback had run.
  // Records written before the column existed still fall back to one entry.
  const storedAttempts = useMemo(() => {
    if (!stored?.attempts) return null;
    try {
      const parsed = JSON.parse(stored.attempts);
      return Array.isArray(parsed) && parsed.length ? (parsed as CvAttempt[]) : null;
    } catch {
      return null;
    }
  }, [stored]);
  const attempts = preview?.attempts ?? storedAttempts ?? (stored?.draft_model
    ? [{model: stored.draft_model, verdict: stored.verify_verdict ?? "unknown", reason: null}]
    : []);
  const coverageBefore = preview?.coverage_before ?? rendered?.coverage_before ?? stored?.master_coverage ?? null;
  const coverageAfter = rendered?.coverage_after ?? preview?.coverage_after ?? stored?.tailored_coverage ?? null;

  async function draft(regen = false) {
    setPhase("drafting");
    setError(null);
    setErrorKind(null);
    setRendered(null);
    try {
      const res = await fetch("/api/tailor/preview", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          url: job.url,
          feedback: regen && feedback.trim() ? feedback.trim() : undefined,
          interview_prep: wantInterviewPrep,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || res.statusText);
        setErrorKind(data.kind ?? null);
        setPhase("error");
        return;
      }
      setPreview(data as CvPreview);
      setYamlText((data as CvPreview).tailored_yaml);
      setPhase("review");
      if (regen) setFeedback("");
    } catch (e: any) {
      setError(e.message);
      setPhase("error");
    }
  }

  async function approve() {
    setPhase("rendering");
    setError(null);
    setErrorKind(null);
    try {
      const res = await fetch("/api/tailor/render", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          url: job.url,
          tailored_yaml: yamlText,
          interview_prep: wantInterviewPrep,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || res.statusText);
        setErrorKind(data.kind ?? null);
        setPhase("error");
        return;
      }
      setRendered(data as CvRender);
      setPhase("rendered");
      onRendered();
    } catch (e: any) {
      setError(e.message);
      setPhase("error");
    }
  }

  const edited = preview !== null && yamlText.trim() !== preview.tailored_yaml.trim();

  return (
    <div id="overlay" className="tailor-overlay"
         onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div id="panel" className="tailor-panel">
        <div id="panel-header">
          <button id="panel-close" onClick={onClose} aria-label="Close" disabled={busy}>&times;</button>
          <h2>Tailored CV</h2>
          <div className="panel-meta">
            {job.company} · {job.title}
            {job.match_score != null && ` · score ${job.match_score}`}
          </div>
          <div className="panel-meta" style={{marginTop: 6}}>
            Filing into <code>cv_output/{stored?.day_dir ?? preview?.day_dir ?? "…"}/{stored?.company_slug ?? preview?.company_slug ?? "…"}/</code>
          </div>
          <Phase phase={phase} />
        </div>

        <div id="panel-body">
          {phase === "idle" && (
            <>
              <h3>Draft a CV for this posting</h3>
              <p>
                The local Qwen model selects from <code>cv/master.yaml</code> and rewords what it
                picks into this posting&rsquo;s vocabulary; DeepSeek then independently checks every
                claim against the master. You review the result before anything is written —
                approving is what produces the PDF.
              </p>
              <p className="tailor-note">
                Takes roughly <strong>60–120 seconds</strong>. Nothing is written to disk until you
                approve.
              </p>
              <label className="tailor-check">
                <input type="checkbox" checked={wantInterviewPrep}
                       onChange={(e) => setWantInterviewPrep(e.target.checked)} />
                Also generate interview prep (an extra model call, ~30s)
              </label>
              <div className="panel-actions">
                <button className="btn" onClick={() => draft(false)}>Draft CV</button>
                <button className="btn secondary" onClick={onClose}>Cancel</button>
              </div>
            </>
          )}

          {phase === "drafting" && (
            <>
              <h3>Drafting and verifying…</h3>
              <p className="tailor-progress">
                <span className="tailor-spinner" /> {elapsed}s elapsed — the local model is
                selecting facts and writing the YAML, then DeepSeek checks it.
              </p>
              <p className="tailor-note">
                Measured: the draft stage is ~90s and the verification ~5s. You can leave this
                open; it is not going to time out.
              </p>
            </>
          )}

          {phase === "error" && (
            <>
              <h3>{errorKind === "unsupported_claim" ? "Nothing was rendered — unsupported claim" : "Failed"}</h3>
              <pre className="tailor-error">{error}</pre>
              <p className="tailor-note">
                {errorKind === "unsupported_claim"
                  ? "The renderer's fabrication check refuses to build a CV whose skills the master cannot support. Fix the YAML below, or regenerate."
                  : "No files were written."}
              </p>
              <div className="panel-actions">
                {yamlText && <button className="btn secondary" onClick={() => setPhase("review")}>
                  Back to the draft
                </button>}
                <button className="btn" onClick={() => draft(false)}>Start over</button>
              </div>
            </>
          )}

          {(phase === "review" || phase === "rendering" || phase === "rendered") && (
            <>
              {verification && (
                <>
                  <h3>Independent check</h3>
                  <div className={`tailor-verdict v-${verification.verdict}`}>
                    <strong>{VERDICT_LABEL[verification.verdict] ?? verification.verdict}</strong>
                    {verification.verify_model && (
                      <span className="tailor-note"> — checked by {verification.verify_model}</span>
                    )}
                    <div className="tailor-verdict-advice">
                      {VERDICT_ADVICE[verification.verdict] ?? ""}
                    </div>
                  </div>

                  {/* Provenance. A CV re-drafted in the cloud after a rejection
                      must not read as the local model's work, and the reason the
                      first attempt was set aside is the most useful thing on the
                      screen for judging whether to trust this one. */}
                  {attempts.length > 1 && (
                    <div className="tailor-escalation">
                      {attempts.map((a, i) => (
                        <div key={`${a.model}-${i}`} className="tailor-attempt">
                          <span className={`chip cv-${a.verdict === "accept" ? "ok" : a.verdict === "reject" ? "bad" : "warn"}`}>
                            {i + 1}. {a.model.split(":")[0]}
                          </span>{" "}
                          <span className="tailor-note">
                            {a.reason ?? `drafted, then ${PAST_VERDICT[a.verdict] ?? a.verdict} by the verifier`}
                          </span>
                          {/* The lesson this attempt taught. Shown because the
                              point of the loop is that the local drafter stops
                              repeating a proven mistake — and a saving the user
                              cannot see is indistinguishable from no saving. */}
                          {a.learned?.length ? (
                            <div className="tailor-note" style={{marginLeft: 8}}>
                              Taught the drafter: {a.learned.map((m) => LEARNED_LABEL[m] ?? m).join(", ")}
                              {" — it is told about this on every future draft."}
                            </div>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  )}

                  {FINDING_FIELDS.map(({key, label, tone}) => {
                    const value = verification[key] as string | undefined;
                    if (!isRealFinding(value)) return null;
                    return (
                      <div key={key} className={`tailor-finding f-${tone}`}>
                        <div className="tailor-finding-label">{label}</div>
                        <div className="tailor-finding-body">{value}</div>
                      </div>
                    );
                  })}
                  {preview?.priority_gaps != null && preview.priority_gaps > 0 && (
                    <p className="tailor-note">
                      {preview.priority_gaps} requirement-level term
                      {preview.priority_gaps === 1 ? "" : "s"} in this posting cannot be evidenced by
                      the master at all. These are real gaps in your history, not drafting mistakes —
                      the verifier is explicitly told not to let a re-draft invent them. Adding a
                      real one to <code>cv/master.yaml</code> is the only thing that closes it.
                    </p>
                  )}
                </>
              )}

              <h3>Coverage</h3>
              <div className="tailor-coverage">
                <div>
                  <span className="tailor-note">Master CV</span>
                  <div className="tailor-cov-num">{coverageBefore != null ? `${coverageBefore}%` : "—"}</div>
                </div>
                <div className="tailor-cov-arrow">→</div>
                <div>
                  <span className="tailor-note">This tailored CV</span>
                  <div className="tailor-cov-num">{coverageAfter != null ? `${coverageAfter}%` : "—"}</div>
                </div>
              </div>
              <p className="tailor-note">
                Requirement-level terms the posting names that this CV evidences. Both figures are
                the same measurement, so they are comparable. A tailored CV scoring a little below
                the master is normal: it drops roles and background prose on purpose.
              </p>

              <h3>The tailored CV {edited && <span className="tailor-edited">— edited</span>}</h3>
              <p className="tailor-note">
                This is the YAML that will be rendered. Every <code>ref</code> must be an id in{" "}
                <code>cv/master.yaml</code>; the renderer refuses to build a CV that claims
                anything the master cannot support. Edits are re-checked before rendering.
              </p>
              <textarea className="tailor-yaml" value={yamlText} spellCheck={false}
                        readOnly={busy} onChange={(e) => setYamlText(e.target.value)} rows={18} />

              {phase === "rendered" && rendered && (
                <>
                  <h3>Written</h3>
                  <div className="tailor-done">
                    <div className="tailor-files">
                      <a className="btn" href={`/api/file?path=${encodeURIComponent(rendered.pdf_path)}`}
                         target="_blank" rel="noopener noreferrer">Open CV (PDF) ↗</a>
                      <a className="btn secondary" href={`/api/file?path=${encodeURIComponent(rendered.docx_path)}`}
                         target="_blank" rel="noopener noreferrer">DOCX (to edit) ↗</a>
                      {rendered.gap_report_path && (
                        <a className="btn secondary" href={`/api/file?path=${encodeURIComponent(rendered.gap_report_path)}`}
                           target="_blank" rel="noopener noreferrer">Gap report ↗</a>
                      )}
                      {rendered.interview_prep_path && (
                        <a className="btn secondary" href={`/api/file?path=${encodeURIComponent(rendered.interview_prep_path)}`}
                           target="_blank" rel="noopener noreferrer">Interview prep ↗</a>
                      )}
                    </div>
                    <div className="tailor-note" style={{marginTop: 8}}>
                      Filed in <code>{rendered.outdir}</code>. Re-rendering later archives the
                      previous version into <code>archived/</code> rather than overwriting it.
                    </div>
                    {rendered.warnings.length > 0 && (
                      <div className="tailor-finding f-warn" style={{marginTop: 10}}>
                        <div className="tailor-finding-label">Renderer warnings</div>
                        <div className="tailor-finding-body">{rendered.warnings.join("\n")}</div>
                      </div>
                    )}
                  </div>
                </>
              )}

              {(preview || stored) && (
                <div className="panel-actions">
                  {phase !== "rendered" && (
                    <>
                      <button className="btn" onClick={approve} disabled={busy}>
                        {busy ? `Rendering… ${elapsed}s` : "Approve & render"}
                      </button>
                      <button className="btn secondary" onClick={() => draft(true)} disabled={busy}>
                        Regenerate
                      </button>
                    </>
                  )}
                  {phase === "rendered" && (
                    <button className="btn secondary" onClick={() => draft(true)} disabled={busy}>
                      Regenerate
                    </button>
                  )}
                  <label className="tailor-check">
                    <input type="checkbox" checked={wantInterviewPrep}
                           onChange={(e) => setWantInterviewPrep(e.target.checked)} disabled={busy} />
                    Interview prep
                  </label>
                </div>
              )}

              {phase !== "rendered" && (
                <details className="tailor-details" open={edited}>
                  <summary>Regenerate with feedback (optional)</summary>
                  <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)} rows={3}
                            placeholder="e.g. lead with the CI/CD migration, and fix what the verifier flagged about Playwright"
                            disabled={busy} />
                  <div className="tailor-note">
                    Left blank, regenerating simply re-runs the draft. Filled in, it is passed to the
                    model as corrections to apply.
                  </div>
                </details>
              )}

              {(preview?.report || stored?.report) && (
                <details className="tailor-details" open={showReport}
                         onToggle={(e) => setShowReport((e.target as HTMLDetailsElement).open)}>
                  <summary>What the model changed and dropped</summary>
                  <pre className="tailor-pre">{preview?.report || stored?.report}</pre>
                </details>
              )}

              {preview?.gap_report && (
                <details className="tailor-details" open={showGap}
                         onToggle={(e) => setShowGap((e.target as HTMLDetailsElement).open)}>
                  <summary>Full gap report</summary>
                  <pre className="tailor-pre">{preview.gap_report}</pre>
                </details>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
