"""Show — and optionally fix — the same job advertised twice under different URLs.

WHY THIS EXISTS
---------------
`dedup.is_new` catches a repost when the URL or the exact
`company::title::location` key repeats. Agencies defeat both by re-advertising
the role under a new advert id with the title and the office address nudged, so
the copy arrives on the board looking like fresh work. The case that prompted
this: an Experis contract marked applied on 9 Sept came back on 24 Sept as
"AI Automation Tester" at a different London address, un-actioned, scoring 72 —
and the only way to tell was to click through to LinkedIn and read "Applied".

By default this REPORTS and writes nothing, because the second half of what it
can do changes what you see:

    python -m app.find_duplicates              # what is duplicated on the board?
    python -m app.find_duplicates --all        # ...including rows the filters dropped
    python -m app.find_duplicates --apply      # link them and carry the status over

`--apply` does two things per cluster (see app/duplicates.py for the rules):

  * records `job_details.duplicate_of`, so the board can show a re-advert as a
    re-advert and link to the original;
  * copies a decision you had ALREADY made — applied, skipped, rejected — onto
    the copies that have no decision of their own, marked
    `user_status.inherited_from` so it is never mistaken for something you chose.

A status you set yourself is never overwritten, and running it twice changes
nothing the second time. It is safe to re-run after you apply to something, which
is also how the other copies of that job get updated.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not delete or hide anything. A duplicate row stays on the board, readable,
with the original one link away — the judgement that two adverts are the same job
is a heuristic, and a heuristic that silently removes a posting is a worse failure
than the duplicate it was fixing. Use `python -m app.add_job --remove "<url>"` for
the copies you decide you do not want.
"""
import argparse
import sys

from app import dedup
from app import duplicates

# The board's own scope. Every status you have ever set lives on a passed row, and
# `--all` is dominated by noise that cannot affect you: the ~17k stored rows
# include 25-country reposts of the same advert, so a full-store audit reports
# ~685 clusters against 8 on the board. Useful when something looks wrong in the
# store; not the default.
BOARD_ONLY = "WHERE passed_filters = 1"


def _describe(row: dict, conn) -> str:
    status = dedup.get_user_status(conn, row["url"]) or {}
    bits = [row.get("location") or "no location"]
    if row.get("source"):
        bits.append(row["source"])
    if row.get("fetched_at"):
        bits.append(f"stored {row['fetched_at']}")
    line = f"      {' · '.join(bits)}"
    state = status.get("my_status")
    if state and status.get("inherited_from"):
        line += f"\n      you: {state} (inherited)"
    elif state:
        line += f"\n      you: {state}"
    else:
        line += "\n      you: nothing yet"
    return line


def _members(cluster: dict) -> list[dict]:
    """The cluster oldest-first: the original, then its re-adverts.

    DuplicateIndex.clusters() already walks each group in chronological order, so
    the original is first and the rest follow — which is what lets the decision
    source be picked as "the earliest advert that carries a status"."""
    return [cluster["original"], *cluster["duplicates"]]


def _decision_source(conn, cluster: dict) -> tuple[dict, str] | None:
    """(row, status) for the earliest advert in this cluster you decided, or None."""
    for member in _members(cluster):
        status = (dedup.get_user_status(conn, member["url"]) or {}).get("my_status")
        if status:
            return member, status
    return None


def _would_inherit(conn, cluster: dict) -> dict[str, str]:
    """{url: status} for the re-adverts that would take on a decision they lack."""
    found = _decision_source(conn, cluster)
    if found is None:
        return {}
    source, status = found
    out = {}
    for member in _members(cluster):
        if member["url"] == source["url"]:
            continue
        if not (dedup.get_user_status(conn, member["url"]) or {}).get("my_status"):
            out[member["url"]] = status
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="write the links and inherit the statuses. Without this "
                             "the run reports and changes nothing.")
    parser.add_argument("--all", action="store_true",
                        help="audit every stored row, not just the ones on the board "
                             "(much noisier: multi-country reposts cluster by the "
                             "thousands, and none of them can appear on the board)")
    parser.add_argument("--db", default=None, help="override the SQLite path")
    args = parser.parse_args(argv)

    where = "" if args.all else BOARD_ONLY
    with dedup.connect(args.db or dedup.DB_PATH) as conn:
        index = duplicates.DuplicateIndex.from_db(conn, where)
        clusters = index.clusters()
        scope = "every stored row" if args.all else "the board"
        total = sum(1 for _ in conn.execute(
            f"SELECT url FROM job_details {where}"))

        if not clusters:
            print(f"No duplicates found among {total} row(s) in scope ({scope}).")
            return 0

        n_dup = sum(len(c["duplicates"]) for c in clusters)
        print(f"{len(clusters)} duplicate cluster(s) among {total} row(s) in scope "
              f"({scope}): {n_dup} row(s) are a re-advert of another.\n")

        linked = inherited = 0
        for cluster in clusters:
            original = cluster["original"]
            pending = _would_inherit(conn, cluster)
            source = _decision_source(conn, cluster)
            print(f"  {original['company']} — {original['title']}")
            print(f"      {original['url']}")
            print(_describe(original, conn))
            if source and source[0]["url"] == original["url"]:
                print("      ← the decision this job carries")
            elif original["url"] in pending:
                print(f"      would inherit: {pending[original['url']]}")
            for dup in cluster["duplicates"]:
                print(f"    ↳ re-advert: {dup['title']}")
                print(f"      {dup['url']}")
                print(_describe(dup, conn))
                if source and source[0]["url"] == dup["url"]:
                    print("      ← the decision this job carries")
                elif dup["url"] in pending:
                    print(f"      would inherit: {pending[dup['url']]}")
            print()

        if args.apply:
            for cluster in clusters:
                original = cluster["original"]
                for dup in cluster["duplicates"]:
                    if dup.get("duplicate_of") != original["url"]:
                        dedup.set_duplicate_of(conn, dup["url"], original["url"])
                        linked += 1
                # Every advert that has no decision of its own takes the
                # cluster's — including the ORIGINAL, when the decision was made
                # on a later advert of the same job.
                for url in _would_inherit(conn, cluster):
                    if duplicates.inherit_status(conn, url):
                        inherited += 1
            print(f"Linked {linked} row(s) to their original; "
                  f"inherited {inherited} status(es).")
        else:
            would = sum(len(_would_inherit(conn, c)) for c in clusters)
            print("Nothing written. Re-run with --apply to record the links"
                  + (f" and inherit {would} status(es)" if would else "")
                  + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
