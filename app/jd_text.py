"""Deciding whether a stored "description" is actually the advert.

WHY THIS EXISTS (found 2026-09-21)
----------------------------------
Adzuna's search API only returns a short snippet, so `aggregator_clients.
fetch_full_description` follows `redirect_url` and scrapes the page for the
real text. For some listings that redirect lands on an Adzuna SEARCH-RESULTS
page rather than a single advert, and the scrape then returns a page listing
half a dozen other jobs — with their titles, their salaries and their
languages.

Measured on the live corpus: 79 of 336 stored postings (23%), and 53 of the
72 on the board, have a description of that kind. The consequences were not
cosmetic:

  * `parse_day_rates` read "From £350 to £550 per day" off the page — a rate
    belonging to a DIFFERENT advert — and put it on the board as this job's
    rate, marked "stated".
  * `language_fit` saw five languages at once. Four unrelated postings
    (two IBM, Anson Mccade, ITV) all reported the identical hit list
    "java, javascript, c#, python, playwright", which is a signature of the
    listing page rather than of any one advert.
  * Capgemini's "SAP Lead Automation Test Engineer" was REJECTED as a
    Playwright-only role on the strength of another company's advert.

So the derived signals are only as good as the text they read, and a listing
page is not this job's text. `advert_text` is the single place that decides
what may be read, and it falls back to the TITLE when the body cannot be
trusted — a title is always this advert's own.
"""
import re

# Markers that appear on an Adzuna results/landing page but never in a single
# advert. Kept as separate named patterns so a failure can say which one fired.
#
# NOTE the absence of "per year - estimated". It looks like a listing marker
# and was one originally, but Adzuna puts the SAME line on the header of a
# single advert, so after the body extractor started working it flagged 26
# already-clean descriptions as pages and made advert_text re-extract them
# pointlessly. Verified before removing: all 26 had no footer, no "back to
# last search", no search widget, and exactly one "Apply for this job" — i.e.
# they were single adverts. It is a header, not a discriminator.
LISTING_PAGE_MARKERS: list[tuple[str, str]] = [
    ("back-to-search", r"\u276e\s*back to last search|back to last search"),
    ("search-widget", r"What\?\s*Where\?\s*Search"),
    ("search-advanced", r"Search\s+Advanced"),
    ("job-in-place", r"\bJob in [A-Z][^.]{0,60}?\s*What\?"),
    ("results-count", r"\b\d[\d,]*\s+(?:jobs|results)\s+(?:found|matching)\b"),
]

_LISTING_RES = [(name, re.compile(p, re.I)) for name, p in LISTING_PAGE_MARKERS]
_BLANK_RUNS_RE = re.compile(r"\n\s*\n(?:\s*\n)+")


def listing_page_marker(text: str) -> str | None:
    """Which marker (if any) says this is a listing page rather than an advert."""
    flat = re.sub(r"\s+", " ", text or "")
    if not flat:
        return None
    for name, pattern in _LISTING_RES:
        if pattern.search(flat):
            return name
    return None


def looks_like_listing_page(text: str) -> bool:
    return listing_page_marker(text) is not None


# The advert body sits between these. Measured against live Adzuna pages on
# 2026-09-21: "Apply for this job" (or the "back to last search" link, when
# the apply button is missing) opens the real advert, and the site's
# "Popular Jobs" footer empire closes it.
_BODY_START_RE = re.compile(r"Apply for this job|\u276e\s*back to last search", re.I)
_BODY_END_RE = re.compile(
    r"\bPopular Jobs\b|\bTop job titles\b|\bTop job types\b|\bTop companies\b|"
    r"\bTop locations\b|\bJobseekers\b|\bRecruiters\b|\bPost a job\b|"
    r"\bCountry selection\b|\bAdzuna Intelligence\b|\bBrowse jobs\b|"
    r"\bValueMyCV\b|\bApplyIQ\b",
    re.I,
)

# Below this, the "advert" is a nav strip rather than a posting.
MIN_ADVERT_BODY_CHARS = 200


def extract_advert_body(text: str) -> str | None:
    """Pull the actual advert out of an Adzuna page that also carries site
    navigation and a "Popular Jobs" sidebar.

    This is the better half of the fix. The first version of this module threw
    the whole page away and fell back to the title, which was safe but lost the
    advert: Adzuna's search API only ever returns a short snippet, so the
    scraped page is the ONLY source of the full text — and 65 of the 69
    postings on the board then had no language signal at all, because a
    snippet rarely names one.

    The real advert IS on the page, above the footer. Verified on six live
    pages: extraction recovered 2,300-9,500 characters each, and the language
    counts became specific to each advert instead of the identical
    five-language list that gave the pollution away.

    The body ends at whichever comes first: the site footer, or the NEXT
    listing's "Apply for this job" / "back to last search". That second bound
    matters because a page whose footer is missing or reworded would otherwise
    run straight on into the next advert — which is the exact failure this
    function exists to prevent.

    Returns None when there is no recognisable advert body, so callers fall
    back to the title rather than to a page of other companies' jobs."""
    flat = (text or "").strip()
    if not flat:
        return None
    start = _BODY_START_RE.search(flat)
    if not start:
        return None

    stops = []
    end = _BODY_END_RE.search(flat, start.end())
    if end:
        stops.append(end.start())
    # One advert has one apply button. A second one further down is the start
    # of the next listing on the page. Searched from halfway past the minimum
    # body length so a stray repeat in the advert's own text does not truncate
    # it to nothing.
    next_start = _BODY_START_RE.search(flat, start.end() + MIN_ADVERT_BODY_CHARS // 2)
    if next_start:
        stops.append(next_start.start())
    stop = min(stops) if stops else len(flat)

    body = flat[start.end():stop].strip()
    body = _BLANK_RUNS_RE.sub("\n\n", body).strip()
    if len(body) < MIN_ADVERT_BODY_CHARS:
        return None
    return body


def advert_text(title: str, description: str) -> str:
    """The text the deterministic readers (language, clearance, day rate) may
    treat as this posting's own.

    Normally "title description". When the description is a listing page, the
    advert body is EXTRACTED from it and the navigation and sidebar are
    dropped; if no body can be extracted, this falls back to the title alone,
    because reading another advert's languages, salary and clearance
    requirements and attributing them to this job is worse than reading less.
    The title is always this advert's own, which is why "Test Engineer (SC)"
    and "SC Cleared Lead Automation Engineer" still trip the clearance rule
    even with no usable body.
    """
    title = title or ""
    description = description or ""
    if not looks_like_listing_page(description):
        return f"{title} {description}".strip()
    body = extract_advert_body(description)
    return f"{title} {body}".strip() if body else title.strip()


def advert_body_is_recoverable(description: str) -> bool:
    """True when a listing page still contains an extractable advert — used to
    tell "this row can be repaired from what is already stored" apart from
    "this row needs re-fetching"."""
    return looks_like_listing_page(description) and extract_advert_body(description) is not None
