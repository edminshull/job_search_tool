"""Thin clients for each ATS's public job-board API.

Every function returns a list of plain dicts with a common shape:
    {"company": str, "title": str, "location": str, "url": str,
     "posted_at": str|None, "description": str}

`description` is HTML or plain text, whatever the ATS gives us, and is
consumed by filters.jd_stack_mismatch() for the JD-based stack dealbreaker
check. It's fetched from the SAME request as the listing wherever the ATS
supports that (Greenhouse ?content=true, Ashby/Lever include it by
default) — no extra per-job request needed, so this doesn't multiply your
call volume. Workable's widget API may or may not include it depending on
account; if absent, `description` is just "" and the stack check is
skipped for that job (title/location filters still apply).

NOTE ON THIS SANDBOX: outbound HTTP to arbitrary domains (e.g.
boards-api.greenhouse.io) is blocked by this cloud environment's network
allowlist — that's a property of THIS dev sandbox, not of the ATS APIs
themselves (verified working via WebFetch during development). Run this
module on a machine/server with normal internet access — your laptop, a
cron box, a small VM — and it will work as-is.

CONFIDENCE NOTE on fetch_smartrecruiters specifically (2026-08-12): unlike
Greenhouse/Ashby/Workable/Lever, which were each confirmed against a real
live response during development, SmartRecruiters' shape here is built
from their docs (developers.smartrecruiters.com) plus two independent
third-party integrations that describe the same shape (jobspipe.dev,
an Apify scraper) — this sandbox's WebFetch got blocked by
api.smartrecruiters.com's robots.txt, so it's NOT been hit live the way
the others were. Verify it the same way Coveo/Treewalk's slugs got
verified: `python -m app.main --company <slug>` against a real
SmartRecruiters company before trusting it in a real run.
"""
import httpx

from app import filters

USER_AGENT = "job-search-pipeline/0.1 (personal use)"
TIMEOUT = 20.0


def fetch_greenhouse(company_display_name: str, slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    resp = httpx.get(
        url, params={"content": "true"}, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        jobs.append({
            "company": company_display_name,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted_at": j.get("first_published") or j.get("updated_at"),
            "description": j.get("content", ""),  # HTML
        })
    return jobs


def fetch_ashby(company_display_name: str, slug: str) -> list[dict]:
    # Ashby's public posting-API endpoint (no auth needed for public boards).
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location") or j.get("locationName") or ""
        jobs.append({
            "company": company_display_name,
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("jobUrl") or j.get("applyUrl", ""),
            "posted_at": j.get("publishedAt"),
            "description": j.get("descriptionPlain") or j.get("descriptionHtml") or "",
        })
    return jobs


def fetch_workable(company_display_name: str, slug: str) -> list[dict]:
    # Workable's public widget API.
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data.get("jobs", []):
        loc = j.get("location") or {}
        loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
        if loc.get("workplace") == "remote":
            loc_str = f"Remote ({loc_str})" if loc_str else "Remote"
        jobs.append({
            "company": company_display_name,
            "title": j.get("title", ""),
            "location": loc_str,
            "url": j.get("url") or j.get("shortlink", ""),
            "posted_at": j.get("published_on") or j.get("created_at"),
            # Workable's widget API doesn't reliably include a description
            # field across all accounts — treat missing as unknown, not
            # as "no dealbreaker language", filters.py handles empty safely.
            "description": j.get("description", ""),
        })
    return jobs


def fetch_lever(company_display_name: str, slug: str) -> list[dict]:
    # Lever's public postings API.
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    jobs = []
    for j in data:
        cats = j.get("categories", {}) or {}
        loc = cats.get("location", "")
        all_locs = cats.get("allLocations") or []
        if all_locs:
            loc = ", ".join(all_locs)
        jobs.append({
            "company": company_display_name,
            "title": j.get("text", ""),
            "location": loc,
            "url": j.get("hostedUrl", ""),
            "posted_at": j.get("createdAt"),  # epoch millis
            "description": j.get("descriptionPlain") or j.get("description", ""),
        })
    return jobs


def _fetch_smartrecruiters_description(slug: str, posting_id: str) -> str:
    """SmartRecruiters' list endpoint (fetch_smartrecruiters below) doesn't
    include the JD text — only the per-posting detail endpoint does, one
    extra request per job. Best-effort: any failure (private board, 404,
    timeout) just means no description, not a crashed run; filters.py
    treats an empty description as "unknown, don't reject on stack alone"."""
    try:
        resp = httpx.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{posting_id}",
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        sections = (resp.json().get("jobAd") or {}).get("sections") or {}
        parts = [
            (sections.get(key) or {}).get("text", "")
            for key in ("jobDescription", "qualifications", "additionalInformation")
        ]
        return "\n\n".join(p for p in parts if p)
    except Exception:
        return ""


def fetch_smartrecruiters(company_display_name: str, slug: str) -> list[dict]:
    # SmartRecruiters' public Posting API — only enabled per-account (not
    # every customer turns it on), same "might just 404" caveat as the
    # other ATSes here. Paginated via limit/offset, 100 postings per page.
    jobs = []
    offset = 0
    limit = 100
    while True:
        url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
        resp = httpx.get(
            url, params={"limit": limit, "offset": offset},
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data.get("content", [])
        if not content:
            break

        for p in content:
            loc = p.get("location") or {}
            loc_str = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
            if loc.get("remote"):
                loc_str = f"Remote ({loc_str})" if loc_str else "Remote"

            title = p.get("name", "")
            posting_id = p.get("id", "")
            job_url = p.get("applyUrl") or p.get("ref") or (
                f"https://jobs.smartrecruiters.com/{slug}/{posting_id}" if posting_id else ""
            )
            if not job_url:
                # No applyUrl/ref/id to build any identifier from — skip
                # rather than store url="", which would make dedup.is_new()
                # treat every subsequent url-less posting as a duplicate of
                # the first one (an exact-match dedup key collision).
                continue

            description = ""
            # Only worth the extra per-job request (see
            # _fetch_smartrecruiters_description) for postings that
            # already look like real candidates — same gating idea as
            # aggregator_clients.fetch_adzuna uses for its full-JD fetch,
            # so a company with hundreds of postings doesn't turn into
            # hundreds of extra requests for roles that'd get filtered
            # out on title/location alone anyway.
            if posting_id and filters.title_is_relevant(title) and filters.location_is_allowed(loc_str):
                description = _fetch_smartrecruiters_description(slug, posting_id)

            jobs.append({
                "company": company_display_name,
                "title": title,
                "location": loc_str,
                "url": job_url,
                "posted_at": p.get("releasedDate"),
                "description": description,
            })

        if len(content) < limit:
            break
        offset += limit
    return jobs


WORKDAY_DEFAULT_REGION = "wd3"   # the tenant is the subdomain: <tenant>.wd3.myworkdayjobs.com
# Workday requires a human-ish UA on the CXS API; the default httpx UA gets 403
# on some tenants. Verified against Acme (2026-09-23).
WORKDAY_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def _workday_endpoint(slug: str) -> tuple[str, str, str]:
    """Parse `slug` into (base_url, tenant, site).

    Workday is the one ATS here whose "slug" is not a single identifier, because
    a Workday career site is addressed by a tenant SUBDOMAIN plus a site path:

        https://<tenant>.<wdN>.myworkdayjobs.com/en-US/<site>/...

    So the slug is written exactly like the path in a careers URL —
    `tenant/site` — and an optional third part picks a non-default region:

        acme/acme        -> acme.wd3.myworkdayjobs.com, site "acme"
        acme/acme/wd1    -> acme.wd1.myworkdayjobs.com

    A single-part slug is REJECTED rather than guessed at. "acme" alone is
    ambiguous — tenant with an unknown site, or a site name? — and a guess
    produces a request to a URL that does not exist. That failure is silent:
    Workday answers with an empty posting list, so the company contributes
    nothing while looking correctly configured.

    THE HOST IS BUILT FROM THE TENANT, and getting that wrong fails SILENTLY.
    `wd3.myworkdayjobs.com` on its own is the shared apex, not a tenant: it does
    not resolve, so the request is refused and the company contributes nothing
    while looking perfectly configured. That is exactly the mistake this function
    made first time round (caught 2026-09-23 by printing the resolved URL rather
    than trusting a unit test), which is why a slug that already looks like a
    hostname is rejected loudly instead of being concatenated into nonsense.

    The second part is usually the same word as the tenant but is NOT always:
    some tenants serve several sites (a graduate site, a subsidiary), so it is
    kept separate rather than assumed.
    """
    parts = [p for p in (slug or "").split("/") if p]
    if not parts:
        raise ValueError("workday slug must be 'tenant/site', e.g. 'acme/acme'")
    if "." in parts[0]:
        raise ValueError(
            f"workday slug {slug!r} looks like a hostname. Give 'tenant/site' "
            f"(e.g. 'acme/acme') — the tenant subdomain and the site path — "
            f"not the full host, which is derived from the tenant.")

    region = WORKDAY_DEFAULT_REGION
    if len(parts) == 1:
        raise ValueError(
            f"workday slug {slug!r} needs both parts: 'tenant/site' "
            f"(e.g. 'acme/acme'). A single name cannot say which is which, "
            f"and guessing yields a URL that returns no jobs at all.")
    if len(parts) == 2:
        tenant, site = parts
    else:
        tenant, site, region = parts[0], parts[1], parts[2]
    if region not in ("wd1", "wd2", "wd3", "wd5", "wd10", "wd12", "wd103"):
        raise ValueError(f"workday region {region!r} is not one of wd1/wd2/wd3/wd5/…")
    return f"https://{tenant}.{region}.myworkdayjobs.com", tenant, site


def _workday_location(primary, additional) -> str:
    """Every location a posting is open to, primary first.

    Joined rather than reduced to one value because the location filter is an
    ALLOWLIST: a role advertised in Birmingham AND as a UK home worker is a role
    this candidate can do, and only the home-worker option makes that visible.
    Deduplicated case-insensitively, because a posting commonly lists its primary
    site again inside `additionalLocations`."""
    out: list[str] = []
    seen: set[str] = set()
    for value in [primary] + list(additional or []):
        text = str(value or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return ", ".join(out)


def _workday_detail(base_url: str, tenant: str, site: str,
                    external_path: str) -> tuple[str, str]:
    """(description_html, location) for one posting.

    The list endpoint gives neither a description NOR a usable location — it
    collapses multi-site roles to "5 Locations" — so this request is the only way
    to learn where a posting really is. That matters more than it sounds: the
    location filter decides whether a posting is kept at all, so a UK role hidden
    inside a multi-location posting is dropped without anyone seeing it.

    Read live off the Acme board, 2026-09-23:

        Senior Test Engineer (Public Sector) -> location "Homeworker - UK",
            additionalLocations [Gdansk, Derry-Londonderry, Belfast, Birmingham]
        Lead Test Engineer (Healthcare)      -> location "Birmingham",
            additionalLocations [Derry-Londonderry, Belfast, "Homeworker - UK"]

    Both accept a UK home worker and so should be kept. But the second would be
    dropped on `location` alone ("Birmingham"), and both are dropped on the
    list's "4 Locations" / "5 Locations" — which is precisely the silent loss
    `_workday_location` exists to prevent.

    Best-effort: any failure degrades to ("", "") — an empty description means
    "unknown, don't reject on stack alone" (see filters.jd_stack_mismatch), never
    a crash."""
    try:
        resp = httpx.get(
            f"{base_url}/wday/cxs/{tenant}/{site}{external_path}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                     "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        info = resp.json().get("jobPostingInfo") or {}
        parts = [info.get("jobDescription") or ""]
        for key in ("qualifications", "additionalInformation"):
            if info.get(key):
                parts.append(str(info[key]))
        return ("\n\n".join(p for p in parts if p),
                _workday_location(info.get("location"), info.get("additionalLocations")))
    except Exception:
        return "", ""


def fetch_workday(company_display_name: str, slug: str,
                  search_text: str | None = None) -> list[dict]:
    """Workday (CXS) public job board.

    POST-based search API, 20 postings per request, paginated by offset.

    `search_text` matters more here than the equivalent does elsewhere: instead
    of guessing which job title a company calls a role ("Senior Test Engineer",
    "QA Analyst", "Quality Engineer"), it can be asked for the concept and the
    SERVER matches it against the whole posting text. But it narrows scope, so it
    is opt-in per company via a `search:` key in companies.yaml rather than
    applied to everyone.

    Because the list gives no usable location, the detail fetch cannot be gated
    on location the way SmartRecruiters' is — it is gated on TITLE, and any
    posting whose title could plausibly be a target role gets expanded. That
    bounds the extra requests to roughly the number of relevant-looking roles
    rather than the size of the board."""
    base_url, tenant, site = _workday_endpoint(slug)
    jobs: list[dict] = []
    offset = 0
    limit = 20
    seen_paths: set[str] = set()

    while True:
        resp = httpx.post(
            f"{base_url}/wday/cxs/{tenant}/{site}/jobs",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            json={"appliedFacets": {}, "limit": limit, "offset": offset,
                  "searchText": search_text or ""},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        postings = data.get("jobPostings") or []
        if not postings:
            break

        for p in postings:
            external_path = p.get("externalPath") or ""
            if not external_path or external_path in seen_paths:
                continue
            seen_paths.add(external_path)

            title = p.get("title", "")
            # The list's `locationsText` is "N Locations" for multi-site roles,
            # so it cannot be trusted to decide keep/drop. Expand anything whose
            # title looks like a target role; `filters.passes_filters` then makes
            # the real location decision on the expanded value.
            description = ""
            location = p.get("locationsText", "")
            if filters.title_is_relevant(title):
                description, expanded = _workday_detail(
                    base_url, tenant, site, external_path)
                # Prefer the expanded locations whenever the detail gave us any:
                # "5 Locations" is not a place, and `filters.passes_filters`
                # cannot make a keep/drop decision from it.
                if expanded:
                    location = expanded

            jobs.append({
                "company": company_display_name,
                "title": title,
                "location": location,
                "url": f"{base_url}/en-US/{site}{external_path}",
                # "Posted Today" / "Posted 3 Days Ago" — a relative phrase,
                # which lib/dates.ts already parses (app/add_job.py accepts the
                # same shape from a hand-pasted advert).
                "posted_at": p.get("postedOn"),
                "description": description,
            })

        total = data.get("total")
        offset += limit
        if len(postings) < limit or (isinstance(total, int) and offset >= total):
            break
    return jobs


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "workable": fetch_workable,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}


def fetch_company(company: dict) -> list[dict]:
    fetcher = FETCHERS.get(company["ats"])
    if fetcher is None:
        raise ValueError(f"No fetcher for ATS type: {company['ats']}")
    # `search` is a pipeline-level control (like adzuna's max_pages), not a
    # field the ATS itself receives — so it is passed only to fetchers that
    # declare they accept it, rather than splatted into every signature.
    if company.get("search") and fetcher is fetch_workday:
        jobs = fetcher(company["name"], company["slug"], search_text=company["search"])
    else:
        jobs = fetcher(company["name"], company["slug"])
    for j in jobs:
        j["source"] = company["ats"]
    return jobs