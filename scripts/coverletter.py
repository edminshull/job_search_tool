#!/usr/bin/env python3
"""Render a cover letter to PDF, styled to match the CV.

Input format — a Markdown file where:

  * the first `# ` heading and a `**Key:** value` block up to the first `---`
    are metadata (printed as a small header block, not sent to the employer)
  * everything after the first `---` is the letter body, one paragraph per
    blank-line-separated block
  * the name and contact line come from cv/master.yaml, so they never drift

Deliberately not a general Markdown renderer. A cover letter is one page of
prose; anything more elaborate belongs in the CV pipeline.

Usage:
    python scripts/coverletter.py jobs/<slug>/cover-letter.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import Master, load_yaml, warn_if_placeholder
from render import ARCHIVE_NAME, archive_existing, latin1_safe, output_dir_for

FONT = "Helvetica"
BODY_SIZE = 10.5
BODY_LEADING = 5.0


def parse(path: Path) -> tuple[str, list[str]]:
    """Return (metadata block, body paragraphs).

    The separator is a horizontal rule **on a line of its own**. Splitting on the
    first `---` SUBSTRING instead was a real defect, and a nasty one: the
    editor's note that sits above the separator explains the convention, so it
    contains the literal characters `---` in prose ("Everything below the `---`
    is the letter body"). The split therefore happened INSIDE the note, and the
    tail of the note plus a literal `---` were printed into the letter. The Acme Digital Play
    cover letter shipped that way; see the self-test.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    split_at = next((i for i, ln in enumerate(lines) if ln.strip() == "---"), None)
    if split_at is None:
        raise SystemExit(
            f"error: {path} has no '---' separator line before the body"
        )
    head = "\n".join(lines[:split_at])
    body = "\n".join(lines[split_at + 1:])

    # Only `**Key:** value` lines become metadata. Anything else in the header
    # area is a note to the reader/editor and must NOT reach the sent letter --
    # an earlier version printed the file's own instructions into the PDF.
    import re
    meta_lines = []
    for line in head.splitlines():
        line = line.strip()
        if re.match(r"^\*\*[^*]+:\*\*", line):
            meta_lines.append(line)

    paragraphs = [p.strip().replace("\n", " ") for p in body.split("\n\n") if p.strip()]
    if not paragraphs:
        raise SystemExit(f"error: {path} has no body paragraphs")

    # A leaked editor's note is the failure this function exists to prevent, so
    # refuse to render rather than quietly send one to an employer. A blockquote
    # or a stray rule in the body means either a note got past the separator or
    # the separator itself was missed.
    leaked = [q for q in paragraphs
              if q.lstrip().startswith(">") or q.strip() == "---"]
    if leaked:
        raise SystemExit(
            f"error: {path}: the letter body contains a stray blockquote or "
            f"separator, which usually means an editor's note leaked: "
            f"{leaked[0][:70]!r}"
        )
    return "\n".join(meta_lines), paragraphs


def render_pdf(master: Master, meta: str, paragraphs: list[str], path: Path) -> None:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    contact = master.contact
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin

    dark = (26, 26, 26)
    grey = (70, 70, 70)

    # Header — matches the CV so the two documents look like a set.
    pdf.set_font(FONT, "B", 16)
    pdf.set_text_color(*dark)
    pdf.cell(width, 8, latin1_safe(str(contact.get("name", ""))),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    bits = [contact.get("location"), contact.get("phone"), contact.get("email")]
    for link in contact.get("links") or []:
        bits.append(link.get("url") or link.get("label") if isinstance(link, dict) else link)
    pdf.set_font(FONT, "", 9)
    pdf.set_text_color(*grey)
    pdf.cell(width, 4.5, latin1_safe("  |  ".join(str(b) for b in bits if b)),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    y = pdf.get_y() + 1.5
    pdf.set_draw_color(150, 150, 150)
    pdf.set_line_width(0.2)
    pdf.line(pdf.l_margin, y, pdf.l_margin + width, y)
    pdf.set_y(y + 4)

    # Metadata block (not sent to the employer) — small and greyed.
    if meta:
        pdf.set_font(FONT, "", 8)
        pdf.set_text_color(120, 120, 120)
        for line in meta.splitlines():
            line = line.replace("**", "").strip()
            if line:
                pdf.multi_cell(width, 4, latin1_safe(line),
                               new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(4)

    pdf.set_font(FONT, "", BODY_SIZE)
    pdf.set_text_color(*dark)
    for i, para in enumerate(paragraphs):
        pdf.multi_cell(width, BODY_LEADING, latin1_safe(para),
                       new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        if i < len(paragraphs) - 1:
            pdf.ln(3.2)

    if pdf.get_y() > pdf.h - 30:
        print("?? WARNING: this letter is running past one page. Tighten it.",
              file=sys.stderr)

    path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(path))


def main() -> int:
    ap = argparse.ArgumentParser(description="Render a cover letter to PDF.")
    ap.add_argument("letter", type=Path, help="Path to jobs/<slug>/cover-letter.md")
    ap.add_argument("--master", type=Path, default=None)
    ap.add_argument("--outdir", type=Path, default=None,
                    help="Exact output directory. Defaults to the same "
                         "output/<DD-MM-YY>/<job-slug>/ the CV uses.")
    ap.add_argument("--stem", default=None, help="Filename stem. Defaults to <Name>-Cover-Letter.")
    ap.add_argument("--no-archive", action="store_true")
    args = ap.parse_args()

    if not args.letter.exists():
        print(f"error: {args.letter} not found", file=sys.stderr)
        return 2

    master = Master.load(args.master) if args.master else Master.load()
    warn_if_placeholder(master)

    meta, paragraphs = parse(args.letter)
    name = (master.contact.get("name") or "CV").replace(" ", "-")
    # Same date directory as the CV, resolved by the SAME function, so a cover
    # letter can never be filed apart from the CV it belongs to. That shared
    # resolver is the point: two independent copies of this path logic is how
    # they drift.
    sibling = args.letter.parent / "job.yaml"
    job_meta = load_yaml(sibling) if sibling.exists() else {}
    outdir = args.outdir or output_dir_for(args.letter, job_meta)
    stem = args.stem or f"{name}-Cover-Letter"
    path = outdir / f"{stem}.pdf"

    if not args.no_archive:
        moved = archive_existing(path, outdir / ARCHIVE_NAME,
                                 date.today().strftime("%Y-%m-%d-%H%M%S"))
        if moved:
            print(f"Archived {moved}")

    render_pdf(master, meta, paragraphs, path)
    words = sum(len(p.split()) for p in paragraphs)
    print(f"Wrote {path}  ({path.stat().st_size:,} bytes, {words} words, "
          f"{len(paragraphs)} paragraphs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
