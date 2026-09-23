"""Re-derive the columns on job_details that are COMPUTED from the posting
itself: the contract verdict (IR35, day rate) and the screening signals
(language tier, clearance).

Both sets of columns were added in 2026-09. Every row fetched before that has
them NULL, so an older posting already in the DB shows as blank on the board
even when its description plainly states a day rate or names Java — and, more
importantly, a clearance-gated posting stored before the rule existed stays on
the board despite being unreachable.

This re-reads each stored description — nothing is re-fetched — and writes
the derived fields back. It is idempotent: running it twice produces the same
result, because it recomputes from the stored text every time. That also makes
it the tool to run after editing filters.yaml's language tiers or clearance
rules, so already-stored postings are re-judged without a re-fetch.

It deliberately does NOT touch passed_filters by default. A stored job that
already passed stays passed even if it now looks out of scope, so tightening a
rule cannot silently empty the board of jobs you were already looking at. Pass
--apply-rejections when you do want the new rules enforced on stored rows.

Usage (run from the repo root):
    python -m app.scripts.backfill_derived_fields --dry-run
    python -m app.scripts.backfill_derived_fields
    python -m app.scripts.backfill_derived_fields --apply-rejections
"""
import argparse
import sys

from app import contract_rates
from app import dedup
from app import filters
from app import jd_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report, but write nothing")
    parser.add_argument("--apply-rejections", action="store_true",
                        help="also demote stored jobs that the CURRENT policy rejects "
                             "(title, location, language, clearance or day rate). "
                             "python -m app.refilter does the same thing AND rescues jobs "
                             "that now pass; use that if you changed a rule in both directions.")
    parser.add_argument("--db", default=None, help="override the SQLite path")
    parser.add_argument("--clean-descriptions", action="store_true",
                        help="also rewrite stored Adzuna listing pages down to just the advert "
                             "body, dropping the site navigation and the other companies' "
                             "listings that share the page. No network calls: the advert is "
                             "already inside the stored text. See app/jd_text.py.")
    args = parser.parse_args(argv)

    model = contract_rates.default_model()
    thresholds = model.day_rates()

    with dedup.connect(args.db or dedup.DB_PATH) as conn:
        rows = list(dedup.iter_passed(conn)) + list(dedup.iter_filtered_out(conn))

        contracts = rates_found = demoted = cleaned = 0
        by_verdict: dict[str, int] = {}
        by_language: dict[str, int] = {}
        by_clearance: dict[str, int] = {}
        for job in rows:
            if args.clean_descriptions and not args.dry_run \
                    and jd_text.looks_like_listing_page(job.get("description") or ""):
                body = jd_text.extract_advert_body(job.get("description") or "")
                if body:
                    job["description"] = body
                    dedup.save_description(conn, job["url"], body)
                    cleaned += 1
            contract_rates.apply_to_job(job, model)
            job.update(filters.screening_signals(job))
            if job.get("employment_type") == "contract":
                contracts += 1
            if job.get("day_rate_max") is not None:
                rates_found += 1
            verdict = job.get("rate_verdict") or "unknown"
            by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
            tier = job.get("language_tier") or "unknown"
            by_language[tier] = by_language.get(tier, 0) + 1
            clearance = job.get("clearance_status") or "unknown"
            by_clearance[clearance] = by_clearance.get(clearance, 0) + 1

            if args.dry_run:
                continue
            dedup.save_derived_fields(conn, job)
            if not args.apply_rejections or not job.get("passed_filters"):
                continue
            # filters.rejection_reason is the single place that knows the
            # whole current policy — title, location, language, clearance and
            # the contract floor. An earlier version of this loop named only
            # the rate and clearance rules, so turning --apply-rejections on
            # silently skipped the ten language mismatches: it reported 11
            # demotions when the real answer was 20.
            if filters.rejection_reason(job):
                dedup.set_passed_filters(conn, job["url"], False)
                demoted += 1

        print(f"Re-evaluated {len(rows)} stored job(s) against contract.yaml")
        print(f"  2026/27 floors: inside IR35 £{thresholds['inside_ir35']['day_rate_gbp']:,.0f}/day, "
              f"outside IR35 £{thresholds['outside_ir35']['day_rate_gbp']:,.0f}/day "
              f"({model.days:.0f} billable days)")
        print(f"  contract postings identified : {contracts}")
        print(f"  with a day rate (stated or derived): {rates_found}")
        print(f"  rate verdicts: " + ", ".join(f"{k}={v}" for k, v in sorted(by_verdict.items())))
        print(f"  language tiers: " + ", ".join(f"{k}={v}" for k, v in sorted(by_language.items())))
        print(f"  clearance: " + ", ".join(f"{k}={v}" for k, v in sorted(by_clearance.items())))
        if args.clean_descriptions:
            print(f"  listing pages cleaned to the advert body: {cleaned}")
        if args.apply_rejections:
            print(f"  demoted (now out of scope)   : {demoted}")
        elif not args.dry_run:
            print("  passed_filters untouched — re-run with --apply-rejections to enforce the day-rate "
                  "floor and the clearance rule on already-stored rows")
        if args.dry_run:
            print("\n(dry run — nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
