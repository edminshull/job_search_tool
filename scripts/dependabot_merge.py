#!/usr/bin/env python3
"""Merge the Dependabot pull requests that have passed CI — and only those.

WHY THIS EXISTS
---------------
Dependabot opens a handful of pull requests a day (see .github/dependabot.yml),
and the repository's own rule is that every one of them is read and tested before
it is merged. Doing that by hand every morning is the tedium this removes. Doing
it *unsafely* is worse than the tedium, so every rule below fails closed.

THE THREE THINGS THAT MAKE THIS SAFE
------------------------------------
1. IT IS BOUND TO A COMMIT, NOT TO A PULL REQUEST. Checks are read from
   /commits/<head-sha>/check-runs, so the evidence is attached to the exact
   commit that will be merged, and `gh pr merge --match-head-commit <sha>` makes
   GitHub refuse the merge if the branch moved after we looked. Without that
   pair, a force-push between "checks are green" and "merge" is a real hole.

2. ALL THREE CHECKS MUST BE PRESENT AND SUCCESSFUL, BY NAME. A missing check is
   treated as a failure, not as an absence of evidence: if the workflow did not
   run, this script does not merge. `pending` is never a pass, and a `cancelled`
   run is never a pass — the CI workflow cancels the previous run on every new
   push to the same branch, so `cancelled` is common and means "look again",
   not "fine".

3. MAJORS NEED A HUMAN. A green CI on a major bump proves the tests pass, not
   that the upgrade is safe: on this repository CI deliberately SKIPS the
   CV-tailoring suite (it needs the gitignored cv/master.yaml) and the three
   container tests, which is exactly the suite that exercises fpdf2,
   python-docx and pypdf. .github/dependabot.yml says majors "need a human, a
   changelog read and a real local test run", so this script reports them and
   never merges them unless --include-majors is passed deliberately.

WHAT IT DOES FOR THE REST
-------------------------
* checks green, merge state clean   -> merge (squash, delete branch)
* checks still running              -> --auto lets GitHub finish the job, else
                                       defer to the next run
* checks failed                     -> comment with the failing job + label
                                       `dependabot-needs-human`, continue to the
                                       next pull request (one bad PR must not
                                       block the others)
* behind / conflicting              -> ask Dependabot to rebase or recreate, once,
                                       and never twice for the same commit

DRY RUN BY DEFAULT
------------------
Running it with no arguments changes nothing at all: it prints what it would do.
Mutation requires --apply. That is the same shape as `git clean -n` and it is
deliberate — the thing this script does is irreversible.

USAGE
-----
    python3 scripts/dependabot_merge.py                  # dry run, everything
    python3 scripts/dependabot_merge.py --verbose
    python3 scripts/dependabot_merge.py --apply --max-merges 1
    python3 scripts/dependabot_merge.py --apply --auto   # let GitHub wait
    python3 scripts/dependabot_merge.py --report /tmp/dependabot.md
    python3 scripts/dependabot_merge.py --include-majors # you are sure

`--repo` defaults to edminshull/job_search_tool and is passed on EVERY gh call.
This repository is a fork of fedorchenko-juli/job_search_tool, and gh resolves
the default repository to the upstream: a bare `gh pr list` here shows the
upstream's pull requests and a bare `gh pr view 7` fails outright. Never drop it.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPO = "edminshull/job_search_tool"

# The three CI jobs in .github/workflows/ci.yml, by the name GitHub reports for
# them. These strings are load-bearing: a typo here means the check is treated as
# MISSING forever, which fails closed (nothing merges) rather than open.
REQUIRED_CHECKS = ("Python tests", "Web tests", "UI tests")

# Conclusions that mean "do not merge". `stale` is included because GitHub uses it
# for a run superseded by a newer one — the evidence is no longer about this
# commit.
FAILED_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
)

NEEDS_HUMAN_LABEL = "dependabot-needs-human"
LABEL_COLOR = "d73a4a"
LABEL_DESCRIPTION = "Dependabot bump whose checks did not pass; needs a person"

# Dependabot's author login is the app, not a bot account.
DEPENDABOT_LOGINS = frozenset({"app/dependabot", "dependabot[bot]"})

# Dependabot signs its own replies as "dependabot" in issue comments.
DEPENDABOT_REPLY_LOGINS = frozenset({"dependabot", "app/dependabot", "dependabot[bot]"})

# Dependabot's refusal to manage a branch a person has edited. Matched on text,
# because it is its own wording and it is the only signal that the rebase request
# will never be honoured.
REFUSAL_MARKERS = (
    "edited by someone other than dependabot",
    "can't rebase",
    "cannot rebase",
)

# Dependencies that CI CANNOT actually verify, and why this list is load-bearing.
#
# app/tests/test_cv_tailor.py deliberately tests the real cv/master.yaml — "a
# fixture master would let a real dangling reference pass" — and cv/ is
# gitignored because it is a real person's CV. CI therefore has no such file and
# that whole suite (137 tests) SKIPS. The suite holds the fpdf2 / python-docx /
# pypdf tests: exactly the dependencies most likely to be the bump. The ci.yml
# header says it outright — "For a dependabot bump to fpdf2 / python-docx / pypdf
# ... run it locally on the PR branch."
#
# So for these three, a green CI is NOT evidence that the bump works. This script
# will not merge them unless --ci-blind allow is passed explicitly, which makes
# "we accepted CI-only evidence here" a decision someone made on purpose.
CI_BLIND_DEPS = ("fpdf2", "python-docx", "pypdf")

# How long we are willing to poll inside one run, when asked to wait.
POLL_SECONDS = 30

PR_FIELDS = ",".join(
    [
        "number",
        "title",
        "headRefName",
        "headRefOid",
        "isDraft",
        "mergeable",
        "mergeStateStatus",
        "author",
        "labels",
        "url",
        "isCrossRepository",
    ]
)


class GhError(RuntimeError):
    """A gh invocation failed. Never swallowed: every failure fails closed."""


# ---------------------------------------------------------------------------
# gh plumbing
# ---------------------------------------------------------------------------


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GhError(f"{' '.join(cmd)} exited {proc.returncode}: {detail}")
    return proc


def gh_pr(repo: str, *args: str, check: bool = True) -> str:
    """Run `gh pr ... --repo <repo>`. --repo goes on every call, always."""
    return _run(["gh", "pr", *args, "--repo", repo], check=check).stdout


def gh_api(*args: str, check: bool = True) -> str:
    return _run(["gh", "api", *args], check=check).stdout


def gh_label(repo: str, *args: str, check: bool = True) -> str:
    return _run(["gh", "label", *args, "--repo", repo], check=check).stdout


def gh_json(text: str):
    return json.loads(text) if text.strip() else None


# ---------------------------------------------------------------------------
# Version classification
# ---------------------------------------------------------------------------


def normalize_version(text: str) -> tuple[int, ...] | None:
    """'>=2.8.8' / '^20.19.43' / 'v1.2' -> (2, 8, 8) / (20, 19, 43) / (1, 2).

    Ignores the operator, a leading v, anything after a comma (a range) and a
    prerelease suffix. Returns None when there is no leading number at all, which
    the caller treats as "cannot classify" -> needs a human.
    """
    if not text:
        return None
    cleaned = text.strip().lstrip("=<>~^v ").strip()
    cleaned = cleaned.split(",")[0].strip()
    cleaned = re.split(r"[-+]", cleaned)[0]
    parts: list[int] = []
    for chunk in cleaned.split("."):
        match = re.match(r"\d+", chunk)
        if not match:
            break
        parts.append(int(match.group()))
    return tuple(parts) if parts else None


def _pad(version: tuple[int, ...]) -> tuple[int, int, int]:
    return (list(version) + [0, 0, 0])[:3]  # type: ignore[return-value]


def classify(title: str) -> tuple[str, str]:
    """Return (level, explanation) for a Dependabot PR title.

    level is one of major / minor / patch / unknown. `unknown` is a first-class
    answer, not a fallback to be guessed at: an unparseable title is reported for
    a human rather than merged on the assumption that it was small.
    """
    grouped = re.search(r"bump the (?P<group>[\w./-]+) group", title, re.IGNORECASE)
    if grouped:
        group = grouped.group("group").lower()
        if "major" in group and "minor" not in group:
            return "major", f"grouped update `{group}` is majors only"
        if "minor" in group or "patch" in group:
            return "minor", f"grouped update `{group}` is minor/patch only"
        return "unknown", f"grouped update `{group}` has no version level in its name"

    span = re.search(r"from (?P<old>\S+) to (?P<new>\S+)", title)
    if not span:
        return "unknown", "no `from X to Y` in the title"

    old = normalize_version(span.group("old"))
    new = normalize_version(span.group("new"))
    if old is None or new is None:
        return "unknown", f"cannot read versions from {span.group(0)!r}"

    old_p, new_p = _pad(old), _pad(new)
    shown = f"{span.group('old')} -> {span.group('new')}"
    if old_p[0] != new_p[0]:
        return "major", f"major version change ({shown})"
    # 0.x is the semver wildcard: nothing about a 0.x bump is promised to be
    # compatible, so a minor move there is treated as a major.
    if old_p[0] == 0 and old_p[1] != new_p[1]:
        return "major", f"0.x minor change, which semver does not promise is safe ({shown})"
    if old_p[1] != new_p[1]:
        return "minor", f"minor version change ({shown})"
    return "patch", f"patch version change ({shown})"


# ---------------------------------------------------------------------------
# Check-run evidence, bound to one commit
# ---------------------------------------------------------------------------


@dataclass
class CheckReport:
    ok: bool
    failing: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    detail: dict[str, dict] = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        if self.failing:
            return "FAILED"
        if self.missing:
            return "MISSING"
        if self.pending:
            return "PENDING"
        return "PASS"


def fetch_check_runs(repo: str, sha: str) -> dict[str, dict]:
    """Latest check run per name, for exactly this commit.

    `--paginate` with `--jq '.check_runs[]'` emits one JSON object per line,
    which is the only shape that survives pagination intact. A re-run creates a
    second check run with the same name, so the newest `started_at` wins.
    """
    output = gh_api(
        "--paginate",
        f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
        "--jq",
        ".check_runs[] | {name, status, conclusion, details_url, started_at, head_sha}",
    )
    latest: dict[str, dict] = {}
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        run = json.loads(line)
        if run.get("head_sha") != sha:
            # Belt and braces: the endpoint is already commit-scoped.
            continue
        name = run.get("name") or "?"
        previous = latest.get(name)
        if previous is None or (run.get("started_at") or "") > (previous.get("started_at") or ""):
            latest[name] = run
    return latest


def assess_checks(repo: str, sha: str) -> CheckReport:
    runs = fetch_check_runs(repo, sha)
    report = CheckReport(ok=False, detail=runs)

    if not runs:
        # No check run exists yet at all — the CI workflow has not registered on
        # this commit. That is "not yet", not "failed": a pull request opened
        # seconds ago must not be reported as broken.
        report.pending.extend(REQUIRED_CHECKS)
        return report

    for name, run in runs.items():
        conclusion = (run.get("conclusion") or "").lower()
        status = (run.get("status") or "").lower()
        if status != "completed":
            report.pending.append(name)
        elif conclusion in FAILED_CONCLUSIONS:
            report.failing.append(name)
        elif conclusion != "success":
            # skipped / neutral: not a pass for a *required* job, which is
            # handled below; for any other check it is not a failure either.
            if name in REQUIRED_CHECKS:
                report.failing.append(f"{name} ({conclusion or 'no conclusion'})")

    for name in REQUIRED_CHECKS:
        if name not in runs:
            report.missing.append(name)
    report.ok = not (report.failing or report.pending or report.missing)
    return report


# ---------------------------------------------------------------------------
# PR-level decisions
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    number: int
    title: str
    url: str
    level: str
    reason: str
    action: str  # MERGE | AUTO | UPDATE | FAILED | NEEDS_HUMAN | DEFER | SKIP
    checks: CheckReport | None = None
    sha: str = ""
    merge_state: str = ""
    note: str = ""
    branch: str = ""


def touched_blind_deps(repo: str, number: int, title: str) -> set[str]:
    """Which CI-blind dependencies this pull request actually changes.

    An individual bump names its package in the title, so the title is enough.
    A GROUPED bump ("Bump the python-minor-patch group with 4 updates") does not,
    and the grouping in .github/dependabot.yml means a grouped pip PR will
    regularly contain fpdf2 or pypdf. For those the diff is the only way to know,
    so it is fetched — one extra call, and only for grouped pull requests.
    """
    named = {dep for dep in CI_BLIND_DEPS if re.search(rf"\b{re.escape(dep)}\b", title)}
    if named or "group" not in title.lower():
        return named

    diff = gh_pr(repo, "diff", str(number), check=False)
    touched = set()
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for dep in CI_BLIND_DEPS:
            if re.search(rf"[\"']?{re.escape(dep)}\b", line):
                touched.add(dep)
    return touched


def find_pr(repo: str, number: int) -> dict | None:
    raw = gh_pr(repo, "view", str(number), "--json", PR_FIELDS, check=False)
    return gh_json(raw) if raw.strip() else None


def list_dependabot_prs(repo: str) -> list[dict]:
    raw = gh_pr(repo, "list", "--state", "open", "--limit", "100", "--json", PR_FIELDS)
    prs = gh_json(raw) or []
    return [pr for pr in prs if (pr.get("author") or {}).get("login") in DEPENDABOT_LOGINS]


def comments_for(repo: str, number: int) -> list[dict]:
    raw = gh_pr(repo, "view", str(number), "--json", "comments", check=False)
    data = gh_json(raw) if raw.strip() else None
    return (data or {}).get("comments") or []


def commit_date(repo: str, sha: str) -> str:
    raw = gh_api(f"repos/{repo}/commits/{sha}", "--jq", ".commit.committer.date", check=False)
    return raw.strip()


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def already_asked_to_update(repo: str, number: int, sha: str) -> bool:
    """True when a rebase/recreate request is newer than the commit it targets.

    Otherwise every run would re-comment `@dependabot rebase` while the rebase is
    still queued, and the pull request would fill with noise.
    """
    asked = _asked_at(repo, number, ("@dependabot rebase", "@dependabot recreate"))
    if not asked:
        return False
    head = _parse_ts(commit_date(repo, sha))
    if head is None:
        return True  # cannot prove it is stale -> do not re-ask
    return max(asked) > head


def _asked_at(repo: str, number: int, commands: tuple[str, ...]) -> list[datetime]:
    stamps = [
        _parse_ts(comment.get("createdAt", ""))
        for comment in comments_for(repo, number)
        if any(command in (comment.get("body") or "") for command in commands)
    ]
    return [stamp for stamp in stamps if stamp is not None]


def newest_refusal(repo: str, number: int, sha: str) -> tuple[datetime | None, str]:
    """The last time Dependabot said it will not touch this branch.

    This matters more than it looks. `@dependabot rebase` is REFUSED outright on
    a branch a person has edited — Dependabot replies "Looks like this PR has
    been edited by someone other than Dependabot. That means Dependabot can't
    rebase it - sorry!" and then does nothing, forever. Clicking "Update branch"
    in the GitHub UI is enough to cause it, because that leaves a merge commit
    authored by a human on the branch.

    Without this check the tool would ask, be refused, and report "waiting" on
    every subsequent run while the pull request stayed permanently unmergeable
    under strict branch protection — a silent stall, which is the one outcome
    this whole script exists to prevent.

    A refusal is only counted while it is NEWER than the head commit. Once the
    branch has been rebuilt — which is exactly what `@dependabot recreate` does —
    the refusal was about a branch state that no longer exists, and holding it
    against the new branch would strand a pull request that is now perfectly
    rebasable.
    """
    head = _parse_ts(commit_date(repo, sha))
    latest: tuple[datetime | None, str] = (None, "")
    for comment in comments_for(repo, number):
        login = (comment.get("author") or {}).get("login", "")
        body = comment.get("body") or ""
        if login not in DEPENDABOT_REPLY_LOGINS:
            continue
        if not any(marker in body.lower() for marker in REFUSAL_MARKERS):
            continue
        stamp = _parse_ts(comment.get("createdAt", ""))
        if stamp is None:
            continue
        if head is not None and stamp <= head:
            continue  # the branch has been rebuilt since; this no longer applies
        if latest[0] is None or stamp > latest[0]:
            latest = (stamp, body)
    return latest


def content_bearing_human_commits(repo: str, number: int) -> list[str]:
    """Human commits on a Dependabot branch that carry content of their own.

    The distinction decides whether recreating the branch is safe. A commit whose
    headline starts with "Merge " is what the "Update branch" button leaves
    behind: it holds no content, so `@dependabot recreate` — which discards the
    branch and rebuilds it from main plus the bump — loses nothing. Any other
    human commit is real work, and recreating would destroy it, so that case
    needs a person instead.
    """
    raw = gh_pr(repo, "view", str(number), "--json", "commits", check=False)
    data = gh_json(raw) if raw.strip() else None
    blockers: list[str] = []
    for commit in (data or {}).get("commits") or []:
        authors = commit.get("authors") or []
        names = " ".join(
            str(author.get("name", "")) + str(author.get("login", ""))
            for author in authors if isinstance(author, dict)
        ).lower()
        if "dependabot" in names:
            continue
        headline = commit.get("messageHeadline", "") or ""
        if not headline.lower().startswith("merge "):
            blockers.append(f"{commit.get('oid', '')[:8]} {headline}".strip())
    return blockers


def already_reported_failure(repo: str, number: int, sha: str) -> bool:
    marker = f"<!-- dependabot-merge-tool:failed:{sha} -->"
    return any(marker in (comment.get("body") or "") for comment in comments_for(repo, number))


def update_decision(repo: str, number: int, base: dict, sha: str, reason: str) -> Decision:
    """Behind or conflicting: ask Dependabot to fix its own branch — unless it won't.

    The refusal path is the interesting one. `@dependabot rebase` is answered with
    "can't rebase it - sorry!" for any branch a person has edited (an "Update
    branch" click is enough), and no amount of asking changes that. So the tool
    escalates once to `@dependabot recreate` — but ONLY when the branch holds no
    human content beyond a merge of main, because recreate discards the branch.
    Anything else is a person's problem, reported rather than guessed at.
    """
    refusal_at, refusal_body = newest_refusal(repo, number, sha)
    if refusal_at is not None:
        if any(stamp > refusal_at for stamp in _asked_at(repo, number, ("@dependabot recreate",))):
            return Decision(**base, reason="Dependabot refused to rebase and has already been "
                                           "asked to recreate", action="NEEDS_HUMAN",
                            note="look at the branch by hand")
        blockers = content_bearing_human_commits(repo, number)
        if blockers:
            return Decision(
                **base,
                reason="Dependabot will not rebase a branch a person has edited, and it carries "
                       f"human work: {'; '.join(blockers)}",
                action="NEEDS_HUMAN",
                note="recreating would discard that commit; update the branch by hand, or close "
                     "the pull request and let Dependabot raise a fresh one",
            )
        return Decision(**base,
                        reason=f"{reason}, and Dependabot will not rebase it because a person "
                               "has edited the branch",
                        action="UPDATE", note="recreate")

    # No refusal on record: the ordinary path. Conflicts need a rebuild anyway.
    note = "recreate" if reason.startswith("conflicts") else "rebase"
    return Decision(**base, reason=reason, action="UPDATE", note=note)


def evaluate(repo: str, pr: dict, *, auto: bool, include_majors: bool, ci_blind: str) -> Decision:
    number = pr["number"]
    title = pr.get("title", "")
    url = pr.get("url", "")
    sha = pr.get("headRefOid", "")
    merge_state = pr.get("mergeStateStatus") or "UNKNOWN"
    mergeable = pr.get("mergeable") or "UNKNOWN"

    level, why = classify(title)
    base = dict(
        number=number, title=title, url=url, level=level, sha=sha,
        merge_state=merge_state, branch=pr.get("headRefName", ""),
    )

    if pr.get("isDraft"):
        return Decision(**base, reason="draft", action="SKIP", note="not ready for review")
    if pr.get("isCrossRepository"):
        return Decision(**base, reason="cross-repository head", action="SKIP",
                        note="branch lives in another repository")

    checks = assess_checks(repo, sha)
    base["checks"] = checks

    if checks.failing:
        return Decision(**base, reason=f"checks failed: {', '.join(sorted(checks.failing))}",
                        action="FAILED")
    if checks.missing:
        return Decision(**base, reason=f"no check recorded for this commit: {', '.join(checks.missing)}",
                        action="FAILED", note="the workflow did not run on this commit")
    if checks.pending:
        if auto:
            return Decision(**base, reason=f"checks still running: {', '.join(sorted(checks.pending))}",
                            action="AUTO", note="handing the wait to GitHub")
        return Decision(**base, reason=f"checks still running: {', '.join(sorted(checks.pending))}",
                        action="DEFER", note="run again later")

    # Checks are green on this exact commit. Now: is it safe to change the tree?
    if level == "major" and not include_majors:
        return Decision(**base, reason=f"major bump - {why}", action="NEEDS_HUMAN",
                        note="run with --include-majors to override")
    if level == "unknown":
        return Decision(**base, reason=f"cannot classify the bump - {why}", action="NEEDS_HUMAN")

    if mergeable == "CONFLICTING" or merge_state == "DIRTY":
        return update_decision(repo, number, base, sha, "conflicts with main")
    if merge_state == "BEHIND":
        return update_decision(repo, number, base, sha, "behind main")
    if merge_state == "UNSTABLE":
        return Decision(**base, reason="GitHub reports UNSTABLE despite green required checks",
                        action="DEFER", note="look again rather than force it")
    if merge_state == "BLOCKED":
        return Decision(**base, reason="merge is blocked by branch protection", action="NEEDS_HUMAN",
                        note="a required check or review is not satisfied")
    if merge_state not in ("CLEAN", "HAS_HOOKS"):
        return Decision(**base, reason=f"merge state {merge_state}", action="DEFER",
                        note="GitHub has not finished computing mergeability")

    # Everything the tool can check is green. Last question: is this a dependency
    # that green CI actually says anything about?
    if ci_blind == "block":
        blind = touched_blind_deps(repo, number, title)
        if blind:
            return Decision(
                **base,
                reason=f"CI cannot verify {', '.join(sorted(blind))} - the suite that "
                       "exercises it skips in CI",
                action="LOCAL_REVIEW",
                note="`make dependabot-review BRANCH=" + str(pr.get("headRefName", "")) + "`",
            )

    return Decision(**base, reason=f"{level} bump, {len(REQUIRED_CHECKS)} checks green on {sha[:8]}",
                    action="MERGE")


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def merge_pr(repo: str, decision: Decision, apply: bool) -> str:
    cmd = [
        "gh", "pr", "merge", str(decision.number),
        "--repo", repo,
        "--squash",
        "--delete-branch",
        "--match-head-commit", decision.sha,
    ]
    if not apply:
        return "would run: " + " ".join(cmd)
    proc = _run(cmd, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise GhError(detail or f"merge exited {proc.returncode}")
    return "merged"


def enable_auto_merge(repo: str, decision: Decision, apply: bool) -> str:
    cmd = [
        "gh", "pr", "merge", str(decision.number),
        "--repo", repo,
        "--squash",
        "--delete-branch",
        "--match-head-commit", decision.sha,
        "--auto",
    ]
    if not apply:
        return "would run: " + " ".join(cmd)
    proc = _run(cmd, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if "auto merge" in detail.lower() or "auto-merge" in detail.lower():
            # Auto-merge is a repository setting; its absence must not stop the
            # run. Defer instead, so the next run re-checks.
            return f"auto-merge unavailable, deferred ({detail.splitlines()[0][:120]})"
        raise GhError(detail or f"auto-merge exited {proc.returncode}")
    return "auto-merge enabled; GitHub will merge when checks pass"


def request_update(repo: str, decision: Decision, apply: bool) -> str:
    command = "@dependabot recreate" if decision.note == "recreate" else "@dependabot rebase"

    # The anti-spam guard has to be command-aware. A REFUSED `@dependabot rebase`
    # is newer than the head commit, so the ordinary check would see "already
    # asked" and never post the escalation — the exact stalemate this path exists
    # to break. Once Dependabot has refused, only a recreate asked AFTER that
    # refusal counts as already-asked.
    refusal_at, _ = newest_refusal(repo, decision.number, decision.sha)
    if refusal_at is not None and command == "@dependabot recreate":
        asked_since = [s for s in _asked_at(repo, decision.number, ("@dependabot recreate",))
                       if s > refusal_at]
        if asked_since:
            return "already asked Dependabot to recreate since it refused; waiting"
    elif already_asked_to_update(repo, decision.number, decision.sha):
        return f"already asked for {command} on {decision.sha[:8]}; waiting"

    if not apply:
        return f"would comment: {command}"
    gh_pr(repo, "comment", str(decision.number), "--body", command)
    return f"asked Dependabot to {command.split()[-1]}"


def ensure_label(repo: str, apply: bool) -> None:
    existing = gh_label(repo, "list", "--search", NEEDS_HUMAN_LABEL, "--json", "name", check=False)
    names = {row["name"] for row in (gh_json(existing) or [])}
    if NEEDS_HUMAN_LABEL in names:
        return
    if not apply:
        return
    gh_label(
        repo, "create", NEEDS_HUMAN_LABEL,
        "--color", LABEL_COLOR,
        "--description", LABEL_DESCRIPTION,
        check=False,
    )


def report_failure(repo: str, decision: Decision, apply: bool) -> str:
    if already_reported_failure(repo, decision.number, decision.sha):
        return "already reported for this commit"
    checks = decision.checks
    detail = checks.detail if checks else {}

    def bullets(names: list[str], with_links: bool = False) -> list[str]:
        if not names:
            return ["- none"]
        rows = []
        for name in sorted(names):
            plain = name.split(" (")[0]
            url = (detail.get(plain) or {}).get("details_url") if with_links else None
            rows.append(f"- [`{name}`]({url})" if url else f"- `{name}`")
        return rows

    lines = [
        f"<!-- dependabot-merge-tool:failed:{decision.sha} -->",
        f"Checks did not pass on `{decision.sha[:8]}`, so this bump has been left alone.",
        "",
        "**Failing**",
        *bullets(checks.failing if checks else [], with_links=True),
        "",
        "**Still running**",
        *bullets(checks.pending if checks else []),
        "",
        "**Not recorded on this commit**",
        *bullets(checks.missing if checks else []),
        "",
        "Nothing has been changed on this branch and the pull request stays open — this is a",
        "report, not a close. To reproduce it locally, which is the only place the",
        "CV-tailoring and container suites actually run:",
        "",
        f"```sh\nmake dependabot-review BRANCH={decision.branch}\n```",
        "",
        "_Posted by `scripts/dependabot_merge.py`._",
    ]
    body = "\n".join(lines)
    if not apply:
        return "would comment the failure report and add the label"
    ensure_label(repo, apply)
    gh_pr(repo, "comment", str(decision.number), "--body", body)
    gh_pr(repo, "edit", str(decision.number), "--add-label", NEEDS_HUMAN_LABEL, check=False)
    return "commented the failure report and added the label"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render(decisions: list[Decision], *, apply: bool, repo: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mode = "APPLY" if apply else "DRY RUN (nothing is changed)"
    out = [
        f"# Dependabot merge run — {stamp}",
        "",
        f"- repository: `{repo}`",
        f"- mode: **{mode}**",
        f"- open Dependabot pull requests: {len(decisions)}",
        "",
        "| PR | Level | Checks | Merge state | Action | Why |",
        "|---|---|---|---|---|---|",
    ]
    for d in decisions:
        checks = d.checks.verdict if d.checks else "n/a"
        title = d.title if len(d.title) <= 58 else d.title[:55] + "…"
        out.append(
            f"| [#{d.number}]({d.url}) {title} | {d.level} | {checks} | {d.merge_state} | "
            f"**{d.action}** | {d.reason} |"
        )

    needs_you = [d for d in decisions if d.action in ("NEEDS_HUMAN", "FAILED", "LOCAL_REVIEW")]
    if needs_you:
        out += ["", "## Needs a person", ""]
        for d in needs_you:
            out.append(f"- [#{d.number}]({d.url}) — {d.reason}")
            if d.note:
                out.append(f"  - {d.note}")

    out += ["", "## What each action means", "",
            "- **MERGE** — every required check passed on the exact head commit; safe to squash-merge.",
            "- **AUTO** — checks are still running; GitHub is asked to merge when they pass.",
            "- **UPDATE** — behind main or conflicting; Dependabot is asked to rebase/recreate.",
            "- **FAILED** — a check failed; the pull request is left open and reported.",
            "- **NEEDS_HUMAN** — a major bump, or something unclassifiable. Never merged by the tool.",
            "- **LOCAL_REVIEW** — CI is blind to this dependency (the suite that exercises it "
            "skips in CI), so it needs a local `make dependabot-review` first.",
            "- **DEFER** — ask again next run; GitHub or CI has not settled.",
            ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Merge Dependabot pull requests whose CI checks passed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help=f"owner/name (default {DEFAULT_REPO}; on a fork, gh's own "
                             "default is the upstream, so this is always passed)")
    parser.add_argument("--apply", action="store_true",
                        help="actually merge, comment and label (default: dry run)")
    parser.add_argument("--max-merges", type=int, default=1,
                        help="how many pull requests to merge in one run (default 1: each "
                             "merge moves main, so the rest deserve a fresh look)")
    parser.add_argument("--auto", action="store_true",
                        help="for pull requests whose checks are still running, ask GitHub "
                             "to merge them when the checks pass")
    parser.add_argument("--include-majors", action="store_true",
                        help="allow major bumps to be merged (they are reported by default)")
    parser.add_argument("--ci-blind", choices=("block", "allow"), default="block",
                        help="what to do about dependencies CI cannot verify "
                             f"({'/'.join(CI_BLIND_DEPS)}): 'block' reports them for a local "
                             "`make dependabot-review` (default); 'allow' accepts CI-only "
                             "evidence and merges them")
    parser.add_argument("--wait-minutes", type=int, default=0,
                        help="poll for pending checks for up to this long before deciding")
    parser.add_argument("--only", type=int, action="append", default=None,
                        help="restrict to this PR number (repeatable)")
    parser.add_argument("--report", type=Path, default=None,
                        help="also write the markdown report to this path")
    parser.add_argument("--verbose", action="store_true", help="per-PR lines on stdout")
    args = parser.parse_args(argv)

    try:
        prs = list_dependabot_prs(args.repo)
    except GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: is `gh` installed and authenticated (`gh auth status`)?", file=sys.stderr)
        return 1

    if args.only:
        wanted = set(args.only)
        prs = [pr for pr in prs if pr["number"] in wanted]

    if not prs:
        print("No open Dependabot pull requests.")
        if args.report:
            args.report.write_text(render([], apply=args.apply, repo=args.repo))
        return 0

    # Highest PR number first is arbitrary but stable; Dependabot numbers rise
    # with age, so this lands the newest bumps first.
    prs.sort(key=lambda pr: pr["number"], reverse=True)

    deadline = time.monotonic() + args.wait_minutes * 60
    merges_done = 0
    decisions: list[Decision] = []

    for pr in prs:
        try:
            decision = evaluate(args.repo, pr, auto=args.auto,
                                include_majors=args.include_majors, ci_blind=args.ci_blind)
        except GhError as exc:
            decisions.append(
                Decision(
                    number=pr["number"], title=pr.get("title", ""), url=pr.get("url", ""),
                    level="?", reason=f"could not read checks: {exc}", action="DEFER",
                    sha=pr.get("headRefOid", ""),
                )
            )
            continue

        # Waiting inside a run is opt-in: cron is the usual place to wait.
        while (
            decision.action == "DEFER"
            and decision.checks is not None
            and decision.checks.pending
            and time.monotonic() < deadline
        ):
            time.sleep(POLL_SECONDS)
            fresh = find_pr(args.repo, decision.number)
            if fresh is None:
                break
            decision = evaluate(args.repo, fresh, auto=args.auto,
                                include_majors=args.include_majors, ci_blind=args.ci_blind)

        try:
            if decision.action == "MERGE":
                if merges_done < args.max_merges:
                    decision.note = merge_pr(args.repo, decision, args.apply)
                    if args.apply:
                        merges_done += 1
                else:
                    decision.action = "DEFER"
                    decision.note = f"hit --max-merges {args.max_merges}; next run"
            elif decision.action == "AUTO":
                if merges_done < args.max_merges:
                    decision.note = enable_auto_merge(args.repo, decision, args.apply)
                    # A queued auto-merge counts against the cap. Several queued
                    # at once would each be validated against the current main and
                    # then land in sequence, and these bumps all touch the same
                    # requirements.txt — so only one may be in flight per run.
                    if args.apply:
                        merges_done += 1
                else:
                    decision.action = "DEFER"
                    decision.note = f"hit --max-merges {args.max_merges}; next run"
            elif decision.action == "UPDATE":
                decision.note = request_update(args.repo, decision, args.apply)
            elif decision.action == "FAILED":
                decision.note = report_failure(args.repo, decision, args.apply)
        except GhError as exc:
            decision.action = "DEFER"
            decision.note = f"action failed, nothing changed: {exc}"

        decisions.append(decision)
        if args.verbose:
            print(f"#{decision.number} {decision.action}: {decision.reason} — {decision.note}")

    markdown = render(decisions, apply=args.apply, repo=args.repo)
    print(markdown)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(markdown + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
