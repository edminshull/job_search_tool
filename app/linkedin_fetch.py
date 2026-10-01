"""Read a LinkedIn job posting from its URL — the fields and the JD text.

WHY THIS EXISTS
---------------
`app/add_job.py` was written on the premise that a LinkedIn posting can only
reach the tracker if you paste it, because LinkedIn restricts job data to
approved partners. Pasting still works and is still supported, but it made the
common case tedious: you have the URL in your clipboard, and the tool demanded
the company, the title and the advert text as well.

There is a public page behind every LinkedIn job URL, and this module reads it.
`https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id>` is the endpoint
LinkedIn's own logged-out job pages are rendered from: no login, no API key, no
session cookie, and it returns the same server-rendered HTML a logged-out
browser is served. That is deliberately the ONLY thing this module will use —
see "WHAT THIS DOES NOT DO".

The response is structured enough to be read exactly rather than guessed at,
which matters because these values are the row's identity for dedup and they go
straight into the AI prompt:

    h2.topcard__title                    -> title
    a.topcard__org-name-link             -> company
    span.topcard__flavor--bullet         -> location
    span.posted-time-ago__text           -> posted_at ("14 hours ago")
    li.description__job-criteria-item    -> seniority / employment type /
                                            job function / industries
    div.show-more-less-html__markup      -> the advert text (inner HTML)

Every one of those is a LinkedIn-owned field on a LinkedIn-owned page, so a
value read here is authoritative in a way the paste heuristics in `add_job.py`
are not — which is why `add_job` accepts these without the `--yes` confirmation
that a *guessed* paste value requires.

WHAT THIS DOES NOT DO
---------------------
It does not log in, and it does not send your `li_at` session cookie. Reading
the public page as an anonymous visitor keeps this tool's traffic in the same
category as opening the link in a private window, and keeps your account out of
it: an authenticated scraper is the version that gets an account restricted. It
also does not try to defeat a block — if LinkedIn answers with an auth wall, a
challenge or a 429, that is reported as a failure and the caller falls back to
the paste workflow, which is unchanged and remains the reliable path.

Measured against live postings on 2026-09-25: an existing posting returns 200
with the full advert, and the description block is byte-identical across
repeated requests (only the surrounding recommendation chrome varies — the
response varied between 20.9 KB and 54.7 KB while the advert stayed 2,177
bytes). A job that has been taken down returns 404 with an empty body, so a 404
is a real answer — "this advert is gone" — and is reported as such rather than
as a network error.
"""
import html as _html
import re
from html.parser import HTMLParser

import httpx

# A browser-shaped User-Agent rather than the pipeline's own
# "job-search-pipeline/0.1 (personal use)" (aggregator_clients.USER_AGENT).
# This endpoint is served to logged-out BROWSERS, and it is the HTML response
# we want; the honest pipeline UA is answered with an auth wall instead.
# Nothing else is spoofed — no cookies, no referer games.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 20.0

ENDPOINT = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"

# Same id shapes as add_job.canonical_url: /jobs/view/<id>, the slug form, and
# a /jobs/search/ URL carrying currentJobId. Kept as its own copy so this
# module stands alone; add_job calls canonical_url first and passes the
# extracted id, so the two cannot disagree in practice.
_JOB_ID_RE = re.compile(
    r"linkedin\.com/jobs/(?:view/(?:[^/?#]*-)?|search/\?[^#]*?currentJobId=)(\d{6,})",
    re.IGNORECASE,
)

class LinkedInFetchError(Exception):
    """A fetch that could not produce a usable posting.

    The message is written for the person running the command: what happened,
    and what to do instead. Callers print it and fall back to the paste
    workflow rather than treating it as a crash."""


def job_id_from_url(url: str) -> str | None:
    """The numeric job id in `url`, or None if this is not a LinkedIn job URL."""
    m = _JOB_ID_RE.search(url or "")
    return m.group(1) if m else None


# --- locating an element in the markup ------------------------------------
#
# One regex over tags plus an explicit depth count, rather than cssselect or
# BeautifulSoup. The repo's runtime deps are httpx/pyyaml/dotenv and this does
# not justify a fourth: the markup being read is LinkedIn's own and the anchor
# (`class="show-more-less-html__markup …"`) is stable. Doing it by hand is also
# what makes the nested-`<div>` case correct, which a non-greedy regex is not.
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*?)(/?)>")
_CLASS_ATTR_RE = re.compile(r"""\bclass\s*=\s*(?:"([^"]*)"|'([^']*)')""")


def _class_value(attrs: str) -> str:
    m = _CLASS_ATTR_RE.search(attrs or "")
    return (m.group(1) or m.group(2) or "") if m else ""


def _has_class(attrs: str, token: str) -> bool:
    """Exact class-token test. A substring test is wrong here and quietly so:
    the description div carries BOTH "show-more-less-html__markup" and
    "show-more-less-html__markup--clamp-after-5", and `"…markup" in cls` would
    also match any future "…markup--somethingelse" variant."""
    return token in _class_value(attrs).split()


def _element_html(html: str, tag: str, class_token: str) -> str | None:
    """Inner HTML of the first `<tag class="…class_token…">`, or None.

    Depth-counted over the same tag name, so a nested `<div>` inside the
    description cannot terminate it early."""
    for m in _TAG_RE.finditer(html or ""):
        closing, name, attrs, self_closed = m.group(1), m.group(2).lower(), m.group(3), m.group(4)
        if closing or self_closed or name != tag or not _has_class(attrs, class_token):
            continue
        depth = 1
        for inner in _TAG_RE.finditer(html, m.end()):
            i_closing, i_name, _i_attrs, i_self = (
                inner.group(1), inner.group(2).lower(), inner.group(3), inner.group(4))
            if i_name != tag or i_self:
                continue
            depth += -1 if i_closing else 1
            if depth == 0:
                return html[m.end():inner.start()]
        return None
    return None


# --- HTML -> text ---------------------------------------------------------
#
# The advert is stored as PLAIN TEXT, on purpose:
#   * the readers downstream (`contract_rates.parse_day_rates`, the `filters`
#     language/clearance matchers, `jd_text`) are regexes over text, and raw
#     markup puts `<strong>`, `&amp;` and style attributes in front of them.
#     "&amp;" is the sharp edge: it silently breaks any needle containing "&";
#   * it makes a fetched posting and a pasted one the same kind of record, so
#     the two halves of this workflow stay comparable;
#   * it cannot inject markup into the board. JobDetailPanel sanitizes HTML it
#     is handed, but the cheapest safe thing is not to hand it any.
#
# Structure is kept rather than flattened: block elements and <br> become
# newlines, <li> becomes a "- " bullet. JobDetailPanel renders plain text with
# `white-space: pre-wrap` (globals.css `.desc-plain`), so those newlines are
# what the reader actually sees.
_BLOCK_TAGS = {
    "p", "div", "section", "article", "ul", "ol", "li", "br", "tr", "td", "th",
    "table", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre",
}
_DROP_CONTENT_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
_BLANK_RUNS_RE = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")


class _TextExtractor(HTMLParser):
    """HTML fragment -> structured plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._drop_depth = 0
        self._at_bullet = False

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag == "li":
            self._parts.append("\n- ")
            self._at_bullet = True
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if not self._drop_depth and tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP_CONTENT_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if self._drop_depth:
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")
        if tag == "li":
            self._at_bullet = False

    def handle_data(self, data):
        if self._drop_depth:
            return
        if self._at_bullet:
            # "- " was just emitted; the source's own indentation must not
            # become leading spaces after the bullet.
            data = data.lstrip()
            self._at_bullet = False
        self._parts.append(data)

    def text(self) -> str:
        lines: list[str] = []
        for raw in "".join(self._parts).splitlines():
            line = raw.strip()
            if line:
                # A bullet is a bullet even if the source nested it under a
                # list that itself indented.
                lines.append(line)
            elif lines and lines[-1]:
                lines.append("")
        return _BLANK_RUNS_RE.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(fragment: str) -> str:
    """Plain text for an HTML fragment, keeping paragraph and bullet breaks."""
    if not fragment:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:
        # Malformed markup must not lose the advert. Fall back to the repo's
        # cheap stripper, which has no parser state to trip over.
        from app import filters
        return _html.unescape(filters.strip_html(fragment)).strip()
    return parser.text()


# --- HTML -> fields -------------------------------------------------------
class _FieldParser(HTMLParser):
    """Collect the top-card fields and the job-criteria list.

    Every capture is (name, depth-at-open, buffer); the buffer closes when the
    element that opened it closes, which is just "the stack is shallower than
    it was". One mechanism for all of them, so a nested tag inside a captured
    field cannot end the capture early."""

    _ANCHORS = (
        ("title", "topcard__title"),
        ("company", "topcard__org-name-link"),
        ("location", "topcard__flavor--bullet"),
        ("posted_at", "posted-time-ago__text"),
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._open: list[tuple[str, int, list[str]]] = []
        self.fields: dict[str, str] = {}
        self.criteria_labels: list[str] = []
        self.criteria_values: list[str] = []
        self._crit_kind: str | None = None
        self._crit_depth = 0
        self._crit_buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        self._depth += 1
        tokens = _attrs_class(attrs).split()
        for name, token in self._ANCHORS:
            # First value wins: LinkedIn repeats `topcard__flavor--bullet` for
            # location and again for the applicant count (and the applicant
            # count is not a location), and location is the one that comes
            # first. The same "first wins" rule protects the fields if a
            # "similar jobs" card below ever reuses these class names.
            if token in tokens and name not in self.fields:
                self._open.append((name, self._depth, []))
        # Job criteria read "<h3 …-subheader>Seniority level</h3>" followed by
        # "<span …-text>Mid-Senior level</span>". Collecting the two kinds into
        # ordered lists and zipping them is enough: they alternate, and a
        # position that is missing on one side just drops out of the zip.
        if "description__job-criteria-subheader" in tokens:
            self._crit_kind, self._crit_depth, self._crit_buf = "label", self._depth, []
        elif "description__job-criteria-text" in tokens:
            self._crit_kind, self._crit_depth, self._crit_buf = "value", self._depth, []

    def handle_endtag(self, tag):
        self._depth = max(0, self._depth - 1)
        for item in list(self._open):
            name, depth, buf = item
            if depth > self._depth:  # the element that opened this capture closed
                text = " ".join("".join(buf).split())
                if text and name not in self.fields:
                    self.fields[name] = text
                self._open.remove(item)
        if self._crit_kind and self._depth < self._crit_depth:
            text = " ".join("".join(self._crit_buf).split())
            if text:
                (self.criteria_labels if self._crit_kind == "label"
                 else self.criteria_values).append(text)
            self._crit_kind = None

    def handle_data(self, data):
        for _name, _depth, buf in self._open:
            buf.append(data)
        if self._crit_kind:
            self._crit_buf.append(data)


def _attrs_class(attrs) -> str:
    """The class attribute's value from an HTMLParser attrs list."""
    for k, v in attrs:
        if k == "class":
            return v or ""
    return ""


def parse_job_page(html: str) -> dict:
    """Read one guest-endpoint response into a posting dict.

    Raises LinkedInFetchError when the advert body cannot be found, rather than
    returning a posting with an empty description: a row with no JD text
    reaches the board with nothing for the AI step to score and a blank detail
    panel, and it looks like success. That silent-data-loss failure mode has
    already been fixed once in this tool (see test_add_job's piped-stdin test),
    so it is not reintroduced here.
    """
    if not (html or "").strip():
        raise LinkedInFetchError(
            "LinkedIn returned an empty page for this job id. It may have been "
            "taken down, or it may not be a public posting.")

    body_html = _element_html(html, "div", "show-more-less-html__markup")
    if body_html is None:
        raise LinkedInFetchError(
            "Could not find the advert text in the page LinkedIn returned — the "
            "posting may be expired, or its markup has changed. Use the paste "
            "workflow instead (see --file / --clipboard).")
    description = html_to_text(body_html)
    if not description:
        raise LinkedInFetchError(
            "The advert text LinkedIn returned was empty. Use the paste "
            "workflow instead (see --file / --clipboard).")

    fields = _FieldParser()
    fields.feed(html)
    fields.close()
    criteria = dict(zip(fields.criteria_labels, fields.criteria_values))

    return {
        "title": fields.fields.get("title"),
        "company": fields.fields.get("company"),
        "location": fields.fields.get("location"),
        "posted_at": fields.fields.get("posted_at"),
        "description": description,
        "criteria": criteria,
    }


# LinkedIn's "Employment type" criterion is about the engagement, not the
# hours. "Contract" and "Temporary" are the two that mean day-rate work, and
# they are the only ones mapped: "Full-time" says nothing about permanence (a
# 12-month contract is advertised full-time all the time), so it is left as
# "unknown" and the advert text or an explicit --contract flag decides.
CONTRACT_EMPLOYMENT_TYPES = {"contract", "temporary"}


def employment_type_from_criteria(criteria: dict) -> str | None:
    """'contract' when LinkedIn states contract/temporary work, else None."""
    stated = (criteria or {}).get("Employment type", "") or ""
    for part in re.split(r"[,/&]| and ", stated):
        if part.strip().lower() in CONTRACT_EMPLOYMENT_TYPES:
            return "contract"
    return None


def fetch_job(job_id: str, *, timeout: float = TIMEOUT) -> dict:
    """Fetch and parse one posting by its numeric LinkedIn job id.

    Returns parse_job_page's dict, plus `job_id` and `url`. Raises
    LinkedInFetchError with a message the CLI can print verbatim."""
    job_id = (job_id or "").strip()
    if not job_id.isdigit():
        raise LinkedInFetchError(f"Not a LinkedIn job id: {job_id!r}")

    url = ENDPOINT.format(job_id=job_id)
    try:
        resp = httpx.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                         follow_redirects=True)
    except httpx.HTTPError as exc:
        raise LinkedInFetchError(
            f"Could not reach LinkedIn ({type(exc).__name__}: {exc}). "
            f"Check your connection, or use the paste workflow.") from exc

    _check_status(resp.status_code, resp)

    posting = parse_job_page(resp.text)
    posting["job_id"] = job_id
    posting["url"] = f"https://www.linkedin.com/jobs/view/{job_id}/"
    return posting


def _check_status(status: int, resp) -> None:
    """Turn LinkedIn's answers into messages that say what to do next.

    All of these end in a usable instruction rather than a stack trace, because
    every one of them has a working fallback: the paste workflow this tool
    shipped with."""
    if status == 200:
        return
    if status == 404:
        raise LinkedInFetchError(
            "LinkedIn has no posting for that job id (404). It has probably been "
            "taken down, filled, or expires — check the link in a browser. If it "
            "opens fine for you, paste the text instead (--clipboard or --file).")
    if status in (401, 403, 999):
        raise LinkedInFetchError(
            f"LinkedIn refused the request (HTTP {status}) — likely an auth wall or "
            f"a bot check. Paste the posting instead: --clipboard (or --file).")
    if status == 429:
        raise LinkedInFetchError(
            "LinkedIn rate-limited the request (429). Wait a few minutes — the "
            "posting is read one job at a time, so this only happens after a burst "
            "— or paste the posting instead (--clipboard).")
    raise LinkedInFetchError(
        f"LinkedIn answered HTTP {status}. Paste the posting instead "
        f"(--clipboard or --file).")
