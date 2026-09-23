#!/usr/bin/env python3
"""Render a tailored CV to Markdown, DOCX and PDF.

Deliberately ATS-hostile-feature-free: no tables, no text boxes, no columns, no
headers/footers, no images, no glyphs a parser chokes on. Just single-column
text with standard section headings, which is what resume parsers actually
read reliably.

Usage:
    python scripts/render.py jobs/acme-data-eng/tailored.yaml
    python scripts/render.py jobs/acme-data-eng/tailored.yaml --formats docx,pdf
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import Master, Tailored, load_yaml, warn_if_placeholder, OUTPUT_DIR

# ---------------------------------------------------------------------------
# Plain-text model (single source for all three renderers)
# ---------------------------------------------------------------------------

# Blank line inserted between roles in Professional Experience. Two units because
# the PDF works in millimetres and DOCX in points; 7pt is ~2.5mm. Tune here.
ROLE_GAP_MM = 2.6
ROLE_GAP_PT = 7.0

SECTION_TITLES = {
    "summary": "Professional Summary",
    "skills": "Core Skills",
    "experience": "Professional Experience",
    "education": "Education",
    "certifications": "Certifications",
    "projects": "Selected Projects",
}


def contact_line(master: Master) -> str:
    c = master.contact
    bits = [c.get("location"), c.get("phone"), c.get("email")]
    for link in c.get("links") or []:
        if isinstance(link, dict):
            bits.append(link.get("url") or link.get("label"))
        else:
            bits.append(str(link))
    return "  |  ".join(str(b) for b in bits if b)


def edu_lines(master: Master, entries: list[dict]) -> list[tuple[str, str]]:
    """Normalise heterogeneous education/cert entries into (heading, detail)."""
    out = []
    for e in entries or []:
        if not isinstance(e, dict):
            out.append((str(e), ""))
            continue
        head_bits = [
            e.get("qualification") or e.get("degree") or e.get("name") or "",
            e.get("institution") or e.get("school") or e.get("issuer") or "",
        ]
        head = ", ".join(b for b in head_bits if b)
        dates = e.get("dates") or " - ".join(
            str(e[k]) for k in ("start", "end") if e.get(k)
        )
        detail_bits = [b for b in (dates, e.get("grade"), e.get("detail"), e.get("notes")) if b]
        out.append((head, "  |  ".join(str(b) for b in detail_bits)))
    return out


def build_text(master: Master, tailored: Tailored) -> list[tuple[str, object]]:
    """Return an ordered list of (kind, payload) blocks for rendering."""
    blocks: list[tuple[str, object]] = []
    blocks.append(("name", master.contact.get("name", "")))
    headline = tailored.display_headline()
    blocks.append(("headline", headline))
    blocks.append(("contact", contact_line(master)))

    for section in tailored.section_order:
        if section == "summary":
            blocks.append(("section", SECTION_TITLES["summary"]))
            blocks.append(("para", tailored.summary))
        elif section == "skills":
            groups = tailored.resolved_skills()
            if groups:
                blocks.append(("section", SECTION_TITLES["skills"]))
                for g in groups:
                    blocks.append(("skillrow", (g["group"], ", ".join(g["items"]))))
        elif section == "experience":
            roles = tailored.resolved_experience()
            if roles:
                blocks.append(("section", SECTION_TITLES["experience"]))
                for i, role in enumerate(roles):
                    if i:
                        # Blank line between roles. Not after the last one — the
                        # next section heading already brings its own space, and
                        # doubling up left a gap before Education.
                        blocks.append(("spacer", None))
                    blocks.append(("role", (role["title"], role["company"], role["dates"])))
                    if role.get("summary_line"):
                        blocks.append(("para", role["summary_line"]))
                    for b in role["bullets"]:
                        blocks.append(("bullet", b["text"]))
        elif section == "education":
            rows = edu_lines(master, tailored.resolved_education())
            if rows:
                blocks.append(("section", SECTION_TITLES["education"]))
                for head, detail in rows:
                    blocks.append(("edu", (head, detail)))
        elif section == "certifications":
            rows = edu_lines(master, tailored.resolved_certifications())
            if rows:
                blocks.append(("section", SECTION_TITLES["certifications"]))
                for head, detail in rows:
                    blocks.append(("edu", (head, detail)))
        elif section == "projects":
            if tailored.resolved_projects():
                blocks.append(("section", SECTION_TITLES["projects"]))
                for p in tailored.resolved_projects():
                    blocks.append(("role", (p.get("name", ""), p.get("context", ""), "")))
                    if p.get("description"):
                        blocks.append(("para", p["description"]))
    return blocks


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(master: Master, tailored: Tailored) -> str:
    lines: list[str] = []
    for kind, payload in build_text(master, tailored):
        if kind == "name":
            lines += [f"# {payload}", ""]
        elif kind == "headline":
            if payload:
                lines += [f"**{payload}**", ""]
        elif kind == "contact":
            lines += [str(payload), ""]
        elif kind == "section":
            lines += ["", f"## {payload}", ""]
        elif kind == "para":
            lines += [str(payload), ""]
        elif kind == "skillrow":
            group, items = payload  # type: ignore[misc]
            lines.append(f"- **{group}:** {items}")
        elif kind == "role":
            title, company, dates = payload  # type: ignore[misc]
            head = " — ".join(b for b in (title, company) if b)
            lines += ["", f"### {head}" + (f"  \n_{dates}_" if dates else "")]
        elif kind == "bullet":
            lines.append(f"- {payload}")
        elif kind == "edu":
            head, detail = payload  # type: ignore[misc]
            lines.append(f"- **{head}**" + (f" — {detail}" if detail else ""))
        elif kind == "spacer":
            lines.append("")
    return "\n".join(lines).strip() + "\n"


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

def render_docx(master: Master, tailored: Tailored, path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    normal.paragraph_format.space_after = Pt(1.5)
    normal.paragraph_format.space_before = Pt(0)

    for section in doc.sections:
        section.top_margin = Inches(0.5)
        section.bottom_margin = Inches(0.5)
        section.left_margin = Inches(0.6)
        section.right_margin = Inches(0.6)

    def bottom_border(paragraph) -> None:
        pPr = paragraph._p.get_or_add_pPr()
        borders = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), "6")
        bottom.set(qn("w:space"), "2")
        bottom.set(qn("w:color"), "999999")
        borders.append(bottom)
        pPr.append(borders)

    for kind, payload in build_text(master, tailored):
        if kind == "name":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(str(payload))
            r.bold = True
            r.font.size = Pt(18)
            p.paragraph_format.space_after = Pt(1)
        elif kind == "headline":
            if not payload:
                continue
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(str(payload))
            r.font.size = Pt(11)
            r.font.color.rgb = RGBColor(0x33, 0x33, 0x33)
            p.paragraph_format.space_after = Pt(1)
        elif kind == "contact":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(str(payload))
            r.font.size = Pt(9)
            r.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
            p.paragraph_format.space_after = Pt(4)
        elif kind == "section":
            # Heading 2 rather than Normal, so the document has a real outline.
            # The DOCX exists to be EDITED by hand, and a flat document of Normal
            # paragraphs gives Word's navigation pane nothing to show and no named
            # style to restyle against. ATS parsers that read document structure
            # also do better with real headings. The visual formatting is set on
            # the run explicitly so the look is unchanged.
            p = doc.add_paragraph(style="Heading 2")
            r = p.add_run(str(payload).upper())
            r.bold = True
            r.font.name = "Calibri"
            r.font.size = Pt(10.5)
            r.font.color.rgb = RGBColor(0x1a, 0x1a, 0x1a)
            p.paragraph_format.space_before = Pt(7)
            p.paragraph_format.space_after = Pt(3)
            bottom_border(p)
        elif kind == "para":
            p = doc.add_paragraph(str(payload))
            p.paragraph_format.space_after = Pt(3)
        elif kind == "skillrow":
            group, items = payload  # type: ignore[misc]
            p = doc.add_paragraph(style="List Bullet")
            r = p.add_run(f"{group}: ")
            r.bold = True
            p.add_run(str(items))
            p.paragraph_format.space_after = Pt(0.5)
        elif kind == "role":
            title, company, dates = payload  # type: ignore[misc]
            # Heading 3 nests roles under the experience heading in Word's
            # navigation pane, which is what makes the editable DOCX navigable.
            p = doc.add_paragraph(style="Heading 3")
            r = p.add_run(str(title))
            r.bold = True
            r.font.name = "Calibri"
            r.font.size = Pt(11)
            r.font.color.rgb = RGBColor(0, 0, 0)
            if company:
                p.add_run(f" \u2014 {company}")
            if dates:
                tab = p.add_run("\t" + str(dates))
                tab.font.size = Pt(9.5)
                tab.font.color.rgb = RGBColor(0x44, 0x44, 0x44)
            p.paragraph_format.space_before = Pt(5)
            p.paragraph_format.space_after = Pt(1.5)
            from docx.enum.text import WD_TAB_ALIGNMENT
            p.paragraph_format.tab_stops.add_tab_stop(Inches(7.1), WD_TAB_ALIGNMENT.RIGHT)
        elif kind == "bullet":
            p = doc.add_paragraph(str(payload), style="List Bullet")
            p.paragraph_format.space_after = Pt(1.5)
        elif kind == "spacer":
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.line_spacing = Pt(ROLE_GAP_PT)
        elif kind == "edu":
            head, detail = payload  # type: ignore[misc]
            p = doc.add_paragraph(style="List Bullet")
            r = p.add_run(str(head))
            r.bold = True
            if detail:
                p.add_run(f" — {detail}")
            p.paragraph_format.space_after = Pt(1)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

UNICODE_FIXES = {
    "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " ",
    "\u2022": "-", "\u2192": "->", "\u00b7": "-",
}


def latin1_safe(text: str) -> str:
    """fpdf2 core fonts are latin-1; strip anything that would break parsing."""
    for bad, good in UNICODE_FIXES.items():
        text = text.replace(bad, good)
    return text.encode("latin-1", "replace").decode("latin-1")


def _payload_strings(obj: object) -> list[str]:
    """Every string inside a render block payload, whatever its shape."""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, (list, tuple)):
        return [s for item in obj for s in _payload_strings(item)]
    if isinstance(obj, dict):
        return [s for value in obj.values() for s in _payload_strings(value)]
    return []


def unrenderable_chars(master: Master, tailored: Tailored) -> list[tuple[str, str]]:
    """Characters in the rendered text that the PDF font will not draw literally.

    `latin1_safe` deliberately substitutes these — em-dash to "-", curly quotes to
    straight, ellipsis to "..." — which is the correct choice for a core font. The
    problem was that nothing SAID SO. Fourteen of sixteen CVs carried " — " in
    their summary and " - " on the page, and the difference surfaced only when the
    human read the PDF and spotted it.

    Anything not in UNICODE_FIXES and outside latin-1 is worse: the encode
    fallback turns it into "?", so a character can become a literal question mark
    in a CV. This returns each distinct character with the block it appeared in,
    so the warning can be acted on.
    """
    found: dict[str, str] = {}
    for kind, payload in build_text(master, tailored):
        for text in _payload_strings(payload):
            for ch in text:
                if ch in UNICODE_FIXES or ch.encode("latin-1", "ignore") == b"":
                    found.setdefault(ch, kind)
    return sorted(found.items())


def render_pdf(master: Master, tailored: Tailored, path: Path) -> None:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    pdf = FPDF(format="A4", unit="mm")
    pdf.set_margins(15, 12, 15)
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    dark = (26, 26, 26)
    grey = (70, 70, 70)

    def line(text: str, size: float, style: str = "", color=grey,
             h: float = 4.35, gap: float = 0.0) -> None:
        pdf.set_font("Helvetica", style, size)
        pdf.set_text_color(*color)
        pdf.multi_cell(width, h, latin1_safe(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if gap:
            pdf.ln(gap)

    for kind, payload in build_text(master, tailored):
        if kind == "name":
            pdf.set_font("Helvetica", "B", 20)
            pdf.set_text_color(*dark)
            pdf.cell(width, 9, latin1_safe(str(payload)), align="C",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif kind == "headline":
            if payload:
                pdf.set_font("Helvetica", "", 11.5)
                pdf.set_text_color(*grey)
                pdf.cell(width, 5.5, latin1_safe(str(payload)), align="C",
                         new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif kind == "contact":
            pdf.set_font("Helvetica", "", 9)
            pdf.set_text_color(*grey)
            pdf.cell(width, 5, latin1_safe(str(payload)), align="C",
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.ln(2.5)
        elif kind == "section":
            pdf.ln(2.2)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(*dark)
            pdf.cell(width, 5.5, latin1_safe(str(payload).upper()),
                     new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            y = pdf.get_y() + 0.6
            pdf.set_draw_color(150, 150, 150)
            pdf.set_line_width(0.2)
            pdf.line(pdf.l_margin, y, pdf.l_margin + width, y)
            pdf.ln(2)
        elif kind == "para":
            line(str(payload), 10, "", grey, 4.6, 1.5)
        elif kind == "skillrow":
            group, items = payload  # type: ignore[misc]
            pdf.set_font("Helvetica", "B", 9.5)
            pdf.set_text_color(*dark)
            pdf.cell(pdf.get_string_width(latin1_safe(f"{group}: ")) + 0.6, 4.35,
                     latin1_safe(f"{group}: "))
            pdf.set_font("Helvetica", "", 9.5)
            pdf.set_text_color(*grey)
            pdf.multi_cell(width - pdf.get_x() + pdf.l_margin, 4.35,
                           latin1_safe(str(items)),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif kind == "role":
            title, company, dates = payload  # type: ignore[misc]
            pdf.ln(0.8)
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(*dark)
            head = str(title) + (f" - {company}" if company else "")
            pdf.cell(width, 5, latin1_safe(head), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if dates:
                pdf.set_font("Helvetica", "", 9)
                pdf.set_text_color(*grey)
                pdf.cell(width, 4.4, latin1_safe(str(dates)),
                         new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif kind == "spacer":
            pdf.ln(ROLE_GAP_MM)
        elif kind == "bullet":
            pdf.set_font("Helvetica", "", 9.5)
            pdf.set_text_color(*grey)
            pdf.set_x(pdf.l_margin + 4)
            pdf.multi_cell(width - 4, 4.35, latin1_safe("- " + str(payload)),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        elif kind == "edu":
            head, detail = payload  # type: ignore[misc]
            pdf.set_font("Helvetica", "B", 9.5)
            pdf.set_text_color(*dark)
            pdf.set_x(pdf.l_margin + 4)
            text = str(head) + (f" - {detail}" if detail else "")
            pdf.multi_cell(width - 4, 4.35, latin1_safe("- " + text),
                           new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


# ---------------------------------------------------------------------------

ARCHIVE_NAME = "archived"

# Applications are filed under the date they were prepared:
#   output/21-09-26/<slug>/Ed-Minshull-CV.pdf
# UK day-first order, because that is how the dates are read in this repo. It
# does NOT sort chronologically across years — 21-09-26 sorts before 03-10-26 —
# so if the file browser ordering starts to matter, switch DATE_DIR_FORMAT to
# "%Y-%m-%d" and re-run the migration.
DATE_DIR_FORMAT = "%d-%m-%y"
UNDATED_DIR = "archive"


def output_dir_for(tailored_path: Path, job_meta: dict) -> Path:
    """The output directory for one tailored CV.

    Jobs live in `output/<DD-MM-YY>/<slug>/`; anything that is not an
    application lives somewhere else, and both exceptions are deliberate.

    **The date comes from the job's own `job.yaml` `date:` field, never from the
    clock.** Rendering an old CV must not move it into today's folder — that
    would scatter one application's artefacts across two dates and make
    "what did I send them, and when?" unanswerable, which is the whole point of
    the per-job archive. `newjob.py` stamps `date:` with today's date when the
    folder is created, so a job prepared now naturally lands in today's
    directory.

    Two exceptions:
      * **Anything outside `jobs/`** (e.g. `cv/full-inventory.tailored.yaml`) is
        a reference document rather than an application, and stays top-level
        undated at `output/<slug>/`. Filing the baseline under a date would imply
        it was sent to someone.
      * **A job with no `job.yaml`, or an unparseable date**, cannot be dated.
        It goes to `output/archive/<slug>/` rather than being labelled with a
        date nobody can vouch for.
    """
    slug = job_slug(tailored_path)
    if tailored_path.parent.parent.name != "jobs":
        return OUTPUT_DIR / slug
    raw = str(job_meta.get("date") or "").strip()
    # Several formats are accepted because the field is hand-edited: `date:` is
    # written as ISO by newjob.py, but someone fixing a date by hand may write it
    # day-first, and silently dumping a datable job into archive/ would be worse
    # than accepting both.
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d-%m-%y", "%d/%m/%Y", "%d/%m/%y"):
        try:
            when = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return OUTPUT_DIR / when.strftime(DATE_DIR_FORMAT) / slug
    return OUTPUT_DIR / UNDATED_DIR / slug


def job_slug(tailored_path: Path) -> str:
    """Directory name for a job.

    jobs/<slug>/tailored.yaml -> <slug>, so each application gets its own output
    directory and every CV can carry the same filename. Falls back to the
    tailored file's own name for anything outside jobs/
    (e.g. cv/full-inventory.tailored.yaml -> full-inventory).
    """
    parent = tailored_path.parent
    if parent.parent.name == "jobs":
        return parent.name
    name = tailored_path.name
    for suffix in (".tailored.yaml", ".tailored.yml", ".yaml", ".yml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return parent.name or "misc"


def archive_existing(path: Path, archive_dir: Path, stamp: str) -> Path | None:
    """Move a superseded render aside before it is overwritten.

    Rendering is destructive by default: a re-tailor silently replaces the PDF
    you may already have sent to an employer. Archiving keeps the previous
    version so you can answer "what did I actually send them?" months later.
    """
    if not path.exists():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{path.stem}-{stamp}{path.suffix}"
    n = 1
    while target.exists():                      # two renders in the same second
        target = archive_dir / f"{path.stem}-{stamp}-{n}{path.suffix}"
        n += 1
    path.rename(target)
    return target


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a tailored CV.")
    ap.add_argument("tailored", type=Path, help="Path to jobs/<slug>/tailored.yaml")
    ap.add_argument("--master", type=Path, default=None)
    ap.add_argument("--formats", default="pdf,docx",
                    help="Comma-separated: pdf,docx,md (default: pdf,docx).")
    ap.add_argument("--outdir", type=Path, default=None,
                    help="Exact output directory. Defaults to "
                         "output/<DD-MM-YY>/<job-slug>/ for a job, and "
                         "output/<job-slug>/ for the un-dated baseline.")
    ap.add_argument("--stem", default=None,
                    help="Output filename stem. Defaults to <Name>-CV.")
    ap.add_argument("--no-archive", action="store_true",
                    help="Overwrite the previous render instead of archiving it.")
    ap.add_argument("--force", action="store_true",
                    help="Render even if the fabrication check fails (not recommended).")
    args = ap.parse_args()

    master = Master.load(args.master) if args.master else Master.load()
    warn_if_placeholder(master)
    tailored = Tailored.load(args.tailored, master)

    unsupported = tailored.unsupported_claims()
    if unsupported:
        print("!! FABRICATION CHECK FAILED — this tailored CV claims things the master "
              "CV does not support:", file=sys.stderr)
        for problem in unsupported:
            print(f"!!   - {problem}", file=sys.stderr)
        print("!! Fix jobs/*/tailored.yaml or add the real evidence to cv/master.yaml, "
              "then re-render.\n", file=sys.stderr)
        if not args.force:
            return 3

    empty_roles = tailored.roles_without_bullets()
    if empty_roles:
        print(f"?? EMPTY ROLES — these will render as a title with no bullets: "
              f"{', '.join(empty_roles)}", file=sys.stderr)
        print("?? Either list achievement refs for them, or remove the role. "
              "Omitting the `achievements` key entirely means 'all'.\n", file=sys.stderr)

    summary_drift = tailored.unsupported_summary_terms()
    if summary_drift:
        print("?? SUMMARY CHECK — the summary uses skill terms the master CV does not "
              "evidence:", file=sys.stderr)
        print(f"??   {', '.join(summary_drift)}", file=sys.stderr)
        print("?? Prose can't be machine-checked, so this is a warning and not a failure. "
              "Confirm each term is a\n?? fair characterisation of work recorded in "
              "cv/master.yaml, or reword the summary.\n", file=sys.stderr)

    # Characters the PDF cannot draw are silently SUBSTITUTED, not refused. The
    # core Helvetica font has no em-dash, so every " — " in a summary came out as
    # " - " across 14 of 16 CVs before anyone noticed — the text on the page was
    # not the text in the file. A silent substitution is the same failure class as
    # the heading bugs: the output looks plausible and is wrong. This makes it
    # loud.
    unrenderable = unrenderable_chars(master, tailored)
    if unrenderable:
        print("?? GLYPH CHECK — these characters cannot be drawn by the PDF font and "
              "will be substituted:", file=sys.stderr)
        for ch, where in unrenderable:
            print(f"??   {ch!r} (U+{ord(ch):04X}) in {where}", file=sys.stderr)
        print("?? Replace them with ASCII punctuation in the source, or switch the "
              "renderer to an embedded\n?? Unicode font. Do not leave them: what is "
              "drawn is not what the file says.\n", file=sys.stderr)

    name = (master.contact.get("name") or "CV").replace(" ", "-")
    job_meta: dict = {}
    sibling = args.tailored.parent / "job.yaml"
    if sibling.exists():
        job_meta = load_yaml(sibling)

    # Each job gets its own directory inside a date directory, so every CV can
    # carry the same filename while applications stay grouped by when they were
    # prepared. That keeps the name you send clean and consistent, and puts all
    # of an application's artefacts in one place. See `output_dir_for` for where
    # the date comes from and the two documented exceptions. `stem` in job.yaml
    # remains an escape hatch for the rare case a different filename is wanted.
    outdir = args.outdir or output_dir_for(args.tailored, job_meta)
    archive_dir = outdir / ARCHIVE_NAME
    stem = args.stem or job_meta.get("stem") or f"{name}-CV"

    formats = [f.strip().lower() for f in args.formats.split(",") if f.strip()]
    outdir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    written: list[Path] = []
    for fmt in formats:
        p = outdir / f"{stem}.{fmt}"
        if not args.no_archive:
            moved = archive_existing(p, archive_dir, stamp)
            if moved:
                print(f"Archived {moved}")
        if fmt == "md":
            p.write_text(render_markdown(master, tailored), encoding="utf-8")
        elif fmt == "docx":
            render_docx(master, tailored, p)
        elif fmt == "pdf":
            render_pdf(master, tailored, p)
        else:
            print(f"!! unknown format '{fmt}' (expected pdf, docx or md)",
                  file=sys.stderr)
            continue
        written.append(p)

    for p in written:
        print(f"Wrote {p}  ({p.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
