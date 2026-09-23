// Parsing and formatting of a job posting's date.
//
// `posted_at` is NOT reliably an ISO timestamp. It is whatever each source
// handed us, and the sources disagree:
//
//   Adzuna            "2026-09-16T04:04:36Z"
//   Greenhouse/Lever  "2026-08-01T00:00:00.000Z"
//   SmartRecruiters   epoch milliseconds, as a NUMBER
//   manually added    free text from the advert, e.g. "2 weeks ago"
//   (app/add_job.py accepts whatever you paste)
//
// So anything that assumes `new Date(posted_at)` is safe is wrong: that
// returns Invalid Date for epoch-as-string and for relative phrases, and
// every recency comparison against it would be silently false. Everything
// goes through parsePostedAt, which returns null rather than a wrong date.

export type PostedAge = {
  date: Date | null;
  days: number | null;   // whole days ago, 0 = today
  label: string;         // "3 days ago", "12 Aug", "no date"
  exact: string;         // tooltip / detail-panel text
};

const RELATIVE_RE =
  /^\s*(?:about\s+|around\s+|circa\s+|over\s+)?(\d+)\s*(minute|hour|day|week|month|year)s?\s*(ago|old)?\s*$/i;

const WORD_RELATIVE: [RegExp, number][] = [
  [/^\s*(just\s+now|today|new)\s*$/i, 0],
  [/^\s*yesterday\s*$/i, 1],
  [/^\s*(a|an|one)\s+day\s+ago\s*$/i, 1],
  [/^\s*(a|an|one)\s+week\s+ago\s*$/i, 7],
  [/^\s*(a|an|one)\s+month\s+ago\s*$/i, 30],
];

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

/**
 * Parses a `posted_at` value into a Date.
 *
 * `now` anchors the RELATIVE phrases ("2 weeks ago", "yesterday") and must be
 * threaded through from `postedAge`, which is the only caller that has an
 * injectable clock. It is not cosmetic: a relative phrase is resolved against
 * this instant and then day-diffed against `now` again, so if the two disagree
 * every relative date is wrong by the difference. That is exactly what happened
 * — postings saying "2 weeks ago" aged as 12 days, "29 days ago" as 27 — and it
 * stayed hidden while the tests' fixture date happened to be close to the real
 * one. It surfaced the moment the fixture and the wall clock drifted apart by
 * two days.
 *
 * Absolute forms (ISO datetimes, epoch millis) ignore `now` entirely, as they
 * should: they do not depend on when they are read.
 */
export function parsePostedAt(raw: unknown, now: Date = new Date()): Date | null {
  if (raw === null || raw === undefined) return null;

  // Epoch milliseconds, arriving from SmartRecruiters as a number (and, via
  // SQLite's looser typing, possibly as a digit-only string).
  if (typeof raw === "number" || (typeof raw === "string" && /^\d{10,13}$/.test(raw.trim()))) {
    const n = Number(raw);
    if (!Number.isFinite(n)) return null;
    const ms = n > 1e11 ? n : n * 1000; // 10-digit = seconds
    const d = new Date(ms);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  const text = String(raw).trim();
  if (!text) return null;

  // Relative phrases, which is what a pasted LinkedIn advert gives us.
  for (const [re, days] of WORD_RELATIVE) {
    if (re.test(text)) {
      const d = new Date(now);
      d.setDate(d.getDate() - days);
      return d;
    }
  }
  const rel = text.match(RELATIVE_RE);
  if (rel) {
    const n = Number(rel[1]);
    const unit = rel[2].toLowerCase();
    // Adjusted a whole DAY at a time via setDate for day/week/month, rather
    // than subtracting a millisecond multiple from `now`. Both give the same
    // answer only when `now` is exactly midnight: at 09:47, subtracting
    // 30 * 86_400_000 ms lands at 09:47 on a day that startOfDay then rounds
    // DOWN, losing a day. Calendar arithmetic keeps the anchor day exact, which
    // is what "N days ago" means.
    const d = new Date(now);
    if (unit === "minute") d.setMinutes(d.getMinutes() - n);
    else if (unit === "hour") d.setHours(d.getHours() - n);
    else if (unit === "day") d.setDate(d.getDate() - n);
    else if (unit === "week") d.setDate(d.getDate() - n * 7);
    else if (unit === "month") d.setMonth(d.getMonth() - n);
    else d.setFullYear(d.getFullYear() - n);
    return d;
  }

  // ISO 8601, with or without a zone. A datetime with no zone (SQLite's
  // CURRENT_TIMESTAMP format, "2026-09-18 16:43:22") is parsed as LOCAL
  // time by JS, which is what we want — those values were written by a
  // process on this machine. A string ending in Z is genuinely UTC.
  const iso = Date.parse(text);
  if (!Number.isNaN(iso)) return new Date(iso);

  // "2026-09-18 16:43:22" — Safari and some Node builds reject the space
  // separator, so retry with a T before giving up.
  const normalised = Date.parse(text.replace(" ", "T"));
  if (!Number.isNaN(normalised)) return new Date(normalised);

  return null; // "Not stated", "Competitive", an empty string, junk...
}

export function postedAge(raw: unknown, now: Date = new Date()): PostedAge {
  // `now` goes into the parse as well as the diff: a relative phrase is
  // resolved against the clock, so parsing with the real one while diffing
  // against an injected one makes every relative date wrong by the gap between
  // them. See parsePostedAt.
  const date = parsePostedAt(raw, now);
  if (!date) {
    return {
      date: null,
      days: null,
      label: "no date",
      exact: raw ? `Unreadable date: "${String(raw).slice(0, 40)}"` : "No posting date given",
    };
  }
  const days = Math.round((startOfDay(now).getTime() - startOfDay(date).getTime()) / 86_400_000);
  return { date, days, label: relativeLabel(days), exact: exactLabel(date, days) };
}

function relativeLabel(days: number): string {
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  if (days < 14) return "1 week ago";
  if (days < 45) return `${Math.round(days / 7)} weeks ago`;
  if (days < 365) return `${Math.round(days / 30)} months ago`;
  return `${Math.round(days / 365)}y ago`;
}

function exactLabel(date: Date, days: number): string {
  const formatted = date.toLocaleDateString("en-GB", {
    weekday: "short", day: "numeric", month: "short", year: "numeric",
  });
  return `${formatted} (${days} day${days === 1 ? "" : "s"} ago)`;
}

// Buckets offered by the toolbar. "all" exists so the recency window is
// always overridable — it is a default, never a hard limit.
export const DATE_WINDOWS: { value: string; label: string; days: number | null }[] = [
  { value: "7", label: "Last 7 days", days: 7 },
  { value: "30", label: "Last month", days: 30 },
  { value: "90", label: "Last 3 months", days: 90 },
  { value: "180", label: "Last 6 months", days: 180 },
  { value: "all", label: "All dates", days: null },
];

export const DEFAULT_DATE_WINDOW = "30";

/** Resolve a window value to its number of days, or null for "all dates".
 *
 *  This exists as a function rather than an inline lookup because the
 *  obvious one-liner
 *      DATE_WINDOWS.find((w) => w.value === v)?.days ?? 30
 *  is WRONG: "all" legitimately has `days: null`, and `null ?? 30` silently
 *  turns the "show everything" option back into a 30-day filter. The
 *  fallback has to apply only when the value is unrecognised, never when it
 *  is recognised and deliberately null. */
export function windowDays(value: string): number | null {
  const found = DATE_WINDOWS.find((w) => w.value === value);
  if (found) return found.days;
  return DATE_WINDOWS.find((w) => w.value === DEFAULT_DATE_WINDOW)?.days ?? 30;
}

/** True if a posting falls inside the window. Undated postings are always
 *  kept: we cannot prove they are stale, and silently hiding a job because
 *  its advert did not state a date is worse than showing it with a marker. */
export function withinWindow(age: PostedAge, days: number | null): boolean {
  if (days === null) return true;
  if (age.days === null) return true;
  return age.days <= days;
}
