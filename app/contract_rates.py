"""Contract day rates: what a contract must pay to be worth the same as a
permanent salary, and how to judge a contract posting against that.

The equivalence is NOT "salary / 220 days". It depends on IR35:

  INSIDE IR35  You are taxed as an employee of the umbrella company, but the
               assignment rate the agency pays has to cover employer NIC,
               the apprenticeship levy, pension and the umbrella's margin
               BEFORE you are paid a salary at all. Matching the target
               salary therefore needs a rate well ABOVE salary/220.

  OUTSIDE IR35 You run a limited company. The company pays corporation tax,
               you take a small salary plus dividends. Employee NIC does not
               apply to dividends, so the same cash in hand needs a LOWER
               headline rate than the inside case.

Both are computed here from the rates in contract.yaml so the arithmetic is
auditable and correctable — see `print_report()` for the full breakdown.

The parsing half of this module (`parse_day_rates`, `detect_ir35`) is what
turns a raw posting into those inputs. Both are deliberately conservative:
returning "unknown" is always preferable to inventing a number, because a
wrong day rate silently changes whether Ed applies to a role.

Run `python -m app.contract_rates` to print the day rates and the workings.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sys

import yaml

from app import jd_text

CONTRACT_CONFIG_PATH = "contract.yaml"

# Shipped beside the real file, which is gitignored because it carries your own
# target salary. A fresh clone has no contract.yaml, so it has to have
# somewhere to fall back to or nothing here runs. See the header of
# contract.yaml.example.
CONTRACT_CONFIG_EXAMPLE_PATH = "contract.yaml.example"

# A UK contract day rate outside this range is not a day rate. The floor
# rejects the "£50 per day" travel-expense lines that appear in real ads;
# the ceiling rejects annual salaries that a malformed pattern might catch.
MIN_PLAUSIBLE_DAY_RATE = 150.0
MAX_PLAUSIBLE_DAY_RATE = 2500.0


def load_config(path: str = CONTRACT_CONFIG_PATH) -> dict:
    """Load the contract-role config from `path`.

    When the default path is absent — the state of a fresh clone, because
    contract.yaml holds a personal target salary and is deliberately not
    committed — fall back to the committed contract.yaml.example. An explicit
    path is honoured as given, so a missing --config still fails loudly rather
    than silently answering with someone else's numbers.
    """
    if path == CONTRACT_CONFIG_PATH and not os.path.exists(path):
        path = CONTRACT_CONFIG_EXAMPLE_PATH
    with open(path) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

class RateModel:
    """Resolves contract.yaml into the handful of numbers the arithmetic
    needs, and exposes the equivalence calculations. Every method is pure —
    no I/O, no globals — so the tests can build one from a dict."""

    def __init__(self, cfg: dict):
        r = cfg["rates"]
        self.tax_year = str(cfg.get("tax_year", "current"))
        self.target_salary = float(cfg["target_permanent_salary_gbp"])
        self.days = float(cfg["working_days_per_year"])

        self.personal_allowance = float(r["personal_allowance"])
        self.basic_rate_limit = float(r["basic_rate_limit"])
        self.higher_rate_limit = float(r["higher_rate_limit"])
        self.basic_rate = float(r["basic_rate"])
        self.higher_rate = float(r["higher_rate"])
        self.additional_rate = float(r["additional_rate"])
        self.taper_threshold = float(r["allowance_taper_threshold"])

        self.ee_nic_pt = float(r["employee_nic_primary_threshold"])
        self.ee_nic_uel = float(r["employee_nic_upper_earnings_limit"])
        self.ee_nic_main = float(r["employee_nic_main_rate"])
        self.ee_nic_upper = float(r["employee_nic_upper_rate"])

        self.er_nic_st = float(r["employer_nic_secondary_threshold"])
        self.er_nic_rate = float(r["employer_nic_rate"])

        self.levy_rate = float(r["apprenticeship_levy_rate"])
        self.levy_allowance = float(r["apprenticeship_levy_allowance"])

        self.div_allowance = float(r["dividend_allowance"])
        self.div_basic = float(r["dividend_basic_rate"])
        self.div_higher = float(r["dividend_higher_rate"])
        self.div_additional = float(r["dividend_additional_rate"])

        self.ct_small_rate = float(r["corporation_tax_small_profits_rate"])
        self.ct_main_rate = float(r["corporation_tax_main_rate"])
        self.ct_small_limit = float(r["corporation_tax_small_profits_limit"])
        self.ct_main_limit = float(r["corporation_tax_main_rate_limit"])
        self.ct_mr_fraction = float(r["corporation_tax_marginal_relief_fraction"])

        self.pension_lower = float(r["pension_lower_qualifying_earnings"])
        self.pension_upper = float(r["pension_upper_qualifying_earnings"])
        self.pension_rate = float(r["pension_employer_min_rate"])

        u = cfg.get("umbrella", {})
        self.umbrella_margin = float(u.get("margin_per_week_gbp", 0)) * float(u.get("weeks_per_year", 0))
        self.umbrella_levy = bool(u.get("apprenticeship_levy", False))
        self.umbrella_pension = bool(u.get("employer_pension", False))

        lc = cfg.get("limited_company", {})
        self.lc_salary = float(lc.get("salary_gbp", self.personal_allowance))
        self.lc_accountancy = float(lc.get("accountancy_fees_per_year_gbp", 0))
        self.lc_pension = float(lc.get("pension_from_profits_gbp", 0))

        s = cfg.get("search", {})
        self.accept_inside = bool(s.get("accept_inside_ir35", True))
        self.accept_outside = bool(s.get("accept_outside_ir35", True))
        self.unknown_is_inside = bool(s.get("unknown_ir35_is_inside", True))
        self.reject_below_threshold = bool(s.get("reject_below_threshold", True))
        self.reject_sources = set(s.get("reject_sources")
                                  or ["stated", "derived_from_advertised_salary"])
        self.adzuna_annualisation_days = float(s.get("adzuna_contract_annualisation_days", 260))

        # Band widths, in taxable-income terms (i.e. measured from the top of
        # the personal allowance). The basic and higher rate bands are fixed
        # WIDTHS, not fixed income points, which is what makes this correct
        # even once the allowance tapers away.
        self.basic_width = self.basic_rate_limit - self.personal_allowance
        self.higher_width = self.higher_rate_limit - self.personal_allowance

    # -- income tax / NIC ---------------------------------------------------

    def allowance_for(self, total_income: float) -> float:
        pa = self.personal_allowance
        if total_income > self.taper_threshold:
            pa = max(0.0, pa - (total_income - self.taper_threshold) / 2.0)
        return pa

    def income_tax(self, gross: float, pa: float | None = None) -> float:
        if gross <= 0:
            return 0.0
        pa = self.allowance_for(gross) if pa is None else pa
        taxable = max(0.0, gross - pa)
        basic = min(taxable, self.basic_width)
        higher = min(max(0.0, taxable - self.basic_width), self.higher_width)
        additional = max(0.0, taxable - self.basic_width - self.higher_width)
        return basic * self.basic_rate + higher * self.higher_rate + additional * self.additional_rate

    def employee_nic(self, gross: float) -> float:
        if gross <= self.ee_nic_pt:
            return 0.0
        main = min(gross, self.ee_nic_uel) - self.ee_nic_pt
        upper = max(0.0, gross - self.ee_nic_uel)
        return main * self.ee_nic_main + upper * self.ee_nic_upper

    def employer_nic(self, gross: float) -> float:
        return max(0.0, gross - self.er_nic_st) * self.er_nic_rate

    def employer_pension(self, gross: float) -> float:
        band = max(0.0, min(gross, self.pension_upper) - self.pension_lower)
        return band * self.pension_rate

    def apprenticeship_levy(self, gross: float) -> float:
        return max(0.0, gross - self.levy_allowance) * self.levy_rate

    def net_from_salary(self, gross: float) -> float:
        """Take-home pay from a PAYE salary, before any pension."""
        pa = self.allowance_for(gross)
        return gross - self.income_tax(gross, pa) - self.employee_nic(gross)

    # -- dividends / corporation tax ---------------------------------------

    def corporation_tax(self, profit: float) -> float:
        """19% small profits rate, 25% main rate, Marginal Relief in between.

        The relief formula is (U - A) x N/A x F with A = augmented profits.
        For a standalone company with no associated companies A == N, so it
        collapses to (250,000 - profit) x 3/200. Modelling this band as a
        flat 19% is a common and expensive mistake: at this revenue level
        the real effective rate is ~26.5%, which is thousands more."""
        if profit <= 0:
            return 0.0
        if profit <= self.ct_small_limit:
            return profit * self.ct_small_rate
        if profit >= self.ct_main_limit:
            return profit * self.ct_main_rate
        gross = profit * self.ct_main_rate
        relief = (self.ct_main_limit - profit) * self.ct_mr_fraction
        return max(gross - relief, profit * self.ct_small_rate)

    def dividend_tax(self, dividends: float, salary: float) -> float:
        """Dividends sit ON TOP of salary for band purposes, and the £500
        allowance is taxed at 0% but still consumes band space."""
        if dividends <= 0:
            return 0.0
        pa = self.allowance_for(salary + dividends)
        total_taxable = max(0.0, salary + dividends - pa)
        salary_taxable = max(0.0, salary - pa)
        div_taxable = max(0.0, total_taxable - salary_taxable)

        basic_left = max(0.0, self.basic_width - min(salary_taxable, self.basic_width))
        higher_used = max(0.0, min(salary_taxable, self.basic_width + self.higher_width) - self.basic_width)
        higher_left = max(0.0, self.higher_width - higher_used)

        remaining = div_taxable
        segments: list[tuple[float, float]] = []
        for space, rate in ((basic_left, self.div_basic), (higher_left, self.div_higher),
                            (float("inf"), self.div_additional)):
            take = min(remaining, space)
            if take > 0:
                segments.append((take, rate))
                remaining -= take
            if remaining <= 0:
                break

        allowance_left = min(self.div_allowance, div_taxable)
        tax = 0.0
        for take, rate in segments:
            zero_rated = min(take, allowance_left)
            allowance_left -= zero_rated
            tax += (take - zero_rated) * rate
        return tax

    # -- inside IR35: umbrella --------------------------------------------

    def inside_assignment_for_salary(self, salary: float) -> float:
        """The annual assignment value needed to pay `salary` through an
        umbrella. This is the grossed-up figure — salary plus everything the
        umbrella must withhold before paying it."""
        total = salary + self.employer_nic(salary) + self.umbrella_margin
        if self.umbrella_levy:
            total += self.apprenticeship_levy(salary)
        if self.umbrella_pension:
            total += self.employer_pension(salary)
        return total

    def inside_salary_for_assignment(self, assignment: float) -> float:
        """Inverse of the above, by bisection (the employer NIC inside makes
        it non-linear, though still monotonic)."""
        return _bisect(lambda s: self.inside_assignment_for_salary(s) - assignment, 0.0, max(assignment, 1.0))

    def inside_breakdown(self, assignment: float) -> dict:
        salary = self.inside_salary_for_assignment(assignment)
        levy = self.apprenticeship_levy(salary) if self.umbrella_levy else 0.0
        pension = self.employer_pension(salary) if self.umbrella_pension else 0.0
        er_nic = self.employer_nic(salary)
        return {
            "assignment_gbp": assignment,
            "salary_gbp": salary,
            "employer_nic_gbp": er_nic,
            "apprenticeship_levy_gbp": levy,
            "employer_pension_gbp": pension,
            "umbrella_margin_gbp": self.umbrella_margin,
            "employee_nic_gbp": self.employee_nic(salary),
            "income_tax_gbp": self.income_tax(salary),
            "net_pay_gbp": self.net_from_salary(salary),
            "on_costs_pct": (assignment - salary) / assignment * 100.0 if assignment else 0.0,
        }

    # -- outside IR35: limited company ------------------------------------

    def outside_breakdown(self, annual_value: float) -> dict:
        salary = min(self.lc_salary, max(0.0, annual_value))
        er_nic = self.employer_nic(salary)
        pension = self.lc_pension
        fees = self.lc_accountancy
        profit = annual_value - salary - er_nic - pension - fees
        ct = self.corporation_tax(profit)
        dividends = max(0.0, profit - ct)
        it = self.income_tax(salary)
        ee_nic = self.employee_nic(salary)
        div_tax = self.dividend_tax(dividends, salary)
        net = salary - it - ee_nic + dividends - div_tax
        return {
            "contract_value_gbp": annual_value,
            "salary_gbp": salary,
            "employer_nic_gbp": er_nic,
            "pension_gbp": pension,
            "accountancy_gbp": fees,
            "profit_before_tax_gbp": profit,
            "corporation_tax_gbp": ct,
            "dividends_gbp": dividends,
            "income_tax_gbp": it,
            "employee_nic_gbp": ee_nic,
            "dividend_tax_gbp": div_tax,
            "net_pay_gbp": net,
            "total_tax_and_costs_gbp": annual_value - net,
            "effective_tax_rate_pct": (annual_value - net) / annual_value * 100.0 if annual_value else 0.0,
        }

    def outside_value_for_net(self, target_net: float) -> float:
        return _bisect(lambda v: self.outside_breakdown(v)["net_pay_gbp"] - target_net,
                       0.0, max(target_net * 4.0, 1000.0))

    # -- the headline answers ---------------------------------------------

    def permanent_net(self, salary: float | None = None) -> float:
        return self.net_from_salary(self.target_salary if salary is None else salary)

    def salary_for_net(self, target_net: float) -> float:
        """The permanent salary whose take-home equals `target_net` — the
        'permanent equivalent' shown next to a contract rate."""
        return _bisect(lambda g: self.net_from_salary(g) - target_net, 0.0, max(target_net * 4.0, 1000.0))

    def day_rates(self, days: float | None = None, salary: float | None = None) -> dict:
        """The two thresholds, as £/day, for matching `salary`."""
        days = self.days if days is None else float(days)
        salary = self.target_salary if salary is None else float(salary)
        inside_assignment = self.inside_assignment_for_salary(salary)
        outside_value = self.outside_value_for_net(self.net_from_salary(salary))
        return {
            "days": days,
            "target_salary_gbp": salary,
            "inside_ir35": {
                "day_rate_gbp": inside_assignment / days,
                "annual_assignment_gbp": inside_assignment,
            },
            "outside_ir35": {
                "day_rate_gbp": outside_value / days,
                "annual_contract_value_gbp": outside_value,
            },
            "difference_gbp": (inside_assignment - outside_value) / days,
            "difference_pct": (inside_assignment / outside_value - 1.0) * 100.0,
        }

    def perm_equivalent(self, day_rate: float, ir35: str, days: float | None = None) -> float:
        """The permanent salary a given day rate is worth, for the status it
        is actually on. Inside IR35 the assignment value converts straight
        back to a gross salary; outside IR35 it converts to take-home first
        and then back to the salary that would produce the same take-home."""
        days = self.days if days is None else float(days)
        annual = day_rate * days
        if ir35 == "inside":
            return self.inside_salary_for_assignment(annual)
        return self.salary_for_net(self.outside_breakdown(annual)["net_pay_gbp"])


def _bisect(f, lo: float, hi: float, iters: int = 200) -> float:
    """Plain bisection on a monotonically increasing f. Used instead of an
    algebraic inversion because the employer-NIC and Marginal-Relief kinks
    make closed forms error-prone to write and easy to get subtly wrong."""
    flo = f(lo)
    if flo >= 0:
        return lo
    fhi = f(hi)
    while fhi < 0:                      # widen until the root is bracketed
        hi *= 2.0
        fhi = f(hi)
    for _ in range(iters):
        mid = (lo + hi) / 2.0
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# Parsing a posting
# ---------------------------------------------------------------------------

_MONEY = r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d{3,5}(?:\.\d{1,2})?)"
_DAY_UNIT = r"(?:per\s+day|/\s*day|a\s+day|per\s+diem|p/?d(?![\w/])|daily\s+rate|day\s*rate)"

_DAY_RANGE_RE = re.compile(rf"£\s*{_MONEY}\s*(?:-|–|—|to)\s*£?\s*{_MONEY}\s*{_DAY_UNIT}", re.I)
_DAY_SINGLE_RE = re.compile(rf"£\s*{_MONEY}\s*{_DAY_UNIT}", re.I)
_DAY_PREFIX_RE = re.compile(rf"{_DAY_UNIT}\s*(?:of|is|:)?\s*£?\s*{_MONEY}", re.I)

# "£250 per day" is a real pattern in UK ads, but so is "£40 per day towards
# travel". The plausibility floor handles most of it; this catches the
# remainder where the figure is large enough to look like a rate.
_EXPENSE_CONTEXT_RE = re.compile(
    r"(expense|allowance|mileage|parking|subsistence|travel|towards|reimburse|onboarding|equipment|per diem allowance)",
    re.I,
)

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _to_money(raw: str) -> float:
    return float((raw or "0").replace(",", ""))


def _plausible(value: float | None) -> bool:
    """None-safe on purpose: callers check a rate that may legitimately be
    absent (a one-sided range, or a posting whose rate was discarded), and a
    bare comparison would raise rather than answer."""
    if value is None:
        return False
    return MIN_PLAUSIBLE_DAY_RATE <= value <= MAX_PLAUSIBLE_DAY_RATE


def parse_day_rates(text: str) -> dict | None:
    """Extract the day rate from a posting.

    Returns {"min": float, "max": float, "evidence": str, "source": "stated"}
    or None. Ranges ("£450 - £550 per day") keep both ends, because whether a
    role is worth applying to often depends on the TOP of the band, not the
    bottom — the same logic as the permanent salary floor.

    Deliberately requires an explicit day unit next to the figure. A bare
    "£500" in a posting is far more likely to be a salary, a project budget or
    an equipment allowance, and guessing there is how a role gets misjudged.
    """
    if not text:
        return None
    flat = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", text))

    found: list[tuple[float, float, str]] = []
    for m in _DAY_RANGE_RE.finditer(flat):
        if _EXPENSE_CONTEXT_RE.search(flat[max(0, m.start() - 45):m.start()]):
            continue
        lo, hi = _to_money(m.group(1)), _to_money(m.group(2))
        if _plausible(lo) and _plausible(hi):
            found.append((min(lo, hi), max(lo, hi), m.group(0).strip()))
    for regex in (_DAY_SINGLE_RE, _DAY_PREFIX_RE):
        for m in regex.finditer(flat):
            if _EXPENSE_CONTEXT_RE.search(flat[max(0, m.start() - 45):m.start()]):
                continue
            v = _to_money(m.group(regex.groups))
            if _plausible(v):
                found.append((v, v, m.group(0).strip()))
    if not found:
        return None
    lo = min(f[0] for f in found)
    hi = max(f[1] for f in found)
    evidence = max(found, key=lambda f: f[1])[2]
    return {"min": lo, "max": hi, "evidence": evidence, "source": "stated"}


# Confidence tiers. An explicit "outside IR35" statement beats an inference
# from "umbrella company" wording, because ads routinely say both — e.g.
# "Outside IR35. Paid via our umbrella partner." Resolving conflicts by tier
# rather than by count is what keeps that from coming back as "unknown".
#
# The separator between the qualifier and "IR35" is [\s-]* and NOT \s+.
# Real ads write "InsideIR35" with no space at all — observed verbatim in a
# live Adzuna posting on 2026-09-21, where the whole IR35 sentence was
# "InsideIR35" — so requiring whitespace silently lost the status on exactly
# the ads that state it most bluntly. "IR-35" and "IR 35" are covered too.
_R = r"ir[\s\-]*35"
_NOT = r"not[\s\-]+"


def _ir35_patterns() -> list[tuple[str, str, int]]:
    outside = rf"outside[\s\-]*(?:of[\s\-]+)?(?:the[\s\-]+)?(?:scope[\s\-]+of[\s\-]+)?{_R}"
    inside = rf"inside[\s\-]*(?:of[\s\-]+)?(?:the[\s\-]+)?(?:scope[\s\-]+of[\s\-]+)?{_R}"
    return [
        # (regex, status, confidence)  — higher confidence wins
        (outside, "outside", 3),
        (rf"{_R}[\s\-]*(?:status|determination)?[\s\-]*[:\-–=][\s\-]*(?:is[\s\-]+)?outside", "outside", 3),
        (rf"outside[\s\-]*{_R}[\s\-]*(?:status|determination|role|contract|assignment|position)", "outside", 3),
        (rf"{_NOT}(?:caught|within|inside|subject[\s\-]+to|deemed|in[\s\-]+scope[\s\-]+of)[\s\-]*(?:by[\s\-]+|to[\s\-]+)?(?:the[\s\-]+)?(?:scope[\s\-]+of[\s\-]+)?{_R}", "outside", 2),
        (rf"outside[\s\-]*(?:the[\s\-]+)?(?:scope|remit)[\s\-]+of[\s\-]+{_R}", "outside", 3),
        (rf"{_R}[\s\-]*(?:status)?[\s\-]*[:\-–=][\s\-]*(?:is[\s\-]+)?no\b", "outside", 2),
        (r"limited[\s\-]+company[\s\-]+(?:contractors?[\s\-]+)?(?:welcome|acceptable|ok|considered|only|basis)", "outside", 1),
        (r"\bltd\.?[\s\-]*co(?:mpany)?[\s\-]+(?:welcome|ok|acceptable|contractors?)", "outside", 1),

        (inside, "inside", 3),
        (rf"{_R}[\s\-]*(?:status|determination)?[\s\-]*[:\-–=][\s\-]*(?:is[\s\-]+)?inside", "inside", 3),
        (rf"inside[\s\-]*{_R}[\s\-]*(?:status|determination|role|contract|assignment|position)", "inside", 3),
        (rf"{_NOT}outside[\s\-]*(?:of[\s\-]+)?(?:the[\s\-]+)?(?:scope[\s\-]+of[\s\-]+)?{_R}", "inside", 3),
        (rf"within[\s\-]*(?:the[\s\-]+)?scope[\s\-]+of[\s\-]+{_R}", "inside", 2),
        (rf"(?:caught|caught[\s\-]+by|subject[\s\-]+to)[\s\-]*(?:the[\s\-]+)?{_R}", "inside", 2),
        (r"deemed[\s\-]+(?:employment|employee|contract)", "inside", 2),
        (rf"{_R}[\s\-]*(?:status)?[\s\-]*[:\-–=][\s\-]*(?:is[\s\-]+)?yes\b", "inside", 2),
        (r"umbrella[\s\-]+(?:company[\s\-]+)?(?:only|basis|required|workers?|payroll|arrangement)", "inside", 1),
        (r"\bpaye[\s\-]+only\b", "inside", 1),
        (r"inside[\s\-]+of[\s\-]+scope", "inside", 1),
    ]


_IR35_COMPILED = [(re.compile(p, re.I), s, c) for p, s, c in _ir35_patterns()]

_NEGATION_RE = re.compile(r"\b(not|no|never|isn'?t|arent|aren'?t|neither)\b[^.;]{0,25}$", re.I)


def detect_ir35(text: str) -> dict:
    """Classify a posting as inside / outside / unknown IR35 status.

    Returns {"status": str, "evidence": str|None, "confidence": int}. The
    status is "unknown" whenever the posting conflicts at its highest
    confidence tier — an ad that says both "Inside IR35" and "Outside IR35"
    is one to read by hand, not one to guess at."""
    if not text:
        return {"status": "unknown", "evidence": None, "confidence": 0}
    flat = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", text))

    by_tier: dict[int, dict[str, str]] = {}
    for regex, status, confidence in _IR35_COMPILED:
        for m in regex.finditer(flat):
            # "not outside IR35" means inside — flip rather than ignore, or
            # the negation silently reads as the opposite of what it says.
            effective = status
            if _NEGATION_RE.search(flat[max(0, m.start() - 30):m.start()]):
                effective = "inside" if status == "outside" else "outside"
            by_tier.setdefault(confidence, {}).setdefault(effective, m.group(0).strip())

    for tier in sorted(by_tier, reverse=True):
        statuses = by_tier[tier]
        if len(statuses) == 1:
            status, evidence = next(iter(statuses.items()))
            return {"status": status, "evidence": evidence, "confidence": tier}
        return {"status": "unknown",
                "evidence": " / ".join(f"{s}: {e}" for s, e in sorted(statuses.items())),
                "confidence": tier}
    return {"status": "unknown", "evidence": None, "confidence": 0}


def classify_rate(day_rate_min: float | None, day_rate_max: float | None, ir35: str,
                  model: RateModel, days: float | None = None) -> dict:
    """Compare a posting's day rate against the equivalent-day-rate
    thresholds and return a verdict.

    pass    meets the threshold for its IR35 status, or — when the status is
            not stated — meets the stricter inside-IR35 one
    review  clears the outside threshold but not the inside one, and the
            advert does not say which applies. Worth one question to the
            agent, not an automatic no.
    fail    below both thresholds
    unknown no day rate stated
    """
    thresholds = model.day_rates(days=days)
    inside = thresholds["inside_ir35"]["day_rate_gbp"]
    outside = thresholds["outside_ir35"]["day_rate_gbp"]
    best = day_rate_max if day_rate_max else day_rate_min

    result = {
        "verdict": "unknown",
        "day_rate_min": day_rate_min,
        "day_rate_max": day_rate_max,
        "rate_source": None,
        "required_day_rate": None,
        "perm_equivalent": None,
        "assumed_ir35": None,
        "reason": "No day rate stated in the posting.",
    }
    if not best:
        return result

    if ir35 == "inside":
        required, assumed = inside, "inside"
    elif ir35 == "outside":
        required, assumed = outside, "outside"
    else:
        required, assumed = (inside, "inside") if model.unknown_is_inside else (outside, "outside")

    result["required_day_rate"] = required
    result["assumed_ir35"] = assumed
    result["perm_equivalent"] = model.perm_equivalent(best, assumed if ir35 == "unknown" else ir35, days)

    if best >= required:
        result["verdict"] = "pass"
        result["reason"] = f"£{best:,.0f}/day clears the £{required:,.0f}/day needed for {assumed} IR35."
        return result
    if ir35 == "unknown" and best >= outside:
        result["verdict"] = "review"
        result["reason"] = (f"£{best:,.0f}/day needs the role to be OUTSIDE IR35 "
                            f"(£{outside:,.0f}/day); inside it would need £{inside:,.0f}/day. "
                            f"IR35 status is not stated — confirm before applying.")
        return result
    result["verdict"] = "fail"
    result["reason"] = (f"£{best:,.0f}/day is below the £{required:,.0f}/day needed for "
                        f"{assumed} IR35.")
    return result


def evaluate_contract(job: dict, model: RateModel, days: float | None = None) -> dict:
    """Everything the pipeline needs to store about a contract posting, from
    a job dict with `title`, `description` and an optional employment type."""
    # advert_text, not title+description: a rate read off an Adzuna listing
    # page belongs to whichever advert happened to be listed above this one.
    # Real case: Capgemini's SAP role was given "£350 to £550 per day, stated"
    # from the line "NTT DATA London Web Automation Test Engineer From £350 to
    # £550 per day".
    text = jd_text.advert_text(job.get("title", ""), job.get("description", ""))
    ir35 = detect_ir35(text)
    rate = parse_day_rates(text)
    rates = {"min": None, "max": None, "source": None}
    if rate:
        rates = {"min": rate["min"], "max": rate["max"], "source": rate["source"]}
    elif job.get("day_rate_min") is not None or job.get("day_rate_max") is not None:
        # The fetcher derived a rate from an annualised salary figure (Adzuna
        # annualises contract pay at 260 days). Weaker than a stated rate, so
        # it is tagged differently and the board shows it as derived.
        #
        # The plausibility band has to be re-applied HERE, not just in
        # parse_day_rates. Measured on a real run: internships, part-time
        # FTCs and stipends arrive as annual figures of a few hundred to a
        # couple of thousand pounds, which /260 turns into "£0.11/day" and
        # "£7/day" — nonsense values that would otherwise fail the floor and
        # quietly delete real postings.
        lo, hi = job.get("day_rate_min"), job.get("day_rate_max")
        top = hi if hi is not None else lo
        if _plausible(top):
            rates = {"min": lo if _plausible(lo) else None, "max": top,
                     "source": job.get("day_rate_source")}
    verdict = classify_rate(rates["min"], rates["max"], ir35["status"], model, days)
    return {
        "employment_type": job.get("employment_type") or "unknown",
        "ir35_status": ir35["status"],
        "ir35_evidence": ir35["evidence"],
        "day_rate_min": rates["min"],
        "day_rate_max": rates["max"],
        "day_rate_source": rates["source"],
        "rate_verdict": verdict["verdict"],
        "rate_required": verdict["required_day_rate"],
        "perm_equivalent": verdict["perm_equivalent"],
        "rate_reason": verdict["reason"],
    }


CONTRACT_FIELDS = (
    "employment_type", "ir35_status", "ir35_evidence", "day_rate_min", "day_rate_max",
    "day_rate_source", "rate_verdict", "rate_required", "perm_equivalent", "rate_reason",
)


@functools.lru_cache(maxsize=1)
def default_model() -> RateModel:
    """The model built from contract.yaml, loaded once per process. Cached
    because the pipeline evaluates one job at a time and re-reading YAML per
    job would be pure waste."""
    return RateModel(load_config())


def apply_to_job(job: dict, model: RateModel | None = None) -> dict:
    """Evaluate `job` in place, merging the contract fields into it, and
    return it. Called once per job as it enters the pipeline, so everything
    downstream (filters, dedup, the board) sees the same verdict."""
    job.update(evaluate_contract(job, model or default_model()))
    return job


def reject_reason(job: dict, model: RateModel | None = None) -> str | None:
    """Why this posting is out of scope on money grounds, or None.

    Only ever fires on a CONTRACT posting whose day rate is below BOTH
    thresholds — the contract equivalent of the permanent salary floor — and
    only when that rate comes from a source trustworthy enough to drop a job
    over (see `search.reject_sources` in contract.yaml).

    Two deliberate escapes:

      * A posting with NO rate is kept. Most contract adverts quote none, and
        those are exactly the ones worth asking an agent about; dropping them
        would empty the board of contract work.
      * A rate that only Adzuna ESTIMATED is never acted on. It is a third
        party's guess at the role, not a statement about it, and discarding
        real postings over a guess is not a trade worth making.

    Permanent postings are never touched by this."""
    model = model or default_model()
    if not model.reject_below_threshold:
        return None
    if "rate_verdict" not in job:
        # Not yet through apply_to_job. Evaluate here rather than returning
        # None, so a caller that forgets the enrichment step cannot silently
        # skip the money check entirely.
        job = {**job, **evaluate_contract(job, model)}
    if job.get("employment_type") != "contract":
        return None
    if job.get("rate_verdict") != "fail":
        return None
    if job.get("day_rate_source") not in model.reject_sources:
        return None
    rate = job.get("day_rate_max") or job.get("day_rate_min")
    return (f"contract day rate £{rate:,.0f}/day is below the "
            f"£{job.get('rate_required') or 0:,.0f}/day needed for a £{model.target_salary:,.0f} equivalent")


# ---------------------------------------------------------------------------
# The printed report
# ---------------------------------------------------------------------------

def print_report(model: RateModel, days_list: list[float] | None = None, out=sys.stdout) -> None:
    w = out.write
    days_list = days_list or [200, 210, 220, 230, 240, 260]
    salary = model.target_salary

    w("\n" + "=" * 78 + "\n")
    w(f"CONTRACT DAY RATES equivalent to a £{salary:,.0f} permanent salary\n")
    w(f"UK tax year {model.tax_year}  |  target take-home £{model.permanent_net():,.2f}/yr\n")
    w("=" * 78 + "\n")

    w("\nWHY THE TWO NUMBERS DIFFER\n")
    w(f"  A £{salary:,.0f} salary costs the employer £{salary + model.employer_nic(salary) + model.employer_pension(salary):,.0f} "
      f"once employer NIC and pension are added.\n")
    w("  Inside IR35 the umbrella is the employer, so all of that comes out of YOUR rate first.\n")
    w("  Outside IR35 you are the business: corporation tax and dividends replace employee NIC,\n")
    w("  which is cheaper for the same take-home — so the headline rate can be lower.\n")

    w("\n" + "-" * 78 + "\n")
    w(f"INSIDE IR35 (umbrella) — to pay yourself a £{salary:,.0f} salary\n")
    w("-" * 78 + "\n")
    b = model.inside_breakdown(model.inside_assignment_for_salary(salary))
    for label, key in (("Gross salary you receive", "salary_gbp"),
                       ("Employer National Insurance (15% over £5,000)", "employer_nic_gbp"),
                       ("Apprenticeship Levy (0.5% over £15,000)", "apprenticeship_levy_gbp"),
                       ("Employer pension (3% of qualifying earnings)", "employer_pension_gbp"),
                       ("Umbrella margin", "umbrella_margin_gbp")):
        w(f"  {label:<48} £{b[key]:>12,.2f}\n")
    w(f"  {'= ASSIGNMENT VALUE REQUIRED':<48} £{b['assignment_gbp']:>12,.2f}\n")
    w(f"\n  On-costs above salary: £{b['assignment_gbp'] - b['salary_gbp']:,.2f} "
      f"({b['on_costs_pct']:.1f}%)\n")
    w(f"  You then pay income tax £{b['income_tax_gbp']:,.2f} and employee NIC "
      f"£{b['employee_nic_gbp']:,.2f}, leaving £{b['net_pay_gbp']:,.2f} in hand.\n")

    w("\n" + "-" * 78 + "\n")
    w("OUTSIDE IR35 (limited company) — to match that same take-home\n")
    w("-" * 78 + "\n")
    target_net = model.net_from_salary(salary)
    value = model.outside_value_for_net(target_net)
    o = model.outside_breakdown(value)
    for label, key in (("Contract value (ex VAT)", "contract_value_gbp"),
                       ("Less your salary", "salary_gbp"),
                       ("Less employer NIC on that salary", "employer_nic_gbp"),
                       ("Less accountancy and running costs", "accountancy_gbp"),
                       ("= Profit before tax", "profit_before_tax_gbp")):
        w(f"  {label:<48} £{o[key]:>12,.2f}\n")
    w(f"  {'Less corporation tax':<48} £{o['corporation_tax_gbp']:>12,.2f}\n")
    w(f"  {'= Dividends available':<48} £{o['dividends_gbp']:>12,.2f}\n")
    w(f"\n  You then pay income tax £{o['income_tax_gbp']:,.2f}, employee NIC "
      f"£{o['employee_nic_gbp']:,.2f} and dividend tax £{o['dividend_tax_gbp']:,.2f},\n")
    w(f"  leaving £{o['net_pay_gbp']:,.2f} in hand "
      f"(effective tax + costs {o['effective_tax_rate_pct']:.1f}%).\n")
    if model.lc_pension:
        w(f"  Plus £{o['pension_gbp']:,.2f} into your pension from company profit.\n")
    else:
        w("  (No pension contribution assumed — see limited_company.pension_from_profits_gbp.)\n")

    w("\n" + "=" * 78 + "\n")
    w("THE THRESHOLDS\n")
    w("=" * 78 + "\n")
    w(f"  Billable days per year is the biggest lever. {model.days:.0f} = ~6 weeks off\n")
    w("  plus a gap between contracts; 260 = every working day, no holiday.\n\n")
    w(f"  {'days/yr':>8}  {'INSIDE IR35':>14}  {'OUTSIDE IR35':>14}  {'difference':>12}\n")
    w(f"  {'-'*8}  {'-'*14}  {'-'*14}  {'-'*12}\n")
    for d in days_list:
        t = model.day_rates(days=d)
        marker = "   <-- default" if abs(d - model.days) < 1e-9 else ""
        w(f"  {d:>8.0f}  £{t['inside_ir35']['day_rate_gbp']:>12,.0f}/d  "
          f"£{t['outside_ir35']['day_rate_gbp']:>12,.0f}/d  "
          f"{t['difference_pct']:>11.1f}%{marker}\n")

    default = model.day_rates()
    w(f"\n  AT THE DEFAULT {model.days:.0f} DAYS:\n")
    w(f"    inside IR35   £{default['inside_ir35']['day_rate_gbp']:,.0f}/day   "
      f"(£{default['inside_ir35']['annual_assignment_gbp']:,.0f}/yr assignment)\n")
    w(f"    outside IR35  £{default['outside_ir35']['day_rate_gbp']:,.0f}/day   "
      f"(£{default['outside_ir35']['annual_contract_value_gbp']:,.0f}/yr contract value)\n")
    w(f"    the inside role must pay {default['difference_pct']:.1f}% more "
      f"(£{default['difference_gbp']:,.0f}/day) for the same money\n")

    w("\n  RULE OF THUMB to sanity-check those against any advert you see:\n")
    for d in (model.days,):
        t = model.day_rates(days=d)
        w(f"    inside  = salary x {(t['inside_ir35']['annual_assignment_gbp']/salary):.4f} / {d:.0f} days\n")
        w(f"    outside = salary x {(t['outside_ir35']['annual_contract_value_gbp']/salary):.4f} / {d:.0f} days\n")

    if not model.lc_pension:
        # The headline outside figure matches cash in hand only. A permanent
        # package at this salary also carries 3% employer pension, so a true
        # like-for-like has to fund that too — but from company profit it
        # costs no NIC and is corporation-tax deductible, so it is cheap.
        matched = RateModel.__new__(RateModel)
        matched.__dict__.update(model.__dict__)
        matched.lc_pension = model.employer_pension(salary)
        w(f"\n  If you also want to match the 3% employer pension a permanent\n")
        w(f"  package carries (£{model.employer_pension(salary):,.2f}), funded from company profit\n")
        w(f"  where it attracts no NIC and is corporation-tax deductible, the\n")
        w(f"  outside figure rises only to £{matched.day_rates()['outside_ir35']['day_rate_gbp']:,.0f}/day.\n")

    w("\n  WHAT A RATE IS WORTH (permanent-salary equivalent, "
      f"{model.days:.0f} days):\n")
    for rate in (250, 300, 325, 350, 375, 400, 450, 500, 600):
        i = model.perm_equivalent(rate, "inside")
        o_ = model.perm_equivalent(rate, "outside")
        flag = ""
        if rate >= default["inside_ir35"]["day_rate_gbp"]:
            flag = "  both OK"
        elif rate >= default["outside_ir35"]["day_rate_gbp"]:
            flag = "  outside only"
        else:
            flag = "  below both"
        w(f"    £{rate:>3}/day  ->  inside £{i:>9,.0f}   outside £{o_:>9,.0f}{flag}\n")

    w("\n  NOTE: VAT is excluded throughout. At this revenue a company is\n")
    w("  below the VAT registration threshold anyway, and above it VAT is\n")
    w("  charged to the client and reclaimed on inputs, so it is close to\n")
    w("  neutral for a contractor.\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the contract day rates equivalent to the permanent "
                    "salary floor, for inside and outside IR35.")
    parser.add_argument("--config", default=CONTRACT_CONFIG_PATH)
    parser.add_argument("--days", type=float, action="append",
                        help="billable days/year to report (repeatable; default prints a range)")
    parser.add_argument("--salary", type=float, help="override the target permanent salary")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.salary:
        cfg["target_permanent_salary_gbp"] = args.salary
    model = RateModel(cfg)

    if args.json:
        default = model.day_rates()
        print(json.dumps({
            "tax_year": model.tax_year,
            "target_salary_gbp": model.target_salary,
            "default_days": model.days,
            "inside_ir35_day_rate_gbp": round(default["inside_ir35"]["day_rate_gbp"], 2),
            "outside_ir35_day_rate_gbp": round(default["outside_ir35"]["day_rate_gbp"], 2),
            "table": [{"days": d,
                       "inside": round(model.day_rates(days=d)["inside_ir35"]["day_rate_gbp"], 2),
                       "outside": round(model.day_rates(days=d)["outside_ir35"]["day_rate_gbp"], 2)}
                      for d in (args.days or [200, 210, 220, 230, 240, 260])],
        }, indent=2))
        return 0

    print_report(model, days_list=args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
