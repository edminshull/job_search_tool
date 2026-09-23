"""Sanity test for ats_clients.py's fetch_smartrecruiters — no real network
calls (this dev sandbox blocks outbound httpx to arbitrary domains, and
api.smartrecruiters.com additionally blocked this session's WebFetch via
robots.txt — see the CONFIDENCE NOTE in ats_clients.py). Mocks httpx.get to
verify: (1) basic field parsing incl. remote-location formatting,
(2) pagination across pages via limit/offset, (3) the full-description
fetch is gated to postings that already pass title/location filters —
same idea as aggregator_clients.fetch_adzuna's gating, so a company with
hundreds of postings doesn't turn into hundreds of extra requests,
(4) a failed detail fetch degrades to an empty description, not a crash,
and (5) "smartrecruiters" is wired into FETCHERS/fetch_company. Run with:
python -m pytest app/tests/test_ats_clients.py
"""
import pytest
from unittest.mock import patch, MagicMock

from app import ats_clients


def _list_resp(content):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"content": content})
    return resp


def _detail_resp(job_description="", qualifications="", additional=""):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    sections = {}
    if job_description:
        sections["jobDescription"] = {"text": job_description}
    if qualifications:
        sections["qualifications"] = {"text": qualifications}
    if additional:
        sections["additionalInformation"] = {"text": additional}
    resp.json = MagicMock(return_value={"jobAd": {"sections": sections}})
    return resp


def _posting(id="1", name="Software Engineer", city="Calgary", region="AB", country="Canada", remote=False):
    return {
        "id": id, "name": name,
        "location": {"city": city, "region": region, "country": country, "remote": remote},
        "releasedDate": "2026-08-01T00:00:00.000Z",
    }


def test_basic_parsing_and_remote_location():
    posting = _posting(remote=True)
    with patch("httpx.get", side_effect=[_list_resp([posting]), _detail_resp("Build things with Python.")]):
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")
    assert len(jobs) == 1
    j = jobs[0]
    assert j["company"] == "TestCo"
    assert j["title"] == "Software Engineer"
    assert j["location"] == "Remote (Calgary, AB, Canada)"
    assert j["posted_at"] == "2026-08-01T00:00:00.000Z"
    assert j["url"] == "https://jobs.smartrecruiters.com/testco/1"  # fallback construction, no applyUrl/ref given
    assert "Python" in j["description"]
    print("fetch_smartrecruiters: basic parsing + remote location + description fetched — OK")


def test_prefers_applyurl_or_ref_when_present():
    posting = _posting(id="2")
    posting["applyUrl"] = "https://jobs.smartrecruiters.com/TestCo/oa_1234"
    with patch("httpx.get", side_effect=[_list_resp([posting]), _detail_resp("JD text")]):
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")
    assert jobs[0]["url"] == "https://jobs.smartrecruiters.com/TestCo/oa_1234"
    print("fetch_smartrecruiters: prefers applyUrl over constructed URL — OK")


def test_pagination_across_pages():
    page1 = [_posting(id=str(i)) for i in range(100)]  # full page -> expect another request
    page2 = [_posting(id="100")]  # short page -> stop

    # Only postings that pass title/location trigger a detail fetch; all
    # 101 here are "Software Engineer" / Calgary, AB -> all gated in, so
    # interleave list/detail responses accordingly.
    responses = [_list_resp(page1)]
    responses += [_detail_resp("JD") for _ in range(100)]
    responses += [_list_resp(page2)]
    responses += [_detail_resp("JD")]

    with patch("httpx.get", side_effect=responses) as mock_get:
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")

    assert len(jobs) == 101
    # 2 list calls (full page then short page) + 101 detail calls
    assert mock_get.call_count == 2 + 101
    # second list call used offset=100
    second_list_call = mock_get.call_args_list[101]  # after 100 detail calls following the first list call
    assert second_list_call.kwargs["params"]["offset"] == 100
    print("fetch_smartrecruiters: pagination via limit/offset — OK")


def test_description_fetch_gated_to_promising_postings():
    promising = _posting(id="1", name="Software Engineer", city="Calgary", region="AB", country="Canada")
    not_promising_title = _posting(id="2", name="Marketing Operations Manager")
    not_promising_location = _posting(id="3", name="Software Engineer", city="Warsaw", region="", country="Poland")

    with patch("httpx.get", side_effect=[
        _list_resp([promising, not_promising_title, not_promising_location]),
        _detail_resp("Only fetched for the promising one"),
    ]) as mock_get:
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")

    assert mock_get.call_count == 2  # 1 list call + exactly 1 detail call
    by_id = {j["url"].rsplit("/", 1)[-1]: j for j in jobs}
    assert "Only fetched" in by_id["1"]["description"]
    assert by_id["2"]["description"] == ""
    assert by_id["3"]["description"] == ""
    print("fetch_smartrecruiters: full-description fetch gated to promising postings only — OK")


def test_description_fetch_failure_degrades_gracefully():
    posting = _posting()
    with patch("httpx.get", side_effect=[_list_resp([posting]), Exception("500 server error")]):
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")
    assert jobs[0]["description"] == ""
    print("fetch_smartrecruiters: detail-fetch failure -> empty description, no crash — OK")


def test_wired_into_fetchers_and_fetch_company():
    assert ats_clients.FETCHERS["smartrecruiters"] is ats_clients.fetch_smartrecruiters
    with patch("httpx.get", side_effect=[_list_resp([]), ]):
        jobs = ats_clients.fetch_company({"name": "TestCo", "ats": "smartrecruiters", "slug": "testco"})
    assert jobs == []
    print("FETCHERS/fetch_company: smartrecruiters wired in correctly — OK")


if __name__ == "__main__":
    # Script runs have no pytest, so no conftest and no monkeypatch: install
    # the pinned fetch-gate config here. Without this, the fixtures below are
    # judged against whatever the user put in filters.yaml and the gating
    # assertions fail. See pinned_filters.py.
    from app.tests import pinned_filters
    pinned_filters.apply()

    test_basic_parsing_and_remote_location()
    test_prefers_applyurl_or_ref_when_present()
    test_pagination_across_pages()
    test_description_fetch_gated_to_promising_postings()
    test_description_fetch_failure_degrades_gracefully()
    test_wired_into_fetchers_and_fetch_company()
    print("\nAll assertions passed.")

# ---------------------------------------------------------------------------
# fetch_workday — added 2026-09-23 with the Kainos entry.
#
# Every case below is a way this fetcher fails SILENTLY rather than loudly, which
# is why they are pinned rather than left to a live run to notice:
#
#   1. A wrong host returns connection-refused (the first bug: the host was built
#      as the shared apex `wd3.myworkdayjobs.com` instead of `<tenant>.wd3...`).
#   2. A wrong site returns an empty posting list, so the company simply never
#      contributes anything and nothing reports a problem.
#   3. `locationsText` is "5 Locations" for multi-site roles, and the location
#      filter is an allowlist — so a UK role hidden in a multi-location posting
#      is dropped on arrival. Verified live: Senior Test Engineer (Public Sector)
#      = "Homeworker - UK" + [Gdansk, Derry-Londonderry, Belfast, Birmingham].
# ---------------------------------------------------------------------------

def _wd_list_resp(postings, total=None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "total": total if total is not None else len(postings),
        "jobPostings": postings,
    })
    return resp


def _wd_detail_resp(description="", location=None, additional=None):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    info = {"jobDescription": description}
    if location is not None:
        info["location"] = location
    if additional is not None:
        info["additionalLocations"] = additional
    resp.json = MagicMock(return_value={"jobPostingInfo": info})
    return resp


def _wd_posting(title, path, loc_text="Belfast", posted="Posted Today"):
    return {"title": title, "externalPath": path, "locationsText": loc_text,
            "postedOn": posted, "timeType": "Full time"}


# The workday tests below run against the LIVE filters.yaml via the
# `real_filters` marker, and that is load-bearing rather than cosmetic.
# conftest.py swaps filters.py for the pinned Canada/Alberta test config, under
# which a QA/Test title is NOT relevant and "Homeworker - UK" is NOT an allowed
# location — so the title-gated detail fetch silently skips and every
# location-expansion assertion tests nothing at all. That is not a hypothetical:
# it is how these tests failed first time round, reporting an empty description
# while looking like a fetcher bug. The SmartRecruiters tests ABOVE keep the
# pinned config, because their fixtures ("Software Engineer" / Calgary) are
# exactly what that config describes.
def test_workday_slug_builds_the_tenant_subdomain():
    """The bug that made this fetcher fail on its first live run: the host is
    `<tenant>.<region>.myworkdayjobs.com`. `wd3.myworkdayjobs.com` alone is the
    shared apex and does not resolve, so every request was connection-refused
    while the config looked entirely correct."""
    base, tenant, site = ats_clients._workday_endpoint("kainos/kainos")
    assert base == "https://kainos.wd3.myworkdayjobs.com"
    assert tenant == "kainos" and site == "kainos"
    # A hostname in the slug is refused outright rather than concatenated.
    with pytest.raises(ValueError, match="looks like a hostname"):
        ats_clients._workday_endpoint("kainos.wd3.myworkdayjobs.com/kainos/kainos")


@pytest.mark.real_filters
def test_workday_slug_requires_both_parts():
    """A single name cannot say whether it is the tenant or the site, and a guess
    produces an empty posting list — a silent no-op, not an error."""
    with pytest.raises(ValueError, match="tenant/site"):
        ats_clients._workday_endpoint("kainos")
    with pytest.raises(ValueError, match="tenant/site"):
        ats_clients._workday_endpoint("")


@pytest.mark.real_filters
def test_workday_slug_accepts_a_non_default_region():
    base, _, _ = ats_clients._workday_endpoint("kainos/kainos/wd1")
    assert base == "https://kainos.wd1.myworkdayjobs.com"
    with pytest.raises(ValueError, match="region"):
        ats_clients._workday_endpoint("kainos/kainos/wd99")


@pytest.mark.real_filters
def test_workday_expands_multi_location_postings_so_uk_roles_survive():
    """THE case that matters. A posting advertised in Birmingham AND as a UK home
    worker is one this candidate can do; judged on the primary location alone it
    is dropped, and judged on the list's "5 Locations" it is dropped too.

    Uses the real values read off the Kainos board."""
    from app import filters
    posting = _wd_posting("Lead Test Engineer (Healthcare)", "/job/Birmingham/x")
    posting["locationsText"] = "4 Locations"
    with patch("httpx.post", return_value=_wd_list_resp([posting])), \
         patch("httpx.get", return_value=_wd_detail_resp(
             "JD text", location="Birmingham",
             additional=["Derry-Londonderry", "Belfast", "Homeworker - UK"])):
        jobs = ats_clients.fetch_workday("Kainos", "kainos/kainos")

    assert len(jobs) == 1
    loc = jobs[0]["location"]
    assert "Homeworker - UK" in loc, "the UK-eligible location must survive"
    assert "Birmingham" in loc, "the primary location must still be reported"
    assert filters.location_is_allowed(loc), (
        "with the locations expanded this posting is keepable; without it the "
        "pipeline drops a role the candidate can actually do")


@pytest.mark.real_filters
def test_workday_location_joins_dedupes_and_tolerates_junk():
    assert ats_clients._workday_location("Belfast", ["Belfast", "London"]) == "Belfast, London"
    assert ats_clients._workday_location("", ["London"]) == "London"
    assert ats_clients._workday_location("London", None) == "London"
    assert ats_clients._workday_location(None, []) == ""
    assert ats_clients._workday_location("London", ["london", "LEEDS"]) == "London, LEEDS"


@pytest.mark.real_filters
def test_workday_parses_fields_and_builds_the_public_url():
    posting = _wd_posting("Test Consultant", "/job/Buenos-Aires/Test-Consultant_JR_18381",
                          loc_text="Buenos Aires", posted="Posted 8 Days Ago")
    with patch("httpx.post", return_value=_wd_list_resp([posting])), \
         patch("httpx.get", return_value=_wd_detail_resp(
             "<p>Automate the tests.</p>", location="Buenos Aires")):
        jobs = ats_clients.fetch_workday("Kainos", "kainos/kainos")
    j = jobs[0]
    assert j["company"] == "Kainos"
    assert j["title"] == "Test Consultant"
    assert j["posted_at"] == "Posted 8 Days Ago"   # relative, parsed by lib/dates.ts
    assert "Automate the tests" in j["description"]
    assert j["url"] == ("https://kainos.wd3.myworkdayjobs.com/en-US/kainos"
                        "/job/Buenos-Aires/Test-Consultant_JR_18381")


@pytest.mark.real_filters
def test_workday_paginates_until_short_page():
    pages = [
        _wd_list_resp([_wd_posting(f"Test Engineer {i}", f"/job/Belfast/t{i}") for i in range(20)],
                      total=25),
        _wd_list_resp([_wd_posting(f"Test Engineer {i}", f"/job/Belfast/t{i}") for i in range(20, 25)],
                      total=25),
    ]
    with patch("httpx.post", side_effect=pages), \
         patch.object(ats_clients, "_workday_detail", return_value=("", "")) as detail:
        jobs = ats_clients.fetch_workday("Kainos", "kainos/kainos")
    assert len(jobs) == 25, "must follow pagination rather than stop at the first page"
    assert detail.call_count == 25, "one detail fetch per relevant-titled posting"


@pytest.mark.real_filters
def test_workday_dedupes_postings_repeated_across_pages():
    """Workday will repeat a posting across pages when the board changes mid-scan.
    Without this the same job is fetched twice and dedup sees it once — but the
    duplicate detail request is wasted, and a stable externalPath is the key."""
    posting = _wd_posting("Test Engineer", "/job/Belfast/dupe")
    pages = [
        _wd_list_resp([posting] * 20, total=40),
        _wd_list_resp([posting] * 20, total=40),
    ]
    with patch("httpx.post", side_effect=pages), \
         patch.object(ats_clients, "_workday_detail", return_value=("", "")):
        jobs = ats_clients.fetch_workday("Kainos", "kainos/kainos")
    assert len(jobs) == 1


@pytest.mark.real_filters
def test_workday_skips_the_detail_fetch_for_irrelevant_titles():
    """The cost control. Kainos has a 125-posting board of mostly consultancy
    roles; expanding every one would be 125 requests for jobs that die on title
    alone. Verified live: 43 listed, 4 with relevant titles."""
    postings = [
        _wd_posting("Regional Sales Director", "/job/Belfast/sales"),
        _wd_posting("Test Engineer", "/job/Belfast/test"),
        _wd_posting("UX Designer", "/job/Belfast/ux"),
    ]
    with patch("httpx.post", return_value=_wd_list_resp(postings)), \
         patch.object(ats_clients, "_workday_detail", return_value=("JD", "London")) as detail:
        jobs = ats_clients.fetch_workday("Kainos", "kainos/kainos")
    assert len(jobs) == 3, "every posting is still returned; only the detail fetch is gated"
    assert detail.call_count == 1, "only the relevant-titled posting should be expanded"
    # The un-expanded ones keep the list's location text, so the location filter
    # still makes the keep/drop decision rather than a blank being assumed fine.
    assert jobs[0]["location"] == "Belfast"


@pytest.mark.real_filters
def test_workday_a_failed_detail_fetch_degrades_gracefully():
    """A 500 on one posting must not lose the whole board — and must leave the
    description empty, which filters.py reads as "unknown, don't reject on stack
    alone" rather than as an absent dealbreaker."""
    posting = _wd_posting("Test Engineer", "/job/Belfast/test")
    failing = MagicMock()
    failing.raise_for_status = MagicMock(side_effect=RuntimeError("500"))
    with patch("httpx.post", return_value=_wd_list_resp([posting])), \
         patch("httpx.get", return_value=failing):
        jobs = ats_clients.fetch_workday("Kainos", "kainos/kainos")
    assert len(jobs) == 1
    assert jobs[0]["description"] == ""
    assert jobs[0]["location"] == "Belfast", "the list's location is kept as a fallback"


@pytest.mark.real_filters
def test_workday_search_text_is_sent_to_the_server():
    """`search:` in companies.yaml is a server-side filter over the whole posting,
    which is how a company entry asks for a concept instead of guessing titles."""
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, payload=json, headers=headers)
        return _wd_list_resp([])

    with patch("httpx.post", fake_post):
        ats_clients.fetch_workday("Kainos", "kainos/kainos", search_text="test")

    assert captured["payload"]["searchText"] == "test"
    assert captured["url"] == "https://kainos.wd3.myworkdayjobs.com/wday/cxs/kainos/kainos/jobs"
    assert "User-Agent" in captured["headers"], "Workday 403s the default httpx UA on some tenants"


@pytest.mark.real_filters
def test_workday_is_wired_into_fetchers_and_passes_search_through():
    """fetch_company must route `search` to workday and NOT splat it into the
    other fetchers' signatures, which take only (name, slug)."""
    assert ats_clients.FETCHERS["workday"] is ats_clients.fetch_workday

    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(payload=json)
        return _wd_list_resp([])

    with patch("httpx.post", fake_post):
        jobs = ats_clients.fetch_company(
            {"name": "Kainos", "ats": "workday", "slug": "kainos/kainos", "search": "test"})
    assert jobs == []
    assert captured["payload"]["searchText"] == "test"

    # A workday entry with no `search` still works (whole-board scan).
    with patch("httpx.post", return_value=_wd_list_resp([])):
        assert ats_clients.fetch_company(
            {"name": "Acme", "ats": "workday", "slug": "acme/acme"}) == []
