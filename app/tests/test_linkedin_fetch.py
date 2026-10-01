"""Offline tests for app/linkedin_fetch.py — reading a posting from its URL.

No network here: every test drives the parser from markup, or drives
fetch_job through a patched httpx.get, following the convention in
test_aggregator_clients.py. Run with:
    python -m pytest app/tests/test_linkedin_fetch.py

The markup in PAGE is modelled on a real guest-endpoint response captured on
2026-09-25 (job 4471638473), trimmed of the recommendation/face-pile chrome
that makes up most of the 20-55 KB body but keeping every structure the parser
depends on: the top-card anchors, the duplicate `topcard__flavor--bullet` span
that carries the applicant count, the job-criteria list, and a description
containing nested tags, `<p><br></p>` padding, entities and bullets.

What these tests protect, in order of how badly it would hurt:
  * a row must never be stored with an empty description (silent data loss —
    it reaches the board looking fine with nothing for the AI step to score);
  * the description must be TEXT with entities decoded, because the readers
    downstream (day rates, language, clearance) are regexes over it and
    "&amp;" silently breaks any needle containing "&";
  * company/title/location must be this advert's, not the applicant count's
    or a "similar jobs" card's;
  * a failure must say what to do, since the paste workflow still works.
"""
from unittest.mock import MagicMock, patch

import pytest

from app import linkedin_fetch


# The description body alone, so a test can state exactly what it expects.
DESCRIPTION_HTML = """
<p><strong>Senior QA Engineer, Python, Java, Risk Systems</strong></p>
<p><br></p>
<p>Sponsorship is <strong>NOT</strong> available. Salary: Up to &pound;90,000 &amp; benefits</p>
<p>Desired skills:</p>
<ul>
  <li>Python and Java on Linux</li>
  <li>R&amp;D experience with Testcontainers</li>
</ul>
<script>var tracking = "should be dropped";</script>
<style>.x { color: red; }</style>
"""

PAGE = f"""<!---->
<section class="top-card-layout container-lined">
  <div class="top-card-layout__card">
    <a href="https://uk.linkedin.com/company/haleybridge?trk=public_jobs_topcard_logo">
      <img alt="Haley Bridge">
    </a>
    <div class="top-card-layout__entity-info-container">
      <a href="https://uk.linkedin.com/jobs/view/x-4471638473?trk=public_jobs_topcard-title"
         class="topcard__link">
        <h2 class="top-card-layout__title font-sans text-lg topcard__title">Senior Quality Assurance Automation Engineer</h2>
      </a>
      <h4 class="top-card-layout__second-subline">
        <div class="topcard__flavor-row">
          <span class="topcard__flavor">
            <a class="topcard__org-name-link topcard__flavor--black-link"
               href="https://uk.linkedin.com/company/haleybridge">Haley Bridge</a>
          </span>
          <span class="topcard__flavor topcard__flavor--bullet">London Area, United Kingdom</span>
        </div>
        <div class="topcard__flavor-row">
          <span class="posted-time-ago__text topcard__flavor--metadata">14 hours ago</span>
          <span class="num-applicants__caption topcard__flavor--metadata topcard__flavor--bullet">53 applicants</span>
        </div>
      </h4>
    </div>
  </div>
</section>

<section class="core-section-container">
  <div class="description__text description__text--rich">
    <section class="show-more-less-html" data-max-lines="5">
      <div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5 relative overflow-hidden">
        <div class="nested-wrapper">{DESCRIPTION_HTML}</div>
      </div>
      <button class="show-more-less-html__button show-more-less-button">Show more</button>
    </section>
  </div>
  <ul class="description__job-criteria-list">
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Seniority level</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">Mid-Senior level</span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Employment type</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">Full-time</span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Industries</h3>
      <span class="description__job-criteria-text description__job-criteria-text--criteria">Financial Services</span>
    </li>
  </ul>
</section>

<section class="similar-jobs">
  <h2 class="topcard__title">A Different Job From A Recommendation Card</h2>
  <a class="topcard__org-name-link">Some Other Company</a>
  <span class="topcard__flavor--bullet">Manchester, United Kingdom</span>
</section>
"""


def _fake_response(text="", status_code=200):
    resp = MagicMock()
    resp.text = text
    resp.status_code = status_code
    return resp


# --- the URL -> job id ----------------------------------------------------
def test_job_id_is_read_from_every_linkedin_url_shape():
    """Same three shapes add_job.canonical_url collapses to one key: the plain
    view URL, the slug form, and a search URL carrying currentJobId."""
    for url in [
        "https://www.linkedin.com/jobs/view/4471638473",
        "https://www.linkedin.com/jobs/view/4471638473/",
        "https://www.linkedin.com/jobs/view/4471638473/?refId=abc&trackingId=xyz",
        "https://uk.linkedin.com/jobs/view/senior-qa-engineer-at-haley-bridge-4471638473",
        "https://www.linkedin.com/jobs/search/?currentJobId=4471638473&keywords=qa",
    ]:
        assert linkedin_fetch.job_id_from_url(url) == "4471638473", url


def test_non_linkedin_urls_have_no_job_id():
    for url in ["https://boards.greenhouse.io/monzo/jobs/123", "", None,
                "https://www.linkedin.com/jobs/view/123"]:  # too short to be an id
        assert linkedin_fetch.job_id_from_url(url) is None


# --- parsing the page -----------------------------------------------------
def test_page_yields_every_field():
    got = linkedin_fetch.parse_job_page(PAGE)
    assert got["title"] == "Senior Quality Assurance Automation Engineer"
    assert got["company"] == "Haley Bridge"
    assert got["location"] == "London Area, United Kingdom"
    assert got["posted_at"] == "14 hours ago"
    assert got["criteria"]["Employment type"] == "Full-time"
    assert got["criteria"]["Seniority level"] == "Mid-Senior level"
    assert got["criteria"]["Industries"] == "Financial Services"


def test_location_is_not_the_applicant_count():
    """`topcard__flavor--bullet` is worn by BOTH the location span and the
    "53 applicants" span. Taking the last one, or concatenating them, would put
    "53 applicants" on the board as the job's location."""
    got = linkedin_fetch.parse_job_page(PAGE)
    assert got["location"] == "London Area, United Kingdom"
    assert "applicants" not in got["location"]


def test_recommendation_cards_do_not_override_the_posting():
    """A "similar jobs" card below the advert reuses the same class names. The
    top card is this advert; a later match is another company's job."""
    got = linkedin_fetch.parse_job_page(PAGE)
    assert got["company"] == "Haley Bridge"
    assert got["title"] == "Senior Quality Assurance Automation Engineer"


def test_description_is_text_with_entities_decoded():
    """The readers downstream are regexes over this text. Raw markup, or an
    "&amp;" left undecoded, silently breaks any needle containing "&" — and
    `<strong>` puts tag names in front of the language matcher."""
    got = linkedin_fetch.parse_job_page(PAGE)
    description = got["description"]
    assert "Senior QA Engineer, Python, Java, Risk Systems" in description
    assert "£90,000 & benefits" in description          # &pound; and &amp;
    assert "R&D experience with Testcontainers" in description
    assert "<p>" not in description and "<strong>" not in description
    assert "&amp;" not in description and "&pound;" not in description


def test_description_keeps_bullets_and_paragraph_breaks():
    """JobDetailPanel renders plain text with `white-space: pre-wrap`
    (globals.css .desc-plain), so the newlines produced here are what the
    reader sees. Flattening them would put the advert on screen as one blob."""
    description = linkedin_fetch.parse_job_page(PAGE)["description"]
    assert "- Python and Java on Linux" in description
    assert "- R&D experience with Testcontainers" in description
    assert "\n\n" in description            # paragraph break survived
    assert "\n\n\n" not in description      # <p><br></p> padding did not pile up


def test_nested_tags_do_not_truncate_the_description():
    """The description div is closed by depth counting, not by the first
    `</div>`. A non-greedy regex would stop at the nested wrapper and lose
    everything after it."""
    description = linkedin_fetch.parse_job_page(PAGE)["description"]
    assert "R&D experience with Testcontainers" in description   # last bullet
    assert "Desired skills:" in description


def test_script_and_style_text_is_not_content():
    description = linkedin_fetch.parse_job_page(PAGE)["description"]
    assert "should be dropped" not in description
    assert "color: red" not in description


def test_a_page_with_no_advert_body_is_an_error_not_a_blank_row():
    """The failure this must never repeat: storing description="" and exiting 0.
    It reaches the board looking like a success, with nothing for the AI step to
    score and a blank detail panel (see test_add_job's piped-stdin test)."""
    with pytest.raises(linkedin_fetch.LinkedInFetchError, match="advert text"):
        linkedin_fetch.parse_job_page(
            '<html><body><h2 class="topcard__title">A Job</h2></body></html>')


def test_an_empty_page_is_an_error():
    with pytest.raises(linkedin_fetch.LinkedInFetchError, match="empty page"):
        linkedin_fetch.parse_job_page("   ")


# --- html_to_text in isolation --------------------------------------------
def test_html_to_text_decodes_entities_and_drops_tags():
    assert linkedin_fetch.html_to_text("<p>Q&amp;A about C#</p>") == "Q&A about C#"


def test_html_to_text_on_a_fragment_with_no_block_tags():
    assert linkedin_fetch.html_to_text("Plain <em>emphasis</em> here") == "Plain emphasis here"


def test_html_to_text_of_nothing_is_empty():
    assert linkedin_fetch.html_to_text("") == ""
    assert linkedin_fetch.html_to_text(None) == ""


# --- employment type ------------------------------------------------------
def test_contract_employment_type_is_recognised():
    assert linkedin_fetch.employment_type_from_criteria(
        {"Employment type": "Contract"}) == "contract"
    assert linkedin_fetch.employment_type_from_criteria(
        {"Employment type": "Temporary"}) == "contract"


def test_full_time_is_not_read_as_permanent():
    """ "Full-time" is about hours, not permanence — a 12-month contract is
    advertised full-time constantly. Reading it as "permanent" would strip the
    Contract chip and the day-rate checks off real contract roles."""
    for stated in ["Full-time", "Part-time", "Internship", ""]:
        assert linkedin_fetch.employment_type_from_criteria(
            {"Employment type": stated}) is None
    assert linkedin_fetch.employment_type_from_criteria({}) is None
    assert linkedin_fetch.employment_type_from_criteria(None) is None


# --- fetch_job: transport and status handling -----------------------------
def test_fetch_job_hits_the_guest_endpoint_and_returns_the_posting():
    with patch("httpx.get", return_value=_fake_response(PAGE)) as mock_get:
        got = linkedin_fetch.fetch_job("4471638473")
    assert mock_get.call_args[0][0] == (
        "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4471638473")
    assert got["job_id"] == "4471638473"
    assert got["url"] == "https://www.linkedin.com/jobs/view/4471638473/"
    assert got["company"] == "Haley Bridge" and got["description"]


def test_fetch_job_sends_no_cookies():
    """Staying anonymous is the whole safety property: an authenticated scrape
    is what gets an account restricted. If this ever fails, that changed."""
    with patch("httpx.get", return_value=_fake_response(PAGE)) as mock_get:
        linkedin_fetch.fetch_job("4471638473")
    headers = mock_get.call_args.kwargs["headers"]
    assert set(headers) == {"User-Agent"}
    assert "cookie" not in {k.lower() for k in headers}
    assert "authorization" not in {k.lower() for k in headers}


@pytest.mark.parametrize("status,fragment", [
    (404, "no posting for that job id"),
    (403, "refused the request"),
    (999, "refused the request"),
    (429, "rate-limited"),
    (500, "HTTP 500"),
])
def test_fetch_failures_explain_what_to_do_instead(status, fragment):
    """Every one of these has a working fallback — the paste workflow — so the
    message must say so rather than surfacing as a traceback."""
    with patch("httpx.get", return_value=_fake_response("", status_code=status)):
        with pytest.raises(linkedin_fetch.LinkedInFetchError) as excinfo:
            linkedin_fetch.fetch_job("4471638473")
    message = str(excinfo.value)
    assert fragment in message
    if status != 500:
        assert "paste" in message.lower()


def test_a_network_error_is_a_fetch_error_not_a_traceback():
    import httpx
    with patch("httpx.get", side_effect=httpx.ConnectError("no route to host")):
        with pytest.raises(linkedin_fetch.LinkedInFetchError, match="Could not reach LinkedIn"):
            linkedin_fetch.fetch_job("4471638473")


def test_fetch_job_rejects_a_non_numeric_id():
    with pytest.raises(linkedin_fetch.LinkedInFetchError, match="Not a LinkedIn job id"):
        linkedin_fetch.fetch_job("senior-qa-engineer")
