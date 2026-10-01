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
    /** The url of the advert this row is a re-advert of, or null when this row is
     *  the original. Decided by app/duplicates.py: same employer, identical title
     *  words, compatible locations — the repost that the url and the exact
     *  company+title+location keys both miss. */
    duplicate_of: string | null;
    match_score: number | null;
    recommendation: string | null;
    genuine_gaps: string | null;
    transferable_strengths: string | null;
    risk_factors: string | null;
    my_status: string | null;
    notes: string | null;
    /** Set only when my_status was COPIED from another advert of the same job you
     *  had already decided, naming that advert. A status you recorded and one a
     *  re-advert inherited are different claims, and the panel says which. */
    status_inherited_from: string | null;
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
    // ATS-scan state, from cv_scans (written by `cv_tailor scan`). This is what
    // the "Scan CV" button produces: whether the master CV would pass this
    // posting's screening, and what real experience is missing from it.
    cv_ats_verdict: string | null;        // pass | borderline | fail | unknown
    cv_ats_coverage: number | null;       // priority-term %, the CV as it stands
    cv_ats_projected: number | null;      // priority-term %, if suggestions were added
    cv_ats_hard_gaps: number | null;      // requirement-level hard skills the CV misses
    cv_ats_suggestions: number | null;    // master achievements worth adding
    cv_ats_updated_at: string | null;
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
    /** Failure modes this attempt PROVED the previous draft had, recorded so the
     *  drafter is taught them from now on. Present only on an escalation that
     *  verified strictly better — a retry that fared no better proves nothing. */
    learned?: string[];
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
    /** What the drafter left out and the pipeline put back: master achievements
     *  and whole roles it dropped, and the required skill groups it omitted.
     *
     *  Reported rather than applied silently, because a code-level correction
     *  that passes as the model's judgement is a lie about what the model decided
     *  — and on the RevTech draft this was nine bullets, including three of the
     *  master's four metrics. Empty or absent means the draft needed no
     *  correction, which is the normal case for the skill groups. */
    readded?: string[];
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
    /** JSON: one CvAttempt per drafting attempt, in order. Written by
     *  cv_tailor.preview so the provenance survives a reload. */
    attempts: string | null;
    /** JSON array of strings naming what cv_tailor restored from the master
     *  (dropped bullets, dropped roles, omitted required skill groups). Read
     *  back on reopen so the explanation for a CV carrying more than the
     *  drafter's own report claims it selected does not vanish with the preview. */
    readded: string | null;
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

// ---------------------------------------------------------------------------
// The ATS scan — what replaced per-posting CV tailoring (2026-10-01)
// ---------------------------------------------------------------------------
// Mirrors the JSON that `python -m app.cv_tailor scan` emits. Every field is
// optional-tolerant on purpose: the panel renders whatever is there and says so
// when something is missing, rather than throwing on a payload from an older
// pipeline run.

/** One posting term, with the posting evidence behind it. */
export type ScanTerm = {
    term: string;
    weight: number;
    /** Which part of the advert demanded it: requirements | duties |
     *  nice-to-have | mentioned. Drives how loudly the UI says it. */
    section: string;
    /** A recognised skill term, as distinct from a phrase the advert used. */
    skill: boolean;
};

/** One thing worth adding to the CV, quoted verbatim from cv/master.yaml.
 *  Never model-written — see app/ats_scan.py. */
export type ScanSuggestion = {
    /** "add_achievement" (the line is not on the CV at all) or
     *  "reword_achievement" (the fact is there, the posting's words are not). */
    kind: string;
    /** How much of this line already appears on one line of the CV, 0-1. */
    on_cv_overlap: number;
    role_id: string | null;
    role_title: string | null;
    company: string | null;
    achievement_id: string | null;
    /** Verbatim from the master. The copy-paste payload. */
    text: string;
    tags: string[];
    closes_terms: ScanTerm[];
    closes_count: number;
    weight_closed: number;
};

/** A whole master role that does not appear on the CV. */
export type ScanMissingRole = {
    id: string | null;
    company: string | null;
    title: string | null;
    start: string | null;
    end: string | null;
    achievements: number;
};

export type ScanAts = {
    verdict: string;                 // pass | borderline | fail | unknown
    reason: string;
    /** False when the posting could not be judged at all — usually because the
     *  stored description is a search-result snippet. The UI must not show a
     *  verdict band for an unassessable posting; confusing "cannot tell" with
     *  "passes" is the failure this whole field exists to prevent. */
    reliable: boolean;
    unreliable_reason: string | null; // snippet | no_headings | null
    sections_found: string[];
    terms_found: number;
    posting_chars: number;
    priority_coverage: number;
    priority_evidenced: number;
    priority_total: number;
    all_term_coverage: number;
    thresholds: { pass_coverage: number };
    hard_gaps: ScanTerm[];
    priority_gaps: ScanTerm[];
    evidenced_priorities: ScanTerm[];
};

export type ScanSource = {
    kind: string | null;             // service_account | export | cache
    cached_from: string | null;
    doc_id: string | null;
    fetched_at: string | null;
    stale: boolean;
    age_hours: number | null;
    reason: string | null;
    chars: number;
};

export type ScanResult = {
    url: string;
    company: string;
    title: string;
    cv_source: ScanSource;
    ats: ScanAts;
    suggestions: ScanSuggestion[];
    unclosable: ScanTerm[];
    roles_missing_from_cv: ScanMissingRole[];
    projection: {
        coverage: number;
        verdict: string | null;
        closes_terms: string[];
        still_open: ScanTerm[];
        note: string;
    };
    /** Another advert of the same job with a usable description, offered when
     *  this one could not be assessed. */
    better_source?: {
        url: string;
        company: string;
        title: string;
        source: string;
        chars: number;
    } | null;
};

/** The stored cv_scans row (GET /api/jobs/scan). */
export type StoredScan = {
    url: string;
    verdict: string | null;
    coverage: number | null;
    projected_coverage: number | null;
    hard_gaps: number | null;
    suggestion_count: number | null;
    cv_source: string | null;
    cv_fetched_at: string | null;
    cv_stale: number | null;
    scan: string | null;
    error: string | null;
    created_at: string | null;
    updated_at: string | null;
};

/** What GET /api/jobs/scan returns: the row plus its decoded payload. */
export type ScanPayload = {
    row: StoredScan;
    result: ScanResult | null;
};


/** Where a tailoring run is in its life, as the UI labels it. */
export type TailorPhase =
    | "idle"        // never run for this job
    | "drafting"    // preview in flight (~90s: local Qwen writes the YAML)
    | "review"      // draft ready, awaiting approval
    | "rendering"   // approved, writing files
    | "rendered"    // files exist on disk
    | "error";
