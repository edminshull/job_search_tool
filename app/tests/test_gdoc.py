"""Tests for reading the master CV out of Google Docs.

Offline and credential-free by design. Everything here runs against synthetic
Docs API payloads, a stubbed `urlopen`, and a tmp_path cache — the one thing
that must never happen in a test is a real fetch of a document carrying a name,
a phone number and an employment history.

What these tests protect, in order of how much damage getting it wrong would do:

1. The paragraph structure. The Docs API returns a document as a tree of
   paragraphs and text runs; flattening it loses the headings, and a heading is
   what makes `keywords.split_sections` weight a term 3.0 instead of 0.5. A CV
   silently turned into one long line would still scan — it would just scan as
   a bag of words, and report nothing.
2. Which copy was read. Every result says whether it came from the private Doc,
   a link-shared export, or a cache, and this is the claim the UI repeats.
3. The cache never becoming a silent substitute for the Doc.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from app import gdoc

# SYNTHETIC, and it must stay that way. This was the author's real document id
# for one commit, and that matters because the id IS the link: with the Doc
# link-shared, publishing the repo publishes a working URL to a CV carrying a
# name, a phone number, an email and a full employment history. Nothing here
# needs a real id — every test below only exercises the URL-parsing shape — so
# the constant is a made-up string of the right length and character set.
DOC_ID = "1AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTtUu"


# ---------------------------------------------------------------------------
# Finding the document id
# ---------------------------------------------------------------------------

def test_a_pasted_address_bar_url_yields_the_document_id():
    """What the user actually has to hand is the URL, not the id. Asking them to
    extract a substring by hand is asking them to make a mistake."""
    for url in (
        f"https://docs.google.com/document/d/{DOC_ID}/edit?usp=sharing",
        f"https://docs.google.com/document/d/{DOC_ID}/edit",
        f"https://docs.google.com/document/d/{DOC_ID}",
        f"https://docs.google.com/document/u/0/d/{DOC_ID}/edit#heading=h.abc",
        f"  https://docs.google.com/document/d/{DOC_ID}/export?format=txt  ",
    ):
        assert gdoc.doc_id_from(url) == DOC_ID, url


def test_a_bare_id_is_accepted_and_junk_is_not():
    assert gdoc.doc_id_from(DOC_ID) == DOC_ID
    for bad in (None, "", "   ", "not a url", "https://docs.google.com/", "/short/id",
                "docs.google.com/document/d/x"):
        assert gdoc.doc_id_from(bad) is None, bad


def test_a_configured_url_and_a_configured_id_agree(monkeypatch):
    monkeypatch.setenv("MASTER_CV_DOC_URL", f"https://docs.google.com/document/d/{DOC_ID}/edit")
    monkeypatch.delenv("MASTER_CV_DOC_ID", raising=False)
    assert gdoc.configured_doc_id() == DOC_ID
    monkeypatch.delenv("MASTER_CV_DOC_URL")
    monkeypatch.setenv("MASTER_CV_DOC_ID", DOC_ID)
    assert gdoc.configured_doc_id() == DOC_ID


# ---------------------------------------------------------------------------
# Extracting the text
# ---------------------------------------------------------------------------

def _paragraph(text: str, bullet: bool = False) -> dict:
    para: dict = {"elements": [{"textRun": {"content": text}}]}
    if bullet:
        para["bullet"] = {"listId": "kix.1", "textStyle": {}}
    return {"paragraph": para}


def _doc(*blocks) -> dict:
    return {"body": {"content": [*blocks, _paragraph("")]}}


def test_each_paragraph_keeps_its_own_line():
    """The structure IS the meaning to a scanner: gap.py weights a term by the
    heading it sits under, and a CV collapsed onto one line has no headings."""
    document = _doc(
        _paragraph("Ed Minshull\n"),
        _paragraph("Senior SDET | Test Automation\n"),
        _paragraph("CORE SKILLS\n"),
        _paragraph("Java, Ruby, Cucumber\n"),
    )
    assert gdoc.extract_doc_text(document).splitlines() == [
        "Ed Minshull",
        "Senior SDET | Test Automation",
        "CORE SKILLS",
        "Java, Ruby, Cucumber",
    ]


def test_bullet_glyphs_are_stripped():
    """The Docs API prefixes a list item's text with the glyph. Left in, '•'
    reaches the term extractor as if it were part of a word."""
    document = _doc(
        _paragraph("\u2022 Built BDD frameworks\n", bullet=True),
        _paragraph("\u25cf Migrated 100 scripts\n", bullet=True),
        _paragraph("- Delivered automation\n", bullet=True),
        _paragraph("Plain paragraph with a - dash\n"),
    )
    lines = gdoc.extract_doc_text(document).splitlines()
    assert lines[0] == "Built BDD frameworks"
    assert lines[1] == "Migrated 100 scripts"
    assert lines[2] == "Delivered automation"
    # A dash in ordinary prose is content, and only a LEADING glyph is a bullet.
    assert lines[3] == "Plain paragraph with a - dash"


def test_blank_lines_between_sections_survive_but_runs_of_them_do_not():
    """Blank lines are what `split_sections` uses to tell a heading from a list
    item, so they cannot be dropped — but a document padded with empty
    paragraphs must not arrive padded."""
    document = _doc(
        _paragraph("REQUIREMENTS\n"),
        _paragraph(""),
        _paragraph(""),
        _paragraph("Java\n"),
        _paragraph(""),
    )
    text = gdoc.extract_doc_text(document)
    assert text.splitlines() == ["REQUIREMENTS", "", "Java"]
    assert text == text.strip()


def test_an_empty_document_is_empty_not_a_stray_blank_line():
    assert gdoc.extract_doc_text({"body": {"content": [_paragraph("")]}}) == ""
    assert gdoc.extract_doc_text({}) == ""


def test_tables_become_one_line_per_row():
    table = {
        "table": {
            "tableRows": [
                {"tableCells": [
                    {"content": [_paragraph("Cucumber\n")]},
                    {"content": [_paragraph("6 years\n")]},
                ]},
            ]
        }
    }
    assert gdoc.extract_doc_text(_doc(_paragraph("SKILLS\n"), table)) == "SKILLS\nCucumber  6 years"


# ---------------------------------------------------------------------------
# Fetching, and the cache
# ---------------------------------------------------------------------------

def _isolate(monkeypatch, tmp_path):
    """Point the cache at a scratch file and clear every ambient setting."""
    cache = tmp_path / "master_cv.json"
    monkeypatch.setenv("MASTER_CV_CACHE", str(cache))
    monkeypatch.setenv("MASTER_CV_DOC_URL", f"https://docs.google.com/document/d/{DOC_ID}/edit")
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("MASTER_CV_CACHE_TTL_HOURS", raising=False)
    return cache


def test_the_export_path_is_used_when_no_service_account_is_configured(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    calls = []

    def fake_get(url, headers):
        calls.append(url)
        return b"Ed Minshull\r\nSenior SDET\r\n"

    monkeypatch.setattr(gdoc, "_http_get", fake_get)
    result = gdoc.master_cv_text()
    assert result["source"] == "export"
    assert result["stale"] is False
    assert result["text"].splitlines() == ["Ed Minshull", "Senior SDET"]
    assert calls and f"/document/d/{DOC_ID}/export" in calls[0]


def test_a_fresh_cache_is_used_without_touching_the_network(monkeypatch, tmp_path):
    cache = _isolate(monkeypatch, tmp_path)

    def boom(url, headers):
        raise AssertionError("a fresh cache must mean no network call at all")

    monkeypatch.setattr(gdoc, "_http_get", boom)
    gdoc._write_cache(DOC_ID, "Cached CV text", "export")
    assert cache.exists()
    result = gdoc.master_cv_text()
    assert result["text"] == "Cached CV text"
    assert result["stale"] is False


def test_a_failed_fetch_falls_back_to_the_cache_and_says_it_is_stale(monkeypatch, tmp_path):
    """Refusing to scan because Google was briefly unreachable would make the
    feature hostage to a network call it does not otherwise need. The staleness
    is reported rather than hidden: a scan against a two-day-old CV is worth
    having, but only if it says so."""
    _isolate(monkeypatch, tmp_path)
    gdoc._write_cache(DOC_ID, "Yesterday's CV", "export")

    def failing(url, headers):
        raise gdoc.GDocError("HTTP 503: backend error")

    monkeypatch.setattr(gdoc, "_http_get", failing)
    result = gdoc.master_cv_text(force=True)
    assert result["text"] == "Yesterday's CV"
    assert result["source"] == "cache"
    assert result["stale"] is True
    assert result["cached_source"] == "export"
    assert "503" in (result["cache_reason"] or "")


def test_a_cache_for_a_different_doc_is_not_reused(monkeypatch, tmp_path):
    """The cache stores which document it came from. Without that check, pointing
    the tool at a different Doc would silently keep scanning the old one."""
    cache = _isolate(monkeypatch, tmp_path)
    cache.write_text(json.dumps({"doc_id": "some-other-document-id-aaaaaaaa",
                                 "fetched_at": gdoc._now().isoformat(),
                                 "source": "export", "text": "A different person's CV"}))
    monkeypatch.setattr(gdoc, "_http_get", lambda url, headers: b"The real CV")
    assert gdoc.master_cv_text()["text"] == "The real CV"


def test_no_doc_configured_is_a_sentence_not_a_traceback(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.delenv("MASTER_CV_DOC_URL", raising=False)
    monkeypatch.delenv("MASTER_CV_DOC_ID", raising=False)
    with pytest.raises(gdoc.GDocError) as exc:
        gdoc.master_cv_text()
    assert "MASTER_CV_DOC_URL" in str(exc.value)


def test_an_unshared_doc_gets_the_404_explanation_not_a_bare_status(monkeypatch, tmp_path):
    """A Doc that exists but has not been shared with the credential answers 404,
    and "no such document" sends people looking for a typo in an id that is
    correct. The hint is the difference between a five-minute fix and an hour."""
    hint = gdoc._http_hint(404)
    assert "not been shared" in hint
    assert "Viewer is enough" in gdoc._http_hint(403)
    assert "rejected" in gdoc._http_hint(401)
    assert gdoc._http_hint(500) == ""


def test_http_errors_become_sentences_rather_than_exceptions_with_a_status(monkeypatch):
    def raise_http(*_args, **_kwargs):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)

    monkeypatch.setattr(gdoc.urllib.request, "urlopen", raise_http)
    monkeypatch.setattr(gdoc.urllib.request, "Request", lambda *a, **k: None)
    with pytest.raises(gdoc.GDocError) as exc:
        gdoc._http_get("https://docs.googleapis.com/v1/documents/x", {})
    assert "403" in str(exc.value)
    assert "Viewer is enough" in str(exc.value)


def test_the_service_account_path_is_the_first_choice_when_it_is_configured(monkeypatch, tmp_path):
    """The private route is the supported one; the link-shared export is a
    fallback for a machine with no key. Reversing that order would quietly keep
    reading a publicly-readable copy after the key was set up."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "key.json"))
    order: list[str] = []
    monkeypatch.setattr(gdoc, "fetch_via_service_account",
                        lambda doc_id: order.append("service_account") or "Private CV")
    monkeypatch.setattr(gdoc, "fetch_via_export",
                        lambda doc_id: order.append("export") or "Public CV")
    result = gdoc.master_cv_text()
    assert result["source"] == "service_account"
    assert result["text"] == "Private CV"
    assert order == ["service_account"]


def test_the_export_is_used_when_the_service_account_fails(monkeypatch, tmp_path):
    """Documented fallback rather than a silent one — and the result records
    which was used, so the UI can say the copy is the link-shared one."""
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(tmp_path / "key.json"))

    def failing(doc_id):
        raise gdoc.GDocError("token exchange failed")

    monkeypatch.setattr(gdoc, "fetch_via_service_account", failing)
    monkeypatch.setattr(gdoc, "fetch_via_export", lambda doc_id: "Public CV")
    result = gdoc.master_cv_text()
    assert result["source"] == "export"
    assert result["text"] == "Public CV"


def test_a_missing_google_auth_is_explained_rather_than_import_erroring():
    """google-auth is optional. Its absence must not take the credential-free
    path down with it, and must not surface as an ImportError from three frames
    deep."""
    message = gdoc.GDocError(
        "google-auth is not installed, so the private-Doc route is unavailable."
    )
    assert "google-auth" in str(message)
