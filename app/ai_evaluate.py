"""Stage 2: AI evaluation of filtered candidates.

Reads every job that passed the deterministic filters (filters.py) but
hasn't been scored yet (dedup.get_unevaluated_candidates), sends it to an LLM
alongside your profile.yaml, and asks for a FUNCTIONAL FIT judgment — not a
title match. Results are stored in the ai_evaluations table (see dedup.py) and
written out to data/scored_candidates.csv, best match first.

Provider: DeepSeek by default, Claude (Anthropic) also supported — see
app/llm_providers.py. Whichever is used, the request shape and the returned
object are identical, so nothing downstream cares which one ran. Selection
order: --provider flag, then LLM_PROVIDER in .env, then whichever API key is
present. The chosen provider and model are printed on every run, because this
is the one step that costs money.

This is the ONLY part of the pipeline that costs money — everything
upstream (fetch, filter, dedup) is free. That's the whole point of doing
filtering deterministically first: by the time a job reaches this script,
it's already passed title/location/stack screening, so the AI-scored
volume should be small.

Setup:
    cp .env.example .env
    # then set DEEPSEEK_API_KEY=sk-...   (or ANTHROPIC_API_KEY=sk-ant-...)

Usage:
    python -m app.ai_evaluate                # evaluate everything unscored
    python -m app.ai_evaluate --limit 20     # cap this run (e.g. to control cost)
    python -m app.ai_evaluate --dry-run      # show what WOULD be sent, call nothing
    python -m app.ai_evaluate --list-models  # what models does my key offer?
"""
import argparse
import csv
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from app import contract_rates
from app import dedup
from app import filters
from app import jd_text
from app import llm_providers

load_dotenv()

OUTPUT_CSV = Path("data/scored_candidates.csv")

REQUIRED_EVAL_FIELDS = ["match_score", "recommendation", "genuine_gaps", "transferable_strengths", "risk_factors"]

# Field order matters here beyond documentation: models tend to emit tool JSON
# in roughly declaration order, and with max_tokens capped, a run of long
# free-text fields can eat the budget before later fields get written — which
# is exactly what caused a real KeyError on 'recommendation' in production
# (2026-08-11, see evaluate_one's retry logic below for the other half of the
# fix). Putting the two short/critical fields (match_score, recommendation)
# FIRST means they're very unlikely to be the ones lost to truncation even if a
# long-text field still gets cut off.
EVALUATION_SCHEMA = {
    "name": "submit_evaluation",
    "description": "Submit a structured fit evaluation for this job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "match_score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Overall functional fit score, 0-100.",
            },
            "recommendation": {
                "type": "string",
                "enum": ["apply", "consider", "skip"],
                "description": "apply = strong functional fit, worth the effort. consider = plausible but real gaps/risk. skip = not a genuine fit despite passing keyword filters.",
            },
            "genuine_gaps": {
                "type": "string",
                "description": "Real, specific gaps between the candidate's experience and this role's requirements. Be honest — don't invent gaps to seem balanced, and don't paper over real ones. Keep to 2-3 sentences.",
            },
            "transferable_strengths": {
                "type": "string",
                "description": "Which of the candidate's competencies/evidence genuinely transfer to this role, and why — cite specifics from their profile, not generic claims. Keep to 2-3 sentences.",
            },
            "risk_factors": {
                "type": "string",
                "description": "Non-skill risks: seniority mismatch, domain mismatch, likely comp mismatch, stack dealbreakers the deterministic filter might have missed, company-stage risk given the candidate's stated preferences, etc. Keep to 2-3 sentences.",
            },
        },
        "required": REQUIRED_EVAL_FIELDS,
    },
}

SYSTEM_PROMPT = """You are evaluating job postings for FUNCTIONAL FIT against a candidate's real \
experience — not title matching, not keyword matching. The candidate's profile is organized by \
competency (what they've actually done), not by job title, specifically so you judge whether their \
demonstrated capabilities transfer to this role's actual responsibilities.

Be honest and specific, not diplomatic. A generic "great candidate!" evaluation is useless — the \
candidate needs real signal on whether to spend an application on this. If the role is a stretch, \
say so and say why. If there's a real gap, name it precisely rather than softening it. Cite \
specific evidence from their profile when claiming a strength transfers; don't just assert \
seniority-level fit in the abstract.

Some facts about this candidate are ALREADY established deterministically and are given to you in \
the SCREENING SIGNALS block. Use them, and do not re-derive or contradict them:

* LANGUAGES. Java and Ruby are the candidate's priority and their strongest languages; treat a \
posting that names either as a language MATCH, and do not record the language as a gap. \
JavaScript/Node are acceptable but not the target. Python and Playwright are things they have \
little of, and a posting built on them SHOULD be marked down in match_score with the gap named \
explicitly in genuine_gaps. C#/.NET, Go, Kotlin, Swift, Scala and PHP are genuine mismatches.
* SECURITY CLEARANCE. The candidate does NOT hold any security clearance and has no history of one. \
Where clearance is flagged as obtainable ("eligible for SC", "BPSS"), that is NOT a blocker but it \
IS a real risk factor and a question to resolve early — say so. Where a posting demands clearance \
the candidate would already have to hold, this is close to disqualifying: score it down hard and \
name it in risk_factors.
* CONTRACT TERMS. For a contract role, judge the money against the equivalent permanent salary \
given in the signals, not against the raw day rate. A day rate below the floor is a real gap. If \
IR35 status is not stated, treat it as an open question rather than assuming the better case."""


def load_profile(path: str = "profile.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# aggregator_clients.fetch_adzuna now tries to fetch the FULL job posting
# (see fetch_full_description there) instead of settling for Adzuna's short
# API snippet — that's the real fix for 2026-08-11's "Adzuna rows scoring
# low because the description was cut off mid-sentence" problem. This flag
# is the fallback for the cases that full-JD fetch still can't recover
# (site blocked scraping, dead redirect, genuinely short posting): tell the
# model explicitly rather than let a thin description read as "no
# responsibilities listed" and quietly tank match_score.
def screening_signal_block(job: dict) -> str:
    """The deterministic findings about this posting, rendered for the prompt.

    These are computed by filters.py, not by the model, and they are the
    things a language model is worst at being consistent about: whether a
    posting names Java or Python, and whether "SC" in it means "must already
    hold" or "we will sponsor you". Handing them over means the model spends
    its judgement on fit rather than on re-deriving facts, and it means the
    same posting cannot come back scored as a Java role on one run and a
    Python one on the next.

    Recomputed here when absent so this works for callers that did not go
    through the pipeline's enrichment step."""
    signals = dict(job)
    if "language_tier" not in signals:
        signals.update(filters.screening_signals(job))
    if "employment_type" not in signals:
        contract_rates.apply_to_job(signals)

    lines = []
    tier = signals.get("language_tier")
    if tier:
        words = signals.get("language_hits") or "none named in the posting"
        tiers = {
            "priority": "PRIORITY (Java/Ruby — the candidate's strongest; not a gap)",
            "secondary": "secondary (JavaScript/Node — acceptable, not the target)",
            "none": "no language named at all — do NOT treat that as a mismatch",
            "mismatch": "MISMATCH (only non-priority languages named)",
        }
        lines.append(f"- Language fit: {tiers.get(tier, tier)}. Words matched: {words}")

    clearance = signals.get("clearance_status")
    if clearance:
        detail = signals.get("clearance_evidence") or ""
        lines.append({
            "blocked": f"- Security clearance: REQUIRES CLEARANCE THE CANDIDATE DOES NOT HOLD ({detail}) "
                       f"— this is close to disqualifying.",
            "possible": f"- Security clearance: mentioned but obtainable, not a blocker ({detail}) "
                        f"— raise it as a question/risk, do not treat it as disqualifying.",
            "none": "- Security clearance: not mentioned.",
        }.get(clearance, f"- Security clearance: {clearance} ({detail})"))

    if signals.get("employment_type") == "contract":
        rate = signals.get("day_rate_max")
        rate_text = f"£{rate:,.0f}/day" if rate else "no rate stated"
        eq = signals.get("perm_equivalent")
        lines.append(
            f"- Contract role: {rate_text}, IR35 {signals.get('ir35_status') or 'unknown'}"
            + (f", worth about £{eq:,.0f} as a permanent salary" if eq else "")
            + f". Verdict: {signals.get('rate_verdict') or 'unknown'}."
        )
    elif signals.get("employment_type") == "permanent":
        lines.append("- Employment type: permanent.")

    if not lines:
        return ""
    return ("SCREENING SIGNALS (already determined deterministically — use these, do not "
            "re-derive them):\n" + "\n".join(lines) + "\n")


def build_user_prompt(profile: dict, job: dict) -> str:
    raw_description = job.get("description", "")
    description = filters.strip_html(raw_description).strip() or "(no JD text available)"
    partial_note = ""
    if raw_description and filters.looks_truncated(raw_description):
        partial_note = (
            "\nNOTE: this description looks like a short snippet, not the full posting — it may "
            "have been cut off mid-sentence. Do NOT lower match_score or invent genuine_gaps just "
            "because a responsibility/requirement isn't mentioned here; judge fit on title, "
            "company, location, and whatever specifics ARE present. If the snippet is too thin to "
            "say anything meaningful about stack or seniority, say so in genuine_gaps rather than "
            "guessing.\n"
        )
    # A stored "description" that is really an Adzuna results page lists OTHER
    # adverts. Sending it here would have the model assess this role using four
    # other companies' languages and responsibilities — measured 2026-09-21:
    # 53 of the 72 postings on the board were in that state. The title is this
    # advert's own, so that is all the model gets.
    if jd_text.looks_like_listing_page(raw_description):
        body = jd_text.extract_advert_body(raw_description)
        if body:
            description = body
            partial_note = (
                "\nNOTE: the stored page for this posting was an Adzuna results page. Only the "
                "advert body has been extracted from it — the site navigation, the \"Popular "
                "Jobs\" sidebar and other companies' listings that appeared on the same page "
                "have been excluded, so anything you do not see here genuinely is not in the "
                "advert.\n"
            )
        else:
            description = "(no reliable advert text available)"
            partial_note = (
                "\nNOTE: the text stored for this posting is an Adzuna search-results page rather "
                "than the advert itself, and no advert body could be recovered from it, so it has "
                "deliberately NOT been included — it lists other companies' jobs. Judge on company, "
                "title, location and the screening signals only, and if something is missing say in "
                "genuine_gaps that the posting text could not be retrieved rather than inferring "
                "requirements.\n"
            )
    signals = screening_signal_block(job)
    signals_section = f"\n{signals}\n---\n" if signals else "\n"
    return f"""CANDIDATE PROFILE:
{yaml.dump(profile, sort_keys=False, allow_unicode=True)}

---

JOB POSTING TO EVALUATE:
Company: {job['company']}
Title: {job['title']}
Location: {job['location']}
URL: {job['url']}
{partial_note}
Description:
{description}

---
{signals_section}
Call submit_evaluation with your structured assessment."""


def _missing_fields(evaluation: dict) -> list[str]:
    return [f for f in REQUIRED_EVAL_FIELDS if f not in evaluation]


def evaluate_one(provider, profile: dict, job: dict, max_retries: int = 1) -> dict:
    """Ask the provider for one structured evaluation and validate that every
    required field came back.

    A forced tool/function call can still emit a truncated/incomplete JSON
    object (this happened in production on 2026-08-11 — see EVALUATION_SCHEMA's
    comment) if max_tokens is hit mid-generation; retry once with a bump to
    max_tokens before giving up, rather than crashing the whole run on one bad
    response."""
    user_content = build_user_prompt(profile, job)
    max_tokens = 1536

    for attempt in range(max_retries + 1):
        evaluation = provider.complete(SYSTEM_PROMPT, user_content, EVALUATION_SCHEMA, max_tokens)
        if evaluation is None:
            if attempt < max_retries:
                max_tokens += 512  # give the retry more room in case it was truncation
                continue
            detail = getattr(provider, "last_failure", None)
            raise RuntimeError(f"{provider.name} returned no submit_evaluation object for "
                               f"{job['url']} (model={provider.model})"
                               + (f"\n  Why: {detail}" if detail else ""))

        missing = _missing_fields(evaluation)
        if not missing:
            return evaluation
        if attempt < max_retries:
            max_tokens += 512
            continue
        raise RuntimeError(f"{provider.name}'s response for {job['url']} is missing required "
                           f"field(s) {missing} after {max_retries + 1} attempt(s): {evaluation}")


# CSV column order, chosen for readability rather than taken from the row
# dict. The contract columns sit next to posted_at so a shortlist can be
# read and sorted by rate in a spreadsheet.
_CSV_ORDER = [
    "match_score", "recommendation", "company", "title", "location",
    "posted_at", "passed_filters", "employment_type", "ir35_status", "day_rate_min",
    "day_rate_max", "rate_verdict", "perm_equivalent",
    "transferable_strengths", "genuine_gaps", "risk_factors", "url",
]


def _csv_fieldnames(rows: list[dict]) -> list[str]:
    """The declared order first, then any key the rows carry that the list
    above does not mention.

    Deriving the tail rather than hardcoding the whole set is what stops a
    column added to dedup.iter_scored_candidates later from crashing every
    evaluation run with "dict contains fields not in fieldnames" — which is
    exactly what adding the ten contract columns did. extrasaction="ignore"
    would have hidden the problem instead of fixing it, silently dropping
    the new data from the export."""
    extra = {key for row in rows for key in row} - set(_CSV_ORDER)
    return _CSV_ORDER + sorted(extra)


def write_csv(conn, path: Path = OUTPUT_CSV) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(dedup.iter_scored_candidates(conn))
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_csv_fieldnames(rows), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Max number of jobs to evaluate this run")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be evaluated, call no API")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-evaluate every posting on the board, INCLUDING ones already "
                             "scored, and overwrite their scores. Use this after the prompt, "
                             "profile.yaml or the stored job text has changed: a score is only "
                             "comparable to another score made from the same inputs, and mixing "
                             "old and new scores on one board is worse than either.")
    parser.add_argument("--provider", choices=["deepseek", "anthropic"],
                        help="Override LLM_PROVIDER / key-based auto-detection for this run")
    parser.add_argument("--list-models", action="store_true",
                        help="Ask the provider which model IDs this key can use, then exit")
    parser.add_argument("--db", help="Database path (default: data/seen_jobs.sqlite3). Matches "
                                     "add_job.py's --db, so a scratch database can be scored "
                                     "without touching the real one.")
    args = parser.parse_args()

    # A provider is only *required* for a real evaluation run. --dry-run is the
    # free smoke test (and --list-models is how you debug a key), so neither may
    # be blocked by a missing key — resolving the provider up front would break
    # exactly the command you reach for when the credentials are the problem.
    def resolve_provider():
        try:
            return llm_providers.build_provider(args.provider)
        except llm_providers.ProviderError as exc:
            sys.exit(f"{exc}\n(Note: --dry-run and --list-models need no API key.)"
                     if not (args.dry_run or args.list_models) else str(exc))

    provider = None
    if args.list_models:
        provider = resolve_provider()
        if not hasattr(provider, "list_models"):
            sys.exit(f"{provider.name} has no model-list endpoint; "
                     f"configured model is {provider.model!r}")
        try:
            models = provider.list_models()
        except Exception as exc:
            sys.exit(f"Could not list models: {exc}")
        print(f"Models available to your {provider.name} key:")
        for m in models:
            print(f"  {m}{'   <- currently configured' if m == provider.model else ''}")
        sys.exit(0)

    profile = load_profile()

    with dedup.connect(args.db or dedup.DB_PATH) as conn:
        queue = dedup.get_unevaluated_candidates(conn, include_evaluated=args.rescore)
        if args.limit:
            queue = queue[: args.limit]

        # Print the provider and model on every run: this is the step that
        # spends money, and the model is what the cost and the scores depend on.
        if args.dry_run:
            try:
                provider = llm_providers.build_provider(args.provider)
                print(f"Provider: {provider.name} | model: {provider.model}  (not called: --dry-run)")
            except llm_providers.ProviderError as exc:
                print(f"Provider: none configured yet — {exc}")
        else:
            provider = resolve_provider()
            print(f"Provider: {provider.name} | model: {provider.model}")

        if args.rescore:
            already = conn.execute(
                "SELECT COUNT(*) FROM job_details jd JOIN ai_evaluations ae ON jd.url = ae.url "
                "WHERE jd.passed_filters = 1").fetchone()[0]
            print(f"--rescore: every posting on the board is being re-evaluated, including the "
                  f"{already} already scored. This SPENDS MONEY on those again.")
        print(f"{len(queue)} candidate(s) queued for AI evaluation.")
        if not queue:
            sys.exit(0)

        if args.dry_run:
            for job in queue:
                print(f"WOULD EVALUATE | {job['company']:20s} | {job['title']}")
            sys.exit(0)

        for i, job in enumerate(queue, 1):
            try:
                evaluation = evaluate_one(provider, profile, job)
            except Exception as e:
                print(f"[WARN] {job['company']} — {job['title']}: evaluation failed — {e}", file=sys.stderr)
                continue
            dedup.save_evaluation(conn, job["url"], evaluation, f"{provider.name}:{provider.model}")
            conn.commit()  # commit per-job so a crash mid-run doesn't lose completed evaluations
            print(f"[{i}/{len(queue)}] {evaluation['match_score']:3d} {evaluation['recommendation']:9s} | "
                  f"{job['company']:20s} | {job['title']}")

        total = write_csv(conn)
        print(f"\nWrote {total} scored candidates to {OUTPUT_CSV} (sorted by match_score desc).")