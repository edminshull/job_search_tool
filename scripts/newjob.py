#!/usr/bin/env python3
"""Scaffold a new job folder from a LinkedIn posting.

Creates jobs/<slug>/ containing:
    post.md    the raw posting text, exactly as pasted
    job.yaml   structured metadata (company, title, url, source, date)

Then tailoring proceeds by writing tailored.yaml into the same folder.

Usage:
    python scripts/newjob.py --company "Northwind" --title "Senior Data Engineer" \
        --url "https://..." --file /tmp/post.txt
    pbpaste | python scripts/newjob.py --company "Northwind" --title "Senior Data Engineer"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model import CVError, JOBS_DIR, load_yaml, slugify

# Values are emitted as JSON strings, because a JSON string is also a valid YAML
# string. Interpolating them bare was a real bug: `--source "Total Jobs (agency:
# Anson McCade)"` produced `source: Total Jobs (agency: Anson McCade)`, and a
# colon inside an unquoted scalar is a YAML syntax error ("mapping values are not
# allowed here"). Any of these four fields could carry one -- a title like
# "QA Engineer: Payments" would break it the same way -- so all four are quoted
# rather than only the one that happened to bite first.
JOB_TEMPLATE = """\
# Metadata for this posting. Referenced by the gap report and by output naming.
slug: {slug}
company: {company}
title: {title}
url: {url}
source: {source}
date: "{today}"
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="Scaffold a job folder from a posting.")
    ap.add_argument("--company", required=True, help="Employer name.")
    ap.add_argument("--title", required=True, help="Role title as advertised.")
    ap.add_argument("--url", default="", help="Link to the posting.")
    ap.add_argument("--source", default="LinkedIn", help="Where it came from.")
    ap.add_argument("--slug", default=None, help="Override the folder name.")
    ap.add_argument("--file", type=Path, default=None,
                    help="File holding the posting text. Reads stdin if omitted.")
    ap.add_argument("--force", action="store_true", help="Overwrite an existing folder.")
    args = ap.parse_args()

    if args.file:
        if not args.file.exists():
            print(f"error: {args.file} not found", file=sys.stderr)
            return 2
        post_text = args.file.read_text(encoding="utf-8")
    else:
        post_text = sys.stdin.read()

    if not post_text.strip():
        print("error: no posting text supplied (pass --file or pipe it on stdin)",
              file=sys.stderr)
        return 2

    slug = args.slug or slugify(f"{args.company}-{args.title}")
    job_dir = JOBS_DIR / slug
    if job_dir.exists() and not args.force:
        print(f"error: {job_dir} already exists (use --force to overwrite)",
              file=sys.stderr)
        return 1
    job_dir.mkdir(parents=True, exist_ok=True)

    (job_dir / "post.md").write_text(post_text.strip() + "\n", encoding="utf-8")
    (job_dir / "job.yaml").write_text(
        JOB_TEMPLATE.format(
            slug=slug,
            company=json.dumps(args.company),
            title=json.dumps(args.title),
            url=json.dumps(args.url),
            source=json.dumps(args.source),
            today=date.today().isoformat(),
        ),
        encoding="utf-8",
    )

    print(f"Created {job_dir}/")
    print(f"  post.md   ({len(post_text.split()):,} words)")
    print("  job.yaml")
    print()
    print("Next:")
    print(f"  ./.venv/bin/python scripts/gap.py {job_dir}/post.md "
          f"-o {job_dir}/gap-report.md")
    print(f"  # then write {job_dir}/tailored.yaml and render it")
    # Validate what we just wrote, so a scaffolding bug surfaces here rather than
    # in the middle of a tailoring run. Reported as a clean error, not a
    # traceback: the cause is always this script's own output, not the user's.
    try:
        load_yaml(job_dir / "job.yaml")
    except CVError as exc:
        print(f"error: wrote invalid YAML to {job_dir}/job.yaml:\n{exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
