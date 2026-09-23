export const AI_STATUS_LABEL: Record<string, string> = {
    apply: "Apply", consider: "Consider", skip: "Skip", not_evaluated: "Not evaluated",
};
export const MY_STATUS_VALUES = ["applied", "interview", "rejected", "skipped", "silence"];
export const MY_STATUS_LABEL: Record<string, string> = {
    consider: "To consider",
    applied: "Applied", interview: "Interview", rejected: "Rejected",
    skipped: "Skipped", silence: "Silence",
};

// Contract-role labels. `employment_type` comes straight from the source
// where one is declared (Adzuna's contract_type, Remotive's job_type) and is
// "unknown" otherwise, which is the common case for ATS boards.
export const EMPLOYMENT_LABEL: Record<string, string> = {
    permanent: "Permanent", contract: "Contract", unknown: "—",
};

export const IR35_LABEL: Record<string, string> = {
    inside: "Inside IR35", outside: "Outside IR35", unknown: "IR35 not stated",
};

// How the day rate was obtained, worst-to-best. "stated" is a figure the
// advert itself quotes; the other two are inferred from Adzuna's annualised
// salary and must never be presented as if the advert said them.
export const RATE_SOURCE_LABEL: Record<string, string> = {
    stated: "stated in the advert",
    derived_from_advertised_salary: "derived from the advertised annual salary ÷ 260",
    estimated_by_adzuna: "Adzuna's own estimate, not the advert",
};

export const RATE_VERDICT_LABEL: Record<string, string> = {
    pass: "Meets floor",
    review: "Check IR35",
    fail: "Below floor",
    unknown: "No rate stated",
};

// Language fit, computed deterministically by app/filters.py from the words
// the advert uses. `none` means no language was named at all, which is NOT a
// mismatch — 40 of the 92 postings on the live board are in that state, and
// treating silence as a rejection would throw away real roles.
export const LANGUAGE_TIER_LABEL: Record<string, string> = {
    priority: "Java/Ruby",
    secondary: "JS/Node",
    none: "no language",
    mismatch: "mismatch",
};

export const LANGUAGE_TIER_HINT: Record<string, string> = {
    priority: "Names Java or Ruby — the priority languages",
    secondary: "Names JavaScript/Node only — acceptable, not the target",
    none: "Names no language at all — not a mismatch, just unknown",
    mismatch: "Names none of the priority languages (Python/Playwright/C#/.NET/Go/…)",
};

export const CLEARANCE_LABEL: Record<string, string> = {
    blocked: "clearance required",
    possible: "clearance?",
    none: "",
};

export const CLEARANCE_HINT: Record<string, string> = {
    blocked: "Requires security clearance you already hold",
    possible: "Clearance mentioned but obtainable — worth asking about",
    none: "",
};

export function formatGBP(value: number | null | undefined): string {
    if (value === null || value === undefined) return "—";
    return "£" + Math.round(value).toLocaleString("en-GB");
}
