"""Data model for the CV tailoring pipeline.

Two documents drive everything:

  cv/master.yaml    The single source of truth. The COMPLETE achievement bank:
                    every role, every accomplishment, every skill. Deliberately
                    longer than any one CV -- per-job tailoring selects from it.

  jobs/<slug>/tailored.yaml
                    A per-job SELECTION + REWRITE layer. It never duplicates
                    facts; it references master entries by id and may override
                    wording. Everything traceable back to a real fact.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
MASTER_PATH = ROOT / "cv" / "master.yaml"
JOBS_DIR = ROOT / "jobs"
OUTPUT_DIR = ROOT / "output"


class CVError(Exception):
    """Raised for malformed CV data. Always includes the offending location."""


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CVError(f"File not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CVError(f"Invalid YAML in {path}:\n{exc}") from exc
    if not isinstance(data, dict):
        raise CVError(f"{path} must contain a YAML mapping at the top level.")
    return data


def index_by_id(items: list[dict], kind: str, where: str) -> dict[str, dict]:
    """Turn a list of id-bearing entries into a dict, rejecting dupes/blanks."""
    out: dict[str, dict] = {}
    for i, item in enumerate(items or []):
        if not isinstance(item, dict):
            raise CVError(f"{where}: {kind}[{i}] must be a mapping.")
        ident = item.get("id")
        if not ident:
            raise CVError(f"{where}: {kind}[{i}] is missing an 'id'.")
        if ident in out:
            raise CVError(f"{where}: duplicate {kind} id '{ident}'.")
        out[ident] = item
    return out


class Master:
    """The complete, canonical CV fact base."""

    def __init__(self, data: dict[str, Any], path: Path = MASTER_PATH):
        self.data = data
        self.path = path
        self.meta = data.get("meta") or {}
        self.contact = data.get("contact") or {}
        self.experience: list[dict] = data.get("experience") or []
        self.education: list[dict] = data.get("education") or []
        self.certifications: list[dict] = data.get("certifications") or []
        self.projects: list[dict] = data.get("projects") or []
        self.skill_groups: list[dict] = data.get("skill_groups") or []
        self.summaries: dict[str, str] = data.get("summaries") or {}

        self.roles_by_id = index_by_id(self.experience, "role", "master.experience")
        self.achievements_by_id: dict[str, dict] = {}
        self.achievement_role: dict[str, str] = {}
        for role in self.experience:
            for ach in index_by_id(
                role.get("achievements") or [], "achievement", f"master role '{role.get('id')}'"
            ).values():
                aid = ach["id"]
                if aid in self.achievements_by_id:
                    raise CVError(f"master: duplicate achievement id '{aid}'.")
                self.achievements_by_id[aid] = ach
                self.achievement_role[aid] = role["id"]

    @property
    def is_placeholder(self) -> bool:
        return bool(self.meta.get("placeholder"))

    @classmethod
    def load(cls, path: Path = MASTER_PATH) -> "Master":
        return cls(load_yaml(path), path)

    def all_fact_text(self) -> str:
        """Everything a keyword could legitimately be claimed to appear in."""
        parts: list[str] = []
        parts.extend(str(v) for v in self.contact.values() if isinstance(v, str))
        parts.extend(self.summaries.values())
        for group in self.skill_groups:
            parts.append(str(group.get("group", "")))
            parts.extend(str(i) for i in group.get("items") or [])
        for role in self.experience:
            for key in ("title", "company", "location", "context"):
                if role.get(key):
                    parts.append(str(role[key]))
            for ach in role.get("achievements") or []:
                parts.append(str(ach.get("text", "")))
                parts.extend(str(t) for t in ach.get("tags") or [])
        for edu in self.education:
            parts.extend(str(v) for v in edu.values() if isinstance(v, str))
        for cert in self.certifications:
            parts.extend(str(v) for v in cert.values() if isinstance(v, str))
        for proj in self.projects:
            parts.append(str(proj.get("name", "")))
            parts.append(str(proj.get("description", "")))
            parts.extend(str(t) for t in proj.get("tags") or [])
        return "\n".join(parts)


class Tailored:
    """A job-specific view over the master CV."""

    def __init__(self, data: dict[str, Any], master: Master, path: Path,
                 job_meta: dict[str, Any] | None = None):
        self.data = data
        self.master = master
        self.path = path
        # Job metadata lives in a sibling job.yaml so it is written once, not
        # duplicated between the posting record and the tailoring.
        self.job = job_meta or data.get("job") or {}
        self.summary: str = (data.get("summary") or "").strip()
        # Optional per-job headline. The master carries ONE headline, which is
        # right for the master but wrong for a posting in a different discipline:
        # sending "… | FinTech" to a children's-media or public-sector role leads
        # the most-read line of the CV with something the posting never asked
        # for. Tailoring may re-lead with a different subset of Ed's real
        # attributes; it may not assert a new one, which is why this string goes
        # through the same unsupported-term check as the summary.
        self.headline: str = (data.get("headline") or "").strip()
        self.section_order: list[str] = data.get("section_order") or [
            "summary",
            "skills",
            "experience",
            "education",
            "certifications",
        ]
        self.skills: list[dict] = data.get("skills") or []
        self.experience: list[dict] = data.get("experience") or []
        self.education: list[dict] = data.get("education") or []
        self.certifications: list[dict] = data.get("certifications") or []
        self.projects: list[dict] = data.get("projects") or []
        self.validate()

    @classmethod
    def load(cls, path: Path, master: Master | None = None) -> "Tailored":
        master = master or Master.load()
        data = load_yaml(path)
        sibling = path.parent / "job.yaml"
        meta = load_yaml(sibling) if sibling.exists() else None
        return cls(data, master, path, meta)

    def display_headline(self) -> str:
        """The headline to print: this job's override, else the master's."""
        if self.headline:
            return self.headline
        return str(self.master.contact.get("headline", "") or "")

    def validate(self) -> None:
        """Fail loudly on dangling references rather than silently dropping facts."""
        for role in self.experience:
            rid = role.get("ref")
            if rid not in self.master.roles_by_id:
                raise CVError(
                    f"{self.path.name}: experience ref '{rid}' is not in master.experience."
                )
            for ach in role.get("achievements") or []:
                aid = ach.get("ref") if isinstance(ach, dict) else ach
                if aid not in self.master.achievements_by_id:
                    raise CVError(
                        f"{self.path.name}: achievement ref '{aid}' (role '{rid}') "
                        f"is not in master."
                    )
        if not self.summary:
            raise CVError(f"{self.path.name}: 'summary' is required.")

    def resolved_experience(self) -> list[dict]:
        """Merge master facts with per-job wording overrides."""
        out = []
        for sel in self.experience:
            role = self.master.roles_by_id[sel["ref"]]
            ach_sels = sel.get("achievements")
            if ach_sels is None:
                # ABSENT means "every achievement for this role"; an explicit
                # empty list means none. Without this distinction a file that
                # named a role but no achievements rendered that role with zero
                # bullets — silently. That is how the full-inventory baseline
                # ended up containing no achievements at all, and it would hit
                # any tailored CV that listed a role without listing its bullets.
                ach_sels = [{"ref": a["id"]} for a in role.get("achievements") or []]
            bullets = []
            for ach_sel in ach_sels:
                if isinstance(ach_sel, str):
                    ach_sel = {"ref": ach_sel}
                ach = self.master.achievements_by_id[ach_sel["ref"]]
                bullets.append(
                    {
                        "id": ach["id"],
                        "text": (ach_sel.get("text") or ach.get("text", "")).strip(),
                        "metrics": ach.get("metrics") or [],
                    }
                )
            out.append(
                {
                    "id": role["id"],
                    "title": sel.get("title") or role.get("title", ""),
                    "company": sel.get("company") or role.get("company", ""),
                    "location": sel.get("location") or role.get("location", ""),
                    "dates": format_dates(role),
                    "summary_line": sel.get("summary_line") or role.get("context", ""),
                    "bullets": bullets,
                }
            )
        return out

    def roles_without_bullets(self) -> list[str]:
        """Roles that will render with no achievements. Almost always a mistake."""
        return [r["id"] for r in self.resolved_experience() if not r["bullets"]]

    def resolved_skills(self) -> list[dict]:
        """Per-job skill groups; falls back to the full master groups."""
        if not self.skills:
            return [
                {"group": g.get("group", ""), "items": list(g.get("items") or [])}
                for g in self.master.skill_groups
            ]
        out = []
        for group in self.skills:
            if isinstance(group, str):
                group = {"group": group, "items": []}
            items = group.get("items")
            if items is None and group.get("ref"):
                match = next(
                    (g for g in self.master.skill_groups if g.get("group") == group["ref"]),
                    None,
                )
                items = list(match.get("items") or []) if match else []
            out.append({"group": group.get("group") or group.get("ref", ""), "items": items or []})
        return [g for g in out if g["items"]]

    def resolved_education(self) -> list[dict]:
        return self.education or self.master.education

    def resolved_certifications(self) -> list[dict]:
        return self.certifications or self.master.certifications

    def resolved_projects(self) -> list[dict]:
        return self.projects or []

    def unsupported_summary_terms(self) -> list[str]:
        """Skill terms asserted in the summary with no backing in the master.

        The skills list is machine-checkable and hard-fails. Prose is not, so this
        is a warning rather than a failure — but it is the surface where a model
        is most likely to drift into claiming a technology from the posting
        rather than from your history. Every flagged term is a question to
        answer, not automatically an error.
        """
        import keywords as kw

        if not self.summary and not self.headline:
            return []
        # The headline is prose too, and it is the line a screener reads first,
        # so it gets the same treatment as the summary.
        terms, _ = kw.analyse_post("\n".join(p for p in (self.headline, self.summary) if p))
        master_text = self.master.all_fact_text()
        _, missing = kw.coverage(terms, master_text)
        flagged = [t.term for t in missing if t.is_skill]
        return sorted(flagged)

    def unsupported_claims(self) -> list[str]:
        """Skill items in the tailored CV with no backing in the master.

        Tailoring is allowed to reword, reorder and drop. It is not allowed to
        invent. This is the guard that keeps that promise: anything listed under
        this job's skills must be traceable to a master skill, project tag, or
        achievement tag.
        """
        import keywords as kw

        master_vocab: set[str] = set()

        def add(text: str) -> None:
            """Record a master claim, both whole and token-wise.

            Token-wise matters: the master lists 'Maven Failsafe Plugin', and a
            tailored CV that says 'Maven' is narrowing a claim the master already
            makes, not inventing one. Comparing whole strings alone would flag
            that as fabrication.
            """
            norm = kw.normalise(str(text)).strip()
            if not norm:
                return
            master_vocab.update(kw.variants(norm))
            for token in kw.tokens_of(str(text)):
                master_vocab.update(kw.variants(token))

        for group in self.master.skill_groups:
            for item in group.get("items") or []:
                add(item)
        for ach in self.master.achievements_by_id.values():
            for tag in ach.get("tags") or []:
                add(tag)
            for metric in ach.get("metrics") or []:
                add(metric)
        for proj in self.master.projects:
            add(proj.get("name", ""))
            for tag in proj.get("tags") or []:
                add(tag)

        problems: list[str] = []
        for group in self.skills:
            if not isinstance(group, dict):
                continue
            for item in group.get("items") or []:
                text = str(item).strip()
                if not text:
                    continue
                variants = kw.variants(kw.normalise(text).strip())
                if variants & master_vocab:
                    continue
                # A multi-word item counts as supported if every meaningful
                # token is somewhere in the master vocabulary.
                tokens = [t for t in kw.normalise(text).split() if t]
                if tokens and all(kw.variants(t) & master_vocab for t in tokens):
                    continue
                problems.append(
                    f"skill '{text}' in group '{group.get('group', '?')}' "
                    f"has no match in master skills/tags"
                )
        return problems


MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def format_dates(role: dict) -> str:
    """Render '2021-03'..'present' as 'Mar 2021 - Present'."""
    start = _fmt_month(role.get("start"))
    end_raw = role.get("end")
    end = "Present" if str(end_raw).lower() in {"present", "now", "current"} else _fmt_month(end_raw)
    if start and end:
        return f"{start} - {end}"
    return start or end or ""


def _fmt_month(value: Any) -> str:
    if value in (None, ""):
        return ""
    text = str(value).strip()
    parts = text.split("-")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        month = int(parts[1])
        if 1 <= month <= 12:
            return f"{MONTHS[month - 1]} {parts[0]}"
    return text


def slugify(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def warn_if_placeholder(master: Master) -> None:
    if master.is_placeholder:
        print(
            "!! cv/master.yaml is still PLACEHOLDER data (meta.placeholder: true).\n"
            "!! Replace it with the real CV before using any output.\n",
            file=sys.stderr,
        )
