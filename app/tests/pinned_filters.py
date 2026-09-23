"""A pinned, test-only filter configuration — used by BOTH run paths.

WHY THIS EXISTS
---------------
app/tests/test_ats_clients.py and app/tests/test_aggregator_clients.py use
filters.py as the GATE that decides whether a posting is worth an extra
detail-fetch request. That is the behaviour those tests are actually about
("don't turn a company with hundreds of postings into hundreds of requests"),
and their fixtures are written against the filter configuration the repo
originally shipped with (Canada/Alberta, "software engineer").

The problem: README.md tells every user to edit filters.yaml for their own
market *before their first run* — "this is the #1 reason a first run returns
nothing". So those tests were silently coupled to one user's config, and
following the README broke them. That is exactly what happened when
filters.yaml was retuned to London/UK + SDET roles.

So the gate is pinned here to an explicit, test-only configuration, and the
client tests can keep using whatever fixtures make their point.

TWO RUN PATHS, ONE CONFIG — both are in use in this repo:
  * `python -m pytest app/tests`  -> conftest.py's autouse fixture installs
    this via monkeypatch, then restores it after each test.
  * `python -m app.tests.test_aggregator_clients` -> each test module's
    __main__ block calls apply() directly (there is no pytest, so there is no
    conftest and no monkeypatch teardown).
Covering only the pytest path is an easy half-fix to make: it looks green
under pytest while `python -m app.tests.<module>` fails on the same
assertions. Both paths are checked in the module docstrings.

test_pipeline.py deliberately does NOT use this: it is the test OF the filter
policy, so it must exercise the real filters.yaml.
"""
import re

from app import filters

# A deliberately arbitrary config — it does not describe anyone's real search,
# it just makes the fixtures in the client tests "promising" or not.
_PRIORITY = ["software engineer", "backend engineer", "full stack"]
_ALLOW = ["engineer", "engineering", "developer"]
_EXCLUDE = ["marketing", "nurse", "recruiter", "sales engineer", "compliance"]
_LOCATION = [r"\bcanada\b", r"\balberta\b", r",\s*ab\b", r"\bedmonton\b", r"\bcalgary\b"]

# Language tiers and clearance, pinned for the same reason as everything else
# here. Deliberately NOT the real config: the real one now treats Python and
# Playwright as dealbreakers and drops postings requiring held clearance, and
# the client-test fixtures are about fetch gating, not about Ed's market. A
# fixture that happens to mention Python must not start failing to fetch
# because filters.yaml changed for an unrelated reason.
_PRIORITY_NAMED = {"java": r"\bjava\b", "ruby": r"\bruby\b"}
_SECONDARY_NAMED = {"javascript": r"\bjavascript\b", "node": r"\bnode(?:\.?js)?\b"}
_DEALBREAKER_NAMED = {"golang": r"\bgolang\b", "python": r"\bpython\b", "react": r"\breact\b"}
_LANGUAGE_TIERS = {"priority": 3, "secondary": 2, "none": 1, "mismatch": 0}
# No pattern ever matches, so the clearance check is effectively off under the
# pinned config. Use a regex that cannot match rather than an empty list: an
# empty list is the shape filters._compile_named refuses, on purpose.
_CLEARANCE_NONE = {"nomatch": r"(?!)"}


def _keyword_alternation(keywords: list[str]) -> re.Pattern:
    """Mirrors filters._compile_keyword_alternation so the pinned config is
    compiled exactly the way the real one is."""
    return re.compile("(" + "|".join(re.escape(k) for k in keywords) + ")", re.IGNORECASE)


def _named(patterns: dict) -> list[tuple[str, re.Pattern]]:
    """Mirrors filters._compile_named."""
    return [(name, re.compile(p, re.IGNORECASE)) for name, p in patterns.items()]


def compiled() -> dict:
    """The filters.py module attributes this config replaces.

    Monkeypatching the COMPILED patterns (rather than re-loading filters.yaml)
    is what makes this work for ats_clients/aggregator_clients, which call
    filters.title_is_relevant()/location_is_allowed() internally.

    Both the flat pattern lists and the NAMED lists they are derived from are
    patched: language_fit()/clearance_check() read the named ones for
    evidence, so pinning only the flat ones would leave the evidence paths
    running against the real filters.yaml."""
    priority_named = _named(_PRIORITY_NAMED)
    secondary_named = _named(_SECONDARY_NAMED)
    dealbreaker_named = _named(_DEALBREAKER_NAMED)
    return {
        "_priority_re": _keyword_alternation(_PRIORITY),
        "_title_allow_re": _keyword_alternation(_ALLOW),
        "_exclusion_re": _keyword_alternation(_EXCLUDE),
        "_location_res": [re.compile(p, re.I) for p in _LOCATION],
        "_dealbreaker_res": [p for _, p in dealbreaker_named],
        "_core_res": [p for _, p in priority_named] + [p for _, p in secondary_named],
        "STACK_PRIORITY_NAMED": priority_named,
        "STACK_SECONDARY_NAMED": secondary_named,
        "STACK_DEALBREAKER_NAMED": dealbreaker_named,
        "LANGUAGE_TIERS": _LANGUAGE_TIERS,
        "CLEARANCE_BLOCKED_NAMED": _named(_CLEARANCE_NONE),
        "CLEARANCE_DOWNGRADE_NAMED": _named(_CLEARANCE_NONE),
        "CLEARANCE_MENTION_NAMED": _named(_CLEARANCE_NONE),
        "CLEARANCE_REJECT_BLOCKED": False,
    }


def apply() -> None:
    """Install the pinned config on the filters module, in place.

    For script runs, which have no monkeypatch to restore afterwards — call it
    once at the top of a test module's __main__ block, before running tests."""
    for attr, value in compiled().items():
        setattr(filters, attr, value)
