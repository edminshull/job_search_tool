"""Finding the same job advertised twice under different URLs.

THE PROBLEM, MEASURED
---------------------
`dedup.is_new` treats a posting as already-seen when either its URL or an exact
`company::title::location` key matches. Both miss the way this actually happens:
an agency re-advertises the role it is still trying to fill, under a NEW advert
id, with the job title and the office address nudged.

The case that prompted this module, from this repo's own store:

    https://…/jobs/land/ad/5877123884   Experis  "AI Automation Tester X2"
                                        Farringdon, Central London   → applied
    https://…/jobs/details/5896693403   Experis  "AI Automation Tester"
                                        Fleet Street, Central London → NOT actioned

Same agency, same contract (Java/Cucumber, AI-driven features, a national-scale
digital transformation programme), re-advertised 15 days later. Different advert
id, so the URL check misses it; different title and different location string, so
the exact key misses it. It arrived on the board as fresh, un-actioned work with
a score of 72, and the only way to find out it was already applied to was to
click through to LinkedIn and read "Applied". Two clicks after that, a third
copy would have been added.

Across the 102 rows on this repo's board there were 12 such pairs — 3 of them
with one side un-actioned and the other already dealt with (Experis: applied,
Red Badger: skipped, Leep Talent: applied).

WHAT COUNTS AS A DUPLICATE
--------------------------
Three conditions, all required, chosen to keep false positives near zero because
the consequence of one is that a real job gets marked "applied":

  1. the same company, after stripping legal suffixes ("Hackajob Ltd" == "Hackajob");
  2. an IDENTICAL set of title content words, after dropping the filler that
     re-adverts churn — "X2", "Contract", "Remote", "(Fixed Term)" and the like.
     So "AI Automation Tester X2" == "AI Automation Tester" == "AI Automation
     Tester (Contract)". This is deliberately equality and not a similarity
     threshold: measured on the real board, Capco's "Principal SDET" and "Senior
     SDET" — genuinely different roles — sit at 0.75, while every true duplicate
     sits at 1.00. A threshold anywhere in between buys nothing and risks that;
  3. the locations are compatible — they name a city in common ("Farringdon,
     Central London" and "Fleet Street, Central London" are both London), or at
     least one of them names no city at all, which is evidence that one advert is
     vaguer than the other rather than that the two are in different places.

That last clause is the loose one, and it is deliberate: the Experis contract is
"Remote-first with occasional travel to London", so its next repost could easily
carry "UK" instead of a London address. Refusing to match a bare "UK" against
"London, UK" would defeat the rule exactly where it matters most. Both readings
were run against the real board and produce the same 12 pairs — the clause costs
nothing there and only decides cases where the title words are already identical
at the same company, where you would apply once either way.

WHAT IT DOES WITH A MATCH
-------------------------
The earliest row of a cluster is the original; every later one is pointed at it
with `job_details.duplicate_of`. If you had already committed to one of them —
applied, skipped, rejected — that decision is copied onto the re-adverts that
have no status of their own, recorded with `user_status.inherited_from` naming
the row it came from. Three rules keep that honest:

  * a row you have actioned yourself is NEVER overwritten — your decision outranks
    a derived one, always;
  * when the cluster contains a decision it is copied FROM, the copy prefers the
    earliest row that carries one, so the provenance is the first time you judged
    this job;
  * an inherited status is always labelled as inherited in the UI and is dropped
    the moment you set a status on that row yourself (see dedup.save_user_status).

The result is that a re-advert of a job you applied to arrives already marked
applied — it stops presenting itself as fresh work — while nothing is hidden from
you: the row is still on the board, still readable, with the original one click
away.
"""
import datetime
import re

from app import dedup

# Legal-entity noise on company names. Agencies are the main reposters here and
# they are inconsistent about it: "Hackajob Ltd" in one advert, "Hackajob" in the
# next. Only suffixes that carry no identity are stripped — nothing is removed
# that could distinguish two employers.
_COMPANY_NOISE = {
    "ltd", "limited", "plc", "llc", "inc", "gmbh", "bv", "sa", "ag",
    "group", "holdings", "uk", "the", "co",
}

# Words a re-advert churns without changing the job. Every one is a headcount
# marker ("X2"), an engagement marker ("Contract"), a work-arrangement marker
# ("Remote", "Hybrid") or pure urgency padding ("ASAP", "Urgent") — none of them
# says anything about WHICH role this is, which is the only question here.
_TITLE_NOISE = {
    "x2", "x3", "x4", "x5", "x6", "2x", "3x",
    "contract", "contractor", "contracting", "permanent", "temp", "temporary",
    "ftc", "fixed", "term", "interim",
    "remote", "hybrid", "onsite", "on-site", "homebased", "home",
    "job", "role", "vacancy", "position", "opportunity", "opening",
    "ref", "reference", "urgent", "urgently", "asap", "immediate", "immediately",
    "new", "now", "hiring", "wanted", "required", "needed",
    "day", "days", "week", "weeks", "month", "months", "year", "years",
    "start", "starting", "date", "duration", "rate", "salary",
}

# Cities that appear in this search's locations. The list exists to stop a
# repost's office move from looking like a different job — and to stop a genuine
# second city from being treated as the same job. It is a set of METROS, so
# London's sub-areas (Farringdon, The City, Bishopsgate, Aldgate, Shadwell, …)
# all resolve to london, which is what makes the Experis pair match.
#
# Named cities only. Nations are deliberately absent even though "Scotland" and
# "Wales" show up in real locations: because a match is "shares ANY city", adding
# "scotland" would make "Edinburgh, Scotland" and "Glasgow, Scotland" intersect
# and read as the same place — the opposite of what this list is for.
#
# Kept as a flat tuple rather than derived from filters.yaml on purpose: this is
# about place identity, not about which places are in scope, and a location the
# search has stopped allowing must still be recognisable as itself.
_CITIES = (
    "london", "bristol", "birmingham", "manchester", "newcastle", "edinburgh",
    "belfast", "coventry", "solihull", "leeds", "glasgow", "cardiff",
    "sheffield", "nottingham", "reading", "liverpool", "swindon", "croydon",
)


def company_key(company: str) -> str:
    """Company name reduced to what identifies the employer."""
    words = re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).split()
    return " ".join(w for w in words if w not in _COMPANY_NOISE)


# Phrases that pad a re-advert's title with a quantity rather than a role:
# "QA Engineer - 6 month contract" and "QA Engineer" are the same job, and so are
# "QA Automation Tester 2x" and "QA Automation Tester". Stripped as PHRASES, not as
# bare digits — "Software Engineer 3" is a level, and dropping the 3 would merge it
# with "Software Engineer 2", which are two different roles.
_TITLE_QUANTITY_RE = re.compile(
    r"\b\d+\s*(?:month|months|week|weeks|year|years|day|days|hour|hours|hr|hrs|"
    r"position|positions|role|roles|head|heads)\b"
    r"|\b[xX]\s*\d+\b|\b\d+\s*[xX]\b",
    re.IGNORECASE,
)


def title_tokens(title: str) -> frozenset:
    """The words of a title that say WHICH role this is."""
    stripped = _TITLE_QUANTITY_RE.sub(" ", title or "")
    words = re.sub(r"[^a-z0-9]+", " ", stripped.lower()).split()
    return frozenset(w for w in words if w not in _TITLE_NOISE)


# Locations that name no particular place — a country, a nation of the UK, or an
# explicit "anywhere". A re-advert of a remote-first role moves between these
# freely: the Experis contract below is "Remote-first with occasional travel to
# London", so its next repost could carry any of them instead of a London address.
_VAGUE_LOCATIONS = (
    "uk", "united kingdom", "gb", "great britain", "england", "britain",
    "remote", "homeworker", "home based", "home-based", "anywhere", "nationwide",
)


def cities_in(location: str) -> set:
    low = (location or "").lower()
    return {c for c in _CITIES if c in low}


def is_vague_location(location: str) -> bool:
    """True when the location names no particular place.

    Requires BOTH: no recognisable city, AND a country/remote token. The city
    check is not optional — "Bristol, UK" contains "UK" but is as specific as
    "Bristol" gets, and an earlier version that matched the token alone made every
    UK city comparable with every other one."""
    low = " ".join(re.sub(r"[^a-z0-9]+", " ", (location or "").lower()).split())
    if not low:
        return True
    if cities_in(location):
        return False
    # "Homeworker - UK" and "Remote (UK)" are as vague as "UK" itself.
    return any(v in low for v in _VAGUE_LOCATIONS)


def locations_compatible(a: str, b: str) -> bool:
    """Could these two location strings be the same place?

    Cities in common, or one of them naming no place in particular, or the two
    being the same string once punctuation is normalised.

    The vague clause is what lets "UK" match "London, UK" — and it is bounded on
    purpose. An earlier version of this rule said "either side names no city",
    which read every location outside the UK city list as unspecified and merged
    33 SumUp "Field Sales Executive" rows from 33 different countries into one
    cluster. Vagueness has to be a property of the STRING ("uk", "remote"), not
    of this module's ignorance of a place name.
    """
    ca, cb = cities_in(a), cities_in(b)
    if ca and cb:
        return bool(ca & cb)
    if is_vague_location(a) or is_vague_location(b):
        return True
    return _norm_location(a) == _norm_location(b)


def _norm_location(location: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (location or "").lower()).split())


def cluster_key(job: dict) -> tuple | None:
    """The (company, title-words) fingerprint rows are grouped by, or None when
    the row is too thin to judge — a job with no title or no company cannot be
    matched, and guessing would be worse than missing it."""
    company = company_key(job.get("company", ""))
    tokens = title_tokens(job.get("title", ""))
    if not company or not tokens:
        return None
    return (company, tuple(sorted(tokens)))


class DuplicateIndex:
    """The stored rows, indexed for near-duplicate lookups.

    Built once and reused rather than queried per job: the pipeline stores
    hundreds of jobs a run and this repo's store holds ~17k rows, so a scan per
    job would be tens of millions of row reads for no benefit. Grouping by an
    exact fingerprint also means no row is ever compared against another one —
    two rows match on the title by CONSTRUCTION if they share a key, which is
    what makes Jaccard-1.0 matching cheap instead of quadratic."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.by_key: dict[tuple, list[dict]] = {}
        for row in rows or []:
            self.add(row)

    def add(self, row: dict) -> None:
        """Index one more row.

        The pipeline needs this: the index is built once from the store, but a
        batch can contain a genuine duplicate PAIR, and the second of the two
        would otherwise be matched against a snapshot that predates the first.
        Inserting in chronological order keeps each group sorted, which is what
        makes the earliest row the original."""
        key = cluster_key(row)
        if key is None:
            return
        group = self.by_key.setdefault(key, [])
        group.append(row)
        group.sort(key=_chronological)

    @classmethod
    def from_db(cls, conn, where: str = "", params: tuple = ()) -> "DuplicateIndex":
        """Every stored row in scope. Not only passed_filters = 1 rows when the
        caller asks for the whole store: a re-advert has to be recognised against a
        first copy that may itself have been filtered out, and pointing at a
        filtered-out original is still the honest answer (the board will not link
        to it, but the audit prints the url)."""
        columns = ("url", "company", "title", "location", "fetched_at",
                   "duplicate_of", "passed_filters", "source")
        sql = f"SELECT {', '.join(columns)} FROM job_details {where}"
        return cls([dict(zip(columns, row)) for row in conn.execute(sql, params)])

    def original_for(self, job: dict) -> dict | None:
        """The stored row this job is a re-advert of, or None.

        The earliest stored row that is location-compatible with `job` — and
        STRICTLY NOT NEWER than it. The recency test is what stops the answer
        inverting: without it, asking about a row that is already in the index
        finds the next advert instead of an earlier one and reports the original as
        a duplicate of its own re-advert.

        For the pipeline's use this costs nothing. A job that has not been stored
        yet carries no fetched_at, which reads as "newer than everything already
        in the store" — which is exactly true."""
        key = cluster_key(job)
        if key is None:
            return None
        for candidate in self.by_key.get(key, []):
            if candidate["url"] == job.get("url"):
                continue  # a row cannot duplicate itself
            if self._is_not_newer(candidate, job) and locations_compatible(
                    candidate.get("location", ""), job.get("location", "")):
                return candidate
        return None

    @staticmethod
    def _is_not_newer(candidate: dict, job: dict) -> bool:
        if not job.get("fetched_at"):
            return True  # not stored yet, so older than nothing
        return _chronological(candidate) <= _chronological(job)

    def clusters(self) -> list[dict]:
        """Every group of two or more rows that describe the same job, as
        {"original": row, "duplicates": [row, ...]}, sorted by the original's url
        so the output is stable between runs.

        Greedy and root-anchored: rows are walked oldest first and join the first
        existing cluster whose ORIGINAL they are compatible with, otherwise they
        start one. Anchoring to the original rather than to the last member keeps
        the result independent of the order rows happen to be read in, and stops
        location compatibility (which is not transitive: London ~ "UK" ~ Bristol)
        from chaining London and Bristol into one cluster."""
        found: list[dict] = []
        for group in self.by_key.values():
            open_clusters: list[dict] = []
            for row in group:  # already chronological
                placed = False
                for cluster in open_clusters:
                    if locations_compatible(cluster["original"].get("location", ""),
                                            row.get("location", "")):
                        cluster["duplicates"].append(row)
                        placed = True
                        break
                if not placed:
                    open_clusters.append({"original": row, "duplicates": []})
            found.extend(c for c in open_clusters if c["duplicates"])
        found.sort(key=lambda c: c["original"]["url"])
        return found


def _chronological(row: dict) -> tuple:
    """Oldest first. fetched_at is this repo's own timestamp, so unlike posted_at
    — which arrives as epoch millis, ISO strings with timezones, and free text
    like "14 hours ago" — it is always comparable. The url breaks ties so two
    rows stored in the same second still sort deterministically."""
    return (row.get("fetched_at") or "", row.get("url") or "")


def index_row(job: dict) -> dict:
    """A just-stored job in the shape the index wants.

    fetched_at is set to NOW rather than read back from SQLite, in SQLite's own
    CURRENT_TIMESTAMP format so it compares correctly against stored values. The
    row was stored a moment ago and is therefore the newest, so it has to sort
    LAST — giving it an empty fetched_at would sort it first and let a job become
    the original of the very row it was detected against."""
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "url": job.get("url"),
        "company": job.get("company"),
        "title": job.get("title"),
        "location": job.get("location"),
        "fetched_at": now,
        "duplicate_of": job.get("duplicate_of"),
    }


def find_duplicate(conn, job: dict, *, index: DuplicateIndex | None = None) -> dict | None:
    """Convenience wrapper for a single job."""
    return (index or DuplicateIndex.from_db(conn)).original_for(job)


def detect(job: dict, index: "DuplicateIndex") -> dict | None:
    """Mark `job` as a re-advert and return the original row, or None.

    Writes nothing: `job["duplicate_of"]` is set for the CALLER to store, so the
    link travels with the row through one save (dedup.save_details) instead of
    being a second UPDATE that a later failure could lose. Status inheritance is
    separate and happens after the row exists — see inherit_status."""
    original = index.original_for(job)
    if original is None:
        return None
    job["duplicate_of"] = original["url"]
    return original


def inherit_status(conn, url: str) -> bool:
    """Give `url` the decision this job's cluster already carries, if it has none.

    Called AFTER the row is stored, because it resolves the cluster through the
    `duplicate_of` links in the database rather than through the index.

    Note this is not simply "copy the original's status": the decision can sit on
    any advert of the job. Red Badger's board pair is exactly that — the older
    advert is the original and carries nothing, while the "skipped" sits on the
    newer one — so looking only at the original would leave the copy you have not
    judged still looking like fresh work, which is the whole problem."""
    if not url:
        return False
    own = conn.execute(
        "SELECT my_status FROM user_status WHERE url = ?", (url,)).fetchone()
    if own and own[0]:
        return False  # you decided this one yourself; a derived value never wins
    found = decision_source(conn, url)
    if found is None:
        return False
    source, status = found
    return dedup.save_inherited_status(conn, url, status, source["url"])


def cluster_members(conn, url: str) -> list[dict]:
    """Every stored advert of the same job as `url`, oldest first.

    Resolved through `duplicate_of` alone. The matching rules in this module are
    applied once, when the links are written; every later reader just follows
    them, so there is no second implementation of "what is a duplicate" to drift
    out of step — and no need to re-run the (comparatively expensive) grouping on
    a status change."""
    root = conn.execute(
        "SELECT COALESCE(duplicate_of, url) FROM job_details WHERE url = ?", (url,)).fetchone()
    if root is None:
        return []
    columns = ("url", "company", "title", "location", "fetched_at", "duplicate_of")
    members = [
        dict(zip(columns, row))
        for row in conn.execute(
            f"SELECT {', '.join(columns)} FROM job_details WHERE url = ? OR duplicate_of = ?",
            (root[0], root[0]))
    ]
    members.sort(key=_chronological)
    return members


def decision_source(conn, url: str) -> tuple[dict, str] | None:
    """The (row, status) this job's cluster was decided by, or None.

    The EARLIEST advert that carries a status — deliberately not "the original".
    Your first judgement about a job is the one that should stand, whichever of
    its adverts you happened to be looking at when you made it."""
    for member in cluster_members(conn, url):
        row = conn.execute(
            "SELECT my_status FROM user_status WHERE url = ?", (member["url"],)).fetchone()
        if row and row[0]:
            return member, row[0]
    return None
