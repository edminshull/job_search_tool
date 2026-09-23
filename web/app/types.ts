export type SortKey =
  | "company" | "title" | "location" | "match_score" | "status" | "my_status"
  | "posted_at" | "day_rate_max" | "perm_equivalent" | "language_rank";

export type SortDir = "asc" | "desc";
export type SaveState = "idle" | "saving" | "saved" | "error";

export type Job = {
    url: string;
    company: string;
    title: string;
    location: string;
    posted_at: string | null;
    description: string;
    source: string | null;
    employment_type: string | null;
    ir35_status: string | null;
    ir35_evidence: string | null;
    day_rate_min: number | null;
    day_rate_max: number | null;
    day_rate_source: string | null;
    rate_verdict: string | null;
    rate_required: number | null;
    perm_equivalent: number | null;
    rate_reason: string | null;
    language_tier: string | null;
    language_rank: number | null;
    language_hits: string | null;
    clearance_status: string | null;
    clearance_evidence: string | null;
    /** Last seen in a board fetch — the honest "is this still advertised?"
     *  signal, as distinct from posted_at, which is what the employer claims. */
    last_listed_at: string | null;
    match_score: number | null;
    recommendation: string | null;
    genuine_gaps: string | null;
    transferable_strengths: string | null;
    risk_factors: string | null;
    my_status: string | null;
    notes: string | null;
    // Tailored-CV state, from cv_tailorings (written by app/cv_tailor.py).
    cv_status: string | null;             // draft | rendered | failed
    cv_pdf_path: string | null;
    cv_docx_path: string | null;
    cv_coverage_master: number | null;    // priority-term %, before tailoring
    cv_coverage_tailored: number | null;  // priority-term %, after tailoring
    cv_verify_verdict: string | null;     // accept | revise | reject | unknown
    cv_updated_at: string | null;
    /** True only when status is "rendered" AND the PDF is really on disk. */
    cv_files_present: boolean;
    status: string; // apply | consider | skip | not_evaluated
};

/** A Job plus the parsed posting date, computed once per refresh. */
export type DatedJob = Job & {
    age: import("@/lib/dates").PostedAge;
};

/** The verifier's findings for a drafted CV (DeepSeek's independent check). */
export type CvVerification = {
    verdict: string;              // accept | revise | reject | unknown
    verify_model?: string;
    unsupported_refs?: string;
    fabricated_claims?: string;
    invented_metrics?: string;
    missing_must_haves?: string;
    required_fixes?: string;
    notes?: string;
};

/** How the CV reached its current state: one entry per drafting attempt, in
 *  order. More than one entry means the first draft was rejected or flagged and
 *  a stronger model re-drafted it — which the user must be able to see, or a
 *  cloud-written CV reads as the local model's work. */
export type CvAttempt = {
    model: string;        // e.g. "local:qwen3-coder-30b-a3b"
    verdict: string;      // accept | revise | reject | unknown
    reason: string | null;
};

/** A drafted (and verified) CV, as returned by /api/tailor/preview. */
export type CvPreview = {
    url: string;
    status: string;
    day_dir: string;
    company_slug: string;
    future_outdir: string;
    tailored_yaml: string;
    report: string;
    draft_model: string;
    verification: CvVerification;
    attempts?: CvAttempt[];
    coverage_before: number | null;
    coverage_after: number | null;
    priority_gaps: number | null;
    gap_report: string;
    interview_prep_md: string | null;
    posting_available: boolean;
};

/** The result of approving a draft: the files that now exist on disk. */
export type CvRender = {
    status: string;
    outdir: string;
    pdf_path: string;
    docx_path: string;
    gap_report_path: string | null;
    interview_prep_path: string | null;
    coverage_before: number | null;
    coverage_after: number | null;
    priority_gaps: number | null;
    warnings: string[];
};

/** The stored record for an already-tailored job (GET /api/jobs/tailoring). */
export type StoredTailoring = {
    url: string;
    status: string | null;
    day_dir: string | null;
    company_slug: string | null;
    tailored_yaml: string | null;
    report: string | null;
    interview_prep_md: string | null;
    outdir: string | null;
    pdf_path: string | null;
    docx_path: string | null;
    gap_report_path: string | null;
    interview_prep_path: string | null;
    draft_model: string | null;
    verify_model: string | null;
    verify_verdict: string | null;
    verify_notes: string | null;
    master_coverage: number | null;
    tailored_coverage: number | null;
    error: string | null;
    created_at: string | null;
    updated_at: string | null;
};

/** The master CV's size and placeholder state (GET /api/master/inventory). */
export type MasterStatus = {
    roles: number;
    achievements: number;
    skill_groups: number;
    placeholder: boolean;
    path: string;
};

/** Where a tailoring run is in its life, as the UI labels it. */
export type TailorPhase =
    | "idle"        // never run for this job
    | "drafting"    // preview in flight (~90s: local Qwen writes the YAML)
    | "review"      // draft ready, awaiting approval
    | "rendering"   // approved, writing files
    | "rendered"    // files exist on disk
    | "error";
