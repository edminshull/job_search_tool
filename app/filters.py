"""Deterministic pre-filters: title allowlist + exclusion keywords +
location allowlist + language fit + security clearance.

These run BEFORE any AI call — the whole point is to keep the AI-scored
volume small and cheap. The actual keyword/pattern lists live in
`filters.yaml` at the repo root (loaded below) so they can be edited
without touching code.

Two of these are new (2026-09) and answer different questions than the title
and location rules do:

  LANGUAGE FIT  Which language is this role actually in? Java and Ruby are
    the priority, JavaScript/Node are acceptable, and a handful of others
    (C#/.NET, Python, Go...) are dealbreakers — but a dealbreaker is only
    acted on when NO priority or secondary language is named anywhere in the
    posting, because "Java and Python" is a Java role. The tier is stored, so
    the board can rank by it and the AI prompt can be told about it.

  CLEARANCE  Does this posting require clearance Ed does not hold? The
    distinction that matters is "must already hold active SC" (he cannot
    apply) versus "must be eligible for SC" or "BPSS" (routine for the
    successful candidate). Only the first is dropped; the second is flagged.

Design note (2026-08-11 rewrite): an EXCLUSION-only approach doesn't scale
to a company like Affirm that posts hundreds of non-engineering roles
(Analyst, Compliance, Marketing, Customer Success, Talent Brand, Sales
Development...) — you can't blacklist your way out of that. So title
filtering is allowlist-first: the title must look like an engineering
role AT ALL before we even consider it, then a smaller exclusion list
catches engineering-adjacent titles that aren't a fit (sales engineer,
hardware/mechanical engineer, etc).
"""
import re

import yaml

from app import contract_rates
from app import jd_text

FILTERS_CONFIG_PATH = "filters.yaml"


def _load_config(path: str = FILTERS_CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


_config = _load_config()

PRIORITY_TITLE_KEYWORDS = _config["priority_title_keywords"]
TITLE_ALLOW_KEYWORDS = _config["title_allow_keywords"]
EXCLUSION_KEYWORDS = _config["exclusion_keywords"]
LOCATION_ALLOW_PATTERNS = _config["location_allow_patterns"]
TRUNCATED_DESCRIPTION_MIN_CHARS = _config["truncated_description_min_chars"]

LANGUAGE_TIERS = _config["language_tiers"]
CLEARANCE_REJECT_BLOCKED = bool(_config["clearance"].get("reject_blocked", True))


def _compile_keyword_alternation(keywords: list[str], config_key: str) -> re.Pattern:
    # An empty keyword list joins to "" and compiles to "()", which matches
    # the empty string at every position — .search() would then match ANY
    # title, silently turning an allowlist into "allow everything" and an
    # exclusion list into "exclude everything". Fail loudly instead.
    if not keywords:
        raise ValueError(
            f"filters.yaml's '{config_key}' is empty — this would silently "
            "match every title instead of none. Add at least one keyword."
        )
    return re.compile("(" + "|".join(re.escape(k) for k in keywords) + ")", re.IGNORECASE)


def _compile_named(patterns: dict, config_key: str) -> list[tuple[str, re.Pattern]]:
    """Compile a {name: regex} mapping into [(name, compiled)], preserving the
    name so a match can be REPORTED as well as acted on.

    Keeping the name is the whole reason these tiers are mappings rather than
    plain lists: "this is a Python shop" is only actionable if the board and
    the prompt can also say which words led there."""
    if not patterns:
        raise ValueError(
            f"filters.yaml's '{config_key}' is empty — an empty pattern list "
            "silently matches nothing (or, for an alternation, everything). "
            "Add at least one entry."
        )
    out = []
    for name, pattern in patterns.items():
        try:
            out.append((name, re.compile(pattern, re.IGNORECASE)))
        except re.error as exc:
            raise ValueError(f"filters.yaml's {config_key}.{name} is not a valid "
                             f"regex ({pattern!r}): {exc}") from None
    return out


_priority_re = _compile_keyword_alternation(PRIORITY_TITLE_KEYWORDS, "priority_title_keywords")
_title_allow_re = _compile_keyword_alternation(TITLE_ALLOW_KEYWORDS, "title_allow_keywords")
_exclusion_re = _compile_keyword_alternation(EXCLUSION_KEYWORDS, "exclusion_keywords")
_location_res = [re.compile(p, re.IGNORECASE) for p in LOCATION_ALLOW_PATTERNS]

STACK_PRIORITY_NAMED = _compile_named(_config["stack_priority"], "stack_priority")
STACK_SECONDARY_NAMED = _compile_named(_config["stack_secondary"], "stack_secondary")
STACK_DEALBREAKER_NAMED = _compile_named(_config["stack_dealbreakers"], "stack_dealbreakers")

CLEARANCE_BLOCKED_NAMED = _compile_named(_config["clearance"]["blocked_patterns"],
                                         "clearance.blocked_patterns")
CLEARANCE_DOWNGRADE_NAMED = _compile_named(_config["clearance"]["downgrade_patterns"],
                                           "clearance.downgrade_patterns")
CLEARANCE_MENTION_NAMED = _compile_named(_config["clearance"]["mention_patterns"],
                                         "clearance.mention_patterns")

# Flat pattern lists, derived from the named ones above. Kept because they are
# the interface app/tests/pinned_filters.py monkeypatches to install its
# test-only config; the named lists are what provide the evidence, so both are
# pinned there together.
_dealbreaker_res = [p for _, p in STACK_DEALBREAKER_NAMED]
_core_res = [p for _, p in STACK_PRIORITY_NAMED] + [p for _, p in STACK_SECONDARY_NAMED]

_html_tag_re = re.compile(r"<[^>]+>")


def strip_html(html_or_text: str) -> str:
    """Cheap HTML-to-text: good enough for keyword matching, not for display."""
    if not html_or_text:
        return ""
    return _html_tag_re.sub(" ", html_or_text)


def looks_truncated(description: str) -> bool:
    """True if `description` looks like a short aggregator teaser rather
    than a full job posting — either it visibly cuts off mid-sentence, or
    it's just too short to contain real requirements/responsibilities.

    Shared by aggregator_clients.py (decide whether it's worth the extra
    request to fetch a full JD) and ai_evaluate.py (fall back to telling
    the model the description is partial, for the cases a full-JD fetch
    still couldn't recover — blocked site, dead link, genuinely short
    posting)."""
    text = strip_html(description or "").strip()
    if not text:
        return True
    if text.endswith("…") or text.endswith("...") or text.rstrip().endswith(".."):
        return True
    return len(text) < TRUNCATED_DESCRIPTION_MIN_CHARS


def title_is_relevant(title: str) -> bool:
    title = title or ""
    if _priority_re.search(title):
        # Unambiguous engineering title — e.g. "Software Engineer, Automated
        # Marketing" — skip the exclusion list entirely so a word like
        # "marketing" elsewhere in the title can't veto it.
        return True
    if not _title_allow_re.search(title):
        return False
    if _exclusion_re.search(title):
        return False
    return True


def location_is_allowed(location: str) -> bool:
    loc = location or ""
    return any(p.search(loc) for p in _location_res)


def jd_stack_mismatch(description: str) -> bool:
    """True if the JD looks like a dealbreaker-language role with no
    mention of your own core stack. Only meaningful when `description` is
    non-empty — callers should treat an empty description as "unknown,
    don't reject on stack alone"."""
    text = strip_html(description)
    if not text:
        return False
    has_dealbreaker = any(p.search(text) for p in _dealbreaker_res)
    has_core = any(p.search(text) for p in _core_res)
    return has_dealbreaker and not has_core


def language_fit(job: dict) -> dict:
    """Which language this role is actually in, as a tier plus evidence.

    Returns language_tier / language_rank / language_hits. This is the
    DETERMINISTIC half of "Java and Ruby are the priority" — it does not
    guess at proficiency, it records which languages the advert names, so the
    board can rank by it and the AI prompt can be told it rather than having
    to re-derive it from prose.

    `none` is deliberately NOT treated as a mismatch. 40 of the 92 postings
    on the live board name no language at all (short aggregator snippets,
    or JDs that talk about "automation frameworks" generically), and
    rejecting those would throw away real roles on the absence of evidence.
    They rank below priority/secondary but above mismatch.
    """
    # jd_text.advert_text, not the raw description: on a listing page the body
    # belongs to other adverts, and reading their languages off it produced
    # four unrelated postings with the identical hit list "java, javascript,
    # c#, python, playwright" — and one false rejection.
    text = strip_html(jd_text.advert_text(job.get("title", ""), job.get("description", "")))
    priority = [name for name, pat in STACK_PRIORITY_NAMED if pat.search(text)]
    secondary = [name for name, pat in STACK_SECONDARY_NAMED if pat.search(text)]
    dealbreakers = [name for name, pat in STACK_DEALBREAKER_NAMED if pat.search(text)]

    if priority:
        tier = "priority"
    elif secondary:
        tier = "secondary"
    elif dealbreakers:
        tier = "mismatch"
    else:
        tier = "none"

    hits = priority + secondary + dealbreakers
    return {
        "language_tier": tier,
        "language_rank": LANGUAGE_TIERS.get(tier, 0),
        "language_hits": ", ".join(hits) if hits else None,
    }


# A "sentence" for clearance purposes. Full stops are too aggressive on their
# own (they appear in "£458.24" and "e.g."), but truncating early only makes
# the check MORE conservative — it can miss a downgrade and leave a posting
# blocked, never the reverse.
_CLAUSE_BREAK_RE = re.compile(r"[.;\n]")


def _clause_around(text: str, start: int, end: int, before: int = 110, after: int = 70) -> str:
    """The clause containing text[start:end], so a downgrade word is only
    honoured when it is part of the SAME requirement. Without this, a posting
    saying "Active SC clearance required" in one bullet and mentioning BPSS in
    an unrelated one would be wrongly downgraded to obtainable."""
    lead = text[max(0, start - before):start]
    cut = max(lead.rfind("."), lead.rfind(";"), lead.rfind("\n"))
    if cut != -1:
        lead = lead[cut + 1:]
    tail = text[end:end + after]
    match = _CLAUSE_BREAK_RE.search(tail)
    if match:
        tail = tail[:match.start()]
    return f"{lead} {tail}"


def clearance_check(job: dict) -> dict:
    """Whether the posting needs clearance Ed already holds, cannot-obtain
    clearance, or is silent.

    blocked   the advert requires him to ALREADY hold it ("Active SC
              clearance required", "SC Cleared", "must hold current SC") —
              he cannot apply, and passes_content_filters drops it
    possible  clearance is mentioned but as something obtainable or merely
              desirable ("must be eligible for SC", "BPSS to start",
              "would be a benefit", "SC-cleared / Non-SC") — kept and FLAGGED
    none      nothing to do with clearance

    Read against real adverts, not invented: see the clearance block in
    filters.yaml for the phrases each tier was derived from."""
    # Same reasoning as language_fit: a listing page's body is not this
    # advert's text. The title still is, which is what keeps "Test Engineer
    # (SC)" and "SC Cleared Lead Automation Engineer" blocked.
    text = strip_html(jd_text.advert_text(job.get("title", ""), job.get("description", "")))
    flat = re.sub(r"\s+", " ", text).strip()
    if not flat:
        return {"clearance_status": "none", "clearance_evidence": None}

    # The gate. A posting that never mentions clearance is "none" no matter
    # what else it says — otherwise generic boilerplate ("ability to work
    # independently", "bonus scheme") would classify half the board as
    # clearance-related. Measured: 85 postings, mostly Freelance Copywriter
    # and Remote Office Assistant, before this gate existed.
    mentions = [name for name, pat in CLEARANCE_MENTION_NAMED if pat.search(flat)]
    if not mentions:
        return {"clearance_status": "none", "clearance_evidence": None}

    downgrades = [name for name, pat in CLEARANCE_DOWNGRADE_NAMED if pat.search(flat)]

    hard: list[tuple[str, str]] = []
    soft: list[tuple[str, str]] = []
    for name, pattern in CLEARANCE_BLOCKED_NAMED:
        for m in pattern.finditer(flat):
            # Only a downgrade in the SAME clause rescues this match.
            clause = _clause_around(flat, m.start(), m.end())
            if any(pat.search(clause) for _, pat in CLEARANCE_DOWNGRADE_NAMED):
                soft.append((name, m.group(0).strip()))
            else:
                hard.append((name, m.group(0).strip()))

    if hard:
        return {"clearance_status": "blocked",
                "clearance_evidence": f"{hard[0][0]}: “{hard[0][1][:70]}”"}
    if soft:
        return {"clearance_status": "possible",
                "clearance_evidence": f"{soft[0][0]} ({', '.join(downgrades[:3])}): “{soft[0][1][:60]}”"}
    # Clearance is spoken about but nothing demands it of Ed — e.g. "BPSS
    # minimum, SC not mandated" with no positive requirement.
    detail = f" ({', '.join(downgrades[:3])})" if downgrades else ""
    return {"clearance_status": "possible",
            "clearance_evidence": f"clearance mentioned, nothing required of you{detail}"}


def screening_signals(job: dict) -> dict:
    """Everything the deterministic screen can say about a posting beyond
    title and location, in one dict ready to store."""
    signals = {}
    signals.update(language_fit(job))
    signals.update(clearance_check(job))
    return signals


def clearance_reject_reason(job: dict) -> str | None:
    """Why a posting is out of reach on clearance grounds, or None. Mirrors
    contract_rates.reject_reason: an explanation string, not a bool, so the
    pipeline can report WHAT matched instead of just that something did."""
    if not CLEARANCE_REJECT_BLOCKED:
        return None
    if "clearance_status" not in job:
        job = {**job, **clearance_check(job)}
    if job.get("clearance_status") != "blocked":
        return None
    return (f"requires clearance you do not hold — "
            f"{job.get('clearance_evidence') or 'clearance required'}")


def passes_content_filters(job: dict) -> bool:
    """Title + location + language + clearance — the properties of the
    POSTING itself, before any question of money.

    Split out from passes_filters so the pipeline can tell "dropped because
    it is not a QA job" apart from "dropped because it does not pay enough".
    Without the split, a reporting line like "N jobs dropped on day rate"
    silently counts every non-QA posting that happened to quote a low rate,
    which makes the day-rate floor look far more active than it is."""
    if not title_is_relevant(job.get("title", "")):
        return False
    if not location_is_allowed(job.get("location", "")):
        return False
    if jd_stack_mismatch(job.get("description", "")):
        return False
    # A posting demanding clearance he already holds is unreachable, so it is
    # dropped rather than scored. "Eligible for SC" and "BPSS" do NOT land
    # here — see clearance_check.
    if clearance_reject_reason(job):
        return False
    return True


def passes_filters(job: dict) -> bool:
    """job must have 'title' and 'location' keys (plain strings); may
    optionally have a 'description' key (HTML or plain text)."""
    if not passes_content_filters(job):
        return False
    # The contract equivalent of contract.yaml's permanent salary floor: a
    # contract posting whose day rate cannot reach the equivalent threshold is
    # out of scope.
    # Checked here rather than in contract_rates' callers so that
    # refilter.py picks up a change to contract.yaml automatically. Never
    # fires on permanent roles, never on a contract role that simply does not
    # quote a rate, and never on a rate only Adzuna estimated — see
    # contract_rates.reject_reason.
    if contract_rates.reject_reason(job):
        return False
    return True


def rejection_reason(job: dict) -> str | None:
    """Why this posting is out of scope, or None if it passes. One place that
    names the actual cause, so the pipeline report can distinguish a title
    miss from a clearance miss from a rate miss instead of lumping every
    failure under whichever check happens to be tested first."""
    if not title_is_relevant(job.get("title", "")):
        return "title not in the QA/testing allowlist"
    if not location_is_allowed(job.get("location", "")):
        return "location not in the London/UK allowlist"
    if jd_stack_mismatch(job.get("description", "")):
        signals = language_fit(job)
        return f"language mismatch — names {signals['language_hits']} only"
    clearance = clearance_reject_reason(job)
    if clearance:
        return clearance
    contract = contract_rates.reject_reason(job)
    if contract:
        return contract
    return None
