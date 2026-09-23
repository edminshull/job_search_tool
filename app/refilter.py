"""Re-run the CURRENT filters.py against jobs already stored in
data/seen_jobs.sqlite3 that were previously filtered out — no re-fetch from
any ATS/aggregator needed, since job_details keeps the full record
(including description) for every job ever seen, pass or fail.

Use this whenever you tweak filters.py (add a keyword, loosen a location
pattern, etc.) and want previously-dropped jobs to get a second look
without waiting for the next scheduled run to re-discover them — which it
wouldn't anyway, since seen_jobs already marks them as seen.

Only moves jobs from filtered-out -> passed (or the reverse, if you
TIGHTENED a filter) by flipping job_details.passed_filters. Nothing is
re-fetched, nothing is deleted. Jobs that flip to passed become visible in
the web board immediately and show up in
dedup.get_unevaluated_candidates() for the next ai_evaluate.py run.

Usage (run from the repo root):
    python -m app.refilter                 # apply the flips
    python -m app.refilter --dry-run       # show what would change, don't write
"""
import sys

from app import contract_rates
from app import dedup
from app import filters


def main(dry_run: bool = False) -> None:
    with dedup.connect() as conn:
        filtered_out = list(dedup.iter_filtered_out(conn))
        print(f"Re-checking {len(filtered_out)} previously filtered-out job(s)...\n")

        rescued = []
        for job in filtered_out:
            # Re-derive the contract verdict AND the screening signals from
            # the STORED text before re-filtering, so a change to
            # contract.yaml's day-rate thresholds, filters.yaml's language
            # tiers, or the clearance rules is honoured here — not just a
            # change to the title/location lists. Nothing is re-fetched.
            contract_rates.apply_to_job(job)
            job.update(filters.screening_signals(job))
            if filters.passes_filters(job):
                rescued.append(job)
                print(f"RESCUED  | {job['company']:20s} | {job['title'][:55]:55s} | {job['url']}")
                if not dry_run:
                    dedup.set_passed_filters(conn, job["url"], True)
                    dedup.save_derived_fields(conn, job)

        print(f"\n{len(rescued)}/{len(filtered_out)} now pass the current filters.")

        passed = list(dedup.iter_passed(conn))
        print(f"\nRe-checking {len(passed)} previously-passed job(s) for a tightened filter...\n")

        demoted = []
        for job in passed:
            contract_rates.apply_to_job(job)
            job.update(filters.screening_signals(job))
            if not filters.passes_filters(job):
                demoted.append(job)
                print(f"DEMOTED  | {job['company']:20s} | {job['title'][:55]:55s} | {job['url']}")
                if not dry_run:
                    dedup.set_passed_filters(conn, job["url"], False)
                    dedup.save_derived_fields(conn, job)

        print(f"\n{len(demoted)}/{len(passed)} no longer pass the current filters.")

        if dry_run:
            print("\n(dry run — no changes written; drop --dry-run to apply)")
        else:
            print("\njob_details.passed_filters updated. Re-run ai_evaluate.py to score the newly rescued jobs,")
            print("and refresh the Next.js board to see them.")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)