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
  onTailorCv: () => void;
};

/** What the CV button and its chip say for each tailoring state. */
function cvStateLabel(
  status: string | null,
  verdict: string | null,
  filesPresent: boolean,
): { label: string; tone: string } | null {
  if (!status) return null;
  if (status === "rendered") {
    // The database claims a render but the file is not on disk. Say that
    // plainly rather than offering a link that 404s — the row's own record is
    // wrong, and re-rendering is the fix.
    if (!filesPresent) return { label: "CV file missing — re-render", tone: "bad" };
    return verdict === "accept"
      ? { label: "CV ready", tone: "ok" }
      : { label: verdict === "reject" ? "CV ready (claims rejected)" : "CV ready (review notes)", tone: verdict === "reject" ? "bad" : "warn" };
  }
  if (status === "draft") return { label: "CV drafted, not rendered", tone: "warn" };
  if (status === "failed") return { label: "CV tailoring failed", tone: "bad" };
  return null;
}

export default function JobDetailPanel({
  job, draftStatus, onDraftStatusChange, draftNotes, onDraftNotesChange, saveState, onSave,
  onClose, onTailorCv,
}: JobDetailPanelProps) {
  const isContract = job.employment_type === "contract";
  const hasRate = job.day_rate_max != null || job.day_rate_min != null;
  const cv = cvStateLabel(job.cv_status, job.cv_verify_verdict, job.cv_files_present);
  // Only offer the file links when the files are actually there.
  const cvDownloadable = job.cv_status === "rendered" && job.cv_files_present;
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

          <h3>Tailored CV</h3>
          <div className="cv-row">
            <button className="btn" onClick={onTailorCv}>
              {job.cv_status ? "Open tailored CV" : "Tailor CV for this job"}
            </button>
            {cv && <span className={`chip cv-${cv.tone}`}>{cv.label}</span>}
          </div>
          {cv ? (
            <p className="tailor-note">
              {job.cv_coverage_master != null && job.cv_coverage_tailored != null && (
                <>Requirement coverage {job.cv_coverage_master}% (master) →{" "}
                  <strong>{job.cv_coverage_tailored}%</strong> (this CV). </>
              )}
              {job.cv_status === "draft" && "Drafted but not rendered — approve it to produce the PDF. "}
              {job.cv_status === "rendered" && !job.cv_files_present && (
                <>The record says this was rendered, but the file is no longer in{" "}
                  <code>cv_output/</code>. Open the CV and re-render — the draft is stored, so this
                  takes seconds and does not need another model call. </>
              )}
              {cvDownloadable && job.cv_pdf_path && (
                <>
                  <a className="link-btn" href={`/api/file?path=${encodeURIComponent(job.cv_pdf_path)}`}
                     target="_blank" rel="noopener noreferrer">Open PDF ↗</a>
                  {job.cv_docx_path && (
                    <a className="link-btn" href={`/api/file?path=${encodeURIComponent(job.cv_docx_path)}`}
                       target="_blank" rel="noopener noreferrer">DOCX ↗</a>
                  )}
                </>
              )}
            </p>
          ) : (
            <p className="tailor-note">
              Selects from <code>cv/master.yaml</code> and rewords into this posting&rsquo;s
              vocabulary, then re-checks every claim against the master before you approve it.
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
