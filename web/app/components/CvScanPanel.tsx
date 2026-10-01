"use client";

import {useEffect, useMemo, useState} from "react";
import {DatedJob, ScanPayload, ScanResult, ScanSuggestion, ScanTerm} from "@/app/types";

/**
 * The ATS scan of the master CV against one posting.
 *
 * This replaces the old tailoring overlay. The CV that gets sent is ONE
 * document, kept in Google Docs, so "draft me a CV for this posting" was the
 * wrong question: it produced a rewritten CV per application that nobody was
 * going to maintain. The two questions actually worth asking per posting are
 * answered here instead, and neither costs a model call:
 *
 *   1. Would the CV I already have pass this posting's screening?
 *   2. Is there real experience I hold that the CV does not currently show?
 *
 * The panel is built around one rule: never present "cannot tell" as a verdict.
 * A scan that could not read the posting says so and explains why, because a
 * green tick on an unread advert is the single most damaging thing this screen
 * could do — it would send someone to an interview believing they had passed a
 * filter that was never applied.
 */

type Props = {
  job: DatedJob;
  stored: ScanPayload | null;
  /** The url a "scan the fuller copy" click asked for that turned out not to be
   *  on the board. Reported rather than swallowed: the click would otherwise do
   *  nothing at all, which reads as a broken button. */
  switchError?: string | null;
  onClose: () => void;
  /** Re-run the board query after a scan, so its summary columns refresh. */
  onScanned: () => void;
  /** Jump the overlay to another advert of the same job — used when THIS
   *  copy's description is a snippet and a fuller one is on the board. */
  onSwitchJob: (url: string) => void;
};

type Phase = "idle" | "scanning" | "result" | "error";

const VERDICT_LABEL: Record<string, string> = {
  pass: "Would pass",
  borderline: "Borderline",
  fail: "Would be filtered out",
  unknown: "Cannot tell",
};

const VERDICT_CLASS: Record<string, string> = {
  pass: "v-pass",
  borderline: "v-borderline",
  fail: "v-fail",
  unknown: "v-unknown",
};

const VERDICT_ADVICE: Record<string, string> = {
  pass: "Nothing on this posting is missing from the CV. Apply as you are.",
  borderline: "No single requirement is missing, but the CV is thinner on this posting's vocabulary than is comfortable. Adding what is suggested below is the cheap fix.",
  fail: "A keyword filter looking for these terms would not find them. Adding the experience below is what changes that — and if it genuinely is not in the master, you do not have it and this is a real gap.",
  unknown: "The posting could not be read well enough to judge. This is a fact about the stored advert, not about the CV — see the note above for what to do about it.",
};

/** How the master CV's own text was obtained. Shown on every scan, because
 *  which copy was read is part of what the verdict means. */
function sourceLine(result: ScanResult | null | undefined): {label: string; warn: boolean} {
  const source = result?.cv_source;
  if (!source || !source.kind) return {label: "Master CV: unknown source", warn: true};
  const when = source.fetched_at ? new Date(source.fetched_at).toLocaleString("en-GB") : null;
  if (source.kind === "service_account") {
    return {label: `Master CV: read from the Google Doc${when ? ` at ${when}` : ""}`, warn: false};
  }
  if (source.kind === "export") {
    return {
      label: `Master CV: read from the Doc's public text export${when ? ` at ${when}` : ""} — the Doc is link-shared, so it is readable by anyone with the URL`,
      warn: true,
    };
  }
  const age = source.age_hours != null ? `${source.age_hours}h old` : "age unknown";
  // A cached copy still says WHERE it originally came from, because that is the
  // question a reader cares about: "is my CV being read out of a document anyone
  // with the link can open?" does not stop mattering because the copy in hand is
  // ten minutes old.
  const via = source.cached_from === "service_account"
    ? " via the service account"
    : source.cached_from === "export"
      ? " via the Doc's public text export"
      : "";
  // `stale` is the claim, not the source. A cache inside its TTL is the copy
  // this scan is supposed to use — warning on every cached read is crying wolf
  // on every ordinary run, and trains the reader to ignore the one time it
  // matters.
  return {
    label: `Master CV: cached copy${via}, ${age}${source.reason ? ` — could not refresh (${source.reason})` : ""}`,
    warn: source.stale === true || source.cached_from === "export",
  };
}

function TermChips({terms, tone}: {terms: ScanTerm[]; tone: string}) {
  if (!terms.length) return null;
  return (
    <div className="scan-chips">
      {terms.map((t) => (
        <span key={t.term} className={`chip scan-term ${tone}${t.skill ? " is-skill" : ""}`}
              title={`${t.section}${t.skill ? " · recognised skill" : ""} · weight ${t.weight}`}>
          {t.term}
        </span>
      ))}
    </div>
  );
}

function CopyButton({text}: {text: string}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const id = setTimeout(() => setCopied(false), 2000);
    return () => clearTimeout(id);
  }, [copied]);

  return (
    <button
      className="btn secondary scan-copy"
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
        } catch {
          // Clipboard access needs a secure context; on plain http the API is
          // absent. Selecting the text is the fallback that always works, so
          // the panel does not need to offer a broken button instead.
          const range = document.createRange();
          const node = document.querySelector(`[data-copy="${CSS.escape(text.slice(0, 24))}"]`);
          if (node) {
            range.selectNodeContents(node);
            const sel = window.getSelection();
            sel?.removeAllRanges();
            sel?.addRange(range);
          }
          setCopied(true);
        }
      }}
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function SuggestionBlock({s, kind}: {s: ScanSuggestion; kind: "add_achievement" | "reword_achievement"}) {
  const terms = s.closes_terms.map((t) => t.term);
  return (
    <div className={`scan-suggestion ${kind === "add_achievement" ? "is-add" : "is-reword"}`}>
      <div className="scan-suggestion-head">
        <span className="scan-suggestion-where">
          {s.company}{s.role_title ? ` · ${s.role_title}` : ""}
          {s.achievement_id ? <> · <code>{s.achievement_id}</code></> : null}
        </span>
        <CopyButton text={s.text} />
      </div>
      <blockquote className="scan-suggestion-text" data-copy={s.text.slice(0, 24)}>{s.text}</blockquote>
      <div className="scan-suggestion-why">
        {kind === "add_achievement"
          ? "Not on the CV at all — this is the one to add."
          : "The fact is already on the CV, but not in this posting's words. Reword the existing bullet."}
        {" Names: "}
        <strong>{terms.slice(0, 6).join(", ")}{terms.length > 6 ? ` +${terms.length - 6}` : ""}</strong>
      </div>
    </div>
  );
}

export default function CvScanPanel({job, stored, switchError, onClose, onScanned, onSwitchJob}: Props) {
  const initial: ScanResult | null = stored?.result ?? null;
  const [scan, setScan] = useState<ScanResult | null>(initial);
  const [phase, setPhase] = useState<Phase>(initial ? "result" : "idle");
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);

  // Reopening on a different advert must not show the previous job's scan.
  // Keyed on the job's url rather than on mount, because the overlay stays
  // mounted while the panel's `job` prop changes when the user follows a
  // "scan the fuller copy" link.
  const [loadedFor, setLoadedFor] = useState(job.url);
  if (loadedFor !== job.url) {
    setLoadedFor(job.url);
    setScan(stored?.result ?? null);
    setPhase(stored?.result ? "result" : "idle");
    setError(null);
  }

  const busy = phase === "scanning";
  useEffect(() => {
    if (!busy) return;
    setElapsed(0);
    const id = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [busy]);

  async function run(refreshMaster: boolean) {
    setPhase("scanning");
    setError(null);
    try {
      const res = await fetch("/api/cv/scan", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({url: job.url, refresh_master: refreshMaster}),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      setScan(data as ScanResult);
      setPhase("result");
      onScanned();
    } catch (e: any) {
      setError(e.message);
      setPhase("error");
    }
  }

  const ats = scan?.ats;
  const verdict = ats?.verdict ?? "unknown";
  const source = useMemo(() => sourceLine(scan), [scan]);
  const adds = (scan?.suggestions ?? []).filter((s) => s.kind === "add_achievement");
  const rewords = (scan?.suggestions ?? []).filter((s) => s.kind !== "add_achievement");

  return (
    <div id="overlay" className="tailor-overlay"
         onClick={(e) => { if (e.target === e.currentTarget && !busy) onClose(); }}>
      <div id="panel" className="tailor-panel scan-panel">
        <div id="panel-header">
          <button id="panel-close" onClick={onClose} aria-label="Close" disabled={busy}>&times;</button>
          <h2>CV scan</h2>
          <div className="panel-meta">
            {job.company} · {job.title}
            {job.match_score != null && ` · AI score ${job.match_score}`}
          </div>
          <div className={`panel-meta scan-source ${source.warn ? "is-warn" : ""}`}>{source.label}</div>
        </div>

        <div id="panel-body">
          {phase === "idle" && (
            <>
              <h3>Does my CV pass this posting?</h3>
              <p>
                The scan reads the master CV and this posting and reports two things: whether a
                keyword screen would find what the advert asks for, and which pieces of experience
                already in <code>cv/master.yaml</code> the CV does not currently show.
              </p>
              <p className="tailor-note">
                No model call, no cost, and nothing is written to the CV. Every suggestion is quoted
                verbatim from the master — the scan cannot invent experience you do not have.
              </p>
              <div className="panel-actions">
                <button className="btn" onClick={() => run(false)}>Run the scan</button>
                <button className="btn secondary" onClick={onClose}>Cancel</button>
              </div>
            </>
          )}

          {phase === "scanning" && (
            <>
              <h3>Scanning…</h3>
              <p className="tailor-progress">
                <span className="tailor-spinner" /> {elapsed}s — reading the master CV and this
                posting. This is keyword arithmetic, not a model call, so it is normally under a
                second; the only slow part is fetching the Doc if the cached copy has expired.
              </p>
            </>
          )}

          {phase === "error" && (
            <>
              <h3>Could not scan</h3>
              <pre className="tailor-error">{error}</pre>
              <p className="tailor-note">
                Nothing was written. The usual causes are an unconfigured master CV Doc (see{" "}
                <code>MASTER_CV_DOC_URL</code> in <code>.env</code>) or a posting with no stored text.
              </p>
              <div className="panel-actions">
                <button className="btn" onClick={() => run(false)}>Try again</button>
                <button className="btn secondary" onClick={onClose}>Close</button>
              </div>
            </>
          )}

          {phase === "result" && scan && (
            <>
              {!ats?.reliable ? (
                <div className="scan-verdict v-unknown">
                  <div className="scan-verdict-head">
                    <span className="scan-verdict-label">{VERDICT_LABEL.unknown}</span>
                  </div>
                  <p className="scan-verdict-reason">{ats?.reason}</p>
                  <p className="scan-verdict-reason scan-dim">
                    The stored description for this advert is {ats?.posting_chars} characters long.
                    A real advert is several thousand.
                  </p>
                  {scan.better_source && (
                    <div className="panel-actions">
                      <button className="btn" onClick={() => onSwitchJob(scan.better_source!.url)}>
                        Scan the fuller copy from {scan.better_source.source} ({scan.better_source.chars} chars)
                      </button>
                    </div>
                  )}
                  {switchError && (
                    <p className="tailor-note scan-switch-error">
                      That copy is no longer on the board, so there is nothing to switch to — it
                      has been filtered out or removed since this scan was taken. Scan this advert
                      again, or add the fuller copy with <code>python -m app.add_job</code>.
                    </p>
                  )}
                </div>
              ) : (
                <div className={`scan-verdict ${VERDICT_CLASS[verdict] ?? "v-unknown"}`}>
                  <div className="scan-verdict-head">
                    <span className="scan-verdict-label">{VERDICT_LABEL[verdict] ?? verdict}</span>
                    <span className="scan-verdict-cov">
                      {ats.priority_coverage}% of requirement-level terms
                      <span className="scan-dim"> ({ats.priority_evidenced}/{ats.priority_total})</span>
                    </span>
                  </div>
                  <p className="scan-verdict-reason">{ats.reason}</p>
                  <p className="scan-verdict-reason scan-dim">{VERDICT_ADVICE[verdict] ?? ""}</p>
                </div>
              )}

              {ats?.reliable && ats.hard_gaps.length > 0 && (
                <>
                  <h3>Requirement-level hard skills the CV does not mention</h3>
                  <TermChips terms={ats.hard_gaps} tone="is-hard" />
                </>
              )}

              {ats?.reliable && (
                <>
                  <h3>Coverage</h3>
                  <p className="scan-coverage">
                    <span className="scan-num">{ats.priority_coverage}%</span> priority-term coverage
                    <span className="scan-dim"> — {ats.priority_evidenced} of {ats.priority_total}
                      {" "}requirement-level terms are evidenced</span>
                  </p>
                  <p className="tailor-note">
                    Priority-term coverage is the figure comparable between postings — the same
                    measurement <code>scripts/gap.py</code> produces. This posting yielded{" "}
                    {ats.terms_found} terms, of which {ats.priority_total} are requirement-level.
                  </p>
                </>
              )}

              {(scan.roles_missing_from_cv?.length ?? 0) > 0 && (
                <>
                  <h3>A whole role is missing from the CV</h3>
                  {scan.roles_missing_from_cv.map((r) => (
                    <div key={r.id ?? r.company ?? "?"} className="scan-role">
                      <strong>{r.company}</strong> — {r.title}
                      <span className="scan-dim"> · {r.start} to {r.end} · {r.achievements} achievement{r.achievements === 1 ? "" : "s"} in the master</span>
                      <div className="scan-role-note">
                        Your employer&rsquo;s name does not appear anywhere on the CV. If this role is
                        simply not on it, that is the largest single thing the scan can find.
                      </div>
                    </div>
                  ))}
                </>
              )}

              {adds.length > 0 && (
                <>
                  <h3>Add to the CV ({adds.length})</h3>
                  <p className="tailor-note">
                    Real experience from <code>cv/master.yaml</code> that is not on the CV, and which
                    this posting asks for. Each is quoted word-for-word from the master.
                  </p>
                  {adds.map((s) => <SuggestionBlock key={s.achievement_id} s={s} kind="add_achievement" />)}
                </>
              )}

              {rewords.length > 0 && (
                <>
                  <h3>Already on the CV — name it in their words ({rewords.length})</h3>
                  <p className="tailor-note">
                    These facts are already on the CV, so there is nothing to add. The posting uses
                    different vocabulary for them, and the fix is a reword of the bullet you already
                    have rather than a new claim.
                  </p>
                  {rewords.map((s) => <SuggestionBlock key={s.achievement_id} s={s} kind="reword_achievement" />)}
                </>
              )}

              {ats?.reliable && (scan.unclosable?.length ?? 0) > 0 && (
                <>
                  <h3>Not closable — you do not have these</h3>
                  <p className="tailor-note">
                    The posting asks for these and nothing in <code>cv/master.yaml</code> evidences
                    them, so no edit to the CV would honestly fix it. These are the real gaps, and
                    the ones worth an answer in an interview.
                  </p>
                  <TermChips terms={scan.unclosable} tone="is-real-gap" />
                </>
              )}

              {ats?.reliable && (scan.suggestions?.length ?? 0) > 0 && (
                <div className="scan-projection">
                  <strong>If you added every line above:</strong> coverage goes{" "}
                  {ats.priority_coverage}% → <strong>{scan.projection.coverage}%</strong>
                  {scan.projection.verdict && scan.projection.verdict !== verdict && (
                    <> and the verdict becomes <strong>{VERDICT_LABEL[scan.projection.verdict] ?? scan.projection.verdict}</strong></>
                  )}
                  {scan.projection.verdict === verdict && <> — the verdict does not change</>}
                  . {scan.projection.note}
                </div>
              )}

              {ats?.reliable && ats.priority_gaps.length > 0 && (
                <details className="tailor-details">
                  <summary>All requirement-level gaps ({ats.priority_gaps.length})</summary>
                  <TermChips terms={ats.priority_gaps} tone="is-gap" />
                </details>
              )}

              {ats?.reliable && ats.evidenced_priorities.length > 0 && (
                <details className="tailor-details">
                  <summary>What the CV already evidences ({ats.evidenced_priorities.length})</summary>
                  <TermChips terms={ats.evidenced_priorities} tone="is-have" />
                </details>
              )}

              <div className="panel-actions">
                <button className="btn secondary" onClick={() => run(true)} disabled={busy}>
                  Re-read the Doc and scan again
                </button>
                <button className="btn secondary" onClick={onClose}>Close</button>
              </div>
              {stored?.row?.updated_at && (
                <p className="panel-meta scan-dim">
                  Stored scan last updated {stored.row.updated_at}. Re-reading the Doc picks up edits
                  you have made to the CV since.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
