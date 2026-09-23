"""Sanity test for aggregator_clients.py's full-JD-fetch logic — no real
network calls (this dev sandbox blocks outbound httpx to arbitrary domains
anyway; see the module docstrings). Mocks httpx.get/httpx.Response to
verify: (1) fetch_full_description prefers a recognized ATS's own JSON API
when the redirect lands there (and reports the resolved ats/slug back —
that's what feeds discover_companies.py), (2) falls back to scraping raw
HTML when it doesn't recognize the destination, (3) returns an empty
result cleanly on any failure, (4) fetch_adzuna only attempts the extra
fetch for jobs that already look like real candidates with a genuinely
thin snippet — not all ~2000 raw aggregator results, and (5) a resolved
Greenhouse/Lever match gets queued into DISCOVERED_COMPANIES for
main.py/discover_companies.py to pick up. Run with:
python -m pytest app/tests/test_aggregator_clients.py
"""
import os

import httpx
import pytest
from unittest.mock import patch, MagicMock

from app import aggregator_clients


def _fake_response(url, json_data=None, text="", status_code=200):
    resp = MagicMock()
    resp.url = url
    resp.text = text
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=json_data or {})
    return resp


def test_full_description_prefers_greenhouse_api():
    redirect_resp = _fake_response("https://job-boards.greenhouse.io/warnermusicgroup/jobs/1234567", text="<html>ignored</html>")
    job_resp = _fake_response("...", json_data={"content": "<p>Full JD from Greenhouse's own API.</p>"})

    with patch("httpx.get", side_effect=[redirect_resp, job_resp]) as mock_get:
        result = aggregator_clients.fetch_full_description("https://www.adzuna.ca/details/1")
    assert result["description"] == "<p>Full JD from Greenhouse's own API.</p>"
    assert result["ats"] == "greenhouse" and result["slug"] == "warnermusicgroup"
    assert mock_get.call_count == 2  # redirect follow, then the single-job API call
    print("fetch_full_description: Greenhouse single-job API preferred, ats/slug reported — OK")


def test_full_description_prefers_lever_api():
    posting_id = "22d02404-b9c9-4b77-9448-add55d961444"
    redirect_resp = _fake_response(f"https://jobs.lever.co/altaml/{posting_id}", text="<html>ignored</html>")
    job_resp = _fake_response("...", json_data={"descriptionPlain": "Full JD from Lever's own API."})

    with patch("httpx.get", side_effect=[redirect_resp, job_resp]):
        result = aggregator_clients.fetch_full_description("https://www.adzuna.ca/details/2")
    assert result["description"] == "Full JD from Lever's own API."
    assert result["ats"] == "lever" and result["slug"] == "altaml"
    print("fetch_full_description: Lever single-job API preferred, ats/slug reported — OK")


def test_full_description_falls_back_to_html_scrape():
    html = "<html><head><style>.x{color:red}</style></head><body><h1>Software Engineer</h1><p>We need Python.</p></body></html>"
    redirect_resp = _fake_response("https://careers.example.com/job/42", text=html)

    with patch("httpx.get", side_effect=[redirect_resp]):
        result = aggregator_clients.fetch_full_description("https://www.adzuna.ca/details/3")
    assert result["description"] is not None
    assert "Software Engineer" in result["description"] and "Python" in result["description"]
    assert "color:red" not in result["description"]  # style block dropped
    assert result["ats"] is None and result["slug"] is None  # unrecognized host -> no discovery signal
    print("fetch_full_description: unrecognized site falls back to HTML scrape, no discovery — OK")


def test_full_description_returns_none_on_failure():
    with patch("httpx.get", side_effect=Exception("connection refused")):
        result = aggregator_clients.fetch_full_description("https://www.adzuna.ca/details/4")
    assert result == {"description": None, "ats": None, "slug": None}
    print("fetch_full_description: network failure -> empty result, no crash — OK")

    assert aggregator_clients.fetch_full_description("") == {"description": None, "ats": None, "slug": None}
    print("fetch_full_description: empty url -> empty result — OK")


def test_adzuna_only_fetches_full_jd_for_promising_thin_snippets():
    """Gating check: fetch_full_description should NOT be called for jobs
    that fail title/location, or whose snippet is already long enough —
    only for the subset that's both a real candidate AND thin. This is
    what keeps a 2000-result run from turning into 2000 extra requests."""
    base_result = lambda **kw: {
        "title": "Software Engineer, Backend",
        "location": {"display_name": "Alberta, Canada"},
        "company": {"display_name": "TestCo"},
        "redirect_url": "https://www.adzuna.ca/details/x",
        "created": "2026-08-01",
        "description": "Short teaser.",
        **kw,
    }
    results = [
        base_result(),  # promising + thin -> SHOULD fetch
        base_result(title="Marketing Operations Manager"),  # fails title -> should NOT fetch
        base_result(location={"display_name": "Remote Poland"}),  # fails location -> should NOT fetch
        base_result(description="A" * 500 + " already a full-length description, well past the floor."),  # not thin -> should NOT fetch
    ]
    page_resp = _fake_response("...", json_data={"results": results})

    import os
    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", return_value=page_resp), \
         patch("app.aggregator_clients.fetch_full_description",
               return_value={"description": "FULL JD TEXT, much longer than the original teaser snippet.",
                              "ats": None, "slug": None}) as mock_full:
        jobs = aggregator_clients.fetch_adzuna({"results_per_page": 50, "max_pages": 1})

    assert mock_full.call_count == 1, f"expected exactly 1 full-JD fetch, got {mock_full.call_count}"
    assert jobs[0]["description"].startswith("FULL JD TEXT")
    assert jobs[1]["description"] == "Short teaser."
    assert jobs[2]["description"] == "Short teaser."
    assert jobs[3]["description"].startswith("AAAA")
    print("fetch_adzuna: full-JD fetch gated to promising+thin results only — OK")


def test_adzuna_queues_discovered_companies():
    """When fetch_full_description resolves a Greenhouse/Lever match, that
    company should land in DISCOVERED_COMPANIES for discover_companies.py
    to pick up after the run — see main.py."""
    result = {
        "title": "Software Engineer, Automated Marketing",
        "location": {"display_name": "Alberta, Canada"},
        "company": {"display_name": "Warner Music Group"},
        "redirect_url": "https://www.adzuna.ca/details/5702928490",
        "created": "2026-04-17",
        "description": "Short teaser…",
    }
    page_resp = _fake_response("...", json_data={"results": [result]})

    import os
    aggregator_clients.DISCOVERED_COMPANIES.clear()
    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", return_value=page_resp), \
         patch("app.aggregator_clients.fetch_full_description",
               return_value={"description": "Full JD text, much longer than the teaser snippet ever was.",
                              "ats": "greenhouse", "slug": "warnermusicgroup"}):
        aggregator_clients.fetch_adzuna({"results_per_page": 50, "max_pages": 1})

    assert aggregator_clients.DISCOVERED_COMPANIES == [
        {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"}
    ]
    print("fetch_adzuna: resolved Greenhouse/Lever match queued into DISCOVERED_COMPANIES — OK")


def test_adzuna_country_is_configurable_not_hardcoded():
    """The country used to be hardcoded to 'ca' in the URL, which made Adzuna
    unusable outside Canada no matter what aggregators.yaml said (2026-09).
    It now comes from params['country'], is NOT forwarded as a query param,
    and 'uk' is translated to Adzuna's actual code 'gb'."""
    import os
    page_resp = _fake_response("...", json_data={"results": []})

    def _url_for(params):
        with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
             patch("httpx.get", return_value=page_resp) as mock_get:
            aggregator_clients.fetch_adzuna({**params, "max_pages": 1})
        return mock_get.call_args.args[0], mock_get.call_args.kwargs.get("params", {})

    url, query = _url_for({"country": "gb", "where": "London", "what": "SDET"})
    assert url == "https://api.adzuna.com/v1/api/jobs/gb/search/1", url
    assert query["where"] == "London"
    assert "country" not in query, "country belongs in the path, not the query string"

    url, _ = _url_for({"country": "UK"})  # the trap
    assert url == "https://api.adzuna.com/v1/api/jobs/gb/search/1", url
    url, _ = _url_for({"country": "usa"})
    assert url == "https://api.adzuna.com/v1/api/jobs/us/search/1", url

    # Omitted -> documented default, so the repo's original config still works
    url, _ = _url_for({})
    assert url == "https://api.adzuna.com/v1/api/jobs/ca/search/1", url

    # A bad value fails loudly instead of silently querying a nonsense endpoint
    for bad in ["england", "g", "united kingdom!!"]:
        try:
            _url_for({"country": bad})
        except ValueError as exc:
            assert "two-letter country code" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for country={bad!r}")
    print("fetch_adzuna: country configurable, uk->gb, defaults preserved, bad values rejected — OK")


def test_adzuna_errors_never_leak_credentials():
    """httpx embeds the full request URL — including app_id and app_key, which
    Adzuna requires as QUERY PARAMETERS — in its exception messages. A single
    failed call therefore printed a live app_key into stderr and the terminal
    (happened for real 2026-09-21). Assert the redaction holds, on both the
    status-error path and the transport-error path."""
    import os
    secret_id, secret_key = "myappid123", "deadbeef" * 4

    # Body deliberately echoes the credentials, as a verbose error page might.
    echoing_body = f"denied for app_id={secret_id}&app_key={secret_key}"
    with patch.dict(os.environ, {"ADZUNA_APP_ID": secret_id, "ADZUNA_APP_KEY": secret_key}), \
         patch("httpx.get", return_value=_fake_response("...", text=echoing_body, status_code=403)):
        try:
            aggregator_clients.fetch_adzuna({"max_pages": 1})
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected the 403 to raise")
    assert secret_key not in message and secret_id not in message, message
    assert message.count("[REDACTED]") == 2, message

    with patch.dict(os.environ, {"ADZUNA_APP_ID": secret_id, "ADZUNA_APP_KEY": secret_key}), \
         patch("httpx.get", side_effect=httpx.ConnectError(
             f"failed for url 'https://api.adzuna.com/x?app_id={secret_id}&app_key={secret_key}'")):
        try:
            aggregator_clients.fetch_adzuna({"max_pages": 1})
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected the transport error to raise")
    assert secret_key not in message and secret_id not in message, message
    print("fetch_adzuna: credential redaction on both error paths — OK")


def test_adzuna_401_says_where_to_find_the_application_id():
    import os
    with patch.dict(os.environ, {"ADZUNA_APP_ID": "edminshull", "ADZUNA_APP_KEY": "k" * 32}), \
         patch("httpx.get", return_value=_fake_response("...", status_code=401)):
        try:
            aggregator_clients.fetch_adzuna({"max_pages": 1})
        except RuntimeError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected the 401 to raise")
    assert "Application ID" in message and "developer.adzuna.com" in message
    assert "k" * 32 not in message
    print("fetch_adzuna: 401 explains the app_id/app_key mix-up — OK")


# ---------------------------------------------------------------------------
# Contract role handling: Adzuna's contract_type field and the annualised
# salary it reports contract pay as.
# ---------------------------------------------------------------------------

def test_adzuna_employment_type_is_read_from_contract_type():
    """Adzuna reports `contract_type` on every result. It is a RESPONSE field
    only — sending it as a request parameter is a 400 — so it is read here
    and the search filter is the boolean `contract=1` in aggregators.yaml."""
    captured = []

    def fake_get(url, params=None, headers=None, timeout=None):
        page = int(url.rsplit("/", 1)[-1])
        if page > 1:
            return _fake_response(url, json_data={"results": []})
        return _fake_response(url, json_data={"results": [
            {"title": "Senior QA Engineer", "company": {"display_name": "Monzo"},
             "location": {"display_name": "London, UK"}, "redirect_url": "https://ex.com/1",
             "description": "x" * 600, "created": "2026-09-16T04:04:36Z",
             "contract_type": "contract", "salary_min": 104000, "salary_max": 117000,
             "salary_is_predicted": "0"},
            {"title": "Test Analyst", "company": {"display_name": "Xe"},
             "location": {"display_name": "London, UK"}, "redirect_url": "https://ex.com/2",
             "description": "y" * 600, "created": "2026-09-16T04:04:36Z",
             "contract_type": "permanent", "salary_min": 60000, "salary_max": 100000,
             "salary_is_predicted": "0"},
            {"title": "SDET", "company": {"display_name": "Acme"},
             "location": {"display_name": "London, UK"}, "redirect_url": "https://ex.com/3",
             "description": "z" * 600, "created": "2026-09-16T04:04:36Z",
             "contract_type": None},
        ]})

    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", side_effect=fake_get):
        jobs = aggregator_clients.fetch_adzuna({"what": "qa", "where": "London", "max_pages": 1})

    by_url = {j["url"]: j for j in jobs}
    assert len(jobs) == 3
    # contract_type is passed through, not guessed at.
    assert by_url["https://ex.com/1"]["employment_type"] == "contract"
    assert by_url["https://ex.com/2"]["employment_type"] == "permanent"
    # A null contract_type stays "unknown" rather than defaulting either way.
    assert by_url["https://ex.com/3"]["employment_type"] == "unknown"


def test_adzuna_contract_salary_is_converted_to_a_day_rate_at_260_days():
    """Adzuna reports contract pay ANNUALISED. Measured live on 2026-09-21:
    for every contract ad quoting both, salary_max / stated day rate was
    exactly 260. So 104,000-117,000 is a 400-450/day role, not a 117k one."""
    def fake_get(url, params=None, headers=None, timeout=None):
        if int(url.rsplit("/", 1)[-1]) > 1:
            return _fake_response(url, json_data={"results": []})
        return _fake_response(url, json_data={"results": [
            {"title": "Senior QA Engineer", "company": {"display_name": "Monzo"},
             "location": {"display_name": "London, UK"}, "redirect_url": "https://ex.com/1",
             "description": "d" * 600, "contract_type": "contract",
             "salary_min": 104000, "salary_max": 117000, "salary_is_predicted": "0"},
        ]})

    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", side_effect=fake_get):
        job = aggregator_clients.fetch_adzuna({"what": "qa", "where": "London", "max_pages": 1})[0]

    assert job["day_rate_min"] == pytest.approx(400.0)
    assert job["day_rate_max"] == pytest.approx(450.0)
    # Derived, NOT stated — the board must not present this as a quoted rate.
    assert job["day_rate_source"] == "derived_from_advertised_salary"


def test_adzuna_predicted_salary_is_tagged_as_weaker_evidence():
    """salary_is_predicted=1 means Adzuna estimated it from similar ads; the
    advert never said it. That is a weaker signal than one the advert states
    and is tagged differently so the board can say so."""
    def fake_get(url, params=None, headers=None, timeout=None):
        if int(url.rsplit("/", 1)[-1]) > 1:
            return _fake_response(url, json_data={"results": []})
        return _fake_response(url, json_data={"results": [
            {"title": "SDET", "company": {"display_name": "Acme"},
             "location": {"display_name": "London, UK"}, "redirect_url": "https://ex.com/9",
             "description": "d" * 600, "contract_type": "contract",
             "salary_min": 78000, "salary_max": 78000, "salary_is_predicted": "1"},
        ]})

    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", side_effect=fake_get):
        job = aggregator_clients.fetch_adzuna({"what": "qa", "where": "London", "max_pages": 1})[0]

    assert job["day_rate_source"] == "estimated_by_adzuna"


def test_permanent_roles_get_no_day_rate_at_all():
    """The 260-day annualisation applies to contract pay only. Turning a
    permanent £100,000 salary into a "£385/day" role would be nonsense."""
    def fake_get(url, params=None, headers=None, timeout=None):
        if int(url.rsplit("/", 1)[-1]) > 1:
            return _fake_response(url, json_data={"results": []})
        return _fake_response(url, json_data={"results": [
            {"title": "Senior QA Engineer", "company": {"display_name": "Monzo"},
             "location": {"display_name": "London, UK"}, "redirect_url": "https://ex.com/1",
             "description": "d" * 600, "contract_type": "permanent",
             "salary_min": 100000, "salary_max": 110000, "salary_is_predicted": "0"},
        ]})

    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", side_effect=fake_get):
        job = aggregator_clients.fetch_adzuna({"what": "qa", "where": "London", "max_pages": 1})[0]

    assert "day_rate_max" not in job
    assert job["employment_type"] == "permanent"


def test_max_days_old_and_contract_are_forwarded_to_adzuna():
    """The two request parameters the recency window and the contract-only
    search depend on. They are NOT consumed by fetch_adzuna (unlike
    max_pages/country) — Adzuna understands them, so they must reach the
    request untouched."""
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        seen.update(params or {})
        return _fake_response(url, json_data={"results": []})

    with patch.dict(os.environ, {"ADZUNA_APP_ID": "id", "ADZUNA_APP_KEY": "key"}), \
         patch("httpx.get", side_effect=fake_get):
        aggregator_clients.fetch_adzuna(
            {"what": "qa", "where": "London", "contract": 1, "max_days_old": 30,
             "max_pages": 1, "country": "gb"})

    assert seen["contract"] == 1
    assert seen["max_days_old"] == 30


if __name__ == "__main__":
    # Script runs have no pytest, so no conftest and no monkeypatch: install
    # the pinned fetch-gate config here. Without this, the Adzuna gating test
    # below is judged against whatever the user put in filters.yaml and fails
    # with "expected exactly 1 full-JD fetch, got 0". See pinned_filters.py.
    from app.tests import pinned_filters
    pinned_filters.apply()

    test_full_description_prefers_greenhouse_api()
    test_full_description_prefers_lever_api()
    test_full_description_falls_back_to_html_scrape()
    test_full_description_returns_none_on_failure()
    test_adzuna_only_fetches_full_jd_for_promising_thin_snippets()
    test_adzuna_queues_discovered_companies()
    test_adzuna_country_is_configurable_not_hardcoded()
    test_adzuna_employment_type_is_read_from_contract_type()
    test_adzuna_contract_salary_is_converted_to_a_day_rate_at_260_days()
    test_adzuna_predicted_salary_is_tagged_as_weaker_evidence()
    test_permanent_roles_get_no_day_rate_at_all()
    test_max_days_old_and_contract_are_forwarded_to_adzuna()
    print("\nAll assertions passed.")