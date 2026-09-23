"""MVP orchestrator: fetch -> filter -> dedup -> write candidates.csv

Pulls from two kinds of sources:
  - companies.yaml   — one ATS board per known company (precise, no noise)
  - aggregators.yaml — keyword+location search across many employers at
                        once (broader reach, more noise — filters.py earns
                        its keep here)

Deliberately stops BEFORE the AI evaluation step for this first pass, so
you can eyeball the filtering quality on real data before spending any
Claude Haiku calls. Wiring in the AI scoring step (ai_evaluate.py) is the
next increment once this looks right.

Usage (run from the repo root):
    python -m app.main                       # everything: companies + aggregators
    python -m app.main --company affirm      # just one company, for debugging
    python -m app.main --skip-aggregators    # companies.yaml only
    python -m app.main --skip-companies      # aggregators.yaml only

Or `make run` for an interactive prompt instead of remembering flags.
"""
import argparse
import csv
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from app import aggregator_clients
from app import ats_clients
from app import contract_rates
from app import dedup
from app import discover_companies
from app import filters
load_dotenv()
OUTPUT_CSV = Path("data/candidates.csv")


def load_yaml_list(path: str, key: str) -> list[dict]:
    with open(path) as f:
        return yaml.safe_load(f)[key]


def process_jobs(jobs: list[dict], conn) -> list[dict]:
    """Apply filters + dedup to a batch of jobs from one source, marking
    every newly-seen job (whether it passed the content filters or not) so
    we never re-fetch/re-consider it on a future run. The full record
    (including the JD description) is always saved to job_details — see
    dedup.save_details — so candidates.csv can stay lean and human-readable
    while the description is still queryable later."""
    kept = []
    refreshed = 0
    for job in jobs:
        # Stamp freshness for EVERY job a fetch returned, new or not: this is
        # what tells the board a posting is still advertised. Done before the
        # is_new branch so both paths record the sighting.
        dedup.mark_listed(conn, job.get("url", ""))
        if not dedup.is_new(conn, job):
            # Already seen, so there is no new candidate here — but refresh
            # the contract columns from the copy just fetched. This is how a
            # posting stored before those columns existed ever acquires an
            # employment type, since dedup means it is never re-inserted.
            contract_rates.apply_to_job(job)
            job.update(filters.screening_signals(job))
            if dedup.refresh_contract_fields(conn, job):
                refreshed += 1
            continue
        dedup.mark_seen(conn, job)
        # Attach the contract verdict (employment type, IR35 status, day rate
        # and what that rate is worth as a salary) BEFORE filtering, so the
        # day-rate floor in filters.passes_filters can act on it, and so the
        # stored row carries the verdict for the board to display.
        contract_rates.apply_to_job(job)
        job.update(filters.screening_signals(job))
        passed = filters.passes_filters(job)
        dedup.save_details(conn, job, passed)
        if passed:
            kept.append(job)
        else:
            # Only count it as a day-rate rejection if it would otherwise
            # have been shown. Otherwise every non-QA posting that happens to
            # quote a low rate gets counted here, and the report reads as if
            # the money floor were rejecting hundreds of relevant roles.
            reason = contract_rates.reject_reason(job)
            if reason and filters.passes_content_filters(job):
                RATE_REJECTIONS.append((job, reason))
            clearance = filters.clearance_reject_reason(job)
            if clearance and _passes_except_clearance(job):
                CLEARANCE_REJECTIONS.append((job, clearance))
    if refreshed:
        REFRESHED[0] += refreshed
    return kept


# Contract postings dropped purely on day rate, collected across the whole
# run so `run()` can report the total once instead of burying it in the
# per-source line. Cleared at the start of every run.
RATE_REJECTIONS: list[tuple[dict, str]] = []
# Postings dropped because they demand clearance Ed does not hold. Kept
# separate from the rate list so the run report can say which rule fired.
CLEARANCE_REJECTIONS: list[tuple[dict, str]] = []


def _passes_except_clearance(job: dict) -> bool:
    """True when clearance is the ONLY reason a posting was dropped."""
    return (filters.title_is_relevant(job.get("title", ""))
            and filters.location_is_allowed(job.get("location", ""))
            and not filters.jd_stack_mismatch(job.get("description", "")))
# Count of already-seen jobs whose contract columns were refreshed — a
# one-element list so process_jobs can accumulate into it without a global.
REFRESHED = [0]


def run(companies: list[dict], aggregators: list[dict]) -> list[dict]:
    all_fetched = 0
    all_candidates = []
    RATE_REJECTIONS.clear()
    CLEARANCE_REJECTIONS.clear()
    REFRESHED[0] = 0

    with dedup.connect() as conn:
        for company in companies:
            try:
                jobs = ats_clients.fetch_company(company)
            except Exception as e:
                print(f"[WARN] {company['name']}: fetch failed — {e}", file=sys.stderr)
                continue
            all_fetched += len(jobs)
            kept = process_jobs(jobs, conn)
            all_candidates.extend(kept)
            print(f"{company['name']:40s} fetched={len(jobs):4d}  new_candidates={len(kept):3d}")

        for aggregator in aggregators:
            try:
                jobs = aggregator_clients.fetch_aggregator(aggregator)
            except Exception as e:
                print(f"[WARN] {aggregator['name']}: fetch failed — {e}", file=sys.stderr)
                continue
            all_fetched += len(jobs)
            kept = process_jobs(jobs, conn)
            all_candidates.extend(kept)
            print(f"{aggregator['name']:40s} fetched={len(jobs):4d}  new_candidates={len(kept):3d}")

    print(f"\nTotal fetched: {all_fetched}  |  Total new candidates after filters+dedup: {len(all_candidates)}")

    contracts = [j for j in all_candidates if j.get("employment_type") == "contract"]
    if contracts:
        print(f"  of which contract roles: {len(contracts)}")
    if REFRESHED[0]:
        print(f"  {REFRESHED[0]} already-seen job(s) had their contract details refreshed")
    if CLEARANCE_REJECTIONS:
        print(f"\n{len(CLEARANCE_REJECTIONS)} role(s) that would otherwise have been shown were dropped "
              f"for requiring clearance you do not hold:")
        for job, reason in CLEARANCE_REJECTIONS[:12]:
            print(f"  - {job['company'][:26]:26} {job['title'][:36]:36} {reason}")
        if len(CLEARANCE_REJECTIONS) > 12:
            print(f"  ... and {len(CLEARANCE_REJECTIONS) - 12} more")
        print("  Set clearance.reject_blocked: false in filters.yaml to keep and flag them instead.")

    if RATE_REJECTIONS:
        print(f"\n{len(RATE_REJECTIONS)} role(s) that would otherwise have been shown were dropped "
              f"on contract day rate:")
        for job, reason in RATE_REJECTIONS:
            print(f"  - {job['company'][:28]:28} {job['title'][:38]:38} {reason}")
        print("  Run `python -m app.contract_rates` to see the full arithmetic.")
        print("  Postings rejected here are stored with passed_filters=0, so "
              "`python -m app.refilter` can bring them back if you change contract.yaml.")
    return all_candidates


CSV_COLUMNS = ["company", "title", "location", "posted_at", "language_tier",
               "language_hits", "clearance_status", "employment_type",
               "ir35_status", "day_rate_min", "day_rate_max", "rate_verdict",
               "perm_equivalent", "url"]


def write_csv(candidates: list[dict], path: Path = OUTPUT_CSV) -> None:
    """Writes only CSV_COLUMNS, explicitly — deliberately NOT the whole job
    dict. `description` (and anything else added to the job schema later)
    lives in data/seen_jobs.sqlite3's job_details table instead; dumping a
    multi-KB JD into a CSV cell makes the file unreadable in Excel/Sheets.

    The contract columns ARE included: they are short scalars, and a
    spreadsheet is the natural place to sort a shortlist by day rate."""
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for c in candidates:
            row = dict(c)
            # Round the money so the CSV does not carry float noise like
            # 375.20900000000003 into a spreadsheet.
            for key in ("day_rate_min", "day_rate_max", "perm_equivalent"):
                if isinstance(row.get(key), float):
                    row[key] = round(row[key], 2)
            writer.writerow(row)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--company", help="Only run a single company slug, for debugging")
    parser.add_argument("--skip-aggregators", action="store_true")
    parser.add_argument("--skip-companies", action="store_true")
    parser.add_argument("--skip-discovery", action="store_true",
                         help="Don't auto-append newly-resolved Greenhouse/Lever companies to companies.yaml")
    args = parser.parse_args()

    companies = [] if args.skip_companies else load_yaml_list("companies.yaml", "companies")
    aggregators = [] if args.skip_aggregators else load_yaml_list("aggregators.yaml", "aggregators")

    if args.company:
        # Matched against the slug's FIRST path segment as well as the whole
        # slug, because workday slugs are two parts ("acme/acme") and the
        # thing you naturally type is the company: `--company acme`. An exact
        # comparison against the full slug would answer "No company with slug
        # 'acme'", which reads like the entry is missing when it is right
        # there. The whole-slug form still wins first, so a company whose short
        # name collides with another's tenant path can be addressed exactly.
        wanted = args.company.strip().lower()
        companies = [c for c in companies
                     if c["slug"].lower() == wanted
                     or c["slug"].split("/")[0].lower() == wanted]
        aggregators = []
        if not companies:
            sys.exit(f"No company with slug '{args.company}' in companies.yaml")

    candidates = run(companies, aggregators)
    write_csv(candidates)
    print(f"Wrote {len(candidates)} new candidates to {OUTPUT_CSV}")

    # Any Adzuna result whose full JD we fetched via a real Greenhouse/Lever
    # API call (see aggregator_clients.fetch_full_description) is a
    # verified-working (ats, slug) — worth tracking directly going forward
    # so future runs get that company's WHOLE board, not just whatever
    # Adzuna happened to surface this run.
    if not args.skip_discovery:
        added = discover_companies.append_new_companies(aggregator_clients.DISCOVERED_COMPANIES)
        if not added:
            print(f"No new companies were added to companies.yaml")
        if added:
            print(f"\nDiscovered {len(added)} new compan{'y' if len(added) == 1 else 'ies'} "
                  f"via Adzuna — appended to companies.yaml:")
            for c in added:
                print(f"  + {c['name']} ({c['ats']}: {c['slug']})")