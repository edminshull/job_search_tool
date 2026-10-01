"""Add a LinkedIn posting to the board from its URL — or from pasted text.

THE COMMON CASE: just the URL
-----------------------------
LinkedIn is where a lot of real postings in this search come from, but it is
not one of the pipeline's sources: its data is restricted to approved partners,
so the other fetchers (companies.yaml ATS boards, Adzuna, Remotive) are
structurally blind to it, and the two halves of this job search would never
meet. This tool closes that loop.

The short form is the one to remember:

    python -m app.add_job --url "https://www.linkedin.com/jobs/view/4471638473"

That reads the posting's public page and takes the company, title, location,
"posted" age and full advert text from it, then writes the row. See
`app/linkedin_fetch.py` for what is read, how, and what the fetcher will NOT do
(it stays anonymous — no login, no session cookie — and does not try to defeat
a block). A 200 from that endpoint is LinkedIn's own structured markup, so
those values are authoritative: unlike a *guessed* paste value, they are stored
without asking you to confirm them with --yes.

WHY PASTING IS STILL HERE
-------------------------
Reading the page is best-effort by nature. If LinkedIn has taken the advert
down (404), serves an auth wall, or rate-limits the request, the fetch reports
exactly that and you fall back to the paste workflow, which is unchanged:

    # fields given explicitly — the reliable path, and the only one that works
    # for a posting that is not reachable by URL
    python -m app.add_job --url "https://www.linkedin.com/jobs/view/123456" \\
        --company Resillion --title "Senior Test Automation Analyst" \\
        --location "London (hybrid)" --file ~/Downloads/posting.txt

    # paste straight from the clipboard — a piped stdin is read automatically,
    # so no flag is needed (`--stdin` says it explicitly and also works)
    pbpaste | python -m app.add_job --url "..." --company X --title Y

    # let it infer company/title/location from a LinkedIn paste (it prints
    # what it inferred and from which line, so you can check before trusting)
    python -m app.add_job --url "..." --infer --file posting.txt

    python -m app.add_job --list        # what have I added by hand?
    python -m app.add_job --list --pending   # added but not yet AI-scored
    python -m app.add_job --remove "https://..."

Order of authority, most to least: an explicit flag, then the URL fetch, then a
labelled line in the paste ("Company: X"), then a --infer guess (which needs
--yes before it is written).

The row is written with passed_filters = 1 on purpose: you have already made
the "is this in scope" judgement by choosing to add it, and the board only
shows passed_filters = 1 rows. Whether it would have survived the deterministic
filters is reported for information, but never blocks the insert.
"""
import argparse
import re
import subprocess
import sys

from app import contract_rates
from app import dedup
from app import filters
from app import linkedin_fetch

DEFAULT_SOURCE = "linkedin"

# LinkedIn's copy-paste and page layout put the company, location and age on
# one middot-separated line under the title — e.g.
#   "Resillion · London, England, United Kingdom · 2 weeks ago"
#   "Xe · London, United Kingdom · 3 days ago"
# It is the single most reliable structure in a LinkedIn paste, so it is tried
# before anything fuzzier.
_MIDDOT_LINE = re.compile(r"^\s*(?P<company>[^·|]+?)\s*[·|]\s*(?P<rest>.+?)\s*$")
_LABELLED = re.compile(
    r"^\s*\**\s*(?P<label>company|employer|organisation|organization|title|role|"
    r"job title|location|based in|posted)\s*\**\s*:\s*(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_AGE = re.compile(r"\b(\d+\s*(?:minute|hour|day|week|month)s?\s*ago|just posted|reposted)\b", re.IGNORECASE)
_NOT_A_FIELD = re.compile(
    r"^\s*(apply|easy apply|save|saved|report this job|about the job|job description|"
    r"description|responsibilities|requirements|benefits|show more|see more|"
    r"set alert|people you can reach out to|meet the hiring team)\b",
    re.IGNORECASE,
)
# Markdown chrome. The pastes in the CV repo are curated documents with a
# header block above the actual posting text, and without this the note
# "# Job posting — captured verbatim from LinkedIn paste" was being read as the
# JOB TITLE in 8 of 16 real files.
_CHROME = re.compile(r"^\s*(#{1,6}\s*|>|\*\*formatting|formatting note|_+\s*$|-{3,}\s*$|\*\*\*+)",
                     re.IGNORECASE)
# "Work Arrangement: Hybrid (3 days per week onsite)" is a labelled META line,
# never a job title — a label prefix disqualifies a line from being the title.
_LABEL_PREFIX = re.compile(r"^\s*\**\s*[A-Za-z][A-Za-z /&'-]{2,24}\s*:\s+\S")
# A job title contains one of these. Without this check, prose from the body of
# a posting ("What you'll be doing", "About Xe", "No visa sponsorship",
# "Typical Job Responsibilities") was being stored as the TITLE — and the title
# goes straight into the AI prompt and onto the board.
_TITLE_WORDS = re.compile(
    r"\b(engineer|engineering|developer|development|tester|testing|analyst|analysis|"
    r"consultant|architect|sdet|qa|quality|test|lead|manager|specialist|scientist|"
    r"administrator|technician|director|programmer|automation)\b",
    re.IGNORECASE,
)
# A "Company · Location" line is only believed if the left side could be a
# company. Real mis-parse: "Senior Level | Financial Services | Agile Quality
# Engineering" yielded company="Senior Level", location="Financial Services".
_NOT_A_COMPANY = re.compile(
    r"^\s*(senior|junior|mid|mid-?level|entry|lead|principal|staff|head of|graduate|"
    r"permanent|contract|temporary|full[- ]time|part[- ]time|hybrid|remote|onsite|"
    r"on-site|flexible|competitive|salary|rate|location|industry|sector|department|"
    r"job|role|position|vacancy|opportunity|about|overview|summary)\b",
    re.IGNORECASE,
)
_MONEY = re.compile(r"[£$€]|\b\d+\s*k\b|\bper\s+(?:annum|year|day)\b", re.IGNORECASE)


def _looks_like_a_person_or_chrome(line: str) -> bool:
    """Reject obvious non-fields so a paste's boilerplate doesn't become the
    job title: LinkedIn puts "Applied", "Saved", the poster's name and their
    headline near the top of a copy."""
    if not line or _NOT_A_FIELD.match(line) or _CHROME.match(line):
        return True
    if len(line) > 120:
        return True
    if line.lower() in {"linkedin", "jobs", "applied", "saved", "easy apply"}:
        return True
    # "1st", "2nd", "3rd" degree markers, follower counts, etc.
    if re.match(r"^\d+(st|nd|rd|th)\b", line, re.IGNORECASE):
        return True
    return False


def _plausible_location(value: str, strict: bool) -> bool:
    """Guard against 'location' values that are plainly something else.

    Real mis-parses this catches: location="GBP75,000 + Benefits" (a salary) and
    location="Financial Services" (an industry). `strict` applies the UK/London
    allowlist as well, which is right for a guessed value but wrong for an
    explicitly labelled "Location:" line — a posting can legitimately be outside
    the search's location patterns, and the user may still want to track it."""
    if not value or len(value) > 60 or _MONEY.search(value):
        return False
    if _NOT_A_COMPANY.match(value) and not filters.location_is_allowed(value):
        return False
    if strict and not filters.location_is_allowed(value):
        return False
    return True


def parse_paste(text: str) -> dict:
    """Best-effort extraction of company / title / location from a pasted
    posting. Returns {field: value} plus {field + '_from': line} and
    {field + '_how': 'labelled'|'guessed'} for every field it found.

    The distinction matters and is surfaced by the CLI: a value taken from an
    explicit "Company: X" label is authoritative, whereas one parsed out of a
    "Company · Location · 3 days ago" line is a guess that can be wrong (see
    _plausible_location). Callers must not write guessed values without telling
    the user."""
    found: dict = {}
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln and set(ln) != {"-"}]

    def put(field: str, value: str, line: str, how: str) -> None:
        if field not in found and value:
            found[field] = value
            found[f"{field}_from"] = line
            found[f"{field}_how"] = how

    # 1. Labelled values win outright — "Company: X" is not a guess.
    for ln in lines[:40]:
        m = _LABELLED.match(ln)
        if not m:
            continue
        label = m.group("label").lower()
        value = m.group("value").strip(" *_")
        if len(value) < 2:
            continue
        if label in ("company", "employer", "organisation", "organization"):
            put("company", value, ln, "labelled")
        elif label in ("title", "role", "job title"):
            put("title", value, ln, "labelled")
        elif label in ("location", "based in"):
            put("location", value, ln, "labelled")
        elif label == "posted":
            put("posted_at", value, ln, "labelled")

    # 2. The middot line: "Resillion · London, England, United Kingdom · 2 weeks ago".
    if "company" not in found or "location" not in found:
        for ln in lines[:25]:
            if _CHROME.match(ln):
                continue
            m = _MIDDOT_LINE.match(ln)
            if not m:
                continue
            company = m.group("company").strip(" *_")
            parts = [p.strip() for p in re.split(r"\s*[·|]\s*", m.group("rest")) if p.strip()]
            age = ""
            if parts and _AGE.search(parts[-1]):
                age = parts.pop()
            if not parts or not company or _NOT_A_COMPANY.match(company):
                continue
            if _looks_like_a_person_or_chrome(company) or len(company) > 60:
                continue
            # Every part on this line must look like a place, or the line is
            # not a company/location line at all.
            if not all(_plausible_location(p, strict=True) for p in parts):
                continue
            put("company", company, ln, "guessed")
            put("location", parts[0], ln, "guessed")
            if age:
                put("posted_at", age, ln, "guessed")
            break

    # 3. Title: the FIRST substantive line that looks like a job title.
    #
    #    Restricted to the top of the paste on purpose. In a real LinkedIn
    #    clipboard paste the title is the first line, so this is where it lives;
    #    deeper in, a "role word" match picks up prose instead ("Develop,
    #    enhance, and maintain scalable automation…" passed a naive
    #    _TITLE_WORDS check). Curated multi-page documents that keep the title
    #    in separate metadata therefore get NO title guess, which is the honest
    #    outcome — better to ask than to store a paragraph as the job title.
    if "title" not in found:
        substantive = [ln for ln in lines if not _looks_like_a_person_or_chrome(ln)]
        claimed = {found.get("company_from"), found.get("location_from"),
                   found.get("posted_at_from")}
        for ln in substantive[:4]:
            if ln in claimed:
                continue
            if _LABEL_PREFIX.match(ln) or _AGE.search(ln) or ln.endswith(":"):
                continue
            if len(ln.split()) > 12 or len(ln) > 100:
                continue
            if not _TITLE_WORDS.search(ln):
                continue
            put("title", ln, ln, "guessed")
            break

    # 4. Location fallback: a bare "London, United Kingdom" line.
    if "location" not in found:
        for ln in lines[:25]:
            if _looks_like_a_person_or_chrome(ln) or len(ln) > 60:
                continue
            if len(ln.split()) <= 5 and _plausible_location(ln, strict=True):
                put("location", ln, ln, "guessed")
                break
    return found


def _is_linkedin_job_url(url: str) -> bool:
    return bool(_LI_JOB_ID.search(url or ""))


# LinkedIn job URLs come in several shapes, all of which identify the same job:
#   /jobs/view/4400123456/?refId=abc&trackingId=xyz     (copied from the job page)
#   /jobs/view/senior-qa-engineer-at-monzo-4400123456  (slug form)
#   /jobs/search/?currentJobId=4400123456               (copied from the results list)
# The id is the only stable part. Without normalising, the SAME job pasted twice
# with different tracking parameters became TWO rows in the tracker — verified:
# three copies of one posting produced three rows — and the URL is the primary
# key that everything else (dedup, notes, status) hangs off.
_LI_JOB_ID = re.compile(r"linkedin\.com/jobs/(?:view/(?:[^/?#]*-)?|search/\?[^#]*?currentJobId=)(\d{6,})",
                        re.IGNORECASE)


def canonical_url(url: str) -> str:
    """The form of `url` used as the tracker's primary key.

    LinkedIn job URLs are reduced to https://www.linkedin.com/jobs/view/<id>/,
    which is stable across tracking parameters and across the page/list/slug
    variants. Anything else is returned stripped, unchanged."""
    url = (url or "").strip()
    m = _LI_JOB_ID.search(url)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return url


def add_job(conn, *, url, company, title, location, description,
            posted_at=None, source=DEFAULT_SOURCE, update=False,
            employment_type=None) -> tuple[bool, str]:
    """Insert one manually-found posting. Returns (inserted, message)."""
    url = canonical_url(url)
    existing = dedup.get_details_by_url(conn, url)
    if existing and not update:
        return False, (f"Already stored ({existing['company']} — {existing['title']}). "
                       f"Re-run with --update to overwrite it.")
    # A different URL for what looks like the same role is worth flagging even
    # though it is allowed: it is usually the same advert from another source.
    twin = conn.execute(
        "SELECT company, title, url FROM job_details "
        "WHERE lower(company) = lower(?) AND lower(title) = lower(?) AND lower(location) = lower(?) "
        "AND url <> ? LIMIT 1",
        (company, title, (location or "").lower(), url),
    ).fetchone()
    job = {
        "url": url, "company": company, "title": title, "location": location,
        "posted_at": posted_at, "description": description, "source": source,
        "employment_type": employment_type or "unknown",
    }
    # Apply the same contract evaluation the pipeline gives fetched postings,
    # so a manually-added contract role arrives on the board with an IR35
    # status, a day rate and a verdict instead of a row of blanks. It matters
    # more here than for fetched jobs: a LinkedIn advert is often the only
    # place the day rate was ever stated.
    contract_rates.apply_to_job(job)
    # And the same deterministic screen every pipeline path applies
    # (`job.update(filters.screening_signals(job))` in main.py/refilter.py/
    # backfill_derived_fields.py). Without it a manually-added row reached the
    # board with language_tier and clearance_status NULL, which is not cosmetic:
    # the board's language filter drops rows whose tier is null, the detail
    # panel shows no language/clearance chip, and `ai_evaluate` reads the stored
    # tier instead of re-deriving it. It matters more now than when this tool
    # only took pastes, because a fetched posting carries the full advert text
    # these rules read — see app/linkedin_fetch.py.
    #
    # This only RECORDS a status; it cannot reject. `clearance_reject_reason` is
    # what the pipeline drops jobs with, and it is deliberately not called here:
    # adding the job IS the in-scope judgement (see the module docstring).
    job.update(filters.screening_signals(job))
    dedup.save_details(conn, job, passed_filters=True)
    dedup.mark_seen(conn, job)  # so a later pipeline run treats it as already seen
    verb = "Updated" if existing else "Added"
    message = f"{verb} {company} — {title}"
    if twin:
        message += (f"\n                 NOTE: you already have '{twin[0]} — {twin[1]}' at the same "
                    f"company/title/location ({twin[2]}) — same advert from another source?")
    return True, message


def report_filter_verdict(company, title, location, description) -> None:
    """Informational only: say whether the deterministic filters would have let
    this through. Never used to block the insert (see the module docstring)."""
    reasons = []
    if not filters.title_is_relevant(title):
        reasons.append("title not in the QA/testing allowlist")
    if not filters.location_is_allowed(location):
        reasons.append("location not in the London/UK allowlist")
    if filters.jd_stack_mismatch(description or ""):
        reasons.append("JD names a stack dealbreaker (C#/.NET/Go/...) with none of your stack")
    would_pass = filters.passes_filters({"title": title, "location": location,
                                         "description": description or ""})
    if would_pass:
        print("  Filters      : would have passed the deterministic filters")
    else:
        print("  Filters      : would NOT have passed — " + "; ".join(reasons or ["unknown reason"]))
        print("                 (added anyway: pasting it is the in-scope judgement)")


def report_contract_terms(employment_type, title, location, description) -> None:
    """Show the day rate, IR35 status and what that rate is worth as a salary
    BEFORE the job is written, so a pasted contract role is judged on the same
    numbers the board will show. Purely informational — a contract that falls
    short of the floor is still added, because pasting it is the in-scope
    judgement (the same rule the filter verdict above follows)."""
    if employment_type != "contract":
        return
    model = contract_rates.default_model()
    ev = contract_rates.evaluate_contract(
        {"title": title, "location": location, "description": description,
         "employment_type": "contract"}, model)

    rate = ev["day_rate_max"]
    if rate is None:
        rate_text = f"not stated in the pasted text (no rate to check it against)"
    else:
        lo = ev["day_rate_min"]
        shown = f"£{lo:,.0f}–£{rate:,.0f}" if lo and abs(lo - rate) > 0.5 else f"£{rate:,.0f}"
        rate_text = f"{shown} per day ({ev['rate_reason']})"
    print(f"  Day rate     : {rate_text}")
    print(f"  IR35         : {ev['ir35_status']}"
          + (f" — read from \"{ev['ir35_evidence']}\"" if ev["ir35_evidence"] else ""))
    if ev["perm_equivalent"]:
        print(f"  Equivalent   : about £{ev['perm_equivalent']:,.0f} as a permanent salary")
    if ev["rate_verdict"] == "fail":
        print("                 BELOW the contract.yaml floor — added anyway, but it is short of "
              "the permanent-salary equivalent.")


def _stdin_is_piped() -> bool:
    """True when stdin is a pipe or a file rather than a terminal.

    The distinction is what lets `pbpaste | python -m app.add_job …` work with no
    flag, while an interactive run with no paste at all still asks for nothing
    and hangs on nothing. Guarded because an inherited-but-closed stdin (some
    CI runners, `subprocess.run` without stdin=) raises on isatty() rather
    than answering."""
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _read_piped_stdin() -> str:
    """Read the piped posting text, treating an unreadable stdin as empty.

    A closed or /dev/null stdin is a normal way to run this (cron, a wrapper
    script) and must not turn into a traceback: the missing-field checks below
    already report the real problem if no text arrives."""
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="Canonical URL of the posting (used as the primary key)")
    ap.add_argument("--company")
    ap.add_argument("--title")
    ap.add_argument("--location")
    ap.add_argument("--posted-at", help="Free text, e.g. '2 weeks ago' or a date")
    ap.add_argument("--file", help="File containing the pasted posting text")
    ap.add_argument("--stdin", action="store_true", help="Read the posting text from stdin")
    ap.add_argument("--clipboard", action="store_true",
                    help="Read the posting text from the system clipboard (macOS pbpaste)")
    ap.add_argument("--fetch", dest="fetch", action="store_true", default=None,
                    help="Read the posting from its LinkedIn URL (see app/linkedin_fetch.py). "
                         "This is the DEFAULT whenever --url holds a LinkedIn job URL and "
                         "--company or --title is missing, so `--url` alone is enough; pass "
                         "--fetch to force it, e.g. to refresh the advert text of a stored row.")
    ap.add_argument("--no-fetch", dest="fetch", action="store_false",
                    help="Never touch the network; use only what you pass or paste.")
    ap.add_argument("--infer", action="store_true",
                    help="Infer missing fields from the pasted text (LinkedIn layout)")
    ap.add_argument("--source", default=DEFAULT_SOURCE, help=f"Source label (default {DEFAULT_SOURCE})")
    ap.add_argument("--contract", dest="employment_type", action="store_const", const="contract",
                    help="Mark as a contract role, so the day-rate/IR35 checks apply. "
                         "The day rate and IR35 status are read from the pasted text either way; "
                         "this only sets the employment type, which the advert may not state plainly.")
    ap.add_argument("--permanent", dest="employment_type", action="store_const", const="permanent",
                    help="Mark as a permanent role (the default is unknown).")
    ap.add_argument("--update", action="store_true", help="Overwrite an existing row for this URL")
    ap.add_argument("--yes", action="store_true",
                    help="Accept values GUESSED from the paste by --infer (off by default: the "
                         "guessers are unreliable, see the note in the docstring)")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be stored, write nothing")
    ap.add_argument("--list", action="store_true", help="List manually-added jobs and exit")
    ap.add_argument("--pending", action="store_true", help="With --list: only ones not yet AI-scored")
    ap.add_argument("--remove", metavar="URL", help="Delete a manually-added job by URL")
    ap.add_argument("--db", help="Database path (default: data/seen_jobs.sqlite3)")
    args = ap.parse_args()

    db_path = args.db or dedup.DB_PATH

    if args.list or args.remove:
        with dedup.connect(db_path) as conn:
            if args.remove:
                row = dedup.get_details_by_url(conn, args.remove)
                if not row:
                    sys.exit(f"No stored job for {args.remove}")
                conn.execute("DELETE FROM job_details WHERE url = ?", (args.remove,))
                conn.execute("DELETE FROM seen_jobs WHERE url = ?", (args.remove,))
                conn.execute("DELETE FROM ai_evaluations WHERE url = ?", (args.remove,))
                conn.execute("DELETE FROM user_status WHERE url = ?", (args.remove,))
                print(f"Removed {row['company']} — {row['title']}")
                return
            rows = conn.execute(
                "SELECT jd.company, jd.title, jd.location, jd.url, jd.source, "
                "       ae.match_score, ae.recommendation, us.my_status "
                "FROM job_details jd "
                "LEFT JOIN ai_evaluations ae ON jd.url = ae.url "
                "LEFT JOIN user_status us ON jd.url = us.url "
                "WHERE jd.source = ? ORDER BY jd.fetched_at DESC",
                (args.source,),
            ).fetchall()
            if args.pending:
                rows = [r for r in rows if r[5] is None]
            if not rows:
                print(f"No manually-added jobs (source={args.source}"
                      f"{', pending only' if args.pending else ''}).")
                return
            print(f"{len(rows)} manually-added job(s), newest first:")
            for company, title, location, url, source, score, rec, status in rows:
                state = f"{score:3d} {rec}" if score is not None else "unscored"
                print(f"  [{state:>13}] {company[:22]:22} | {title[:44]:44} | {location[:26]:26} | "
                      f"you={status or '-'}")
            return

    if not args.url:
        sys.exit("--url is required (or use --list / --remove).")

    text = ""
    if args.file:
        text = open(args.file).read()
    elif args.stdin or _stdin_is_piped():
        # The README's clipboard recipe is `pbpaste | python -m app.add_job …`,
        # and this branch is what makes it true. Without the pipe check that
        # command stored description="" — silently, exit code 0 — so the job
        # reached the board with no JD text and the AI step had nothing to score
        # (verified 2026-09-23). A pipe is read automatically, the way `cat` and
        # `git apply` do it; an interactive terminal is never read, so a plain
        # run with no source still behaves exactly as before.
        text = _read_piped_stdin()
    elif args.clipboard:
        try:
            text = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            sys.exit(f"--clipboard needs macOS's pbpaste ({type(exc).__name__}). "
                     f"Use --file or pipe the text with --stdin instead.")
        if not text.strip():
            sys.exit("The clipboard is empty — copy the LinkedIn posting text first.")

    company, title, location, posted_at = args.company, args.title, args.location, args.posted_at

    # --- read the posting by URL ------------------------------------------
    #
    # Auto by default, because that is the whole point of the short form: if
    # the URL is a LinkedIn job URL and the fields are still missing, there is
    # nothing to lose by looking (and the alternative is a network-free error
    # telling the user to go and paste something they can see in a browser).
    # The gap is filled only where a field is MISSING, so an explicit --title
    # is never overwritten by the page's title — the same rule --infer follows.
    fetched: dict = {}
    employment_type = args.employment_type
    job_id = linkedin_fetch.job_id_from_url(args.url)
    if args.fetch is True and not job_id:
        sys.exit("--fetch reads LinkedIn job URLs only (…/jobs/view/<id>). "
                 f"For a posting elsewhere, pass the fields or paste the text: {args.url}")
    want_fetch = args.fetch if args.fetch is not None else bool(job_id) and not (company and title)
    if want_fetch:
        if not job_id:
            sys.exit(f"Could not find a LinkedIn job id in {args.url!r} — expected "
                     f"…/jobs/view/<id>.")
        try:
            fetched = linkedin_fetch.fetch_job(job_id)
        except linkedin_fetch.LinkedInFetchError as exc:
            # Reported, not fatal: everything below still works from a paste,
            # and the missing-field check will name what is actually absent.
            print(f"Could not read the posting from LinkedIn: {exc}", file=sys.stderr)
            fetched = {}
        else:
            for field, value in (("company", company), ("title", title),
                                 ("location", location), ("posted_at", posted_at)):
                if value or not fetched.get(field):
                    continue
                if field == "company":
                    company = fetched[field]
                elif field == "title":
                    title = fetched[field]
                elif field == "location":
                    location = fetched[field]
                else:
                    posted_at = fetched[field]
            if not text.strip():
                # Same rule as the fields: a paste the user supplied is their
                # own text and wins. The fetched advert is the fallback.
                text = fetched["description"]
            if employment_type is None:
                # Only ever sets "contract" (see employment_type_from_criteria):
                # LinkedIn's "Full-time" says nothing about permanence, so it is
                # left alone rather than guessed at.
                employment_type = linkedin_fetch.employment_type_from_criteria(
                    fetched.get("criteria", {}))

    inferred = {}
    if args.infer and text:
        inferred = parse_paste(text)
        # Explicit flags always win. Labelled paste values ("Company: X") are
        # trusted. Guessed values are NOT written silently: measured against the
        # 16 real postings in the CV repo, the heuristics alone got all three
        # fields right 3 times out of 16 and twice produced confident nonsense
        # (company="Senior Level", location="GBP75,000 + Benefits"), so they are
        # offered for confirmation instead.
        for field, current in (("company", company), ("title", title), ("location", location)):
            value = inferred.get(field)
            if not value or current:
                continue
            if inferred.get(f"{field}_how") == "labelled" or args.yes:
                if field == "company":
                    company = value
                elif field == "title":
                    title = value
                else:
                    location = value
        if not posted_at and inferred.get("posted_at"):
            posted_at = inferred["posted_at"]

    suggested = {
        f: inferred[f] for f in ("company", "title", "location")
        if inferred.get(f) and inferred.get(f"{f}_how") == "guessed"
        and inferred.get(f) not in (company, title, location)
    }
    if suggested and not args.yes and not args.dry_run:
        print("Guessed from the paste, but NOT used (they are unreliable — see --yes):")
        for f, v in suggested.items():
            print(f"  {f:9}: {v!r}   (from line {inferred.get(f'{f}_from')!r})")
        print("  Pass them explicitly, or re-run with --yes to accept these guesses.")

    missing = [name for name, val in (("--company", company), ("--title", title)) if not val]
    if missing:
        hint = " Pass them explicitly, or paste the posting and add --infer."
        if want_fetch and not fetched:
            hint = (" Reading the posting from its URL did not work (see above) — paste "
                    "it instead (--clipboard or --file) and add --infer, or pass them.")
        elif want_fetch and fetched:
            hint = " The LinkedIn page did not carry them — pass them explicitly."
        sys.exit(f"Missing required field(s): {', '.join(missing)}.{hint}")
    if location is None:
        location = ""  # stored as empty; the filters verdict will say so

    print("About to store:")
    print(f"  Company      : {company}")
    print(f"  Title        : {title}")
    print(f"  Location     : {location or '(not given)'}")
    print(f"  URL          : {canonical_url(args.url)}"
          + (f"\n                 (canonicalised from {args.url})"
             if canonical_url(args.url) != args.url else ""))
    from_page = bool(fetched) and text == fetched.get("description")
    print(f"  Description  : {len(text)} characters"
          f"{' (read from the LinkedIn page)' if from_page else ''}" if text
          else "  Description  : (none)")
    for field in ("company", "title", "location", "posted_at"):
        if inferred.get(field) in (company, title, location, posted_at) and inferred.get(f"{field}_from"):
            print(f"  {inferred.get(f'{field}_how', 'inferred'):8} {field:8}: "
                  f"from line {inferred[f'{field}_from']!r}")
    if fetched:
        # Say which of the stored values came off the page, so a value that was
        # NOT used (because you passed --title yourself) is visibly not used.
        stored = {"company": company, "title": title, "location": location, "posted_at": posted_at}
        used = [f for f, v in stored.items() if v and fetched.get(f) == v]
        if used:
            print(f"  {'fetched':8} {'/'.join(used)}: from the LinkedIn page "
                  f"(job {fetched['job_id']})")
        if fetched.get("criteria"):
            print("  Criteria     : " + "; ".join(f"{k}: {v}"
                                                 for k, v in fetched["criteria"].items()))
        if employment_type == "contract" and args.employment_type is None:
            print("  Employment   : contract — LinkedIn states a contract employment type "
                  "(override with --permanent)")
    if args.url and not _is_linkedin_job_url(args.url) and "linkedin" in args.url:
        print("  NOTE         : this looks like a LinkedIn search/listing URL, not a job URL "
              "(…/jobs/view/<id>) — the URL is the dedup key, so a search URL may collide "
              "with future postings.")

    report_filter_verdict(company, title, location, text)
    report_contract_terms(employment_type, title, location, text)

    if args.dry_run:
        print("  Result       : --dry-run, nothing written.")
        return

    with dedup.connect(db_path) as conn:
        inserted, message = add_job(
            conn, url=args.url, company=company, title=title, location=location,
            description=text, posted_at=posted_at, source=args.source, update=args.update,
            employment_type=employment_type,
        )
    print(f"  Result       : {message}")
    if not inserted:
        sys.exit(1)
    print("\nIt is now on the board (http://localhost:3000) and will be picked up by "
          "`python -m app.ai_evaluate`.")


if __name__ == "__main__":
    main()
