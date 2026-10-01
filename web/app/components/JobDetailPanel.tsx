import DOMPurify from "dompurify";
import {
  CLEARANCE_LABEL, formatGBP, IR35_LABEL, LANGUAGE_TIER_HINT, LANGUAGE_TIER_LABEL,
  MY_STATUS_LABEL, MY_STATUS_VALUES, RATE_SOURCE_LABEL, RATE_VERDICT_LABEL,
} from "@/app/constants";
import {DatedJob, SaveState} from "@/app/types";
import StatusBadge from "@/app/components/StatusBadge";
import {formatDescription} from "@/lib/formatDescription";

type JobDetailPanelProps = {
  job: DatedJob;
  draftStatus: string;
  onDraftStatusChange: (value: string) => void;
  draftNotes: string;
  onDraftNotesChange: (value: string) => void;
  saveState: SaveState;
  onSave: () => void;
  onClose: () => void;
  /** Opens the tailoring overlay. The panel does not own its state: a
   *  tailored CV belongs to the URL, not to this panel, and closing the panel
   *  mid-draft must not discard it. */
  onScanCv: () => void;
  /** The board row for the advert this one is a re-advert of, when that row is
   *  on the board. Null when this row is the original, or when the original was
   *  filtered out and so has no row to open — the notice then shows the url as
   *  text instead of offering a button that would go nowhere. */
  duplicateOriginal: DatedJob | null;
  /** The board row the inherited status was copied from. Not necessarily the
   *  same row as duplicateOriginal: the decision can sit on any advert of the
   *  job, and it is the one you judged that matters. */
  inheritedSource: DatedJob | null;
  onOpenJob: (job: DatedJob) => void;
};

/** What the CV button's chip says for each ATS-scan verdict.
 *
 *  "unknown" is deliberately its own tone rather than folded into "warn": the
 *  scan could not read the posting, which is a fact about the stored advert and
 *  not a judgement about the CV. Dressing it as a warning beside a real
 *  borderline would train the reader to ignore both. */
function atsStateLabel(
  verdict: string | null,
  scannedAt: string | null,
): { label: string; tone: string } | null {
  if (!scannedAt) return null;
  if (verdict === "pass") return { label: "CV would pass", tone: "ok" };
  if (verdict === "borderline") return { label: "CV borderline", tone: "warn" };
  if (verdict === "fail") return { label: "CV would be filtered", tone: "bad" };
  return { label: "CV scan inconclusive", tone: "unev" };
}

export default function JobDetailPanel({
  job, draftStatus, onDraftStatusChange, draftNotes, onDraftNotesChange, saveState, onSave,
  onClose, onScanCv, duplicateOriginal, inheritedSource, onOpenJob,
}: JobDetailPanelProps) {
  const isContract = job.employment_type === "contract";
  const hasRate = job.day_rate_max != null || job.day_rate_min != null;
  const ats = atsStateLabel(job.cv_ats_verdict, job.cv_ats_updated_at);
  // Only offer the file links when the files are actually there.
  const rateText = hasRate
    ? job.day_rate_min != null && job.day_rate_min !== job.day_rate_max
      ? `${formatGBP(job.day_rate_min)} – ${formatGBP(job.day_rate_max)} per day`
      : `${formatGBP(job.day_rate_max)} per day`
    : null;

  return (
    <div id="overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div id="panel">
        <div id="panel-header">
          <button id="panel-close" onClick={onClose} aria-label="Close">&times;</button>
          <h2>{job.title}</h2>
          <div className="panel-meta">
            {job.company} · {job.location} · <StatusBadge status={job.status} />
            {job.match_score != null && ` · score ${job.match_score}`}
            {isContract && ` · Contract${job.ir35_status && job.ir35_status !== "unknown" ? ` · ${IR35_LABEL[job.ir35_status] ?? job.ir35_status}` : ""}`}
          </div>
          <div className="panel-meta" style={{marginTop: 6}}>
            Posted {job.age.label}
            {job.age.date && ` — ${job.age.exact}`}
          </div>
          {/* Saying this in the panel is the point of the whole feature: the way
              you found out before was to open the advert on LinkedIn and read
              "Applied". */}
          {job.duplicate_of && (
            <div className="panel-meta dup-note" style={{marginTop: 6}}>
              <span className="chip dup-chip">re-advert</span>{" "}
              the same job is already stored under another advert
              {duplicateOriginal ? (
                <>
                  {" — "}
                  <button className="linklike" onClick={() => onOpenJob(duplicateOriginal)}>
                    {duplicateOriginal.company}
                    {duplicateOriginal.age.label ? ` · ${duplicateOriginal.age.label}` : ""}
                  </button>
                </>
              ) : (
                <span className="contract-evidence"> ({job.duplicate_of})</span>
              )}
            </div>
          )}
        </div>
        <div id="panel-body">
          {(job.language_tier || job.clearance_status) && (
            <>
              <h3>Screening</h3>
              <div className="contract-block">
                <div className="contract-row">
                  <span className="contract-key">Language fit</span>
                  <span>
                    <span className={`chip lang-${job.language_tier}`}>
                      {LANGUAGE_TIER_LABEL[job.language_tier ?? ""] ?? job.language_tier}
                    </span>
                    <span className="contract-evidence">
                      {" "}{LANGUAGE_TIER_HINT[job.language_tier ?? ""] ?? ""}
                      {job.language_hits ? ` — matched: ${job.language_hits}` : ""}
                    </span>
                  </span>
                </div>
                <div className="contract-row">
                  <span className="contract-key">Security clearance</span>
                  <span>
                    {job.clearance_status && job.clearance_status !== "none" ? (
                      <>
                        <span className={`chip clearance-${job.clearance_status}`}>
                          {CLEARANCE_LABEL[job.clearance_status] ?? job.clearance_status}
                        </span>
                        <span className="contract-evidence"> {job.clearance_evidence ?? ""}</span>
                      </>
                    ) : (
                      <span className="contract-evidence">Not mentioned in the posting.</span>
                    )}
                  </span>
                </div>
              </div>
            </>
          )}

          {isContract && (
            <>
              <h3>Contract terms</h3>
              <div className="contract-block">
                <div className="contract-row">
                  <span className="contract-key">Employment</span>
                  <span>Contract</span>
                </div>
                <div className="contract-row">
                  <span className="contract-key">IR35 status</span>
                  <span>
                    {IR35_LABEL[job.ir35_status ?? "unknown"] ?? job.ir35_status ?? "unknown"}
                    {job.ir35_evidence && <span className="contract-evidence"> — read from &ldquo;{job.ir35_evidence}&rdquo;</span>}
                  </span>
                </div>
                <div className="contract-row">
                  <span className="contract-key">Day rate</span>
                  <span>
                    {rateText ?? <em>not stated in the posting</em>}
                    {rateText && job.day_rate_source && (
                      <span className="contract-evidence"> ({RATE_SOURCE_LABEL[job.day_rate_source] ?? job.day_rate_source})</span>
                    )}
                  </span>
                </div>
                <div className="contract-row">
                  <span className="contract-key">Equivalent salary</span>
                  <span>
                    {job.perm_equivalent != null ? `≈ ${formatGBP(job.perm_equivalent)}` : "—"}
                    {job.rate_required != null && (
                      <span className="contract-evidence"> (floor for this status: {formatGBP(job.rate_required)}/day)</span>
                    )}
                  </span>
                </div>
                {job.rate_verdict && (
                  <div className="contract-row">
                    <span className="contract-key">Verdict</span>
                    <span className={`chip rate-${job.rate_verdict}`}>
                      {RATE_VERDICT_LABEL[job.rate_verdict] ?? job.rate_verdict}
                    </span>
                  </div>
                )}
                {job.rate_reason && <div className="contract-reason">{job.rate_reason}</div>}
              </div>
            </>
          )}

          <h3>My status</h3>
          <div className="panel-status-row">
            <select value={draftStatus} onChange={(e) => onDraftStatusChange(e.target.value)}>
              <option value="">— not set —</option>
              {MY_STATUS_VALUES.map((v) => (
                <option key={v} value={v}>{MY_STATUS_LABEL[v]}</option>
              ))}
            </select>
          </div>
          {/* A status the tool copied across from another advert of this job is
              NOT the same claim as one you set, and presenting it as yours would
              be the kind of quiet half-truth this system exists to avoid. Saving
              any status here clears the inherited marker — after that it IS your
              decision. See app/duplicates.py. */}
          {job.status_inherited_from && (
            <div className="inherit-note">
              <strong>{MY_STATUS_LABEL[job.my_status ?? ""] ?? job.my_status}</strong> was
              inherited from another advert of this same job, not set here.{" "}
              {inheritedSource ? (
                <button className="linklike" onClick={() => onOpenJob(inheritedSource)}>
                  Open that advert
                </button>
              ) : (
                <span className="contract-evidence">{job.status_inherited_from}</span>
              )}
              {" "}Saving a status below makes it yours.
            </div>
          )}

          <h3>Notes</h3>
          <textarea
            value={draftNotes}
            onChange={(e) => onDraftNotesChange(e.target.value)}
            placeholder="e.g.: applied via referral, follow up by Friday"
          />

          <div className="panel-actions">
            <button className="btn" onClick={onSave}>Save</button>
            {saveState === "saving" && <span className="save-status">Saving...</span>}
            {saveState === "saved" && <span className="save-status">✓ Saved</span>}
            {saveState === "error" && <span className="save-status">Error</span>}
            <a className="btn secondary" href={job.url} target="_blank" rel="noopener noreferrer">
              Open job ↗
            </a>
          </div>

          <h3>Master CV</h3>
          <div className="cv-row">
            <button className="btn" onClick={onScanCv}>
              {job.cv_ats_updated_at ? "Open CV scan" : "Scan CV for this job"}
            </button>
            {ats && <span className={`chip cv-${ats.tone}`}>{ats.label}</span>}
          </div>
          {ats ? (
            <p className="tailor-note">
              {job.cv_ats_coverage != null && (
                <>Priority-term coverage <strong>{job.cv_ats_coverage}%</strong>. </>
              )}
              {job.cv_ats_hard_gaps ? (
                <>{job.cv_ats_hard_gaps} requirement-level hard skill
                  {job.cv_ats_hard_gaps === 1 ? "" : "s"} the CV does not mention. </>
              ) : null}
              {job.cv_ats_suggestions ? (
                <>{job.cv_ats_suggestions} piece{job.cv_ats_suggestions === 1 ? "" : "s"} of master
                  experience worth adding. </>
              ) : null}
              {job.cv_ats_projected != null && job.cv_ats_projected !== job.cv_ats_coverage && (
                <>Adding all of them would take it to {job.cv_ats_projected}%. </>
              )}
            </p>
          ) : (
            <p className="tailor-note">
              Scans the master CV against this posting with no model call: whether a keyword screen
              would find what the advert asks for, and which real experience the CV is not showing.
            </p>
          )}

          {/* Older per-posting tailored CVs, kept reachable.
              Per-posting tailoring was retired (2026-10-01) because the CV that
              gets sent is now the single master document. The renders it
              produced are still on disk and still in the database, though, and
              silently dropping the link to a CV you may have already sent would
              lose work rather than remove a feature. So it is shown when — and
              only when — one really exists. */}
          {job.cv_status === "rendered" && job.cv_files_present && job.cv_pdf_path && (
            <p className="tailor-note">
              A tailored CV was rendered for this job before tailoring was retired.{" "}
              <a className="link-btn" href={`/api/file?path=${encodeURIComponent(job.cv_pdf_path)}`}
                 target="_blank" rel="noopener noreferrer">Open PDF ↗</a>
              {job.cv_docx_path && (
                <a className="link-btn" href={`/api/file?path=${encodeURIComponent(job.cv_docx_path)}`}
                   target="_blank" rel="noopener noreferrer">DOCX ↗</a>
              )}
            </p>
          )}

          {job.match_score != null && (
            <>
              <h3>Transferable strengths</h3>
              <div>{job.transferable_strengths || "—"}</div>
              <h3>Genuine gaps</h3>
              <div>{job.genuine_gaps || "—"}</div>
              <h3>Risk factors</h3>
              <div>{job.risk_factors || "—"}</div>
            </>
          )}

          <h3>Job description</h3>
          {job.description ? (
            (() => {
              const formatted = formatDescription(job.description);
              return formatted.kind === "html"
                ? <div className="desc" dangerouslySetInnerHTML={{__html: DOMPurify.sanitize(formatted.html)}} />
                : <div className="desc desc-plain">{formatted.text}</div>;
            })()
          ) : (
            <div className="desc"><em>No description.</em></div>
          )}
        </div>
      </div>
    </div>
  );
}
