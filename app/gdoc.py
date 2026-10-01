"""The master CV, read from the Google Doc that Ed actually edits.

WHY THIS EXISTS
---------------
The CV used to live in two places that could disagree: `cv/master.yaml`, the
structured fact bank the engine drafts from, and the rendered CV that actually
gets sent. Once per-job tailoring was dropped (2026-10-01) the rendered CV
became the ONLY thing that matters — the question is no longer "which facts do
I select for this posting" but "does the CV I would send pass this posting's
ATS, and is there real experience I hold that the CV does not currently show".
Both halves of that need the CV's own text, not a selection layer over it.

So this module fetches the Doc and hands back its plain text. It answers one
question and no others: *what is on the CV right now*.

TWO WAYS IN, DELIBERATELY
-------------------------
1. SERVICE ACCOUNT (`GOOGLE_SERVICE_ACCOUNT_FILE`). The Doc stays PRIVATE and
   is shared with a robot account as a Viewer. This is the supported setup: a
   CV carries a name, a phone number, an email and an employment history, and
   "anyone with the link" is the wrong access model for all four. Needs
   `google-auth` (an optional dependency — see requirements.txt).
2. PLAIN EXPORT (`https://docs.google.com/document/d/<id>/export?format=txt`).
   Works with no credentials at all, but only because the Doc is readable by
   anyone holding the URL. It is kept because it is how this was built and
   debugged, and because it is the only path available on a machine that has
   not been given the key. It is NOT the recommended configuration.

The configured method is tried first; the other is a documented fallback rather
than a silent one, and the result records which was used so the UI can say so.
That matters here more than usual: "your CV says X" is a claim about a document
this process cannot see the sharing settings of, and a stale or link-shared read
should be visible as such rather than indistinguishable from the private one.

THE CACHE IS NOT AN OPTIMISATION
--------------------------------
Every scan needs the CV text, and an ATS scan is deterministic and free while
the network is neither. `data/master_cv.json` holds the last good fetch, and is
used whenever a live fetch fails — marked `stale: true` with the time it was
taken. Refusing to scan because Google was briefly unreachable would make the
whole feature hostage to a network call it does not otherwise need, and the
staleness is reported rather than hidden: a scan against a two-day-old CV is
worth having, but only if it says so.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_PATH = ROOT / "data" / "master_cv.json"

DOC_ID_ENV = "MASTER_CV_DOC_ID"
DOC_URL_ENV = "MASTER_CV_DOC_URL"
SERVICE_ACCOUNT_ENV = "GOOGLE_SERVICE_ACCOUNT_FILE"
CACHE_PATH_ENV = "MASTER_CV_CACHE"
TTL_ENV = "MASTER_CV_CACHE_TTL_HOURS"

# How long a cached copy is trusted before a live fetch is attempted. Long by
# web standards and short by CV standards: the Doc changes every few weeks, and
# a scan is never more than one posting away from a refresh.
DEFAULT_TTL_HOURS = 12

# The Docs API and the export endpoint. Kept as module constants so a test can
# point them at a local server without monkeypatching urllib.
DOCS_API = "https://docs.googleapis.com/v1/documents/{doc_id}"
EXPORT_URL = "https://docs.google.com/document/d/{doc_id}/export?format=txt"

# A Google Doc id is the long opaque token in the middle of the URL. Matching
# it rather than trusting the whole pasted string is what lets the user paste
# the address bar contents — which is what they have — instead of being told to
# extract a substring by hand.
_DOC_ID_RE = re.compile(r"/(?:document|d)/(?:e/)?([A-Za-z0-9_-]{20,})")

USER_AGENT = "job-search-tool/1.0 (+master CV fetch)"

TIMEOUT_SECONDS = 30


class GDocError(RuntimeError):
    """Any reason the CV text could not be produced. Raised, never sys.exit()'d:
    the web app runs this as a subprocess and shows the sentence in JSON."""


def doc_id_from(value: str | None) -> str | None:
    """The Doc id inside a URL, or the value itself if it already IS one.

    Accepts what a human actually has to hand — the full
    `https://docs.google.com/document/d/<id>/edit?usp=sharing` address — because
    asking them to strip the query string and the path around it is asking them
    to make a mistake."""
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    match = _DOC_ID_RE.search(text)
    if match:
        return match.group(1)
    # No slashes and no scheme: assume it is a bare id. Guarded on content so a
    # typo'd URL ("docs.google.com/...") is not accepted as an id.
    if "/" not in text and " " not in text and len(text) >= 20:
        return text
    return None


def configured_doc_id() -> str | None:
    """The Doc this checkout is pointed at, from either env var."""
    return (doc_id_from(os.environ.get(DOC_URL_ENV))
            or doc_id_from(os.environ.get(DOC_ID_ENV)))


def cache_path() -> Path:
    override = os.environ.get(CACHE_PATH_ENV)
    return Path(override) if override else DEFAULT_CACHE_PATH


def _ttl() -> timedelta:
    raw = os.environ.get(TTL_ENV)
    if raw:
        try:
            return timedelta(hours=float(raw))
        except ValueError:
            pass
    return timedelta(hours=DEFAULT_TTL_HOURS)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_stamp(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def extract_doc_text(document: dict) -> str:
    """Plain text from a Docs API `documents.get` response.

    Every paragraph is emitted on its own line, including empty ones, because
    the CV's structure IS its meaning to a scanner: `keywords.split_sections`
    weights a term by the heading it sits under, and a CV flattened onto one
    line has no headings at all. Losing the newlines would quietly turn a
    section-weighted scan into a bag of words.

    Bullets come back as list paragraphs whose text already begins with the
    bullet glyph; the glyph is stripped so '•' never reaches the term extractor
    as if it were part of a word. Tab-separated cell runs inside tables are
    joined with a space for the same reason."""
    body = (document or {}).get("body") or {}
    lines: list[str] = []
    for element in body.get("content") or []:
        paragraph = element.get("paragraph")
        if paragraph is None:
            table = element.get("table")
            if table is not None:
                lines.extend(_table_lines(table))
            continue
        lines.append(_paragraph_text(paragraph))
    return _tidy(lines)


def _paragraph_text(paragraph: dict) -> str:
    chunks: list[str] = []
    for element in paragraph.get("elements") or []:
        run = element.get("textRun")
        if run is None:
            continue
        chunks.append(run.get("content") or "")
    text = "".join(chunks)
    # The Docs API includes the paragraph break as a trailing "\n" inside the
    # run; keeping it would double every line.
    text = text.rstrip("\n")
    # Presence of `bullet` is the signal, NOT anything inside it. This read
    # `bullet.textStyle` at first, which the Docs API leaves as an empty object
    # for the ordinary list styles — so `if bullet:` was never true and every
    # bullet arrived with its glyph attached. Found by test_bullet_glyphs_are_stripped.
    if paragraph.get("bullet"):
        text = _strip_bullet_glyph(text)
    return text


def _strip_bullet_glyph(text: str) -> str:
    stripped = text.lstrip()
    for glyph in ("\u2022", "\u25cf", "\u25cb", "\u25aa", "-", "*"):
        if stripped.startswith(glyph + " "):
            return stripped[len(glyph):].lstrip()
    return text


def _table_lines(table: dict) -> list[str]:
    lines: list[str] = []
    for row in table.get("tableRows") or []:
        cells: list[str] = []
        for cell in row.get("tableCells") or []:
            parts = [
                _paragraph_text(e.get("paragraph") or {})
                for e in cell.get("content") or []
                if e.get("paragraph") is not None
            ]
            cells.append(" ".join(p for p in parts if p).strip())
        line = "  ".join(c for c in cells if c)
        if line:
            lines.append(line)
    return lines


def _tidy(lines: list[str]) -> str:
    """Collapse runs of blank lines and drop the leading/trailing ones.

    The Docs API pads every document with an empty final paragraph; left in, it
    would make an otherwise-empty CV look like it had content."""
    out: list[str] = []
    for line in lines:
        line = line.replace("\t", " ").rstrip()
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _service_account_token(scopes: list[str]) -> str:
    """An access token from the service-account key file.

    Imported lazily and its absence turned into a sentence, because
    `google-auth` is an optional dependency: the export path below works
    without it, and a hard ImportError at module import would take that path
    down with it."""
    key_file = os.environ.get(SERVICE_ACCOUNT_ENV)
    if not key_file:
        raise GDocError(
            f"{SERVICE_ACCOUNT_ENV} is not set. Share the Doc with a service "
            f"account as a Viewer and point that variable at its JSON key."
        )
    if not Path(key_file).expanduser().exists():
        raise GDocError(f"{SERVICE_ACCOUNT_ENV} points at {key_file}, which does not exist.")
    try:
        from google.auth.transport.requests import Request  # type: ignore
        from google.oauth2 import service_account  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise GDocError(
            "google-auth is not installed, so the private-Doc route is unavailable. "
            "Install it with `./.venv/bin/python -m pip install google-auth`, or set "
            f"{DOC_URL_ENV} and keep using the link-shared export (see app/gdoc.py)."
        ) from exc
    credentials = service_account.Credentials.from_service_account_file(
        str(Path(key_file).expanduser()), scopes=scopes)
    credentials.refresh(Request())
    return credentials.token


def _http_get(url: str, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = exc.read().decode("utf-8", "replace")
            payload = json.loads(body)
            detail = (payload.get("error") or {}).get("message") or ""
        except Exception:
            detail = ""
        raise GDocError(
            f"HTTP {exc.code} from {url.split('?')[0]}"
            + (f": {detail}" if detail else "")
            + _http_hint(exc.code)
        ) from exc
    except urllib.error.URLError as exc:
        raise GDocError(f"Could not reach {url.split('?')[0]}: {exc.reason}") from exc


def _http_hint(code: int) -> str:
    if code == 401:
        return (" — the service-account key was rejected. Check that the key file is "
                "the one for this project and that its clock is not skewed.")
    if code == 403:
        return (" — the credential cannot read this Doc. Share the document with the "
                "service account's client_email (Viewer is enough), or enable the "
                "Google Docs API for the project.")
    if code == 404:
        return (" — no such document, or it has not been shared with the credential. "
                "A Doc that exists but is not shared also answers 404, deliberately.")
    return ""


def fetch_via_service_account(doc_id: str) -> str:
    """Private-Doc route: Docs API, read-only scope."""
    token = _service_account_token(["https://www.googleapis.com/auth/documents.readonly"])
    raw = _http_get(
        DOCS_API.format(doc_id=doc_id),
        {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
    )
    return extract_doc_text(json.loads(raw.decode("utf-8")))


def fetch_via_export(doc_id: str) -> str:
    """Credential-free route: the Doc's own text export. Needs the Doc to be
    readable by whoever holds the URL, which is exactly the access model
    app/gdoc.py's docstring argues against — hence the warning, not silence."""
    raw = _http_get(
        EXPORT_URL.format(doc_id=doc_id),
        {"User-Agent": USER_AGENT, "Accept": "text/plain"},
    )
    text = raw.decode("utf-8-sig", "replace")
    return _tidy(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))


def _looks_configured_for_service_account() -> bool:
    return bool(os.environ.get(SERVICE_ACCOUNT_ENV))


def _read_cache() -> dict | None:
    path = cache_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("text") else None


def _write_cache(doc_id: str, text: str, source: str) -> None:
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"doc_id": doc_id, "fetched_at": _now().isoformat(timespec="seconds"),
             "source": source, "text": text},
            ensure_ascii=False,
        ))
    except OSError:
        # A cache that cannot be written is not a reason to fail a scan; the
        # text has already been fetched, which is the thing that was asked for.
        pass


def master_cv_text(*, doc_id: str | None = None, force: bool = False) -> dict:
    """The CV's plain text, plus where it came from and how old it is.

    Returns a dict rather than a string because every caller needs to be able
    to say WHICH copy it read. A scan that silently used a week-old cache, or
    silently used a link-shared export, would be presenting a claim about a
    document under a heading that implies it is the document.

    Keys: text, source ("service_account" | "export" | "cache"), doc_id,
    fetched_at (ISO string), stale (bool), age_hours (float | None).
    """
    resolved = doc_id or configured_doc_id()
    cached = _read_cache()

    if not resolved:
        if cached:
            return _as_cached(cached, reason="no Doc configured", stale=None)
        raise GDocError(
            f"No master CV Doc is configured. Set {DOC_URL_ENV} to the Doc's "
            f"address (the one in your browser's address bar), then run "
            f"`python -m app.cv_tailor master-doc` to fetch it."
        )

    fetched = _parse_stamp((cached or {}).get("fetched_at"))
    fresh = bool(cached
                 and cached.get("doc_id") == resolved
                 and fetched is not None
                 and _now() - fetched <= _ttl())

    if fresh and not force:
        # A cache inside its TTL is not a degraded answer — it is the copy this
        # scan is supposed to use, and calling it stale would cry wolf on every
        # ordinary run. `stale` means "we could not refresh and this may be out
        # of date", which is a different claim. See _as_cached.
        return _as_cached(cached, reason=None, stale=False)

    errors: list[str] = []
    # Service account first when it is configured — that is the supported route.
    # Otherwise the export, which is the only thing that can work on a machine
    # with no key.
    attempts = (
        [("service_account", fetch_via_service_account),
         ("export", fetch_via_export)]
        if _looks_configured_for_service_account()
        else [("export", fetch_via_export)]
    )
    for source, fetcher in attempts:
        try:
            text = fetcher(resolved)
        except GDocError as exc:
            errors.append(f"{source}: {exc}")
            continue
        if not text.strip():
            errors.append(f"{source}: the document is empty")
            continue
        _write_cache(resolved, text, source)
        return {
            "text": text,
            "source": source,
            "doc_id": resolved,
            "fetched_at": _now().isoformat(timespec="seconds"),
            "stale": False,
            "age_hours": 0.0,
        }

    if cached:
        return _as_cached(cached, reason="; ".join(errors), stale=True)
    raise GDocError(
        "Could not read the master CV Doc. " + " | ".join(errors)
        if errors else "Could not read the master CV Doc."
    )


def _as_cached(cached: dict, reason: str | None, stale: bool | None) -> dict:
    """A cache-sourced result.

    `stale` is passed in rather than assumed, because "this came from the cache"
    and "this may be out of date" are different claims and the UI repeats this
    one verbatim. `None` means "nobody could check" — no Doc is configured, so
    there is nothing to refresh from — and that is resolved by age: an
    unverifiable cache is only stale if it is older than the TTL."""
    fetched = _parse_stamp(cached.get("fetched_at"))
    age = None
    if fetched is not None:
        age = round((_now() - fetched).total_seconds() / 3600.0, 2)
    if stale is None:
        stale = age is None or age > _ttl().total_seconds() / 3600.0
    return {
        "text": cached.get("text") or "",
        "source": "cache",
        "cached_source": cached.get("source"),
        "doc_id": cached.get("doc_id"),
        "fetched_at": cached.get("fetched_at"),
        "stale": stale,
        "age_hours": age,
        "cache_reason": reason,
    }
