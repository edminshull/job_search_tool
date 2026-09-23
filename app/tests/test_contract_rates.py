"""Tests for app/contract_rates.py — the day-rate equivalence model and the
IR35 / day-rate parsing that feeds it.

The tax arithmetic is the part worth pinning hardest. It decides whether Ed
applies to a contract or not, so a silent change to a rate or a threshold in
contract.yaml (a dropped digit in the secondary threshold, a Budget update
half-applied) would change real decisions without anything looking broken.
`test_shipped_config_matches_the_verified_2026_27_rates` is the guard for
that: it fails loudly if contract.yaml's numbers stop matching the values
verified against gov.uk, forcing whoever changes them to do so deliberately.

The parser tests are all drawn from real advert text, including the
"InsideIR35" spelling with no space and the "£50 per day travel allowance"
line that must not be mistaken for a rate.
"""
import pytest

from app import contract_rates as cr


@pytest.fixture
def model():
    """Built from the shipped contract.yaml, so these tests cover the file
    the pipeline actually reads rather than a fixture that could drift."""
    return cr.default_model()


@pytest.fixture
def loose_model():
    """A copy with the 2026/27 rates but tamable policy knobs."""
    import copy

    cfg = copy.deepcopy(cr.load_config())
    return cr.RateModel(cfg)


# The arithmetic assertions below are worked at a FIXED salary rather than at
# whatever the local contract.yaml happens to hold. contract.yaml carries a
# personal target salary and is gitignored, so a fresh clone loads
# contract.yaml.example, whose example salary is a different number — and a pin
# that moved with the configured salary would not be pinning anything. The value
# is arbitrary and deliberately unrelated to the shipped example: £100,000 is a
# round figure sitting just below the personal-allowance taper.
PINNED_SALARY = 100000


@pytest.fixture
def pinned_model():
    """The shipped config with the target salary pinned, for the arithmetic
    that is only meaningful at a known salary."""
    import copy

    cfg = copy.deepcopy(cr.load_config())
    cfg["target_permanent_salary_gbp"] = PINNED_SALARY
    return cr.RateModel(cfg)


# ---------------------------------------------------------------------------
# The tax model
# ---------------------------------------------------------------------------

def test_shipped_config_matches_the_verified_2026_27_rates(model):
    """Pins every rate in contract.yaml to the value verified against gov.uk
    for 2026/27 (see README.md for the sources). If a future edit changes one
    of these, that is a decision someone has to make on purpose."""
    assert model.tax_year == "2026/27"
    assert (model.personal_allowance, model.basic_rate_limit, model.higher_rate_limit) == (12570, 50270, 125140)
    assert (model.basic_rate, model.higher_rate, model.additional_rate) == (0.20, 0.40, 0.45)
    assert (model.ee_nic_pt, model.ee_nic_uel) == (12570, 50270)
    assert (model.ee_nic_main, model.ee_nic_upper) == (0.08, 0.02)
    # 15% above a £5,000 secondary threshold, from April 2025.
    assert (model.er_nic_st, model.er_nic_rate) == (5000, 0.15)
    assert (model.levy_rate, model.levy_allowance) == (0.005, 15000)
    # Dividend rates ROSE by 2 points from April 2026. Any outside-IR35
    # figure quoted in 2025 assumed 8.75%/33.75% and is now too optimistic,
    # so this assertion is the thing that stops that creeping back in.
    assert model.div_basic == 0.1075
    assert model.div_higher == 0.3575
    assert model.div_allowance == 500
    assert (model.ct_small_rate, model.ct_main_rate) == (0.19, 0.25)
    assert (model.ct_small_limit, model.ct_main_limit) == (50000, 250000)
    assert model.pension_rate == 0.03
    assert (model.pension_lower, model.pension_upper) == (6240, 50270)
    assert model.days == 220


def test_income_tax_and_nic_on_a_100k_salary(model):
    # £100,000: 20% on the £37,700 basic band + 40% on the £49,730 above it.
    # Note this is exactly the taper threshold, and the taper is `>` not `>=`,
    # so the personal allowance is still the full £12,570 here.
    assert model.income_tax(100000) == pytest.approx(27432.00, abs=0.01)
    assert model.employee_nic(100000) == pytest.approx(4010.60, abs=0.01)
    assert model.net_from_salary(100000) == pytest.approx(68557.40, abs=0.01)


def test_personal_allowance_taper_applies_above_100k(model):
    # The taper is not triggered by the £100k test salary above, but
    # perm_equivalent returns salaries past the threshold for high day rates,
    # and the taper is what makes those correct: ignoring it understates tax by
    # thousands.
    assert model.allowance_for(90000) == 12570
    assert model.allowance_for(100000) == 12570
    assert model.allowance_for(110000) == pytest.approx(7570)
    assert model.allowance_for(140000) == 0
    assert model.net_from_salary(102000) < 102000 - model.income_tax(102000) + 1  # sanity: no sign error


def test_employer_costs_are_the_gross_up(model):
    assert model.employer_nic(100000) == pytest.approx(14250.00, abs=0.01)
    assert model.employer_pension(100000) == pytest.approx(1320.90, abs=0.01)
    assert model.apprenticeship_levy(100000) == pytest.approx(425.00, abs=0.01)
    assert model.employer_nic(5000) == 0
    assert model.employer_nic(4000) == 0


def test_corporation_tax_uses_marginal_relief_not_a_flat_19_percent(model):
    # £50,000 or less -> straight small profits rate.
    assert model.corporation_tax(50000) == pytest.approx(9500.00, abs=0.01)
    # £250,000 and above -> main rate.
    assert model.corporation_tax(250000) == pytest.approx(62500.00, abs=0.01)
    # In between, the effective rate climbs to ~26.5%. Treating this band as
    # 19% is the classic error and understates tax by thousands.
    ct = model.corporation_tax(100000)
    assert ct == pytest.approx(0.25 * 100000 - 150000 * 0.015, abs=0.01)
    assert ct > 0.19 * 100000
    assert model.corporation_tax(0) == 0
    assert model.corporation_tax(-500) == 0


def test_dividend_tax_uses_the_2026_27_rates_and_the_500_allowance(model):
    # £45,305 of dividends on top of a £12,570 salary: the allowance is free,
    # the rest of the basic band at 10.75%, the remainder at 35.75%.
    tax = model.dividend_tax(45305.29, 12570)
    expected = 0 * 500 + 0.1075 * (37700 - 500) + 0.3575 * (45305.29 - 37700)
    assert tax == pytest.approx(expected, abs=0.01)
    assert model.dividend_tax(0, 12570) == 0
    assert model.dividend_tax(400, 12570) == 0  # inside the allowance


# ---------------------------------------------------------------------------
# The equivalence itself
# ---------------------------------------------------------------------------

def test_the_two_day_rates_at_220_days(pinned_model):
    rates = pinned_model.day_rates()
    assert rates["inside_ir35"]["day_rate_gbp"] == pytest.approx(532.71, abs=0.05)
    assert rates["outside_ir35"]["day_rate_gbp"] == pytest.approx(492.22, abs=0.05)
    # Inside IR35 must always need MORE than outside for the same take-home,
    # because the umbrella's employer NIC comes out of the rate. If this ever
    # inverts, the model is wrong, not the world.
    assert rates["inside_ir35"]["day_rate_gbp"] > rates["outside_ir35"]["day_rate_gbp"]


def test_both_thresholds_actually_deliver_the_target_take_home(model):
    """The brute-force check that the numbers mean what they claim: paying
    exactly the stated day rate must reproduce the target salary's take-home.
    Written relative to model.target_salary rather than a literal, so that it
    still means something against the example config on a fresh clone."""
    target = model.net_from_salary(model.target_salary)
    rates = model.day_rates()
    inside = model.inside_breakdown(rates["inside_ir35"]["day_rate_gbp"] * model.days)
    outside = model.outside_breakdown(rates["outside_ir35"]["day_rate_gbp"] * model.days)
    assert inside["net_pay_gbp"] == pytest.approx(target, abs=1.0)
    assert outside["net_pay_gbp"] == pytest.approx(target, abs=1.0)
    # ...and the headline salary inside IR35 is the target itself.
    assert inside["salary_gbp"] == pytest.approx(model.target_salary, abs=1.0)


def test_day_rate_scales_inversely_with_billable_days(pinned_model):
    # The whole reason 220 and not 260 is the default: fewer billable days
    # must mean a higher rate for the same annual money.
    at_220 = pinned_model.day_rates(days=220)["inside_ir35"]["day_rate_gbp"]
    at_260 = pinned_model.day_rates(days=260)["inside_ir35"]["day_rate_gbp"]
    assert at_220 == pytest.approx(532.71, abs=0.05)
    assert at_260 == pytest.approx(450.75, abs=0.05)
    assert at_220 / at_260 == pytest.approx(260 / 220, rel=1e-6)


def test_perm_equivalent_inverts_the_model_exactly(model):
    """perm_equivalent is what the board displays next to a day rate, so it
    has to survive a round trip through the tax model or it is just a
    plausible-looking number."""
    for rate in (200, 325, 375, 500, 600):
        for status in ("inside", "outside"):
            implied = model.perm_equivalent(rate, status)
            assert model.net_from_salary(implied) == pytest.approx(
                model.inside_breakdown(rate * model.days)["net_pay_gbp"]
                if status == "inside"
                else model.outside_breakdown(rate * model.days)["net_pay_gbp"],
                abs=0.01,
            )


def test_multiplying_the_target_salary_moves_the_rates_proportionally(loose_model):
    base = loose_model.day_rates()["inside_ir35"]["day_rate_gbp"]
    loose_model.target_salary = loose_model.target_salary * 2
    doubled = loose_model.day_rates()["inside_ir35"]["day_rate_gbp"]
    assert doubled > 1.9 * base


# ---------------------------------------------------------------------------
# Day-rate parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Rate: £500 per day", (500, 500)),
    ("£450/day", (450, 450)),
    ("£475pd", (475, 475)),
    ("£475 p/d", (475, 475)),
    ("Up to £600 a day", (600, 600)),
    ("day rate of £525", (525, 525)),
    ("£450 - £550 per day", (450, 550)),
    ("£450-£550/day", (450, 550)),
    ("circa £500 per day", (500, 500)),
    ("£1,000 per day", (1000, 1000)),
    ("Rate is £380.50 per day", (380.50, 380.50)),
])
def test_parses_the_day_rate_shapes_uk_ads_use(text, expected):
    got = cr.parse_day_rates(text)
    assert got is not None, f"failed to parse {text!r}"
    assert (got["min"], got["max"]) == pytest.approx(expected)


def test_uses_the_top_of_a_quoted_range():
    # The salary-floor logic works off the top of a band, for the same reason:
    # whether a role is worth applying to depends on what it can reach.
    got = cr.parse_day_rates("£400 - £480 per day, inside IR35")
    assert got["max"] == 480


@pytest.mark.parametrize("text", [
    "Permanent role, £100,000 - £110,000 per annum",
    "Salary up to £90,000",
    "Competitive salary, plus benefits",
    "£50 per day travel allowance",
    "Expenses: £40 per day subsistence",
    "Mileage £45 per day",
    "",
])
def test_does_not_invent_a_day_rate(text):
    """A bare figure or an expense line is not a rate. Guessing here is how
    a role gets misjudged in either direction."""
    assert cr.parse_day_rates(text) is None


def test_ignores_a_low_expense_line_but_keeps_the_real_rate():
    got = cr.parse_day_rates("Rate £450 per day plus £50 per day travel allowance")
    assert (got["min"], got["max"]) == (450, 450)


# ---------------------------------------------------------------------------
# IR35 detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Inside IR35", "inside"),
    # The spelling a real Adzuna advert used, with no space at all.
    ("6 Month contract | InsideIR35 | On site 2 days per week into London", "inside"),
    ("Outside IR35 determination", "outside"),
    ("OutsideIR35", "outside"),
    ("IR35 status: Outside", "outside"),
    ("IR35: Inside", "inside"),
    ("This role falls within scope of IR35", "inside"),
    ("The engagement is caught by IR35", "inside"),
    ("This is deemed employment", "inside"),
    ("Umbrella company only", "inside"),
    ("PAYE only, no Ltd companies", "inside"),
    ("Limited company contractors welcome", "outside"),
    ("Outside IR-35 status", "outside"),
    # Negations must flip, not be ignored: reading "not outside IR35" as
    # outside is the worst possible failure mode for this function.
    ("This role is not outside IR35", "inside"),
    ("This role is not inside IR35", "outside"),
    # An explicit statement beats an inference, because ads say both.
    ("Outside IR35. Paid via our umbrella partner.", "outside"),
    # Genuinely ambiguous -> unknown, for a human to read.
    ("Inside IR35 in some teams and outside IR35 in others", "unknown"),
    ("Contract role, competitive rate", "unknown"),
    ("", "unknown"),
])
def test_ir35_detection(text, expected):
    assert cr.detect_ir35(text)["status"] == expected


def test_ir35_evidence_is_reported_so_the_board_can_show_its_working():
    got = cr.detect_ir35("6 Month contract | InsideIR35 | London")
    assert got["status"] == "inside"
    assert got["evidence"]
    assert "insid" in got["evidence"].lower()


# ---------------------------------------------------------------------------
# The verdict, and what the pipeline does with it
# ---------------------------------------------------------------------------

def test_verdicts_for_the_cases_that_matter(model):
    inside_floor = model.day_rates()["inside_ir35"]["day_rate_gbp"]   # 375.21
    outside_floor = model.day_rates()["outside_ir35"]["day_rate_gbp"]  # 324.74

    # Clearly enough, either status.
    assert cr.classify_rate(500, 500, "inside", model)["verdict"] == "pass"
    assert cr.classify_rate(400, 400, "outside", model)["verdict"] == "pass"
    # Fails outside but clears inside -> a real fail, it is not enough money.
    assert cr.classify_rate(inside_floor + 1, inside_floor + 1, "outside", model)["verdict"] == "pass"
    assert cr.classify_rate(outside_floor - 10, outside_floor - 10, "inside", model)["verdict"] == "fail"
    # The interesting middle: only worth it if the role is genuinely outside.
    middle = (inside_floor + outside_floor) / 2
    review = cr.classify_rate(middle, middle, "unknown", model)
    assert review["verdict"] == "review"
    assert "OUTSIDE IR35" in review["reason"]
    # No rate at all is never a fail.
    assert cr.classify_rate(None, None, "inside", model)["verdict"] == "unknown"


def test_unknown_ir35_is_judged_against_the_stricter_threshold(model):
    """Default policy: assume the worse case. A rate that only works outside
    IR35 comes back as "review" rather than a pass."""
    assert model.unknown_is_inside is True
    middle = (model.day_rates()["inside_ir35"]["day_rate_gbp"]
              + model.day_rates()["outside_ir35"]["day_rate_gbp"]) / 2
    assert cr.classify_rate(middle, middle, "unknown", model)["verdict"] == "review"


def test_the_policy_knobs_in_contract_yaml_do_what_they_say(loose_model):
    middle = (loose_model.day_rates()["inside_ir35"]["day_rate_gbp"]
              + loose_model.day_rates()["outside_ir35"]["day_rate_gbp"]) / 2
    # Assuming outside IR35 makes the same rate a pass.
    loose_model.unknown_is_inside = False
    assert cr.classify_rate(middle, middle, "unknown", loose_model)["verdict"] == "pass"
    # Turning the floor off stops any rejection.
    loose_model.reject_below_threshold = False
    job = {"employment_type": "contract", "rate_verdict": "fail", "day_rate_max": 100, "rate_required": 375}
    assert cr.reject_reason(job, loose_model) is None


def test_only_a_contract_with_a_stated_low_rate_is_rejected(model):
    """The three ways a job escapes the day-rate floor."""
    low = {"url": "u1", "title": "Senior QA Engineer",
           "description": "Inside IR35. £200 per day.", "employment_type": "contract"}
    cr.apply_to_job(low, model)
    assert low["rate_verdict"] == "fail"
    assert cr.reject_reason(low, model) is not None

    # 1. A permanent role is never dropped on day rate.
    permanent = dict(low, employment_type="permanent")
    cr.apply_to_job(permanent, model)
    assert cr.reject_reason(permanent, model) is None

    # 2. A contract with NO stated rate is kept — most contract ads are like
    #    this, and dropping them would empty the board of the roles worth
    #    asking an agent about.
    silent = {"url": "u3", "title": "Senior QA Engineer",
              "description": "Contract role, competitive rate. Inside IR35.",
              "employment_type": "contract"}
    cr.apply_to_job(silent, model)
    assert silent["rate_verdict"] == "unknown"
    assert cr.reject_reason(silent, model) is None

    # 3. One that clears the outside floor but not the inside one is kept and
    #    flagged for a question, not rejected. The rate is derived from the two
    #    thresholds rather than hardcoded, so it stays exactly between them
    #    whatever target salary the gitignored contract.yaml holds.
    rates = model.day_rates()
    between = (rates["outside_ir35"]["day_rate_gbp"]
               + rates["inside_ir35"]["day_rate_gbp"]) / 2.0
    awkward = {"url": "u4", "title": "SDET",
               "description": f"Contract. Up to £{between:.0f} per day.",
               "employment_type": "contract"}
    cr.apply_to_job(awkward, model)
    assert awkward["rate_verdict"] == "review"
    assert cr.reject_reason(awkward, model) is None


def test_reject_reason_evaluates_a_job_that_never_went_through_apply_to_job(model, monkeypatch):
    """Guards the footgun where a caller runs passes_filters without the
    enrichment step and the money check silently does nothing."""
    monkeypatch.setattr(cr, "default_model", lambda: model)
    raw = {"url": "u5", "title": "QA Engineer", "employment_type": "contract",
           "description": "Inside IR35, £150 per day."}
    assert "rate_verdict" not in raw
    assert cr.reject_reason(raw) is not None


def test_apply_to_job_prefers_a_stated_rate_over_adzunas_annualised_one(model):
    job = {"url": "u6", "title": "SDET", "employment_type": "contract",
           "description": "Outside IR35. Rate: £520 per day.",
           "day_rate_min": 380.0, "day_rate_max": 400.0,
           "day_rate_source": "derived_from_advertised_salary"}
    cr.apply_to_job(job, model)
    assert job["day_rate_max"] == 520
    assert job["day_rate_source"] == "stated"


def test_implausible_derived_rates_are_discarded_not_judged(model):
    """Real run, 2026-09-21: internships, part-time FTCs and stipends reach
    Adzuna with annual figures of a few hundred to a couple of thousand
    pounds, which dividing by 260 turns into "£0.11/day" and "£7/day".
    Judging a posting against those would fail the floor and delete a real
    job — so they are discarded and the posting falls back to "no rate"."""
    for nasty in ({"day_rate_min": 0.115, "day_rate_max": 0.135},
                  {"day_rate_min": 7.0, "day_rate_max": 7.0},
                  {"day_rate_min": 0.19, "day_rate_max": 7.69}):
        job = {"url": "u7", "title": "Senior QA Engineer", "employment_type": "contract",
               "description": "Contract role, rate discussed on application.",
               "day_rate_source": "derived_from_advertised_salary", **nasty}
        cr.apply_to_job(job, model)
        assert job["day_rate_max"] is None, f"kept an implausible rate: {nasty}"
        assert job["rate_verdict"] == "unknown"
        assert cr.reject_reason(job, model) is None


def test_a_plausible_derived_rate_is_still_used(model):
    """The guard must not throw away the good derived values too — most
    contract ads quote no rate in their text, so this path matters."""
    job = {"url": "u8", "title": "Senior QA Engineer", "employment_type": "contract",
           "description": "Contract role, 6 months, London.",
           "day_rate_min": 400.0, "day_rate_max": 450.0,
           "day_rate_source": "derived_from_advertised_salary"}
    cr.apply_to_job(job, model)
    assert job["day_rate_max"] == 450.0
    assert job["rate_verdict"] == "pass"
    assert job["day_rate_source"] == "derived_from_advertised_salary"


def test_a_one_sided_derived_rate_does_not_crash(model):
    """Adzuna sometimes returns only one of salary_min/salary_max. A None on
    the other side used to reach the plausibility comparison and raise
    TypeError mid-run — in refilter, after it had already printed rescues."""
    for side in ({"day_rate_min": None, "day_rate_max": 450.0},
                 {"day_rate_min": 450.0, "day_rate_max": None}):
        job = {"url": "u8b", "title": "SDET", "employment_type": "contract",
               "description": "Contract.", "day_rate_source": "derived_from_advertised_salary",
               **side}
        cr.apply_to_job(job, model)
        assert job["day_rate_max"] == 450.0
        assert cr.reject_reason(job, model) is None


def test_a_rate_only_adzuna_estimated_is_flagged_but_never_dropped(model):
    """56 of the 61 postings the floor would have dropped on a real run were
    dropped on Adzuna's own estimate. That is a guess about the role, not a
    statement about it, so it is shown as "Below floor" and kept."""
    job = {"url": "u9", "title": "QA Engineer", "location": "London, UK",
           "employment_type": "contract", "description": "Contract. London.",
           "day_rate_min": 200.0, "day_rate_max": 200.0,
           "day_rate_source": "estimated_by_adzuna"}
    cr.apply_to_job(job, model)
    assert job["rate_verdict"] == "fail"
    assert cr.reject_reason(job, model) is None   # kept, but flagged

    # ...whereas the same rate from the advert itself IS acted on.
    stated = dict(job, day_rate_source="stated",
                  description="Contract. £200 per day. London.")
    cr.apply_to_job(stated, model)
    assert cr.reject_reason(stated, model) is not None

    # And from the advertiser's own annualised figure.
    derived = dict(job, day_rate_source="derived_from_advertised_salary")
    cr.apply_to_job(derived, model)
    assert cr.reject_reason(derived, model) is not None


# ---------------------------------------------------------------------------
# Storing the verdict, and healing rows stored before the columns existed
# ---------------------------------------------------------------------------

def test_contract_fields_survive_a_db_round_trip(tmp_path):
    from app import dedup

    db = str(tmp_path / "t.sqlite3")
    job = {"url": "https://ex.com/1", "company": "Monzo", "title": "Senior QA Engineer",
           "location": "London, UK", "posted_at": "2026-09-16T04:04:36Z",
           "description": "d" * 500, "source": "adzuna", "employment_type": "contract",
           "day_rate_min": 400.0, "day_rate_max": 450.0, "day_rate_source": "stated"}
    cr.apply_to_job(job, cr.default_model())

    with dedup.connect(db) as conn:
        dedup.save_details(conn, job, True)
        got = dedup.get_details_by_url(conn, job["url"])

    assert got["employment_type"] == "contract"
    assert got["day_rate_max"] == 450.0
    assert got["rate_verdict"] == "pass"
    assert got["perm_equivalent"] is not None
    assert got["ir35_status"] in ("inside", "outside", "unknown")


def test_refresh_fills_in_contract_fields_without_touching_passed_filters(tmp_path):
    """The self-healing path: dedup means a job stored before the contract
    columns existed is never re-inserted, so a refresh from the freshly
    fetched copy is the only way it ever gets an employment type."""
    from app import dedup

    db = str(tmp_path / "t.sqlite3")
    url = "https://ex.com/2"
    with dedup.connect(db) as conn:
        dedup.save_details(conn, {
            "url": url, "company": "Capco", "title": "Senior QA Engineer",
            "location": "London, UK", "posted_at": "2026-09-16T04:04:36Z",
            "description": "Contract. Inside IR35. £420 per day.", "source": "adzuna",
        }, True)

        # Before: every contract column NULL, because the row predates them.
        before = dedup.get_details_by_url(conn, url)
        assert before["employment_type"] is None
        assert before["day_rate_max"] is None
        assert before["passed_filters"] == 1

        fresh = {"url": url, "company": "Capco", "title": "Senior QA Engineer",
                 "location": "London, UK", "posted_at": "2026-09-16T04:04:36Z",
                 "description": "Contract. Inside IR35. £420 per day.", "source": "adzuna",
                 "employment_type": "contract"}
        cr.apply_to_job(fresh, cr.default_model())
        assert dedup.refresh_contract_fields(conn, fresh) is True
        # Idempotent: a second refresh of identical data writes nothing.
        assert dedup.refresh_contract_fields(conn, fresh) is False

        after = dedup.get_details_by_url(conn, url)
        assert after["employment_type"] == "contract"
        assert after["ir35_status"] == "inside"
        assert after["day_rate_max"] == 420.0
        assert after["rate_verdict"] == "pass"
        # The description and the pass/fail flag are both left alone.
        assert after["description"] == before["description"]
        assert after["passed_filters"] == 1


def test_refresh_never_downgrades_a_known_employment_type_to_unknown(tmp_path):
    """The ATS fetchers do not report an employment type at all, so a
    Greenhouse refresh of a job Adzuna already told us is contract must not
    erase that."""
    from app import dedup

    db = str(tmp_path / "t.sqlite3")
    url = "https://ex.com/3"
    with dedup.connect(db) as conn:
        job = {"url": url, "company": "Monzo", "title": "SDET", "location": "London, UK",
               "description": "Contract, inside IR35, £500 per day.",
               "employment_type": "contract"}
        cr.apply_to_job(job, cr.default_model())
        dedup.save_details(conn, job, True)

        ats_copy = {"url": url, "company": "Monzo", "title": "SDET", "location": "London, UK",
                    "description": "Contract, inside IR35, £500 per day.",
                    "employment_type": "unknown"}
        assert dedup.refresh_contract_fields(conn, ats_copy) is False
        assert dedup.get_details_by_url(conn, url)["employment_type"] == "contract"
