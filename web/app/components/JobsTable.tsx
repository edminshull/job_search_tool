import {
  CLEARANCE_HINT, CLEARANCE_LABEL, formatGBP, IR35_LABEL, LANGUAGE_TIER_HINT,
  LANGUAGE_TIER_LABEL, MY_STATUS_LABEL, MY_STATUS_VALUES, RATE_VERDICT_LABEL,
} from "@/app/constants";
import {postedAge} from "@/lib/dates";
import {DatedJob, SortDir, SortKey} from "@/app/types";
import StatusBadge from "@/app/components/StatusBadge";

const COLUMNS: [SortKey, string, string][] = [
  ["company", "Company", ""],
  ["title", "Title", ""],
  ["location", "Location", ""],
  // The posting date is the second thing looked at after the title, so it
  // sits before the score rather than off the right-hand edge where it was
  // easy to miss entirely.
  ["posted_at", "Posted", "Newest first when clicked"],
  ["language_rank", "Lang", "Languages the posting names. Java/Ruby are the priority; click to sort best-first"],
  ["day_rate_max", "Rate", "Day rate for contract roles"],
  ["match_score", "Score", ""],
  ["status", "AI status", ""],
  ["my_status", "My status", ""],
];

/** Stale postings are dimmed past 45 days, so a 5-month-old advert is
 *  obvious at a glance even with the date window widened. */
function ageClass(days: number | null): string {
  if (days === null) return "age-unknown";
  if (days <= 7) return "age-fresh";
  if (days <= 30) return "age-recent";
  if (days <= 45) return "age-ok";
  return "age-stale";
}

type JobsTableProps = {
  rows: DatedJob[];
  sortKey: SortKey;
  sortDir: SortDir;
  onToggleSort: (key: SortKey) => void;
  onSelectJob: (job: DatedJob) => void;
  onQuickStatusChange: (job: DatedJob, status: string) => void;
};

/** A posting whose posted date is stale (or unknown) but which a board fetch
 *  recently returned — so it is still advertised. The distinction exists because
 *  `posted_at` is the EMPLOYER'S CLAIM and can be years out of date on a live
 *  role: Trading 212's London QA job carries 2023-06-28 and is still open.
 *  Without this the recency window hides real jobs. */
function staleButListed(j: DatedJob): boolean {
  if (!j.last_listed_at) return false;
  const seen = postedAge(j.last_listed_at);
  if (seen.days === null || seen.days > 30) return false;
  return j.age.days === null || j.age.days > 30;
}

/** The tooltip explains BOTH dates when they disagree, since "no date, still
 *  listed" is otherwise a confusing pairing to meet in a table. */
function listedTitle(j: DatedJob): string {
  const parts = [j.age.exact];
  if (j.last_listed_at) {
    const seen = postedAge(j.last_listed_at);
    parts.push(`Last seen on the board ${seen.label}`);
  }
  return parts.join(" · ");
}

export default function JobsTable({rows, sortKey, sortDir, onToggleSort, onSelectJob, onQuickStatusChange}: JobsTableProps) {
  return (
    <table>
      <thead>
        <tr>
          {COLUMNS.map(([key, label, hint]) => (
            <th key={key} title={hint} className={sortKey === key ? "active-sort" : ""} onClick={() => onToggleSort(key)}>
              {label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((j) => (
          <tr key={j.url} onClick={() => onSelectJob(j)}>
            <td className="company-cell">{j.company}</td>
            <td>
              <span className="title-text">{j.title}</span>
              {j.employment_type === "contract" && (
                <span className="chip contract-chip" title={j.rate_reason ?? undefined}>
                  Contract{j.ir35_status && j.ir35_status !== "unknown"
                    ? ` · ${IR35_LABEL[j.ir35_status] ?? j.ir35_status}`
                    : ""}
                </span>
              )}
              {/* Only ever "possible" here: a "blocked" posting is dropped by
                  the filters and never reaches the board. */}
              {j.clearance_status && j.clearance_status !== "none" && (
                <span
                  className={`chip clearance-${j.clearance_status}`}
                  title={`${CLEARANCE_HINT[j.clearance_status] ?? ""}${j.clearance_evidence ? ` — ${j.clearance_evidence}` : ""}`}
                >
                  {CLEARANCE_LABEL[j.clearance_status] ?? j.clearance_status}
                </span>
              )}
            </td>
            <td className="loc-cell">{j.location}</td>
            <td className={`date-cell ${ageClass(j.age.days)}`} title={listedTitle(j)}>
              {j.age.label}
              {/* A role that is still advertised but has a stale posted date.
                  Shown rather than hidden, because it is genuinely open: this is
                  how Trading 212's London QA role reaches the board at all. */}
              {staleButListed(j) && <span className="listed-chip" title="Still listed on the employer's board">still listed</span>}
            </td>
            <td className="lang-cell">
              {j.language_tier ? (
                <span
                  className={`chip lang-${j.language_tier}`}
                  title={`${LANGUAGE_TIER_HINT[j.language_tier] ?? ""}${j.language_hits ? ` — matched: ${j.language_hits}` : ""}`}
                >
                  {LANGUAGE_TIER_LABEL[j.language_tier] ?? j.language_tier}
                </span>
              ) : (
                <span className="rate-none">—</span>
              )}
            </td>
            <td className="rate-cell" title={j.rate_reason ?? undefined}>
              {j.day_rate_max != null ? (
                <>
                  <span className="rate-value">
                    {j.day_rate_min != null && j.day_rate_min !== j.day_rate_max
                      ? `${formatGBP(j.day_rate_min)}–${formatGBP(j.day_rate_max)}`
                      : formatGBP(j.day_rate_max)}
                    <span className="rate-unit">/day</span>
                  </span>
                  {j.rate_verdict && (
                    <span className={`chip rate-${j.rate_verdict}`}>
                      {RATE_VERDICT_LABEL[j.rate_verdict] ?? j.rate_verdict}
                    </span>
                  )}
                  {/* A derived figure is NOT a quoted rate, and must not read
                      like one — the advert never said this number. */}
                  {j.day_rate_source && j.day_rate_source !== "stated" && (
                    <span className="rate-derived" title="Not quoted in the advert — inferred from the annualised salary">
                      ~derived
                    </span>
                  )}
                </>
              ) : j.employment_type === "contract" ? (
                <span className="rate-missing">no rate stated</span>
              ) : (
                <span className="rate-none">—</span>
              )}
            </td>
            <td className="score-cell">{j.match_score ?? "—"}</td>
            <td><StatusBadge status={j.status} /></td>
            <td onClick={(e) => e.stopPropagation()}>
              <select
                className="my-status-select"
                value={j.my_status ?? ""}
                onChange={(e) => onQuickStatusChange(j, e.target.value)}
              >
                <option value="">—</option>
                {MY_STATUS_VALUES.map((v) => (
                  <option key={v} value={v}>{MY_STATUS_LABEL[v]}</option>
                ))}
              </select>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
