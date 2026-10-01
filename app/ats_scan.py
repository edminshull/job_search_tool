"""Would this CV pass this posting's ATS, and what real experience is missing from it?

WHY THIS REPLACES TAILORING (Ed, 2026-10-01)
--------------------------------------------
Per-posting tailoring rewrote a CV for every application: it selected from the
master, reworded into the posting's vocabulary and reordered. That was the wrong
shape for how the search is actually run. The CV that gets sent is ONE document,
edited in Google Docs, and the useful per-posting questions are narrower than
"write me another CV":

  1. Would the CV I already have pass this posting's screening? (an ATS scan)
  2. Is there real experience I hold which the CV does not currently show, and
     which this posting is asking for? (suggestions to ADD to the CV)

Both are answered here, deterministically. Neither needs a model.

WHAT "PASS" MEANS, AND WHY IT IS A RULE RATHER THAN A SCORE
-----------------------------------------------------------
`scripts/keywords.py` already does the hard part properly: it splits a posting
into requirements / duties / nice-to-haves by heading, weights terms by the
section they appear under (3.0 for a requirement, 1.5 for a duty), matches on
stems and surface variants so "containerised" finds "containers", and suppresses
the employer's own name. `scripts/gap.py` calls the result "priority-term
coverage" and is explicit that it is the ONE figure comparable between postings.

What was missing is a verdict, and the temptation is to make one up as a
weighted score out of 100. That would be worse than useless: a number that looks
like a measurement but rests on hand-picked weights cannot be argued with, and
cannot be checked. So the verdict here is a rule with three named bands, and
every band states the evidence that put it there:

  * FAIL       — the posting demands a hard skill, under a requirements heading,
                 that the CV does not mention. One is enough. A keyword filter
                 that is looking for Kubernetes does not care that you are
                 strong on everything else.
  * BORDERLINE — every requirement-level hard skill is covered, but priority-term
                 coverage is below PASS_COVERAGE, so the CV is missing more of
                 the posting's vocabulary than a comfortable margin allows.
  * PASS       — neither of the above.

The thresholds are constants you can change in one place, and the scan returns
the counts behind them so a wrong verdict is a bug report rather than an
argument about taste.

SUGGESTIONS CANNOT FABRICATE, BY CONSTRUCTION
---------------------------------------------
A suggestion is never written by a model. It is an achievement that ALREADY
EXISTS in `cv/master.yaml` — Ed's own record, his own words — which the posting
asks for and the CV does not currently evidence. The scan therefore computes
three disjoint sets from the same terms:

  * on the CV already                     -> nothing to do
  * in the master but NOT on the CV       -> ADD IT (this is the suggestion)
  * in neither                            -> you do not have this; say so plainly

Only the middle set produces suggestions, and each one carries the achievement
id it came from and the posting terms it would close, so the claim is traceable
to a line in the master rather than to prose. That is what makes the output safe
to paste into a real CV, and it is why this module contains no prompt text.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# `scripts/` is not a package (see cv_tailor.slugify for the same idiom); the
# vendor engine's own modules are only importable with it on sys.path.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import gap as gap_engine  # type: ignore  # noqa: E402
import keywords as kw  # type: ignore  # noqa: E402


# The bands. Deliberately named constants rather than literals buried in an
# `if`, because these are the numbers a user will want to argue with — and
# arguing with one named number is a conversation, whereas arguing with an
# opaque score is not.
PASS_COVERAGE = 85.0

# How many rows of each list the payload carries. The scan is read in a side
# panel, not printed; a posting with 200 terms must not push a 40KB JSON through
# a subprocess boundary to show the top of a list.
MAX_GAPS = 25
MAX_SUGGESTIONS = 12
MAX_EVIDENCED = 20

# Cut-offs for the "is this term really a gap" judgement. A term mentioned once
# in passing at the bottom of an advert is not the same claim as one that heads
# the requirements, and `weight` already encodes that (see Term.weight).
_MIN_GAP_WEIGHT = 2.0

# Below this many characters the posting is a search-result snippet, not an
# advert. MEASURED on this repo's own board, 2026-10-01, over the 127 rows that
# passed the filters and using the text the scan actually reads
# (cv_tailor._post_text, not the raw column): the MEDIAN was 500 characters, 65
# were under 800, only 43 were over 3,000, and 2 had no recoverable advert text
# at all. Adzuna returns a teaser and the full-body fetch does not always
# recover the rest, so more than half the board cannot be judged at all. That is a fact about the stored data, not about the
# CV, and it has to be reported as such — a verdict computed from 500 characters
# of prose is noise wearing a percentage sign.
SNIPPET_CHARS = 1000


def _section_of(term) -> str:
    """Where the posting put this term, most demanding first."""
    if term.in_requirements:
        return "requirements"
    if term.in_duties:
        return "duties"
    if term.in_nice:
        return "nice-to-have"
    return "mentioned"


def _term_row(term) -> dict:
    return {
        "term": term.term,
        "weight": round(term.weight, 2),
        "section": _section_of(term),
        "skill": bool(term.is_skill),
    }


def _terms(term_list) -> dict:
    """A list of Terms back into the dict `keywords.coverage` expects."""
    return {t.term: t for t in term_list}


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def _unreliable_reason(unreliable: str | None) -> str:
    """Why no verdict could be reached, in terms the reader can act on.

    Two different problems produce the same "unknown", and conflating them
    wastes the reader's time in opposite directions: a truncated snippet is
    fixed by re-fetching the advert, whereas an unheaded one is fixed by reading
    the employer's own board. Naming the problem names the fix."""
    if unreliable == "snippet":
        return (
            "The stored description is a search-result snippet rather than the advert, "
            "so there is not enough of the posting to judge anything against. This is "
            "NOT a pass and NOT a fail. Scanning the employer's own copy of this job, "
            "if the board has one, is the fix."
        )
    return (
        "This posting's text has no recognisable requirements or duties headings — it is "
        "stored as flat prose, which is what an aggregator's page scrape leaves behind. "
        "There is no requirement-level evidence to judge the CV against, so this is NOT a "
        "pass and NOT a fail."
    )


def verdict_for(hard_gaps: list, priority_coverage: float, *,
                reliable: bool = True, unreliable: str | None = None) -> tuple[str, str]:
    """(verdict, why) — the rule, in one place, with its reasoning attached.

    Ordered deliberately: an unassessable posting beats everything, then a
    missing requirement beats a low percentage. A CV that mentions 95% of a
    posting's vocabulary but not the one tool the requirements section names is
    a worse bet than one at 80% that names it, and averaging those two facts
    into a single number loses exactly the distinction that decides the outcome.

    The `reliable` gate first is not defensive padding. It is the fix for a
    measured false PASS: Lendable's "Senior Quality Engineer - AI" exists twice
    on the board, and the Adzuna copy's stored text is 8,500 characters of flat
    prose with no requirement or duties headings at all — an ATS page scrape
    that dropped the advert's structure. With no headings, `keywords.py` has no
    section to weight against, so almost nothing qualifies as priority, and the
    scan reported "PASS — priority-term coverage 100% (4/4)" on a posting it had
    not actually read. Four terms and a green tick is worse than no answer."""
    if not reliable:
        return "unknown", _unreliable_reason(unreliable)
    if hard_gaps:
        names = ", ".join(t.term for t in hard_gaps[:4])
        more = f" (+{len(hard_gaps) - 4} more)" if len(hard_gaps) > 4 else ""
        return "fail", (
            f"{len(hard_gaps)} requirement-level hard skill"
            f"{'s' if len(hard_gaps) != 1 else ''} the CV does not mention: {names}{more}."
        )
    if priority_coverage < PASS_COVERAGE:
        return "borderline", (
            f"Every requirement-level hard skill is covered, but priority-term "
            f"coverage is {priority_coverage:.0f}%, below the {PASS_COVERAGE:.0f}% "
            f"this scan treats as a comfortable margin."
        )
    return "pass", (
        f"Every requirement-level hard skill is covered and priority-term coverage "
        f"is {priority_coverage:.0f}%."
    )


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan(job: dict,
         posting_text: str,
         cv_text: str,
         master_text: str,
         master_data: dict | None = None,
         cv_source: dict | None = None,
         posting_truncated: bool = False) -> dict:
    """The whole scan for one posting. Pure: no network, no model, no DB.

    `cv_text`     the CV as it would be sent (from the Google Doc).
    `master_text` the full fact base, flattened — everything Ed could claim.
    `master_data` the parsed master, for turning a closing term back into the
                  achievement that closes it.

    Splitting `cv_text` from `master_text` is the entire trick: the difference
    between them is precisely "real experience you are not currently showing".
    """
    extra_stop = gap_engine.noise_terms({"company": job.get("company", "")})
    terms, by_section = kw.analyse_post(
        posting_text, extra_stop, title=job.get("title", ""))

    # Did the posting even HAVE headings to weight against? See verdict_for:
    # without them the priority set collapses, and a collapsed priority set
    # turns into a confident pass on a posting that was never really read.
    sections_found = sorted(name for name, group in by_section.items() if group)
    # Two independent ways to be unassessable, kept apart because they have
    # different fixes (see _unreliable_reason).
    snippet = posting_truncated or len(posting_text) < SNIPPET_CHARS
    unreliable = "snippet" if snippet else (None if sections_found else "no_headings")
    reliable = unreliable is None

    on_cv, missing_from_cv = kw.coverage(terms, cv_text)
    in_master, missing_everywhere = kw.coverage(terms, master_text)

    in_master_keys = {t.term for t in in_master}
    closable = [t for t in missing_from_cv if t.term in in_master_keys]
    unclosable = [t for t in missing_from_cv if t.term not in in_master_keys]

    # Coverage figures. `priority` is the comparable one — gap.py's own docstring
    # says so, and it is the only reason the number means anything between two
    # postings of different lengths.
    priority_on_cv = [t for t in on_cv if t.is_priority]
    priority_gaps, _background_gaps = kw.split_priority(missing_from_cv)
    priority_total = len(priority_on_cv) + len(priority_gaps)
    priority_coverage = (100.0 * len(priority_on_cv) / priority_total) if priority_total else 100.0

    hard_gaps = [t for t in priority_gaps
                 if t.is_skill and t.in_requirements and t.weight >= _MIN_GAP_WEIGHT]
    verdict, why = verdict_for(hard_gaps, priority_coverage,
                               reliable=reliable, unreliable=unreliable)

    # Suggestions are driven by PRIORITY gaps only. Offering a bullet because it
    # happens to contain a word the advert used once in passing is how a CV grows
    # without getting better; the first version of this did exactly that and
    # proposed four edits to a CV it had just scored 100%.
    suggestions = _suggestions(master_data, _priority_only(closable), cv_text)
    projection = _projection(terms, cv_text, suggestions, priority_coverage,
                             reliable=reliable, unreliable=unreliable)

    return {
        "url": job.get("url"),
        "company": job.get("company"),
        "title": job.get("title"),
        "cv_source": cv_source or {},
        "ats": {
            "verdict": verdict,
            "reason": why,
            "reliable": reliable,
            "unreliable_reason": unreliable,
            "sections_found": sections_found,
            "terms_found": len(terms),
            "posting_chars": len(posting_text),
            "priority_coverage": round(priority_coverage, 1),
            "priority_evidenced": len(priority_on_cv),
            "priority_total": priority_total,
            "all_term_coverage": round(kw.coverage_pct(on_cv, missing_from_cv), 1),
            "thresholds": {"pass_coverage": PASS_COVERAGE},
            "hard_gaps": [_term_row(t) for t in hard_gaps[:MAX_GAPS]],
            "priority_gaps": [_term_row(t) for t in priority_gaps[:MAX_GAPS]],
            "evidenced_priorities": [_term_row(t) for t in priority_on_cv[:MAX_EVIDENCED]],
        },
        "suggestions": suggestions,
        "unclosable": [_term_row(t) for t in _priority_only(unclosable)[:MAX_GAPS]],
        "roles_missing_from_cv": _roles_missing(master_data, cv_text),
        "projection": projection,
    }


def _priority_only(term_list: list) -> list:
    """Just the terms worth acting on, from a list of Terms.

    The split between priority and background is the whole reason gap.py's
    figures are comparable at all, and it applies to every list this module
    shows a human — not only to the ones a percentage is computed from. Without
    it the "you do not have this" list filled up with 'products', "you'll" and
    'keep', which are words the advert used, not things anyone can have."""
    return [t for t in term_list if t.is_priority]


def _suggestions(master_data: dict | None, closable: list, cv_text: str) -> list[dict]:
    """Master achievements that would close posting terms the CV misses.

    Every suggestion is a chunk of the master, quoted verbatim. Nothing here
    composes new prose: `text` is the master's own `text` field, and the module
    docstring explains why that constraint is structural rather than a choice.

    Each one is classed as either an ADDITION or a REWORDING, and the difference
    is not cosmetic. `closable` is computed from the master's *tags* as well as
    its prose, because a tag is a claim the master can support — but a tag is
    not printed on the CV. So a term can be "in the master, missing from the CV"
    while the achievement that carries it is already ON the CV under different
    wording. Telling Ed to add a bullet he already has would be a small lie
    about his own CV, and it is a self-inflicted one: it is detectable by
    comparing the achievement's text against the CV's text, which is what this
    does. The honest advice in that case is "the fact is already there; name it
    in the posting's words", not "add this"."""
    if not master_data or not closable:
        return []
    closable_by_term = _terms(closable)
    cv_lines = _cv_lines(cv_text)

    found: list[dict] = []
    for role in master_data.get("experience") or []:
        for ach in role.get("achievements") or []:
            # One coverage() call per achievement rather than one per
            # (achievement, term) pair: this is the engine's own matcher, so a
            # term counts as closed here exactly when it would count as closed
            # anywhere else in the pipeline.
            text = (ach.get("text") or "").strip()
            haystack = " ".join(str(x) for x in (
                [text] + list(ach.get("tags") or []) + list(ach.get("metrics") or [])
            ))
            closed, _ = kw.coverage(closable_by_term, haystack)
            if not closed:
                continue
            overlap = _text_overlap(text, cv_lines)
            found.append({
                "kind": "reword_achievement" if overlap >= ON_CV_OVERLAP else "add_achievement",
                "on_cv_overlap": round(overlap, 2),
                "role_id": role.get("id"),
                "role_title": role.get("title"),
                "company": role.get("company"),
                "achievement_id": ach.get("id"),
                # Verbatim from cv/master.yaml — see the module docstring.
                "text": text,
                "tags": list(ach.get("tags") or []),
                "closes_terms": [_term_row(t) for t in closed],
                "closes_count": len(closed),
                "weight_closed": round(sum(t.weight for t in closed), 2),
            })

    # Rank by how much of the posting's demand the line actually answers, not by
    # how many terms it happens to collide with: one requirement-level hit is
    # worth more than five passing mentions, and `weight` is what encodes that.
    # Additions edge out rewordings at equal weight, because a whole bullet that
    # is missing from the CV is the bigger omission of the two.
    found.sort(key=lambda s: (-s["weight_closed"], s["kind"] != "add_achievement",
                              -s["closes_count"], s["achievement_id"] or ""))
    return found[:MAX_SUGGESTIONS]


# How much of an achievement's wording has to appear on the CV before the
# achievement counts as already there. Not 1.0: a CV bullet is routinely a
# lightly trimmed version of the master line, and demanding an exact match would
# classify every one of them as missing. Not 0.5 either — at that level a bullet
# that merely shares a vocabulary with another one starts reading as present.
ON_CV_OVERLAP = 0.7

# Tokens too short to carry meaning. Dropped so that "for", "the" and "of"
# cannot pad the overlap of two unrelated sentences toward the threshold.
_MIN_TOKEN = 4


def _text_overlap(text: str, cv_lines: list[set[str]]) -> float:
    """Fraction of an achievement's wording that appears in ONE line of the CV.

    Per LINE, taking the best line, rather than one set of every token in the
    document. The document-wide version was measured wrong: it scored an
    ABSENT role's bullet at 0.8 against a CV that does not mention that employer
    at all, because "test", "performance", "software" and "testing" are in every
    QA bullet there is. A bullet genuinely on the CV matches one specific line
    almost exactly; a bullet that is absent only ever matches scattered words.

    The denominator is the achievement's own token count, so a short CV line
    cannot score highly against a long achievement line — which is what makes
    "any shared words" useless for this and this check usable."""
    meaningful = [kw.stem_key(t) for t in kw.tokens_of(text)]
    meaningful = [t for t in meaningful if len(t) >= _MIN_TOKEN]
    if not meaningful or not cv_lines:
        return 0.0
    return max(sum(1 for t in meaningful if t in line) for line in cv_lines) / len(meaningful)


def _cv_lines(cv_text: str) -> list[set[str]]:
    """The CV split into lines, each as a set of normalised tokens."""
    return [
        {kw.stem_key(t) for t in kw.tokens_of(line)}
        for line in cv_text.splitlines()
        if line.strip()
    ]


def _projection(terms: dict, cv_text: str, suggestions: list[dict],
                priority_coverage: float, *, reliable: bool = True,
                unreliable: str | None = None) -> dict:
    """What the scan would say if every suggestion were added.

    Worth computing because it answers the only question that decides whether
    the suggestions are worth acting on: does this actually change the verdict,
    or is it busywork? It is a projection and says so — it scores the CV text
    with the suggested lines appended, without pretending the additions are
    already on the CV.
    """
    if not suggestions:
        return {
            "coverage": round(priority_coverage, 1),
            "verdict": None,
            "closes_terms": [],
            "still_open": [],
            "note": "No master experience would close any of this posting's gaps.",
        }
    additions = "\n".join(s["text"] for s in suggestions if s.get("text"))
    projected_text = f"{cv_text}\n{additions}"
    on_cv, missing = kw.coverage(terms, projected_text)
    priority_on_cv = [t for t in on_cv if t.is_priority]
    priority_gaps, _ = kw.split_priority(missing)
    total = len(priority_on_cv) + len(priority_gaps)
    coverage = (100.0 * len(priority_on_cv) / total) if total else 100.0
    hard_gaps = [t for t in priority_gaps
                 if t.is_skill and t.in_requirements and t.weight >= _MIN_GAP_WEIGHT]
    verdict, _why = verdict_for(hard_gaps, coverage, reliable=reliable,
                                unreliable=unreliable)
    closed = sorted({row["term"] for s in suggestions for row in s["closes_terms"]})
    return {
        "coverage": round(coverage, 1),
        "verdict": verdict,
        "closes_terms": closed,
        "still_open": [_term_row(t) for t in priority_gaps[:MAX_GAPS]],
        "note": "If every suggestion above were added to the CV.",
    }


def _roles_missing(master_data: dict | None, cv_text: str) -> list[dict]:
    """Master roles whose EMPLOYER does not appear on the CV at all.

    A role-level answer, separate from the term-level one, because a whole job
    missing from a CV is a different kind of omission from a missing keyword: a
    posting wanting Kubernetes is answered by a bullet, whereas one wanting a
    tool the candidate used years ago may be answered by a job that was left off
    the CV entirely. On this repo's own data that is not hypothetical: an
    earlier commercial role is in the master and absent from the Doc, and it is
    the largest single thing the scan can report.

    The test is the employer's distinctive name and NOTHING ELSE. An earlier
    version also accepted the job title as evidence the role was present, which
    made the check useless rather than lenient: the omitted role's title shares
    a word ("consultant") with a DIFFERENT job that IS on the CV, so the role
    was reported present by a word it had in common with an unrelated one."""
    if not master_data:
        return []
    cv_norm = kw.stemmed_text(cv_text)
    out: list[dict] = []
    for role in master_data.get("experience") or []:
        if not (role.get("achievements") or []):
            continue
        key = _employer_token(str(role.get("company") or ""))
        if not key or key in cv_norm:
            continue
        out.append({
            "id": role.get("id"),
            "company": role.get("company"),
            "title": role.get("title"),
            "start": role.get("start"),
            "end": role.get("end"),
            "achievements": len(role.get("achievements") or []),
        })
    return out


# Employer names carry legal suffixes that will never appear in a CV's text
# ("Example FinTech Ltd", "Example Computer Services"), so the comparison token
# is the FIRST word that is not one of them — the distinctive name — rather than
# the longest, which would pick "computer" out of "Example Computer Services"
# and "group" out of "Example Group".
_LEGAL_NOISE = {"ltd", "limited", "inc", "llc", "plc", "group", "holdings",
                "services", "service", "solutions", "company", "computer",
                "technologies", "technology", "consulting", "the", "and"}


def _employer_token(company: str) -> str | None:
    for raw in re.split(r"[^A-Za-z0-9]+", company):
        if len(raw) <= 2 or raw.lower() in _LEGAL_NOISE:
            continue
        return kw.stem_key(raw.lower())
    return None
