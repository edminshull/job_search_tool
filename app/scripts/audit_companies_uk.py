"""Audit companies.yaml for UK/London relevance — ONE list request per company.

Written 2026-09 while retuning this repo from the original author's market
(Alberta/Canada) to Ed's (London/UK). companies.yaml ships as the original
author's 204 target companies, and the README says to edit it, but "which of
these actually post UK roles?" is not answerable by reading company names:
plenty of US-headquartered companies hire in London, and plenty don't.

So this asks the ATS boards directly, using the same clients the pipeline uses
(ats_clients.fetch_company), and counts postings whose location string looks
London / UK-wide. It is deliberately cheap: greenhouse, ashby, lever and
workable list endpoints return every posting's location in ONE request, and
for smartrecruiters (the only fetcher that gates a per-posting detail fetch on
the filters) the filter is neutered so no details are downloaded at all.

Run:  python -m app.scripts.audit_companies_uk
      python -m app.scripts.audit_companies_uk --csv /tmp/uk_audit.csv
"""
import argparse
import csv
import re
import sys
import time

from app import ats_clients, filters

# Deliberately looser than filters.yaml's location_allow_patterns, which is
# tuned for what Ed will APPLY to. Here we want to know whether a company has
# any UK footprint at all, so "Manchester" or "Edinburgh" still counts as a
# finding worth reporting, separately from London.
LONDON_RE = re.compile(r"\blondon\b", re.IGNORECASE)
UK_RE = re.compile(
    r"\b(london|united kingdom|great britain|england|scotland|wales|"
    r"northern ireland|belfast|manchester|edinburgh|glasgow|bristol|leeds|"
    r"birmingham|cambridge|oxford|cardiff|sheffield|nottingham|uk)\b",
    re.IGNORECASE,
)


class _NoDetailFetches:
    """Context manager that stops the smartrecruiters fetcher from spending an
    extra request per posting, without changing what it returns. It returns
    every posting either way; the filter only gates the description fetch."""

    def __enter__(self):
        self._real = filters.title_is_relevant
        filters.title_is_relevant = lambda title: False
        return self

    def __exit__(self, *exc):
        filters.title_is_relevant = self._real
        return False


def audit(companies: list[dict], verbose: bool = True) -> list[dict]:
    # TWO PHASES, and the split is load-bearing. Phase 1 fetches EVERY company
    # inside _NoDetailFetches, where filters.title_is_relevant is temporarily
    # replaced by `lambda: False` — that is what stops the smartrecruiters
    # fetcher from downloading a description per posting. Phase 2 then scores
    # the results AFTER that patch is undone.
    #
    # Scoring inside phase 1 silently produces zero candidates for every
    # company, because the neutered predicate is still installed. That is not
    # hypothetical: the first version of this script did exactly that and
    # reported "0 candidates across 204 companies" with complete confidence.
    fetched: list[tuple[dict, list[tuple[str, str]], str]] = []
    with _NoDetailFetches():
        for i, company in enumerate(companies, 1):
            try:
                jobs = ats_clients.fetch_company(company)
                pairs = [(j.get("title", ""), j.get("location") or "") for j in jobs]
                fetched.append((company, pairs, ""))
            except Exception as exc:  # a dead slug must not kill the audit
                fetched.append((company, [], f"{type(exc).__name__}: {str(exc)[:80]}"))
            if verbose:
                print(f"[fetch {i:3d}/{len(companies)}] {company['name'][:34]:34} "
                      f"{company['ats']:16} {len(fetched[-1][1]):5d} postings", flush=True)
            time.sleep(0.05)  # be polite to the ATS APIs

    rows = []
    for company, pairs, error in fetched:
        row = {"name": company["name"], "ats": company["ats"], "slug": company["slug"],
               "postings": len(pairs), "london": 0, "uk": 0, "candidates": 0,
               "candidate_titles": "", "error": error}
        hits = []
        for title, loc in pairs:
            if LONDON_RE.search(loc):
                row["london"] += 1
            if UK_RE.search(loc):
                row["uk"] += 1
            # The pipeline's real gate: title + location. jd_stack_mismatch
            # needs the JD text, which a list-only audit never downloads, so it
            # can only ever remove MORE candidates than this counts.
            if filters.title_is_relevant(title) and filters.location_is_allowed(loc):
                hits.append(f"{title} [{loc}]")
        row["candidates"] = len(hits)
        row["candidate_titles"] = " | ".join(hits[:8])
        rows.append(row)

    if verbose:
        print()
        for i, r in enumerate(rows, 1):
            flag = f"{r['candidates']} CANDIDATE(S)" if r["candidates"] else (
                "London" if r["london"] else ("uk" if r["uk"] else ""))
            status = f"ERROR {r['error']}" if r["error"] else f"{r['postings']:4d} postings"
            print(f"[{i:3d}/{len(rows)}] {r['name'][:34]:34} {r['ats']:16} {status:28} {flag}",
                  flush=True)
    return rows


def report(rows: list[dict]) -> None:
    ok = [r for r in rows if not r["error"]]
    with_london = sorted([r for r in ok if r["london"]], key=lambda r: -r["london"])
    with_uk = [r for r in ok if r["uk"] and not r["london"]]
    none_uk = [r for r in ok if not r["uk"]]
    with_candidates = sorted([r for r in ok if r["candidates"]], key=lambda r: -r["candidates"])

    print("\n" + "=" * 78)
    print(f"Audited {len(rows)} companies: {len(ok)} responded, {len(rows) - len(ok)} errored")
    print(f"  London postings   : {sum(r['london'] for r in ok)} across {len(with_london)} companies")
    print(f"  UK (non-London)   : {sum(r['uk'] for r in ok) - sum(r['london'] for r in ok)} "
          f"across {len(with_uk)} companies")
    print(f"  No UK postings    : {len(none_uk)} companies")
    print(f"  *** CANDIDATES (title+location match): {sum(r['candidates'] for r in ok)} "
          f"across {len(with_candidates)} companies")
    print("=" * 78)

    print("\n--- Companies that would actually produce CANDIDATES for this search ---")
    if not with_candidates:
        print("  NONE. Every company in companies.yaml is pruned or yields no QA/SDET role.")
    for r in with_candidates:
        print(f"  {r['candidates']:3d} candidate(s) / {r['postings']:4d} postings  {r['name']} ({r['ats']})")
        if r["candidate_titles"]:
            print(f"        {r['candidate_titles'][:150]}")

    print("\n--- Companies with LONDON postings but no matching QA/SDET title ---")
    for r in with_london:
        if not r["candidates"]:
            print(f"  {r['london']:4d} london / {r['postings']:4d} total  {r['name']} ({r['ats']})")

    if with_uk:
        print("\n--- UK postings but NO London ---")
        for r in with_uk:
            print(f"  {r['uk']:4d} uk / {r['postings']:4d} total  {r['name']} ({r['ats']})")

    errors = [r for r in rows if r["error"]]
    if errors:
        print("\n--- Errored (dead slug / no fetcher) ---")
        for r in errors:
            print(f"  {r['name'][:34]:34} {r['ats']:16} {r['error']}")

    print(f"\n--- {len(none_uk)} companies with no UK postings at all (prune candidates) ---")
    for r in sorted(none_uk, key=lambda r: r["name"]):
        print(f"  {r['name']} ({r['ats']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", help="write the full per-company table here")
    ap.add_argument("--limit", type=int, help="audit only the first N companies (debugging)")
    args = ap.parse_args()

    import yaml
    companies = yaml.safe_load(open("companies.yaml"))["companies"]
    if args.limit:
        companies = companies[: args.limit]

    rows = audit(companies)
    report(rows)

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nWrote {args.csv}")


if __name__ == "__main__":
    sys.exit(main())
