#!/usr/bin/env python3
"""Keyword / ATS gap report: job post vs your CV.

Reports two things, because they answer different questions:

  MASTER coverage    Can you legitimately claim this term at all?
                     (measured against the full achievement bank)
  TAILORED coverage  Did the term actually make it into the CV you're sending?

A term can be in the master and missing from the tailored CV -- that's a
tailoring miss to fix. A term missing from both is either a genuine gap or a
phrasing problem.

Usage:
    python scripts/gap.py jobs/acme-data-eng/post.md
    python scripts/gap.py jobs/acme-data-eng/post.md --tailored jobs/acme-data-eng/tailored.yaml \
        -o jobs/acme-data-eng/gap-report.md
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import keywords as kw
from model import Master, Tailored, load_yaml, warn_if_placeholder

MAX_ROWS = 40


def noise_terms(job_meta: dict) -> frozenset[str]:
    """Employer-name words and initials, excluded from extraction.

    Postings abbreviate their employer freely ("DT products and services"), and
    an unexplained two-letter acronym reads as a required technology. Initials
    are safe to suppress because `analyse_post` reinstates any stop-term that
    appears under a requirements or duties heading — so a real skill that
    happens to match the initials still survives.

    Only the primary `company` is used. Parent groups are deliberately excluded:
    a group called "Nucleus Financial Platforms" would otherwise suppress
    'financial', which is a genuine requirement word in most postings.

    `recruiter` is INCLUDED, and it has to be. The documented convention for an
    advert with an unnamed employer is `company: ""` plus a `recruiter:` key (see
    lead-qa-engineer-leep-talent and automation-tester-anson-mccade). With the
    company deliberately blank, the agency name is the only proper noun in the
    header and nothing suppressed it — so "Anson McCade" put `anson` into the
    term list as if the posting had required it. An agency name is at least as
    much noise as an employer name: it describes who is advertising, not what
    the job needs. Low practical impact (one background term on the affected
    report), but it made the unnamed-employer convention behave inconsistently
    with the named-employer one.
    """
    words: set[str] = set()
    names = [job_meta.get("company", ""), job_meta.get("recruiter", "")]
    parts = [w for name in names for w in re.split(r"[^A-Za-z0-9]+", name or "") if w]
    words.update(w.lower() for w in parts if len(w) > 2)
    # Initials are taken from the employer only. Joining employer and agency
    # initials would build an acronym that appears nowhere ("Anson McCade" +
    # a company would yield a meaningless three-letter string), and suppressing
    # a term that was never in the posting can only lose real requirements.
    company_parts = [
        w for w in re.split(r"[^A-Za-z0-9]+", job_meta.get("company", "") or "") if w
    ]
    initials = "".join(w[0] for w in company_parts).lower()
    if 2 <= len(initials) <= 4:
        words.add(initials)
    return frozenset(words)


def tailored_as_text(master: Master, tailored: Tailored) -> str:
    """Flatten a tailored CV back to plain text so coverage can be measured.

    The HEADLINE is included, and that is load-bearing. It is the most-read line
    of the CV, so a term appearing only there is genuinely on the page -- but it
    was omitted here, which meant an overridden headline's vocabulary counted as
    absent and its terms were reported as "dropped in tailoring". The Appvia run
    caught it: "Quality Engineering" sits in that CV's headline and the report
    still listed it as a drop.
    """
    parts = [tailored.display_headline(), tailored.summary]
    for group in tailored.resolved_skills():
        parts.append(group["group"])
        parts.extend(str(i) for i in group["items"])
    for role in tailored.resolved_experience():
        parts.extend([role["title"], role["company"], role.get("summary_line", "")])
        parts.extend(b["text"] for b in role["bullets"])
    for edu in tailored.resolved_education():
        parts.extend(str(v) for v in edu.values() if isinstance(v, str))
    for cert in tailored.resolved_certifications():
        parts.extend(str(v) for v in cert.values() if isinstance(v, str))
    return "\n".join(parts)


def table(terms: list[kw.Term], limit: int = MAX_ROWS, show_section: bool = True) -> list[str]:
    if not terms:
        return ["_None._"]
    if show_section:
        lines = ["| Term | Where it appears | Mentions |", "| --- | --- | ---: |"]
    else:
        lines = ["| Term | Mentions |", "| --- | ---: |"]
    for t in terms[:limit]:
        where = []
        if t.in_title:
            where.append("**job title**")
        if t.in_requirements:
            where.append("**requirements**")
        if t.in_duties:
            where.append("duties")
        if t.in_nice:
            where.append("nice-to-have")
        if not where:
            where.append("body text")
        if show_section:
            lines.append(f"| {t.term} | {', '.join(where)} | {t.count} |")
        else:
            lines.append(f"| {t.term} | {t.count} |")
    if len(terms) > limit:
        lines.append(f"| _...and {len(terms) - limit} more at lower weight_ | | |" if show_section
                     else f"| _...and {len(terms) - limit} more_ | |")
    return lines


def build_report(
    post_text: str,
    master: Master,
    tailored: Tailored | None = None,
    title: str = "",
    company: str = "",
    recruiter: str = "",
) -> str:
    # Feed the employer's own name and initials in as noise. The recruiter goes in
    # too, and it has to reach here explicitly: with an unnamed employer the
    # documented convention is `company: ""` plus a `recruiter:` key, so the
    # agency name is the only proper noun in the header and nothing suppressed it.
    extra_stop = noise_terms({"company": company, "recruiter": recruiter})
    terms, by_section = kw.analyse_post(post_text, extra_stop, title=title)

    master_present, master_missing = kw.coverage(terms, master.all_fact_text())
    master_pct = kw.coverage_pct(master_present, master_missing)

    priority, background = kw.split_priority(master_missing)
    must_skills = [t for t in priority if t.is_skill]
    must_other = [t for t in priority if not t.is_skill]

    job = (tailored.job if tailored else {}) or {}
    heading = title or job.get("title") or "Job post"

    out: list[str] = []
    out.append(f"# Keyword & ATS gap report: {heading}")
    out.append("")
    if job.get("company") or company:
        out.append(f"**Company:** {job.get('company') or company}  ")
    if job.get("url"):
        out.append(f"**Posting:** {job['url']}  ")
    out.append(f"**Generated:** {date.today().isoformat()}  ")
    out.append(f"**Terms extracted:** {len(terms)} "
               f"— {len(master_present)} evidenced, {len(priority)} priority gaps, "
               f"{len(background)} background")
    out.append("")

    if master.is_placeholder:
        out.append("> **WARNING:** `cv/master.yaml` still holds placeholder data. "
                   "Every number below is meaningless until the real CV is loaded.")
        out.append("")

    out.append("## Headline")
    out.append("")
    # Two coverage figures, because one alone is misleading.
    #
    # The raw figure counts every extracted term, including the employer's own
    # marketing prose. A posting that pastes in a page of "why people choose to
    # grow their careers here" pushes it down hard without changing the role by
    # a word: the same UBDS advert scored 43% anonymously and 26% with the
    # benefits section attached, with an identical priority-gap list. So the raw
    # number is NOT comparable between postings.
    #
    # Priority-term coverage counts only requirement-, duty- and job-title-level
    # terms. That IS comparable, and it is the one that answers "how well do I
    # match this role".
    priority_present = [t for t in master_present if t.is_priority]
    priority_total = len(priority_present) + len(priority)
    priority_pct = (100.0 * len(priority_present) / priority_total) if priority_total else 0.0
    out.append(f"- **Priority-term coverage: {priority_pct:.0f}%** — "
               f"{len(priority_present)}/{priority_total} requirement-level terms are "
               f"evidenced. **Compare this one between postings.**")
    out.append(f"- **All-term coverage: {master_pct:.0f}%** — "
               f"{len(master_present)}/{len(terms)} extracted terms appear in your "
               f"achievement bank. Not comparable between postings: it falls as the "
               f"advert gets longer, because most extracted terms are background prose.")
    if tailored:
        t_text = tailored_as_text(master, tailored)
        t_present, t_missing = kw.coverage(terms, t_text)
        t_pct = kw.coverage_pct(t_present, t_missing)
        lost = [t for t in master_present if t not in t_present]

        # PRIORITY-term coverage for the tailored CV, which is the figure that
        # belongs beside the master's priority coverage.
        #
        # Without it the report invited a false comparison: the headline showed
        # master priority coverage (say 83% of 35 requirement-level terms) next
        # to tailored ALL-term coverage (26% of 227 terms), and those are
        # different measurements of different denominators. A tailored CV that
        # faithfully reproduces the master's priority terms while dropping
        # background prose — exactly what good tailoring does — read as a
        # catastrophic regression. Diagnosed 2026-09-23 on the Capco SDET
        # posting, where the drafted CV was demonstrably sound.
        t_priority_present = [t for t in t_present if t.is_priority]
        t_priority_missing = [t for t in t_missing if t.is_priority]
        t_priority_total = len(t_priority_present) + len(t_priority_missing)
        t_priority_pct = (100.0 * len(t_priority_present) / t_priority_total
                          if t_priority_total else 0.0)
        # Priority terms the master CAN evidence and this CV does not. The
        # generic `lost` list is long and mostly background; this one is short
        # and every row is a real tailoring miss, which is what makes it worth
        # acting on.
        t_lost_priority = [t for t in lost if t.is_priority]

        out.append(f"- **Tailored CV coverage: {t_pct:.0f}%** — "
                   f"{len(t_present)}/{len(terms)} terms appear in the CV you're sending")
        out.append(f"- **Tailored priority-term coverage: {t_priority_pct:.0f}%** — "
                   f"{len(t_priority_present)}/{t_priority_total} requirement-level terms "
                   f"made it into this CV. **This is the master figure's counterpart — "
                   f"compare it to the line above, and aim to raise it.**")
        out.append(f"- **Priority gaps you can't currently evidence: {len(priority)}** "
                   f"({len(must_skills)} hard skills, {len(must_other)} other)")
        if t_lost_priority:
            out.append(f"- **Priority terms dropped in tailoring: {len(t_lost_priority)}** "
                       f"({', '.join(t.term for t in t_lost_priority[:8])}"
                       + (", …" if len(t_lost_priority) > 8 else "")
                       + f") — the master evidences these, so they are closable by re-including "
                         f"or rewording, not by adding anything")
        if lost:
            out.append(f"- **In your master but dropped in tailoring: {len(lost)}** "
                       f"— see below")
    else:
        t_present, t_missing, lost = [], [], []
        t_priority_pct = None
        t_lost_priority = []
        out.append(f"- **Priority gaps you can't currently evidence: {len(priority)}** "
                   f"({len(must_skills)} hard skills, {len(must_other)} other)")
    out.append("")

    out.append("## Priority gaps: hard skills")
    out.append("")
    if not must_skills:
        out.append("_No hard-skill gaps at requirement level. Strong position._")
    else:
        out.append("Skills or tools the posting asks for (at requirement level, or mentioned "
                   "repeatedly) that appear **nowhere** in your master CV. Work these first.")
        out.append("")
        out.extend(table(must_skills, 30))
    out.append("")

    if must_other:
        out.append("## Priority gaps: methods, domains and other phrasing")
        out.append("")
        out.append("Not tools, but requirement-level language you don't currently mirror. "
                   "Often closable by rewording an existing achievement rather than adding one.")
        out.append("")
        out.extend(table(must_other, 25))
        out.append("")

    out.append("## Well-evidenced priorities")
    out.append("")
    # Include evidenced lexicon skills even at low posting weight. A named tool
    # in the "desirable" section that we already hold is still something to put
    # on the CV, and filtering to priority-only hid exactly those: k6 is named
    # outright under Performance Testing and was invisible in the report.
    p_present = [t for t in master_present if t.is_priority or t.is_skill]
    if not p_present:
        out.append("_None yet._")
    else:
        out.append("In the posting *and* provable from your history. These should be visible "
                   "on the first half-page. Terms named anywhere in the posting are included, "
                   "even from the desirable list.")
        out.append("")
        out.extend(table(p_present, 30))
    out.append("")

    if tailored and lost:
        out.append("## Dropped in tailoring")
        out.append("")
        out.append("In your master CV and in the posting, but absent from the version you're "
                   "sending. Sometimes cutting is right; sometimes it's collateral damage.")
        out.append("")
        # Show EVERY dropped term, priority first, so this table always accounts
        # for the count printed above it. Filtering to priority-only terms made
        # the two disagree in eleven reports -- "dropped in tailoring: 15" above a
        # table listing one row -- and it hid exactly what this section exists to
        # reveal. The background drops are the collateral damage: prose the advert
        # asks for that the CV quietly stopped saying.
        out.extend(table(sorted(lost, key=lambda t: (not t.is_priority, -t.weight, t.term)), 25))
        out.append("")

    if background:
        out.append("## Background terms (lower priority)")
        out.append("")
        out.append("Weighted low: mentioned once, outside requirements. Included so you can "
                   "spot anything the priority split got wrong.")
        out.append("")
        out.extend(table(background, 25, show_section=False))
        out.append("")

    out.append("## How to use this")
    out.append("")
    out.append("1. Work the hard-skill gaps first — those are what screeners and ATS filters "
               "key on, and they're the least ambiguous to resolve.")
    out.append("2. For each gap decide: **add the real evidence**, **reword an existing "
               "achievement in the posting's vocabulary**, or **accept it as a genuine gap**.")
    out.append("3. Never add a term you cannot defend in an interview. A claim that wins the "
               "screen and collapses in the interview is a net loss.")
    out.append("4. A missing term is not proof you lack the skill — it often just means your "
               "CV describes the same work in different words. Check before believing it.")
    out.append("5. Re-run after tailoring to confirm the gaps you meant to close did close.")
    out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Keyword/ATS gap report for a job post.")
    ap.add_argument("post", type=Path, help="Path to the job post text/markdown file.")
    ap.add_argument("--master", type=Path, default=None, help="Override cv/master.yaml.")
    ap.add_argument("--tailored", type=Path, default=None,
                    help="Optional tailored.yaml to compare against.")
    ap.add_argument("--company", default="", help="Employer name, excluded from extraction.")
    ap.add_argument("-o", "--out", type=Path, default=None, help="Write report here.")
    ap.add_argument("--title", default="", help="Override the report title.")
    args = ap.parse_args()

    if not args.post.exists():
        print(f"error: job post not found: {args.post}", file=sys.stderr)
        return 2

    master = Master.load(args.master) if args.master else Master.load()
    warn_if_placeholder(master)

    tailored = None
    if args.tailored:
        tailored = Tailored.load(args.tailored, master)

    # Fall back to the folder's job.yaml so the report is labelled even before
    # a tailored.yaml exists.
    job_meta: dict = {}
    sibling = args.post.parent / "job.yaml"
    if sibling.exists():
        job_meta = load_yaml(sibling)
    company = args.company or job_meta.get("company", "")
    title = args.title or job_meta.get("title", "")
    # Read the recruiter from job.yaml for the same reason the company is read:
    # an agency named on an advert whose employer is unnamed is noise, not a
    # requirement. See noise_terms.
    recruiter = job_meta.get("recruiter", "")

    report = build_report(
        args.post.read_text(encoding="utf-8"), master, tailored, title, company,
        recruiter,
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
