"""Queue postings whose stored description is an Adzuna listing page for
re-fetching by the next pipeline run.

WHY THIS EXISTS
---------------
Adzuna's search API returns only a short snippet, so `fetch_full_description`
follows the listing's redirect to get the real advert text. For some listings
that redirect lands on a SEARCH-RESULTS page, and the scrape stored half a
dozen OTHER jobs as this posting's description. Measured 2026-09-21: 79 of 336
stored postings, 53 of the 72 on the board.

That mattered in three ways, all real:

  * Capgemini's "SAP Lead Automation Test Engineer" was rejected as a
    Playwright-only role assembled from four other companies' adverts;
  * "£350 to £550 per day" belonging to an NTT DATA / INNOVATIVE TECH PEOPLE
    advert was shown against it as its own stated rate;
  * the AI step was being asked to assess roles using other companies' tech
    stacks.

The readers are now protected (app/jd_text.py refuses the body and uses the
title), and the ROOT CAUSE is fixed in fetch_full_description, which no longer
accepts a listing page as a job description. What that fix cannot do is repair
rows already stored, because the polluted text does not identify back to an
advert: re-fetching the same URL returns the same listing page. Verified —
eight sample rows were re-fetched directly and every one returned the listing
page again.

So this does the one thing that DOES work: it clears the dedup key for those
postings, which makes the next `python -m app.main` treat them as new and
re-store them. Because the root cause is fixed, that re-fetch now falls back to
Adzuna's own snippet — which is short, but is about the right job, unlike the
listing page.

NOTHING IS DELETED. The job_details rows, AI evaluations, your statuses and
your notes all stay exactly as they are; only the `seen_jobs` dedup entry is
removed so the pipeline looks at the posting again. If a posting is no longer
within its query's date window it simply does not come back, and keeps its
current text with the readers falling back to the title.

Usage (run from the repo root):
    python -m app.scripts.queue_polluted_descriptions --dry-run
    python -m app.scripts.queue_polluted_descriptions            # queue them
    python -m app.main --skip-companies                          # then re-fetch
"""
import argparse
import sys

from app import dedup
from app import jd_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report, but write nothing")
    parser.add_argument("--only-board", action="store_true",
                        help="only rows currently on the board (passed_filters = 1)")
    parser.add_argument("--db", default=None)
    args = parser.parse_args(argv)

    with dedup.connect(args.db or dedup.DB_PATH) as conn:
        where = "WHERE passed_filters = 1" if args.only_board else ""
        rows = conn.execute(
            f"SELECT url, company, title, description FROM job_details {where}").fetchall()

        polluted = [r for r in rows if jd_text.looks_like_listing_page(r[3] or "")]

        print(f"{len(polluted)} of {len(rows)} stored posting(s) have a listing page rather "
              f"than an advert stored as their description.\n")
        if not polluted:
            return 0

        cleared = 0
        for url, company, title, desc in polluted:
            marker = jd_text.listing_page_marker(desc or "")
            if args.dry_run:
                print(f"  QUEUE  {str(company)[:26]:26} {str(title)[:36]:36} ({marker})")
                continue
            # Removes the dedup key only. job_details — and therefore the AI
            # evaluation, my_status and notes, which live in their own tables
            # keyed by url — is untouched.
            cur = conn.execute("DELETE FROM seen_jobs WHERE url = ?", (url,))
            if cur.rowcount:
                cleared += 1
                print(f"  QUEUED {str(company)[:26]:26} {str(title)[:36]:36} ({marker})")

        if args.dry_run:
            print(f"\n(dry run — nothing written; drop --dry-run to queue all {len(polluted)})")
        else:
            print(f"\n{cleared} posting(s) queued for re-fetch.")
            print("Now run:  python -m app.main --skip-companies")
            print("then:     python -m app.scripts.backfill_derived_fields")
    return 0


if __name__ == "__main__":
    sys.exit(main())
