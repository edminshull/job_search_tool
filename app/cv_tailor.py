"""Tailor the CV for one job posting — the engine behind the board's button.

This is the job_search side of the CV pipeline that used to live in a separate
repository. The engine itself (`scripts/`, `cv/master.yaml`, `context/`) was
vendored here unchanged, because `render.py`'s fabrication guard is the whole
reason the output can be trusted and reimplementing it would have thrown that
away. What this module adds is the orchestration that repository did not have:
a two-model draft/verify loop, and the file layout the dashboard expects.

    cv/master.yaml                 the fact base — every role, achievement, skill.
                                   Edits here are the ONLY way a new fact enters
                                   any CV, and they are made by hand, on purpose.
    cv_output/<DD-MM-YY>/<company>/ one folder per application
    cv_output/Ed-Minshull-CV.*     the master full inventory, rendered ON DEMAND

THE TWO-MODEL PIPELINE, AND WHY IT IS TWO
-----------------------------------------
Stage 1 drafts with the local Qwen3-Coder-30B (app/llm_providers.py →
LocalOpenAIProvider). Stage 2 verifies that draft with DeepSeek. The split is
not redundancy for its own sake — it is that selecting and rewording facts is a
mechanical task the local model does well and for free, while deciding whether a
rewording is still the same fact is the judgement call this whole system exists
to get right, and that one is not handed to the 30B model.

Measured 2026-09 on the live llama-server: a forced tool call is honoured, a
realistic selection sub-task came back with valid master refs and no invented
skills in ~7s. The binding constraint is the 32,768-token slot: cv/master.yaml
is ~40 KB, so the master plus a long posting can fill it. Stage 1 therefore
receives the DETERMINISTIC gap report alongside the master — the posting's
requirement terms, and which of them the master can and cannot evidence. That
turns "write a CV" into "close these specific gaps", which is both a smaller
prompt and a far better task for a small model.

NOTHING HERE IS THE LAST LINE OF DEFENCE. `render.py` is: it re-validates every
`ref` against the master and hard-fails (exit 3) on a skill the master cannot
support, and this module refuses to write any file when it does.

TWO STEPS, BECAUSE THE PREVIEW IS THE POINT
-------------------------------------------
`preview` drafts, verifies and reports — and stores the YAML in the database
without writing a single file into cv_output/. `render` takes that stored YAML
(or an edited one) and produces the documents. So approving renders exactly
what you read, a preview you abandon leaves no trace on disk, and there is no
window in which a half-built application folder exists.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

from app import dedup
from app import llm_providers
from app import filters

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "bin" / "python"
SCRIPTS = ROOT / "scripts"
MASTER_PATH = ROOT / "cv" / "master.yaml"
FULL_INVENTORY = ROOT / "cv" / "full-inventory.tailored.yaml"
OUTPUT_ROOT = ROOT / "cv_output"

# `dd-mm-yy`, deliberately matching the CV engine's own DATE_DIR_FORMAT. Not
# `%d-%m-%Y`: the vendored scripts parse both back, but new folders should look
# like the ones the engine used to write, and the renderer's own docstring notes
# that this order does not sort across years — which is acceptable for a folder
# you navigate by name, not sort programmatically.
DATE_DIR_FORMAT = "%d-%m-%y"

# Which provider drafts and which verifies. Both are overridable so either half
# can be moved (local↔cloud) without a code change; the defaults are the split
# the docstring above justifies.
DRAFT_PROVIDER_ENV = "CV_DRAFT_PROVIDER"
VERIFY_PROVIDER_ENV = "CV_VERIFY_PROVIDER"
DEFAULT_DRAFT_PROVIDER = "local"
DEFAULT_VERIFY_PROVIDER = "deepseek"

# Stage 1 writes the whole YAML document into one string field. That is not a
# stylistic choice: nested object/array schemas are exactly where llama.cpp's
# grammar-constrained tool calling is least predictable, and a YAML string also
# means the model's output can be shown to the user verbatim and diffed. The
# cost is that the YAML must be PARSED before it can be validated, which the
# stage-1 retry below handles.
# Skill groups that every tailored CV must carry, by exact master group name.
#
# These two hold the AI and local-LLM work, and they were being DROPPED — not by
# the master, which has always had them, but by the drafter. Found 2026-09-23:
# the Kainos draft omitted both, and the resulting CV had no Playwright, Python,
# Ollama, llama.cpp, Qwen or local-LLM content at all. The old repo's tailored
# CVs carried `Personal & Prototype Work` in four of its last six, so this was a
# REGRESSION introduced by an over-correction in the draft prompt ("list only the
# skill items that serve this posting, the selection is the tailoring") — an
# instruction that is right about items and gets applied, by a small model, to
# whole groups.
#
# So it is enforced in code rather than requested in a prompt. A model that
# ignores the instruction still cannot omit them, which is the same reasoning as
# render.py's fabrication guard: the code holds the line, not the model's good
# intentions.
#
# Deliberately NOT "include every group": a posting in a different discipline
# should still be able to drop eight or nine of the master's groups. These two
# are different because the perspective they evidence is the thing most likely to
# distinguish this candidate, and because they are the two a relevance-minded
# model reliably judges irrelevant.
REQUIRED_SKILL_GROUPS = ("AI-Assisted Engineering", "Personal & Prototype Work")

# Individual items held in place alongside the groups above. `Claude Code` is the
# reason this exists: it is COMMERCIAL AI-assisted engineering (AI-assisted test
# generation adopted in a commercial role), and the local model dropped it
# from the group while keeping the group — deciding that a public-sector test role
# had no use for it, twice running. The group being present is not the same as the
# work being present, which is the whole lesson of this section.
#
# Enforced the same way as the groups and for the same reason: the remedy for a
# model repeatedly judging a genuine differentiator irrelevant is code, not a
# stronger adjective in a prompt.
REQUIRED_SKILL_ITEMS = {
    "AI-Assisted Engineering": ("Claude Code",),
    # Pinned individually for the same reason as Claude Code, and found the same
    # way: on the Trading 212 CV the drafter wrote "DeepSeek Harness" where the
    # master says "DeepSeek V4 Flash". That is not a fabrication — `deepseek
    # harness` IS a real master project tag — but it renames the thing at a
    # different level, and it silently dropped the item Ed had specifically asked
    # for. The group was present, in the right place, with eight items, so every
    # GROUP-level check passed while the CV said something other than what the
    # master says.
    #
    # The rule this encodes: THE MASTER'S NAME IS THE NAME. A tailored CV chooses
    # which master items to show, and may reorder them, but it may not invent a
    # synonym for a fact the master already words precisely.
    "Personal & Prototype Work": ("Playwright", "Python", "Ollama", "llama.cpp",
                                  "Qwen3-Coder", "DeepSeek V4 Flash",
                                  "Local LLM Deployment"),
}


def merge_required_groups(tailored_data: dict, master_data: dict) -> tuple[dict, list[str]]:
    """Force REQUIRED_SKILL_GROUPS into a draft's skills, at master order.

    Returns (data, notes) where `notes` names what was re-inserted, so the UI can
    say the draft was corrected rather than letting a code-level fix masquerade as
    the model's judgement.

    Insertion position follows the MASTER's group order rather than appending, so
    the CV reads in the same sequence as every other CV and the two required
    groups are not bolted onto the end where they look like an afterthought.

    Only groups the drafter left out entirely are re-added — a group already
    present keeps the drafter's own item selection and ORDER, because trimming
    irrelevant items inside a group is legitimate tailoring and the thing most
    likely to serve this posting."""
    groups = [
        {"group": g.get("group"), "items": list(g.get("items") or [])}
        for g in (master_data.get("skill_groups") or [])
        if g.get("group") in REQUIRED_SKILL_GROUPS
    ]
    if not groups:
        return tailored_data, []

    # A draft that omits `skills` altogether emits EVERY master group, so there is
    # nothing to enforce — re-adding would duplicate the whole list.
    if tailored_data.get("skills") in (None, []):
        return tailored_data, []

    present = {str(g.get("group")): g for g in tailored_data.get("skills") or []
               if isinstance(g, dict)}
    missing = [g for g in groups if str(g["group"]) not in present]

    # Restore named items into groups the drafter KEPT but trimmed them out of.
    notes: list[str] = []
    restored: list[str] = []
    for group_name, items in REQUIRED_SKILL_ITEMS.items():
        entry = present.get(group_name)
        if not isinstance(entry, dict):
            continue
        current = list(entry.get("items") or [])
        if not current:
            # An empty list would emit the group with no items at all, so fill it.
            entry["items"] = list(items)
            restored.extend(items)
            continue
        for item in items:
            if item not in current:
                current.append(item)
                restored.append(item)
        entry["items"] = current
    if restored:
        notes.append(f"{', '.join(restored)} (re-added to their group)")

    if not missing:
        return (tailored_data, notes) if restored else (tailored_data, [])

    master_order = [str(g.get("group")) for g in (master_data.get("skill_groups") or [])]

    def rank(entry: dict) -> int:
        name = str(entry.get("group"))
        return master_order.index(name) if name in master_order else len(master_order)

    merged = [present.get(str(g.get("group")), g) for g in (tailored_data.get("skills") or [])]
    merged = [g for g in merged if g is not None] + missing
    merged.sort(key=rank)           # stable, so the drafter's own order is preserved
    out = dict(tailored_data)
    out["skills"] = merged
    return out, notes + [str(g["group"]) for g in missing]


DRAFT_MAX_TOKENS = 4096
# The verifier writes prose, not a document, and was still truncating at 2000
# tokens on a posting with several real gaps: its findings for the LEGO ad ran
# past 5,000 characters, so the response was cut off and the whole verification
# was lost as "returned nothing usable" — the check silently not happening,
# which is the one failure mode that matters here. Generous on purpose: this
# call is ~5s and a lost verdict costs a re-run.
VERIFY_MAX_TOKENS = 4096


class TailorError(RuntimeError):
    """A tailoring step failed in a way the caller should surface verbatim.

    Raised rather than sys.exit()'d because the web app runs this in a
    subprocess and shows the message to the user — an error that reads like a
    sentence is the entire difference between a usable button and a stack
    trace in a JSON field."""


# ---------------------------------------------------------------------------
# Paths and layout
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """The master CV engine's own slug rule, reused rather than reinvented so
    a folder created here looks like one the CLI would have created.

    `model.slugify` is the authority; it is imported lazily because `scripts/`
    is not a package and is only importable with that directory on sys.path."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import model  # type: ignore
        return model.slugify(text)
    finally:
        sys.path.pop(0)


def application_dir(day_dir: str, company_slug: str) -> Path:
    return OUTPUT_ROOT / day_dir / company_slug


def master_inventory_stem() -> str:
    return "Ed-Minshull-CV"


def _existing_record(conn, url: str) -> dict | None:
    return dedup.get_tailoring(conn, url)


def _day_and_slug(record: dict | None, job: dict) -> tuple[str, str]:
    """The application's date directory and company folder.

    The date is FIXED WHEN THE APPLICATION IS FIRST PREPARED and reused on
    every later render — the vendored renderer's own rule, and for its stated
    reason: re-rendering an old CV must not move it into today's folder and
    scatter one application's files across two dates. The same applies to the
    company slug, which is derived once so a later posting edit cannot rename
    the folder out from under files that may already have been sent."""
    if record and record.get("day_dir") and record.get("company_slug"):
        return record["day_dir"], record["company_slug"]
    return date.today().strftime(DATE_DIR_FORMAT), slugify(job.get("company") or "unknown")


# Block-level tags that must become a LINE BREAK, not a space. `filters.strip_html`
# replaces every tag with a space, which is right for keyword matching (it only
# needs the words) and wrong for anything that reads the posting's STRUCTURE.
_HTML_BLOCK_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|tr|td|th|table|h[1-6]|section|article|header|footer|blockquote)"
    r"[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(#\d+|#x[0-9a-f]+|[a-z]+);", re.IGNORECASE)
_HTML_ENTITIES = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
                  "rsquo": "'", "lsquo": "'", "rdquo": '"', "ldquo": '"', "mdash": " - ",
                  "ndash": "-", "hellip": "...", "bull": "-", "middot": "-"}


def html_to_structured_text(html: str) -> str:
    """HTML -> plain text that keeps the posting's BLOCK STRUCTURE.

    This exists because the gap report weights terms by WHERE they appear: a
    term under "Essential Requirements" counts triple, and the same term under
    "Benefits" counts almost nothing. `scripts/keywords.split_sections` decides
    that by looking for headings that sit on their OWN LINE, isolated by blank
    lines.

    `filters.strip_html` turns every tag into a SPACE, so Workday's posting —
    where the whole advert is one line of `<h1>…</h1><ul><li>…</li></ul>` —
    collapses into a single line with no headings at all. Every term then falls
    into "other", section weighting is silently lost, and the report claims the
    posting has almost no requirement-level terms. Measured on the Kainos Lead
    Test Engineer advert (2026-09-23): 144 terms extracted, only 3 classified as
    priority, with Jenkins, Selenium, CI/CD and SQL all reported as "body text"
    despite sitting under "Essential Requirements". The coverage percentage that
    feeds the UI was therefore meaningless, and the drafting prompt lost the
    requirement/gap split that makes it useful to a small model.

    So block tags become newlines and inline tags become spaces, entities are
    decoded (Workday emits &#39;), and the result is whitespace-normalised so
    no line is blank-by-accident — a spurious blank line would separate a
    heading from its body and change the bucket."""
    if not html:
        return ""
    text = _HTML_BLOCK_RE.sub("\n", html)
    text = _HTML_TAG_RE.sub(" ", text)

    def entity(match: re.Match) -> str:
        body = match.group(1)
        if body.startswith("#"):
            try:
                code = int(body[2:], 16) if body[1].lower() == "x" else int(body[1:])
                return chr(code)
            except (ValueError, IndexError):
                return match.group(0)
        return _HTML_ENTITIES.get(body.lower(), match.group(0))

    text = _HTML_ENTITY_RE.sub(entity, text)
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        # Collapse runs of blank lines to exactly one: split_sections treats a
        # blank line as the signal that a line is an isolated heading, so the
        # count of them matters.
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _post_text(job: dict) -> str:
    """The posting as plain text WITH its structure, for scripts/gap.py.

    The stored description may be HTML (ATS boards), plain text, or an Adzuna
    results page that lists other companies' adverts. `jd_text` handles the last
    case; the first needs `html_to_structured_text` rather than
    `filters.strip_html`, for the section-weighting reason documented there."""
    from app import jd_text

    raw = job.get("description") or ""
    if raw and jd_text.looks_like_listing_page(raw):
        body = jd_text.extract_advert_body(raw)
        return html_to_structured_text(body) if body else ""
    return html_to_structured_text(raw)


def _day_dir_to_iso(day_dir: str) -> str:
    """`23-09-26` → `2026-09-23`.

    The two-digit year is expanded against the 2000s rather than parsed as-is:
    `%y` maps 26 to year 26, and a job.yaml dated 0026 would send the renderer's
    date logic somewhere no folder exists. Falls back to today rather than
    raising, because a hand-edited day_dir must not fail a render."""
    try:
        day, month, year = (int(p) for p in day_dir.split("-"))
        return date(year + 2000 if year < 100 else year, month, day).isoformat()
    except (TypeError, ValueError):
        return date.today().isoformat()


def _write_job_metadata(dest: Path, job: dict, day_dir: str) -> None:
    """job.yaml beside tailored.yaml.

    `render.py` reads this sibling for the output date, the stem and the company
    name that gap.py suppresses as noise — so a missing job.yaml is not a
    cosmetic omission, it sends the render to output/archive/ instead of the
    dated folder. The date is written back from `day_dir` rather than from the
    clock, which is what keeps a re-render in the folder it already owns."""
    meta = {
        "slug": dest.name,
        "company": job.get("company") or "",
        "title": job.get("title") or "",
        "url": job.get("url") or "",
        "source": job.get("source") or "job_search board",
        "date": _day_dir_to_iso(day_dir),
    }
    if job.get("location"):
        meta["location"] = job["location"]
    (dest / "job.yaml").write_text(
        yaml.safe_dump(meta, sort_keys=False, allow_unicode=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# The vendored engine, invoked as subprocesses
# ---------------------------------------------------------------------------

def _run_script(args: list[str]) -> subprocess.CompletedProcess:
    """Run a vendored script with the repo venv's interpreter.

    A subprocess rather than an import: the scripts are CLI-first (they read
    argv, print to stdout and use exit codes as their contract — render.py's
    exit 3 IS the fabrication signal), and importing them would mean
    reimplementing that contract. The venv interpreter is used explicitly
    because python-docx/fpdf2 are not installed system-wide, and a failure here
    should say so rather than produce a confusing ImportError traceback."""
    if not VENV_PY.exists():
        raise TailorError(
            f"No virtualenv at {VENV_PY}. Create it once with:\n"
            f"  python3 -m venv .venv && ./.venv/bin/python -m pip install "
            f"python-docx fpdf2 PyYAML pypdf httpx python-dotenv"
        )
    return subprocess.run(
        [str(VENV_PY)] + args,
        cwd=str(ROOT), capture_output=True, text=True, timeout=600,
    )


# Two distinct labels, matched separately on purpose. They are NOT
# interchangeable: master coverage is a share of requirement-level terms,
# all-term coverage is a share of all 227-odd extracted terms including
# background prose. Reporting one against the other produced a false
# "83% → 26%" regression on a draft that was demonstrably sound (diagnosed
# 2026-09-23), which is why the tailored figure is now the PRIORITY one.
COVERAGE_RE = re.compile(r"coverage: (\d+)%")
PRIORITY_GAP_RE = re.compile(r"Priority gaps you can't currently evidence: (\d+)")
DROPPED_PRIORITY_RE = re.compile(r"Priority terms dropped in tailoring: (\d+)")


def parse_coverage(report_md: str) -> dict:
    """Pull the figures the UI shows out of gap.py's markdown report.

    Matched by their labels rather than by position, so adding a line to the
    report cannot silently shift which number is read as which.

    `master_coverage` and `tailored_coverage` are BOTH priority-term coverage,
    and are therefore comparable — that is the whole point. All-term coverage
    is deliberately not surfaced: gap.py itself says it is not comparable
    between postings."""
    out = {"master_coverage": None, "tailored_coverage": None,
           "priority_gaps": None, "dropped_priority": None}
    for line in report_md.splitlines():
        if line.startswith("- **Priority-term coverage:"):
            m = COVERAGE_RE.search(line)
            if m:
                out["master_coverage"] = float(m.group(1))
        elif line.startswith("- **Tailored priority-term coverage:"):
            m = COVERAGE_RE.search(line)
            if m:
                out["tailored_coverage"] = float(m.group(1))
        elif "Priority gaps you can't currently evidence:" in line:
            m = PRIORITY_GAP_RE.search(line)
            if m:
                out["priority_gaps"] = int(m.group(1))
        elif "Priority terms dropped in tailoring:" in line:
            m = DROPPED_PRIORITY_RE.search(line)
            if m:
                out["dropped_priority"] = int(m.group(1))
    return out


def write_gap_report(dest: Path, job: dict, posting: str, tailored_path: Path | None) -> dict:
    """Write gap-report.md and return the parsed coverage figures.

    The script is invoked with the venv interpreter and paths, exactly as a
    human would run it — the report is the same artifact either way, so a
    number the UI shows can always be reproduced by hand from the file it
    came from."""
    dest.mkdir(parents=True, exist_ok=True)
    post_file = dest / "post.md"
    post_file.write_text((posting or "(no posting text available)").strip() + "\n",
                         encoding="utf-8")
    out = dest / "gap-report.md"
    args = [str(SCRIPTS / "gap.py"), str(post_file), "-o", str(out),
            "--company", job.get("company") or "", "--title", job.get("title") or ""]
    if tailored_path is not None and tailored_path.exists():
        args += ["--tailored", str(tailored_path)]
    proc = _run_script(args)
    if proc.returncode != 0:
        return {"error": (proc.stderr or proc.stdout or "").strip()[:600],
                "master_coverage": None, "tailored_coverage": None,
                "priority_gaps": None, "dropped_priority": None}
    parsed = parse_coverage(out.read_text(encoding="utf-8"))
    parsed["path"] = str(out)
    return parsed


def render_cv_job(day_dir: str, company_slug: str, stem: str | None = None,
                  formats: str = "pdf,docx") -> dict:
    """Render one application's tailored.yaml into PDF + DOCX.

    `--outdir` is passed explicitly rather than left to the renderer's own
    date logic, so the folder this module decided on (and stored in the
    database) is the folder that gets the files. Without it the renderer would
    re-derive the path from job.yaml's date, which is the same value — but two
    sources of truth for a path is a bug waiting for the first hand-edited
    job.yaml.

    Exit 3 is the FABRICATION CHECK and is treated as a hard failure: the
    renderer writes nothing in that case, and this returns the reason instead
    of pretending a CV was produced."""
    dest = application_dir(day_dir, company_slug)
    tailored = dest / "tailored.yaml"
    if not tailored.exists():
        raise TailorError(f"No tailored.yaml at {tailored} — preview and approve first.")
    outdir = dest
    proc = _run_script([
        str(SCRIPTS / "render.py"), str(tailored),
        "--master", str(MASTER_PATH),
        "--outdir", str(outdir),
        "--stem", stem or master_inventory_stem(),
        "--formats", formats,
    ])
    if proc.returncode == 3:
        raise TailorError("The renderer's fabrication check FAILED, so no CV was written:\n"
                          + "\n".join(l for l in proc.stderr.splitlines() if l.strip().startswith("!!")))
    if proc.returncode != 0:
        raise TailorError(f"render.py exited {proc.returncode}:\n"
                          f"{(proc.stderr or proc.stdout).strip()[:800]}")

    stem = stem or master_inventory_stem()
    warnings = [l for l in proc.stderr.splitlines() if l.strip().startswith("??")]
    return {
        "outdir": str(outdir),
        "pdf_path": str(outdir / f"{stem}.pdf"),
        "docx_path": str(outdir / f"{stem}.docx"),
        "warnings": warnings,
    }


def render_master_inventory() -> dict:
    """Render the un-tailored master CV to the ROOT of cv_output/.

    ON DEMAND ONLY — never called by a tailoring run. Per Ed, 2026-09-23: the
    inventory is updated "when I have a new skill or something comes up from
    the tailored cv for a particular role", which is a human judgement about
    whether a fact is real. Regenerating it automatically after every job would
    turn a deliberate act into a side effect and, worse, make the inventory look
    current whenever it merely looked recent."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    stem = master_inventory_stem()
    proc = _run_script([
        str(SCRIPTS / "render.py"), str(FULL_INVENTORY),
        "--master", str(MASTER_PATH),
        "--outdir", str(OUTPUT_ROOT),
        "--stem", stem,
        "--formats", "pdf,docx",
    ])
    if proc.returncode != 0:
        raise TailorError(f"Master inventory render failed (exit {proc.returncode}):\n"
                          f"{(proc.stderr or proc.stdout).strip()[:800]}")
    return {
        "pdf_path": str(OUTPUT_ROOT / f"{stem}.pdf"),
        "docx_path": str(OUTPUT_ROOT / f"{stem}.docx"),
        "warnings": [l for l in proc.stderr.splitlines() if l.strip().startswith("??")],
    }


def master_fact_summary() -> dict:
    """Counts for the UI's master-status line — and a placeholder tripwire.

    A placeholder master (meta.placeholder: true) means every CV is fictional;
    it is surfaced here rather than discovered in a PDF."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import model  # type: ignore
        master = model.Master.load(MASTER_PATH)
        return {
            "roles": len(master.experience),
            "achievements": len(master.achievements_by_id),
            "skill_groups": len(master.skill_groups),
            "placeholder": bool(master.is_placeholder),
            "path": str(MASTER_PATH),
        }
    finally:
        sys.path.pop(0)


# ---------------------------------------------------------------------------
# Stage 1 — draft (local Qwen)
# ---------------------------------------------------------------------------

DRAFT_SCHEMA = {
    "name": "submit_tailored_cv",
    "description": "Submit the tailored CV as a YAML document, plus an account of what you changed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tailored_yaml": {
                "type": "string",
                "description": (
                    "The COMPLETE tailored.yaml document as YAML text. It must contain "
                    "`summary`, `skills`, and `experience`. Every `ref` must be an id that "
                    "appears in the master CV given to you."
                ),
            },
            "report": {
                "type": "string",
                "description": (
                    "Plain text: which roles and achievements you selected and why, every "
                    "bullet you reworded (with the master text it came from), which gaps the "
                    "posting requires that the master cannot evidence, and anything you were "
                    "unsure about. Be specific and honest, including about gaps."
                ),
            },
        },
        "required": ["tailored_yaml"],
    },
}

DRAFT_SYSTEM = """You are tailoring a CV for one specific job application.

You work with a repository that separates FACTS from PRESENTATION, and your job is to \
select and present facts that already exist. You are not writing a CV from scratch.

THE MODEL
- The MASTER CV given to you is the FACT BASE. It holds every role, achievement and skill \
the candidate actually has. It is the only source of truth available to you.
- What you write is a SELECTION over it: it selects, reorders and rewords. It never adds.

THE FIVE HARD RULES — violating any of these makes the output worse than useless, because \
it produces a CV that wins a screening and collapses in an interview.

1. NEVER add a skill, fact or technology that is not in the master. Every `ref` you write \
must be an id that literally appears in the master text given to you. A made-up ref makes \
the renderer refuse to build the CV.
2. NEVER invent a metric. If an achievement's `metrics:` is empty in the master, NO NUMBER \
EXISTS. Do not estimate, round, or extrapolate one. Most achievements have no number.
3. NEVER hide a missing must-have. If the posting requires something the master cannot \
evidence, say so in your report. Do not imply adjacent experience is the same thing.
4. REWORD FREELY, CHANGE FACTS NEVER. Using the posting's own noun for work the candidate \
already did is correct tailoring and is the main thing that gets a CV past an ATS. Claiming \
work they did not do is fabrication. Example of correct: master says "results tracked in \
Datadog dashboards", posting says "observability dashboards" — writing "results tracked in \
Datadog observability dashboards" is the SAME FACT in the posting's vocabulary. Example of \
fabrication: turning that into "built an observability platform".
5. Select AGGRESSIVELY. A focused CV beats a comprehensive one. The most relevant \
achievement goes FIRST under the current role, because the first bullet is the one that \
gets read — even when it is not the most impressive in the abstract. Drop roles and bullets \
that do not serve this application.

THE ONE MISTAKE THAT MATTERS MOST — DO NOT ADOPT THE EMPLOYER'S WORLD

A CV is a claim about what the candidate HAS DONE. The posting is a description of what the \
employer IS. The posting's own setting is the employer's — its sector, its products, its \
clients, its industry — and NONE of it is the candidate's history.

Rewording is about VOCABULARY: choosing the posting's noun for work the candidate already \
did. It is NEVER about ATTRIBUTION: moving the candidate's work into the employer's world. \
Those look similar sentence by sentence, and only one of them is true.

The failure to avoid, from a real draft for a public sector role at Kainos:
  master says:  "nine years at a FinTech employer across AML transaction monitoring,
                 sanctions screening and compliance case management"
  draft said:   "Experience in public sector domains including financial crime
                 compliance, AML transaction monitoring, sanctions screening and
                 compliance case management"
That draft kept every genuine activity and silently attached them to a sector the \
candidate has never worked in, because the posting was a public sector role. Every fact in \
it was real; the sentence was still a lie, and it would not survive the first interview \
question. This is the single most likely way this task goes wrong.

So, when the posting names a sector, industry, product, client type or regulatory regime \
that the MASTER does not evidence:

  * the posting's context is NOT the candidate's context. Do not write it into the summary, \
the skills or any bullet — not as an adjective describing their work.
  * leave it as a gap, and say so in your report. Gaps are expected and are handled in the \
interview; a claimed sector is not recoverable.
  * describe the candidate's real domain exactly as the master states it, even when that is \
less flattering for this posting. "Nine years in regulated financial services and FinTech" \
is stronger than a claim that dissolves under questioning.
  * relevant work does not transfer a label. Testing compliance systems in financial \
services is compliance-systems experience IN financial services — it is not "public sector", \
"government", "healthcare" or "defence" experience because the employer operates there.
  * the POSTING CONTEXT block below names the sectors this posting uses and the domains the \
master can actually support. Treat that as the boundary.

HOW TO USE THE GAP REPORT
The GAP REPORT lists the posting's requirement-level terms, which of them the master can \
evidence, and which it cannot. Treat it as your task list: lead with what closes the most \
important gaps. A term the master CANNOT evidence is a real gap — leave it a gap. Never \
close a gap by inventing the fact.

OUTPUT SHAPE — the YAML document you submit must have:

summary: >
  The rewritten professional summary. Start from the CLOSEST variant in the master's \
`summaries` and edit it so its first clause answers the posting's first requirement. Use \
only facts the master supports.
  KEEP IT SHORT — THREE TO FIVE LINES, roughly 60-90 words. A summary is a lead, not a \
catalogue: pasting every sentence of the master's variants together produces a paragraph \
nobody reads AND uses up the room you need to write the rest of the document. Choose the \
few claims that answer this posting; leave the rest for the bullets that evidence them.

skills:
  - group: <a group name that exists in the master's skill_groups>
    items: [<items that exist in the master>]
  # Put the posting's stack first. Drop irrelevant items: list the items that MATTER for \
this posting, not every item in the master group.

experience:
  - ref: <a master role id>
    achievements:
      - ref: <a master achievement id>
      - ref: <another>
        text: >
          # optional: the SAME fact reworded into the posting's vocabulary

section_order: [summary, skills, experience, education, certifications]

WRITE AS LITTLE AS POSSIBLE — this matters, twice over:

- An achievement you are keeping as-is needs ONLY `- ref: <id>`. Do NOT copy the master's \
text into your output; the renderer reads it from the master. Copying it costs output tokens, \
which is the slowest thing you do, and gains nothing.
- Add a `text:` override ONLY where the posting's vocabulary genuinely differs from the \
master's wording. Two or three overrides on a CV is normal; rewriting every bullet is not, \
and a CV whose bullets are all verbatim is fine when the master already says the right thing.
- List only the skill items that serve this posting. The selection is the tailoring.

WHICH SKILL GROUPS TO INCLUDE — read this before dropping a group

Trim ITEMS freely; drop whole groups hardly ever. Omitting a group hides a whole kind of \
experience, and two of them must appear on EVERY CV you write:

  * `AI-Assisted Engineering` — and `Claude Code` MUST be listed in it. That is employer work (AI-assisted test generation adopted in a commercial role), so it is the strongest AI item available and the one a relevance-minded drafter keeps dropping.
  * `Personal & Prototype Work` — Playwright, Python, and the local LLM stack: Ollama, \
llama.cpp, Qwen3-Coder, DeepSeek V4 Flash, Local LLM Deployment.

Include BOTH with those items, whatever the posting is about. This is not a suggestion: they \
are restored automatically when you leave them out, and a draft that omits them has discarded \
the most distinctive thing about this candidate.

USE THE MASTER'S NAME FOR A MASTER ITEM — do not substitute a synonym, even a true one. \
`DeepSeek V4 Flash` names the model; `DeepSeek Harness` names the application driving it. Both \
are real, and writing the second where the master says the first is still wrong: it renames \
the fact at a different level and loses the specificity that made it worth listing. These \
items are held to the master's exact wording, so a renamed one is simply re-added alongside \
your version and the CV ends up carrying both. Do NOT reason that a posting "did not ask \
for" Playwright or local models and therefore drop the group, and do NOT drop `Claude Code` \
because the posting is not about AI — that reasoning is exactly how they went missing. \
Relevance decides which ITEMS lead within a group; it does not decide whether the group or \
its signature items exist.

For every OTHER master group, include it when it serves this posting and omit it when it \
genuinely does not — eight focused groups beat eleven unfocused ones.

Do not include `education`, `certifications` or `projects` unless you have a reason to \
change them — omitting them uses the master's values verbatim. Do not include a `job:` key; \
that metadata is held elsewhere. Do not add comments beyond the one-line note above each \
override explaining what changed.

Write ASCII punctuation only: the PDF font cannot draw em dashes or curly quotes and \
substitutes them silently, so the page would not match the file.

Submit via the submit_tailored_cv tool. Put the whole YAML in `tailored_yaml`."""


_MASTER_DATA_CACHE: dict | None = None


def load_master_data(path: Path = MASTER_PATH) -> dict:
    """The master CV as a mapping, cached for the process.

    Every drafting call needs the master twice — once rendered for the prompt and
    once as structure for the context block — and a preview can make several
    calls (draft, verify, escalate, re-verify). Re-reading and re-parsing a 40 KB
    YAML file each time is pure waste, so it is read once per process."""
    global _MASTER_DATA_CACHE
    if _MASTER_DATA_CACHE is None:
        _MASTER_DATA_CACHE = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return _MASTER_DATA_CACHE


def master_for_prompt(master_path: Path = MASTER_PATH) -> str:
    """The master CV, reshaped for a 32k-token window.

    Every id, every achievement text, every tag and every skill group is kept:
    they are what the model must cite and what it may draw on, so trimming them
    would change the task rather than shorten it. What is dropped is the things
    no model needs and that cost real tokens — `meta`, the summary that is not
    going to be used as a starting point, the key order the loader does not
    care about, and the extensive human-facing comments in the file itself.

    Written as YAML rather than prose because the master IS YAML: keeping the
    structure means "the ids you may cite" is legible at a glance, and a model
    that has seen the schema is far less likely to invent a ref shape."""
    data = yaml.safe_load(master_path.read_text(encoding="utf-8"))

    out: dict = {
        "contact": {"name": (data.get("contact") or {}).get("name", ""),
                    "headline": (data.get("contact") or {}).get("headline", "")},
        "summaries": data.get("summaries") or {},
        "skill_groups": [
            {"group": g.get("group"), "items": list(g.get("items") or [])}
            for g in (data.get("skill_groups") or [])
        ],
        "experience": [],
    }
    for role in data.get("experience") or []:
        entry = {"id": role.get("id"), "company": role.get("company"),
                 "title": role.get("title"), "start": role.get("start"), "end": role.get("end")}
        if role.get("context"):
            entry["context"] = " ".join(str(role["context"]).split())
        entry["achievements"] = [
            {
                "id": a.get("id"),
                "text": " ".join(str(a.get("text", "")).split()),
                **({"tags": list(a["tags"])} if a.get("tags") else {}),
                # metrics is included EVEN WHEN EMPTY, and the empty list is the
                # point: rule 2's "metrics: [] means no number exists" is only
                # actionable if the model can see which achievements have none.
                "metrics": list(a.get("metrics") or []),
                **({"impact": a["impact"]} if a.get("impact") else {}),
            }
            for a in (role.get("achievements") or [])
        ]
        out["experience"].append(entry)

    if data.get("education"):
        out["education"] = data["education"]
    if data.get("certifications"):
        out["certifications"] = data["certifications"]
    if data.get("projects"):
        out["projects"] = data["projects"]

    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=100)


def posting_context_block(job: dict, master_data: dict, gap_report: str) -> str:
    """The deterministic facts that bound what this CV may claim about CONTEXT.

    Modelled on app/ai_evaluate.py's `screening_signal_block`, and for the same
    reason: there are things a language model is simply worse at than a
    `for` loop, and the context of a claim is one of them. Rendering them as
    plain statements means the model spends its judgement on wording rather than
    on re-deriving facts, and the same posting cannot come out differently twice.

    This block exists because of one real, expensive failure — the Kainos
    public-sector draft that relabelled nine years of FinTech compliance work as
    "experience in public sector domains". Diagnosing it showed the information
    WAS available: the master's Domain group is FinTech-only, and gap.py had even
    listed "public"/"sector" as priority gaps. What the model was never told is
    the thing that makes the inference safe, which is that a gap in the POSTING's
    context is not a licence to re-file the candidate's history under it.

    Three facts, in the order they do the work:

      1. Where the candidate has actually worked, named. "Have you worked here?"
         is the question the claim has to survive, and knowing the answer is
         "no, none of these" does most of the work on its own.
      2. The candidate's evidenced domains, exactly as the master states them.
      3. Which of the posting's requirement-level terms CANNOT be evidenced —
         the closable-by-rewording list. Naming what must stay a gap removes the
         ambiguity that let "public sector" read as available.
    """
    employers = [r.get("company", "") for r in master_data.get("experience") or []]
    domains: list[str] = []
    other_groups: list[str] = []
    for group in master_data.get("skill_groups") or []:
        name = str(group.get("group") or "")
        items = [str(i) for i in group.get("items") or []]
        if "domain" in name.lower():
            domains.extend(items)
        else:
            other_groups.append(name)

    lines: list[str] = []
    lines.append(f"- The candidate's employers are: {', '.join(e for e in employers if e)}.")
    lines.append(f"- The posting's employer is: {job.get('company') or '(not stated)'}. "
                 f"The candidate has NOT worked there, and has not worked for its clients.")
    if domains:
        lines.append(f"- The ONLY domains the master evidences: {', '.join(domains)}. "
                     f"Anything else a posting names is the employer's world, not the "
                     f"candidate's history.")
    lines.append(f"- The master's skill groups are: {', '.join(other_groups)}. "
                 f"A domain term absent from the list above cannot be claimed.")

    gaps = _priority_gap_terms(gap_report)
    if gaps:
        lines.append(f"- Requirement-level terms this posting uses that the master CANNOT "
                     f"evidence: {', '.join(gaps)}. These are GAPS. They stay gaps. Do not "
                     f"close one by rewording the candidate's experience into it — a term "
                     f"here that names a sector, industry, product or regulatory regime must "
                     f"not appear as a description of the candidate.")

    return ("POSTING CONTEXT (deterministic — these bound what this CV may claim about WHERE "
            "and FOR WHOM the candidate worked; use them, do not re-derive them):\n"
            + "\n".join(lines) + "\n")


def _priority_gap_terms(gap_report: str) -> list[str]:
    """The term column of gap.py's two priority-gap tables, in report order.

    Read from the report rather than recomputed, so the prompt and the report the
    user reads cannot disagree about what counts as a gap. Bounded to a sane
    number because this goes into a 32k-token window alongside the whole master."""
    terms: list[str] = []
    in_priority_table = False
    for line in gap_report.splitlines():
        if line.startswith("## Priority gaps"):
            in_priority_table = True
            continue
        if line.startswith("## ") and not line.startswith("## Priority gaps"):
            in_priority_table = False
            continue
        if not in_priority_table or not line.startswith("| "):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or cells[0] in ("Term", "") or set(cells[0]) <= {"-"}:
            continue
        if cells[0].startswith("_..."):
            continue
        terms.append(cells[0])
    return terms[:40]


def build_draft_prompt(job: dict, posting: str, gap_report: str, master_yaml: str,
                       feedback: str | None = None, master_data: dict | None = None) -> str:
    """Stage 1's user turn.

    ORDER IS ARGUMENT, not formatting. The POSTING CONTEXT block comes FIRST,
    before the advert text, because it is the boundary the rest must be read
    within — a model that meets the constraints after 3,000 words of the
    employer's own marketing has already absorbed that framing. (The Kainos
    draft is the evidence: the posting's public-sector language won.)

    Then the gap report, which is the task list, then the master, which is the
    material. Anything the verifier rejected on a previous attempt is appended
    last, where a model reading top-to-bottom acts on it."""
    trimmed_report = gap_report
    if len(trimmed_report) > 14000:
        # The report's gap tables are long-tailed; the headline numbers and the
        # priority-gap sections are what the drafting task needs. Truncation is
        # announced rather than silent so a thin draft can be traced back to it.
        trimmed_report = (trimmed_report[:14000]
                          + "\n\n[...gap report truncated here — the full report is written "
                            "beside the CV; the priority-gap sections above are complete...]\n")

    feedback_block = ""
    if feedback:
        feedback_block = f"""
---
A PREVIOUS ATTEMPT AT THIS CV WAS REJECTED. The verifier's findings were:

{feedback}

Fix every point above. If a finding says a bullet claims something the master does not
support, REWORD it to match the master's fact or REMOVE it. Do not argue with the finding,
and do not add anything to the master to make it true.
"""

    context = ""
    if master_data is not None:
        context = "\n" + posting_context_block(job, master_data, gap_report) + "\n---\n"

    return f"""Tailor the CV for this job posting.
{context}
JOB
Company: {job.get('company') or '(not stated)'}
Title: {job.get('title') or '(not stated)'}
Location: {job.get('location') or '(not stated)'}
URL: {job.get('url') or '(none)'}

POSTING TEXT
{posting or '(the posting text could not be retrieved — tailor on company, title and the gap report alone, and say so in your report)'}

---
GAP REPORT (what this posting demands, versus what the master can evidence)
{trimmed_report or '(no gap report could be generated — work from the posting and master directly)'}

---
MASTER CV — THE FACT BASE. Every `ref` you write must be an id below.
{master_yaml}
{feedback_block}
Submit the tailored CV via submit_tailored_cv."""


def _reserialise_with_groups(data: dict, original_text: str) -> str:
    """The draft as YAML text with re-inserted skill groups written back in.

    The drafter returns the document as a STRING, and that string is what gets
    stored, shown to the user and rendered — so a change made to the parsed dict
    is invisible unless the text is rebuilt. Re-serialising the whole document is
    what keeps the two in step.

    Verified value-preserving: every reworded `text:` override, every `ref`, the
    summary and the section order all survive the round trip (tested). The one
    thing PyYAML drops is COMMENTS, including the per-override notes the prompt
    asks the model to write. That is a real if small cost, and the audit trail
    does not rest on it: the model's `report` is stored separately, and the
    verifier independently checks every reword against the master — a stronger
    guarantee than a comment a model wrote about itself.

    Nothing is rebuilt when the drafter already included every required group, so
    a compliant draft keeps its comments verbatim."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100) or original_text


def parse_drafted_yaml(raw: str) -> dict:
    """Parse and sanity-check the model's YAML string.

    THE STRUCTURAL CHECKS HERE EXIST BECAUSE A TRUNCATED DRAFT PARSES CLEANLY.
    A response cut off at max_tokens mid-document is very often still valid
    YAML — `- ref: emp-1` with its `text:` block lopped off is a perfectly
    well-formed list item — so `yaml.safe_load` returns a document and the draft
    sails on to fail later, in the gap report or the renderer, as a confusing
    "achievement ref 'None'". That happened on the LEGO posting (2026-09-23):
    the model spent its budget on a 2,200-character summary, was cut off inside
    the experience section, and the first structural complaint surfaced two
    stages further downstream.

    So a ref that parsed as None is treated as what it almost always is — a
    truncation — and reported as such, which the caller retries with a larger
    budget. Raises rather than returning None, because the retry needs to tell
    the model WHAT was wrong and "it failed to parse" is not actionable."""
    text = raw.strip()
    # Models sometimes wrap YAML in a fence even when the schema asked for a
    # raw string; stripping it is safer than rejecting an otherwise good draft.
    if text.startswith("```"):
        text = re.sub(r"^```(?:ya?ml)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TailorError(f"The drafted YAML does not parse: {exc}")
    if not isinstance(data, dict):
        raise TailorError(f"The drafted YAML is a {type(data).__name__}, not a mapping.")
    if not (data.get("summary") or "").strip():
        raise TailorError("The drafted YAML has no `summary`, which is required.")
    experience = data.get("experience")
    if not experience:
        raise TailorError("The drafted YAML has no `experience` section.")

    # Structural completeness, not reference validity — whether a ref EXISTS is
    # _validate_against_master's job, and it has the master to check against.
    for i, role in enumerate(experience):
        if not isinstance(role, dict):
            raise TailorError(f"experience[{i}] is a {type(role).__name__}, not a mapping")
        if not role.get("ref"):
            raise TailorError(
                f"experience[{i}] has no `ref`. The document is most likely cut off "
                f"mid-write — write the summary more concisely and try again")
        # `achievements` ABSENT means "all of them" (the engine's rule), so only
        # an explicitly present list is checked.
        if role.get("achievements") is None:
            continue
        for j, ach in enumerate(role["achievements"]):
            ref = ach.get("ref") if isinstance(ach, dict) else ach
            if not ref:
                raise TailorError(
                    f"experience[{i}].achievements[{j}] has no `ref` (read as None). The "
                    f"document is most likely cut off mid-write — keep the summary short, "
                    f"omit text you are not changing, and try again")
    for i, group in enumerate(data.get("skills") or []):
        if not isinstance(group, dict) or not group.get("group"):
            raise TailorError(f"skills[{i}] has no `group` name")
    return data


def draft_cv(job: dict, posting: str, gap_report: str, provider=None,
             feedback: str | None = None, max_retries: int = 1) -> dict:
    """Stage 1: produce a tailored.yaml dict.

    A malformed YAML document is retried once with the parser's complaint fed
    back, the same way ai_evaluate.py retries a truncated tool call — a small
    model gets indentation or a stray fence wrong far more often than it gets
    the selection wrong, and failing a whole run over it would be the wrong
    trade. A retry that also fails raises, so a model that simply cannot emit
    YAML fails loudly instead of producing an empty CV."""
    provider = provider or llm_providers.build_provider(
        _env_choice(DRAFT_PROVIDER_ENV, DEFAULT_DRAFT_PROVIDER))
    master_yaml = master_for_prompt()
    master_data = load_master_data()
    user_content = build_draft_prompt(job, posting, gap_report, master_yaml, feedback,
                                       master_data=master_data)

    last_error = None
    for attempt in range(max_retries + 1):
        result = provider.complete(DRAFT_SYSTEM, user_content, DRAFT_SCHEMA, DRAFT_MAX_TOKENS)
        if result is None:
            detail = getattr(provider, "last_failure", None)
            last_error = (f"{provider.name} ({provider.model}) returned no submit_tailored_cv "
                          f"object" + (f" — {detail}" if detail else ""))
        else:
            try:
                data = parse_drafted_yaml(result.get("tailored_yaml") or "")
                # Enforcement, not a request: the drafter drops the AI and
                # local-LLM groups unless something holds them in place. Applied
                # here so BOTH the stored YAML and the preview show the same
                # document — correcting it later, at render time, would mean the
                # YAML a user approves is not the YAML that gets rendered.
                data, readded = merge_required_groups(data, master_data)
                yaml_text = result.get("tailored_yaml") or ""
                if readded:
                    yaml_text = _reserialise_with_groups(data, yaml_text)
                return {"data": data, "report": (result.get("report") or "").strip(),
                        "provider": provider.name, "model": provider.model,
                        "yaml_text": yaml_text, "readded_groups": readded}
            except TailorError as exc:
                last_error = str(exc)
        if attempt < max_retries:
            # Feed the failure back rather than repeating the identical prompt:
            # the whole reason a first attempt fails here is mechanical.
            user_content = build_draft_prompt(job, posting, gap_report, master_yaml,
                                              feedback=last_error, master_data=master_data)
            continue
    raise TailorError(f"Drafting failed after {max_retries + 1} attempt(s). Last problem: "
                      f"{last_error}")


# ---------------------------------------------------------------------------
# Stage 2 — verify (DeepSeek)
# ---------------------------------------------------------------------------

VERIFY_SCHEMA = {
    "name": "submit_verification",
    "description": "Report on a drafted tailored CV: whether it is faithful to the master CV.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["accept", "revise", "reject"],
                "description": (
                    "accept = every claim traces to a master fact and the selection serves this "
                    "posting. revise = usable but something specific must change (list it in "
                    "required_fixes). reject = it claims something the master does not support."
                ),
            },
            "unsupported_refs": {
                "type": "string",
                "description": (
                    "Every `ref` in the draft that is NOT an id in the master CV, quoted "
                    "exactly. Write 'none' if every ref is valid."
                ),
            },
            "fabricated_claims": {
                "type": "string",
                "description": (
                    "Every place the draft claims a skill, technology, responsibility or metric "
                    "that the master does not support — especially a rewritten bullet whose "
                    "meaning changed rather than its vocabulary. Quote the draft text and the "
                    "master text it should have stayed faithful to. Write 'none' if clean. Be "
                    "strict here: this is the check the whole system exists for."
                ),
            },
            "invented_metrics": {
                "type": "string",
                "description": (
                    "Any number in the draft that is not in the master achievement it comes "
                    "from. Write 'none' if there are no invented numbers."
                ),
            },
            "missing_must_haves": {
                "type": "string",
                "description": (
                    "The posting's stated requirements that the master cannot evidence, named "
                    "plainly. These are real gaps and must be reported, not buried. Write "
                    "'none' if there are none."
                ),
            },
            "required_fixes": {
                "type": "string",
                "description": "Specific, actionable fixes needed for a revise verdict. 'none' if accept.",
            },
            "notes": {
                "type": "string",
                "description": (
                    "Anything else the candidate should know before sending this: a role left "
                    "out that would have helped, a reworded bullet that is defensible but "
                    "needs care in an interview, a risk. 2-4 sentences."
                ),
            },
        },
        "required": ["verdict", "unsupported_refs", "fabricated_claims",
                     "invented_metrics", "missing_must_haves", "required_fixes", "notes"],
    },
}

VERIFY_SYSTEM = """You are checking a tailored CV for FABRICATION and for honesty about gaps, \
before it is sent to an employer. You are not the model that wrote it, and your job is to be \
the reason it cannot quietly overstate.

The candidate has a MASTER CV holding every fact they can legitimately claim. A tailored CV \
may select from it, reorder it, and reword it into the posting's vocabulary. It may NOT \
claim a skill, technology, responsibility or metric that the master does not support.

Judge each of these separately and strictly:

1. REFERENCE VALIDITY. Every `ref` in the draft must be an id that appears in the master. \
List any that do not.
2. FABRICATION. Compare every rewritten bullet against the master achievement it references. \
Rewording that keeps the same fact and adopts the posting's noun is CORRECT and must not be \
flagged. A change that widens what was done ("results tracked in dashboards" becoming "built a \
dashboards platform"), promotes a contribution ("supported" becoming "led"), or adds a \
technology the master never mentions IS fabrication and must be flagged with both texts quoted.
3. METRICS. Any number in the draft must appear in the master achievement it came from. \
Achievements whose `metrics` is empty have NO number, so a number attributed to one is invented.
4. MISSING MUST-HAVES. The posting's requirement-level demands that the master cannot \
evidence must be reported plainly. Do not soften them and do not let an adjacent skill stand \
in for one the posting actually asked for. A tailored CV that quietly omits a gap it cannot \
close is the failure this check exists to catch.

Do not be diplomatic and do not be encouraging. "Looks strong" is worthless here. If it is \
clean, say accept and write 'none' in each findings field. If you are unsure whether a \
reworded bullet still describes the same fact, flag it as a required fix — a fix costs one \
regeneration, a fabrication costs an interview.

Write ASCII punctuation only."""


def build_verify_prompt(job: dict, master_yaml: str, drafted_yaml_text: str,
                        posting: str) -> str:
    return f"""Check this tailored CV against the master CV it was built from.

JOB
Company: {job.get('company') or '(not stated)'}
Title: {job.get('title') or '(not stated)'}
URL: {job.get('url') or '(none)'}

POSTING TEXT — this is what the CV must not overstate relative to
{(posting or '(not available)')[:6000]}

---
MASTER CV — every id and fact the candidate can legitimately claim
{master_yaml}

---
DRAFTED TAILORED CV — CHECK THIS
{drafted_yaml_text}

---
Report via submit_verification. Check the refs, the rewrites, the numbers and the stated \
must-haves separately, and be strict about rewrites that changed what was actually done."""


def verify_cv(job: dict, draft: dict, posting: str, provider=None) -> dict:
    """Stage 2: check the draft against the master.

    The verdict is advisory to the USER, never a gate that silently discards
    work: a `revise` returns the findings and the draft together so a human
    decides whether to regenerate, edit by hand, or send it anyway. Only
    `render.py`'s fabrication check is a hard gate, because it is mechanical and
    cannot be argued with."""
    provider = provider or llm_providers.build_provider(
        _env_choice(VERIFY_PROVIDER_ENV, DEFAULT_VERIFY_PROVIDER))
    master_yaml = master_for_prompt()
    result = provider.complete(
        VERIFY_SYSTEM,
        build_verify_prompt(job, master_yaml, draft["yaml_text"], posting),
        VERIFY_SCHEMA,
        VERIFY_MAX_TOKENS,
    )
    if result is None:
        detail = getattr(provider, "last_failure", None)
        return {
            "verdict": "unknown",
            "verify_model": f"{provider.name}:{provider.model}",
            "notes": ("The verification call returned nothing usable, so this draft has NOT "
                      "been independently checked."
                      + (f" Reason: {detail}" if detail else "")),
            "unsupported_refs": "", "fabricated_claims": "", "invented_metrics": "",
            "missing_must_haves": "", "required_fixes": "",
        }
    result["verify_model"] = f"{provider.name}:{provider.model}"
    return result


# ---------------------------------------------------------------------------
# Stage 3 — interview prep (optional, on demand)
# ---------------------------------------------------------------------------

INTERVIEW_PREP_SYSTEM = """You are writing interview preparation for a candidate who is about \
to apply for a specific job, using their master CV, the tailored CV being sent, and the gap \
report for that posting.

The gap report is the most important input. A must-have the candidate cannot evidence is \
very likely to be the first thing an interviewer probes, so it goes in the FIRST section \
with an honest answer — not buried at the bottom, and not answered by implying an adjacent \
skill is the same thing.

Structure the document as:
1. **The gaps they will probe** — each stated requirement the candidate cannot evidence, \
with what they should actually say. Never suggest claiming experience they do not have; \
suggest how to describe the real adjacent experience honestly and what to say they would do \
to close it.
2. **Their strongest evidence for this role** — specific achievements from the tailored CV \
that answer this posting's requirements, each with the story and the detail worth having ready.
3. **Likely technical questions** — grounded in this posting's stack, with the candidate's \
real experience as the answer, including where their experience is thin.
4. **Questions to ask them** — specific to this posting and company, including anything the \
posting leaves ambiguous.
5. **Practical notes** — anything about the role, contract terms, clearance, location or \
seniority that they should resolve early.

Use only facts from the master and tailored CV. Do not invent experience, metrics or \
answers. Markdown, ASCII punctuation only. Do not describe the candidate as strong, \
compelling or impressive — be accurate, including about the gaps."""


def generate_interview_prep(job: dict, tailored_yaml_text: str, gap_report: str,
                            posting: str, provider=None) -> str:
    provider = provider or llm_providers.build_provider(
        _env_choice(VERIFY_PROVIDER_ENV, DEFAULT_VERIFY_PROVIDER))
    user = f"""Write the interview prep for this application.

JOB
Company: {job.get('company') or '(not stated)'}
Title: {job.get('title') or '(not stated)'}
URL: {job.get('url') or '(none)'}

POSTING TEXT
{(posting or '(not available)')[:12000]}

---
GAP REPORT
{(gap_report or '(not available)')[:10000]}

---
MASTER CV
{master_for_prompt()}

---
TAILORED CV BEING SENT
{tailored_yaml_text}

---
Write the prep document as markdown. Answer plainly; do not pad it."""
    schema = {
        "name": "submit_interview_prep",
        "description": "Submit the interview preparation document as markdown.",
        "input_schema": {
            "type": "object",
            "properties": {"markdown": {"type": "string",
                                        "description": "The complete prep document in markdown."}},
            "required": ["markdown"],
        },
    }
    result = provider.complete(INTERVIEW_PREP_SYSTEM, user, schema, 3000)
    if result is None:
        detail = getattr(provider, "last_failure", None)
        raise TailorError("The interview prep call returned nothing usable"
                          + (f" — {detail}" if detail else ""))
    return (result.get("markdown") or "").strip()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _env_choice(env_name: str, default: str) -> str:
    import os
    return (os.environ.get(env_name) or default).strip().lower()


def preflight(need_draft: bool = True) -> list[str]:
    """Blocking problems, as sentences. Empty list means go.

    Checked BEFORE any model call so a misconfiguration costs nothing and says
    what to do. The local-server check is the one that matters most in practice:
    a stopped llama-server otherwise surfaces as a connection error several
    seconds into a request the UI is waiting on."""
    problems: list[str] = []
    if not MASTER_PATH.exists():
        problems.append(f"No master CV at {MASTER_PATH}.")
    if not VENV_PY.exists():
        problems.append(f"No virtualenv at {VENV_PY} — see the note in _run_script.")
    if need_draft and _env_choice(DRAFT_PROVIDER_ENV, DEFAULT_DRAFT_PROVIDER) == "local":
        problem = llm_providers.check_local_available()
        if problem:
            problems.append(problem)
    try:
        if need_draft:
            llm_providers.build_provider(_env_choice(DRAFT_PROVIDER_ENV, DEFAULT_DRAFT_PROVIDER))
        llm_providers.build_provider(_env_choice(VERIFY_PROVIDER_ENV, DEFAULT_VERIFY_PROVIDER))
    except llm_providers.ProviderError as exc:
        problems.append(str(exc))
    return problems


def load_job(conn, url: str) -> dict:
    row = conn.execute(
        "SELECT url, company, title, location, description, posted_at, source "
        "FROM job_details WHERE url = ?", (url,)).fetchone()
    if row is None:
        raise TailorError(f"No job on the board with url {url!r}.")
    keys = ["url", "company", "title", "location", "description", "posted_at", "source"]
    return dict(zip(keys, row))


def _record_failure(conn, url: str, old: dict | None, error: str) -> None:
    """Record why a run failed, without losing what a previous run produced.

    `status` is what the board shows, so it must answer "is there a usable CV
    here?" rather than "did the last attempt succeed?". A failed re-render over
    a good existing CV leaves the files in place, so reporting the row as broken
    would be wrong — and worse, it would invite a full regeneration (another
    ~90s) to fix something that may be a typo in one ref.

    The error is always stored, whatever the status, so the message survives
    even when the status stays `rendered`."""
    still_usable = bool((old or {}).get("pdf_path"))
    dedup.save_tailoring(
        conn, url,
        status=("rendered" if still_usable else "failed"),
        error=error,
    )
    conn.commit()


def _verifier_critique(verification: dict) -> str:
    """The verifier's findings, as corrections for the next drafter.

    `required_fixes` leads because it is written as imperatives. The finding
    fields are appended because they carry the evidence — the draft text and the
    master text it should have stayed faithful to — and a model told only "you
    over-claimed the domain" has to guess which sentence to cut, whereas one
    shown both strings usually does not.

    `missing_must_haves` is deliberately EXCLUDED. Those are real gaps in the
    candidate's history, not drafting mistakes, and handing them to the drafter
    as things to fix invites exactly one outcome: inventing the experience. The
    verifier's own note on the Kainos draft made the point — the public-sector
    gap "must be removed rather than restated"."""
    parts: list[str] = []
    fixes = (verification.get("required_fixes") or "").strip()
    if fixes and not _is_none_text(fixes):
        parts.append(f"REQUIRED FIXES (do all of these):\n{fixes}")
    for key, label in (("fabricated_claims", "CLAIMS THAT ARE NOT IN THE MASTER CV"),
                       ("unsupported_refs", "REFS THAT DO NOT EXIST IN THE MASTER CV"),
                       ("invented_metrics", "INVENTED NUMBERS")):
        value = (verification.get(key) or "").strip()
        if value and not _is_none_text(value):
            parts.append(f"{label}:\n{value}")

    # Nothing actionable. `notes` is deliberately NOT a trigger: it is a free-text
    # observation field that says things like "Otherwise a faithful subset", so
    # treating its presence as a reason to re-draft would escalate on clean
    # verifications — and an `unknown` verdict (the checker returned nothing)
    # would spend a cloud call being told "Faithful." as a correction.
    if not parts:
        return ""
    notes = (verification.get("notes") or "").strip()
    if notes and not _is_none_text(notes):
        parts.append(f"OTHER OBSERVATIONS:\n{notes}")
    return ("\n\n".join(parts)
            + "\n\nEvery fix above must be applied by REWORDING or REMOVING. Do not add any "
              "fact, skill, technology or metric to close a gap — a gap the master cannot "
              "evidence stays a gap, and the interview prep is where it gets handled.")


def _is_none_text(value: str) -> bool:
    """True for the schema's 'none' placeholder, which is not a finding."""
    return bool(re.fullmatch(r"(none|n/?a|no|nothing)[.!]?", (value or "").strip(), re.IGNORECASE))


def _escalate_draft(job: dict, posting: str, gap_report: str, verification: dict,
                    first_draft: dict, verify_provider, before: dict) -> dict | None:
    """Re-draft with the stronger provider, using the verifier's corrections.

    Returns None when escalation is not possible, and that is a normal outcome
    rather than an error: the cloud key may not be configured, or the escalation
    itself may fail. The first draft and its verdict are then shown as-is, which
    is still a usable outcome — the user can edit the YAML, or act on the
    verifier's `required_fixes` by hand.

    The escalation is NOT silently trusted either: its output is verified again,
    and it is kept only when the new verdict is genuinely better. Without that,
    a retry that produced something worse would replace a rejected-but-fixable
    draft with a rejected-and-unfixable one, and the verdict shown would be
    describing the wrong document."""
    critique = _verifier_critique(verification)
    if not critique:
        return None

    try:
        cloud = llm_providers.build_provider("deepseek")
    except llm_providers.ProviderError as exc:
        # No cloud key. Say so in the reason rather than failing — the caller
        # surfaces it so the user knows the local verdict is final, not pending.
        return {"draft": first_draft, "verification": verification,
                "reason": f"Not escalated: {exc}"}

    try:
        second = draft_cv(job, posting, gap_report, provider=cloud, feedback=critique)
    except (TailorError, llm_providers.ProviderError) as exc:
        return {"draft": first_draft, "verification": verification,
                "reason": f"Escalated to {cloud.name} but its draft failed: {exc}"}

    recheck = verify_cv(job, second, posting, provider=verify_provider)

    # Keep the escalation only if it improved matters. Ordered worst-to-best so
    # a single comparison decides it.
    rank = {"reject": 0, "unknown": 1, "revise": 2, "accept": 3}
    if rank.get(recheck.get("verdict"), 1) <= rank.get(verification.get("verdict"), 1):
        return {"draft": first_draft, "verification": verification,
                "reason": (f"Escalated to {cloud.name}, but its draft verified no better "
                           f"({recheck.get('verdict')} vs {verification.get('verdict')}), "
                           f"so the original is shown")}

    first_verdict = verification.get("verdict")
    now_verdict = recheck.get("verdict")
    # Past-tense forms written out rather than built with + "ed": the first
    # attempt produced "now reviseed", which is the kind of small wrongness that
    # makes a user distrust the surrounding text.
    past = {"reject": "rejected", "revise": "flagged for changes", "accept": "accepted",
            "unknown": "unverified"}
    return {"draft": second, "verification": recheck,
            "reason": (f"The {first_draft['provider']} draft was {past.get(first_verdict, first_verdict)} "
                       f"by the verifier, so it was re-drafted with {cloud.name} using those "
                       f"corrections — now {past.get(now_verdict, now_verdict)}")}


def preview(conn, url: str, feedback: str | None = None, include_interview_prep: bool = False,
            draft_provider=None, verify_provider=None) -> dict:
    """Draft + verify, and write NOTHING into cv_output/.

    Everything the user needs to judge the draft is returned and stored: the
    YAML, the verifier's findings, the coverage numbers before and after, and
    the list of posting terms the master cannot evidence — which is the signal
    for whether cv/master.yaml is missing something real. That last one is the
    documented trigger for updating the master, and it is surfaced rather than
    acted on: only a human can say whether a term describes work they did."""
    problems = preflight(need_draft=True)
    if problems:
        raise TailorError("Cannot tailor a CV yet:\n- " + "\n- ".join(problems))

    job = load_job(conn, url)
    posting = _post_text(job)
    record = _existing_record(conn, url)
    day_dir, company_slug = _day_and_slug(record, job)
    dest = application_dir(day_dir, company_slug)

    # Master-only coverage first. This is what the draft has to improve on, and
    # it is computed BEFORE any model runs, so it is also the honest "before"
    # figure in the UI rather than a number derived from the model's own output.
    #
    # The report is written to a scratch directory OUTSIDE the repo: previewing
    # must leave no application folder behind, and a preview that is never
    # approved should leave no trace anywhere that looks like an application.
    scratch = Path(tempfile.mkdtemp(prefix="cv-preview-"))
    try:
        before = write_gap_report(scratch, job, posting, None)

        draft = draft_cv(job, posting, _read_report(before), provider=draft_provider,
                         feedback=feedback)
        verification = verify_cv(job, draft, posting, provider=verify_provider)
        attempts = [{
            "model": f"{draft['provider']}:{draft['model']}",
            "verdict": verification.get("verdict"),
            "reason": None,
        }]

        # ESCALATION. The local model writes most CVs well, but it has one
        # characteristic failure: it adopts the POSTING's framing as if it were
        # the candidate's history. Verified on the Kainos public-sector role
        # (2026-09-23) — the draft relabelled nine years of FinTech compliance
        # work as "experience in public sector domains", which is exactly the
        # claim that wins a screening and collapses at interview.
        #
        # When the verifier rejects the draft, the choice is not "ship it or
        # lose it": a stronger model is available, and the verifier has already
        # written the corrections. So the rejected draft is retried in the cloud
        # WITH those corrections. The local model stays the default because it
        # is free and usually right; the cloud is paid for only on the drafts
        # that need it, and only when its key is actually configured.
        #
        # `revise` also escalates, not just `reject`: a revise means the
        # verifier found something specific that must change, and regenerating
        # on a stronger model is more likely to fix it than showing the user a
        # to-do list.
        if verification.get("verdict") in ("reject", "revise"):
            escalation = _escalate_draft(job, posting, _read_report(before), verification,
                                         draft, verify_provider, before)
            if escalation is not None:
                draft = escalation["draft"]
                verification = escalation["verification"]
                attempts.append({
                    "model": f"{draft['provider']}:{draft['model']}",
                    "verdict": verification.get("verdict"),
                    "reason": escalation["reason"],
                })

        # Re-run the report against the draft so the "after" figure measures the
        # real thing. The draft is written to the scratch dir and read back by
        # gap.py, which parses it with the SAME loader the renderer uses — so a
        # draft with a dangling ref fails here, before the user is shown a
        # coverage figure it cannot deliver.
        (scratch / "tailored.yaml").write_text(draft["yaml_text"].strip() + "\n",
                                               encoding="utf-8")
        # A report that cannot be built for a structurally valid draft means the
        # engine itself rejected it, so raise rather than returning None
        # coverage: the UI would render "— → —" and a verifier verdict beside
        # it, which reads as "measured and poor" instead of "not measured".
        after = write_gap_report(scratch, job, posting, scratch / "tailored.yaml")
        if after.get("error"):
            raise TailorError(
                "The tailored draft was rejected by the CV engine, so no coverage could be "
                "measured and nothing was written:\n" + str(after["error"])[:700])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    interview_prep_md = None
    if include_interview_prep:
        interview_prep_md = generate_interview_prep(
            job, draft["yaml_text"], _read_report(after), posting, provider=verify_provider)

    dedup.save_tailoring(
        conn, url,
        status="draft",
        day_dir=day_dir,
        company_slug=company_slug,
        tailored_yaml=draft["yaml_text"].strip() + "\n",
        report=draft["report"],
        interview_prep_md=interview_prep_md,
        draft_model=f"{draft['provider']}:{draft['model']}",
        verify_model=verification.get("verify_model"),
        verify_verdict=verification.get("verdict"),
        verify_notes=json.dumps({k: v for k, v in verification.items()
                                 if k not in ("verify_model", "verdict")}, indent=2),
        master_coverage=before.get("master_coverage"),
        tailored_coverage=after.get("tailored_coverage"),
        error=None,
    )
    conn.commit()

    return {
        "url": url,
        "status": "draft",
        "day_dir": day_dir,
        "company_slug": company_slug,
        "future_outdir": str(dest),
        "tailored_yaml": draft["yaml_text"],
        "report": draft["report"],
        "draft_model": f"{draft['provider']}:{draft['model']}",
        "verification": verification,
        # The full provenance: which model drafted, in what order, and why the
        # first attempt was set aside. Without it, a CV that arrived after an
        # escalation looks like the local model's work when it is not — and the
        # point of escalating is defeated if the user cannot tell it happened.
        "attempts": attempts,
        "coverage_before": before.get("master_coverage"),
        "coverage_after": after.get("tailored_coverage"),
        "priority_gaps": after.get("priority_gaps"),
        "gap_report": _read_report(after),
        "interview_prep_md": interview_prep_md,
        "posting_available": bool(posting),
    }


def _read_report(parsed: dict) -> str:
    """gap.py's markdown, from the file it just wrote."""
    path = parsed.get("path")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    if parsed.get("error"):
        return f"(gap report failed: {parsed['error']})"
    return ""


def render(conn, url: str, tailored_yaml: str | None = None,
           include_interview_prep: bool = False, draft_provider=None,
           verify_provider=None) -> dict:
    """Write the approved CV to disk and render it.

    `tailored_yaml` is the text the user approved — possibly edited in the
    preview. When it is supplied it is re-verified, because an edit is a new
    claim and the verifier's previous verdict was about different text. The
    record is updated either way, so the stored draft is always the artifact
    that was rendered and the two can never drift."""
    old = _existing_record(conn, url)
    if old is None and not tailored_yaml:
        raise TailorError("Nothing has been previewed for this job yet.")
    job = load_job(conn, url)
    posting = _post_text(job)
    day_dir, company_slug = _day_and_slug(old, job)
    dest = application_dir(day_dir, company_slug)

    yaml_text = (tailored_yaml or (old or {}).get("tailored_yaml") or "").strip()
    if not yaml_text:
        raise TailorError("No tailored YAML to render.")

    edited = bool(tailored_yaml) and tailored_yaml.strip() != ((old or {}).get("tailored_yaml") or "").strip()

    # Validate through the ENGINE's own loader BEFORE writing anything. This is
    # what turns "a dangling ref" from a renderer crash into a message naming
    # the offending ref, and it happens before a single byte reaches cv_output/.
    data = parse_drafted_yaml(yaml_text)
    validation_error = _validate_against_master(data)

    verification = None
    if edited and not validation_error:
        # An edited draft gets re-checked: the verdict on file was about the
        # text the user changed. Skipped when validation already failed, since
        # there is nothing sendable to verify.
        verification = verify_cv(job, {"yaml_text": yaml_text, "data": data}, posting,
                                 provider=verify_provider)

    if validation_error:
        # A refusal must NOT erase the draft. The draft cost ~90 seconds of
        # model time, and it is usually one bad ref away from being fine — so
        # the record keeps its YAML, its verification and its coverage, and the
        # status only reports the failure when there is no rendered CV to fall
        # back on. Without this, one click against a stale draft makes a
        # perfectly good existing CV look broken AND throws the draft away,
        # forcing a full regeneration to fix a typo.
        _record_failure(conn, url, old, error=validation_error)
        raise TailorError(validation_error)

    dest.mkdir(parents=True, exist_ok=True)
    _write_job_metadata(dest, job, day_dir)
    (dest / "tailored.yaml").write_text(yaml_text + "\n", encoding="utf-8")

    gap = write_gap_report(dest, job, posting, dest / "tailored.yaml")

    try:
        rendered = render_cv_job(day_dir, company_slug)
    except TailorError as exc:
        # The renderer refused or crashed. Nothing usable was produced THIS
        # time, so record the failure — but keep any previously rendered CV's
        # status, because those files are still on disk and still sendable.
        _record_failure(conn, url, old, error=str(exc))
        raise

    # Interview prep is the LAST step and its failure is NOT fatal, on purpose.
    # By this point the CV itself has been built, so an extra model call falling
    # over must not present a finished CV as a failed run — or, worse, leave the
    # application folder looking half-built. It is reported as a warning and can
    # be retried on its own.
    prep_path = None
    prep_md = (old or {}).get("interview_prep_md")
    prep_warning = None
    if include_interview_prep and not prep_md:
        try:
            prep_md = generate_interview_prep(job, yaml_text, _read_report(gap), posting,
                                              provider=verify_provider)
        except (TailorError, llm_providers.ProviderError) as exc:
            prep_md = None
            prep_warning = (f"The CV rendered, but the interview prep call failed: {exc}")
    if prep_md:
        prep = dest / "interview-prep.md"
        prep.write_text(prep_md.rstrip() + "\n", encoding="utf-8")
        prep_path = str(prep)

    manifest = {
        "url": url,
        "company": job.get("company"),
        "title": job.get("title"),
        "prepared": day_dir,
        "draft_model": (old or {}).get("draft_model"),
        "verify_model": (verification or {}).get("verify_model") or (old or {}).get("verify_model"),
        "verify_verdict": (verification or {}).get("verdict") or (old or {}).get("verify_verdict"),
        "coverage_master_before": gap.get("master_coverage"),
        "coverage_tailored_after": gap.get("tailored_coverage"),
        "priority_gaps": gap.get("priority_gaps"),
        "posting_text_available": bool(posting),
        "files": {
            "pdf": rendered["pdf_path"],
            "docx": rendered["docx_path"],
            "tailored_yaml": str(dest / "tailored.yaml"),
            "gap_report": gap.get("path"),
            "interview_prep": prep_path,
        },
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    dedup.save_tailoring(
        conn, url,
        status="rendered",
        day_dir=day_dir,
        company_slug=company_slug,
        tailored_yaml=yaml_text + "\n",
        interview_prep_md=prep_md,
        outdir=str(dest),
        pdf_path=rendered["pdf_path"],
        docx_path=rendered["docx_path"],
        gap_report_path=gap.get("path"),
        interview_prep_path=prep_path,
        verify_verdict=manifest["verify_verdict"],
        verify_notes=(json.dumps(verification, indent=2) if verification
                      else (old or {}).get("verify_notes")),
        master_coverage=gap.get("master_coverage"),
        tailored_coverage=gap.get("tailored_coverage"),
        error=None,
    )
    conn.commit()

    return {
        "status": "rendered",
        "outdir": str(dest),
        "pdf_path": rendered["pdf_path"],
        "docx_path": rendered["docx_path"],
        "gap_report_path": gap.get("path"),
        "interview_prep_path": prep_path,
        "coverage_before": gap.get("master_coverage"),
        "coverage_after": gap.get("tailored_coverage"),
        "priority_gaps": gap.get("priority_gaps"),
        "warnings": (rendered.get("warnings") or []) + ([prep_warning] if prep_warning else []),
        "verification": verification,
    }


def _validate_against_master(data: dict) -> str | None:
    """Run the engine's own reference validation. None if valid, else a
    sentence naming every problem.

    `model.Tailored.validate()` raises on the FIRST dangling ref, which is fine
    for a CLI but unhelpful in a UI: the user would fix one ref, re-run, and
    meet the next. Every ref is collected here so one regeneration can fix
    them all — and the error text names both the bad ref and the ids that
    exist, which is what makes a retry prompt actionable."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import model  # type: ignore
        master = model.Master.load(MASTER_PATH)
    except Exception as exc:  # a broken master is the operator's problem
        return f"cv/master.yaml could not be loaded: {exc}"
    finally:
        sys.path.pop(0)

    problems: list[str] = []
    for role in data.get("experience") or []:
        if not isinstance(role, dict):
            problems.append(f"an experience entry is a {type(role).__name__}, not a mapping")
            continue
        rid = role.get("ref")
        if rid not in master.roles_by_id:
            problems.append(f"experience ref '{rid}' is not a role id in cv/master.yaml "
                            f"(valid role ids: {', '.join(sorted(master.roles_by_id))})")
            continue
        for ach in role.get("achievements") or []:
            aid = ach.get("ref") if isinstance(ach, dict) else ach
            if aid not in master.achievements_by_id:
                problems.append(f"achievement ref '{aid}' (under role '{rid}') is not an "
                                f"achievement id in cv/master.yaml")

    for group in data.get("skills") or []:
        if isinstance(group, dict) and not group.get("group"):
            problems.append("a skills entry has no `group` name")

    if not problems:
        return None
    return ("The tailored CV references facts the master cannot support, so nothing was "
            "rendered:\n- " + "\n- ".join(problems))


# ---------------------------------------------------------------------------
# CLI — how the web app drives this
# ---------------------------------------------------------------------------

def _emit(payload: dict) -> None:
    """One JSON object on stdout, always, so the caller parses one thing.

    The trailing newline matters for the humans who run this from a shell: a
    JSON object butted directly against the next prompt is genuinely awkward to
    read, and `python -c 'json.load(sys.stdin)'`-style piping is unaffected."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tailor the CV for one job posting.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("preview", help="Draft + verify; writes no CV files.")
    p.add_argument("--url", required=True)
    p.add_argument("--feedback", default=None,
                   help="Regenerate using this critique (e.g. the verifier's required_fixes).")
    p.add_argument("--interview-prep", action="store_true")
    p.add_argument("--provider", default=None, help="Override the drafting provider.")

    r = sub.add_parser("render", help="Write and render the approved CV.")
    r.add_argument("--url", required=True)
    r.add_argument("--yaml-file", type=Path, default=None,
                   help="The approved YAML (an edit of the preview). Omit to use the stored draft.")
    r.add_argument("--interview-prep", action="store_true")

    sub.add_parser("master-inventory", help="Re-render the master full inventory (on demand only).")

    m = sub.add_parser("master-status", help="Counts and placeholder state for cv/master.yaml.")
    m.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    if args.cmd in ("master-inventory", "master-status"):
        try:
            if args.cmd == "master-inventory":
                _emit({"ok": True, **render_master_inventory()})
            else:
                _emit({"ok": True, **master_fact_summary()})
            return 0
        except TailorError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1

    with dedup.connect() as conn:
        try:
            if args.cmd == "preview":
                result = preview(conn, args.url, feedback=args.feedback,
                                 include_interview_prep=args.interview_prep,
                                 draft_provider=args.provider)
                _emit({"ok": True, **result})
            else:
                yaml_text = None
                if args.yaml_file:
                    yaml_text = args.yaml_file.read_text(encoding="utf-8")
                result = render(conn, args.url, tailored_yaml=yaml_text,
                                include_interview_prep=args.interview_prep)
                _emit({"ok": True, **result})
            return 0
        except TailorError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1
        except llm_providers.ProviderError as exc:
            _emit({"ok": False, "error": str(exc)})
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
