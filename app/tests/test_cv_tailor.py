"""Offline tests for app/cv_tailor.py — the CV tailoring pipeline.

No network, no model calls, no filesystem writes into cv_output/: both providers
are stubbed, so these lock in the parts that are actually logic rather than
inference:

  * the comparison the UI shows (a priority-term figure against a priority-term
    figure — the bug that made a sound draft read as an 83%→26% collapse);
  * the refusal to render a CV whose refs the master cannot support, and the
    fact that this happens BEFORE anything is written;
  * the two-step contract — a preview writes no files, and an approval renders
    exactly the text that was previewed.

The real master CV is used, because the refs being validated are its refs. That
is deliberate: a fixture master would let a real dangling reference pass.
"""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from app import cv_tailor as ct
from app import dedup


# --- a stubbed provider ----------------------------------------------------
class StubProvider:
    """Stands in for DeepSeek or the local llama-server.

    Returns whatever object it was built with, in the same shape
    `provider.complete()` returns, and records the prompts it was given so a
    test can assert what the model was actually asked."""

    def __init__(self, result, name="stub", model="stub-model"):
        self._result = result
        self.name = name
        self.model = model
        self.calls = []
        self.last_failure = None

    def complete(self, system, user, schema, max_tokens):
        self.calls.append({"system": system, "user": user, "schema": schema,
                           "max_tokens": max_tokens})
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _draft_result(yaml_text, report="report text"):
    return {"tailored_yaml": yaml_text, "report": report}


# --- the real master's ids ------------------------------------------------
@pytest.fixture(scope="module")
def master_ids():
    data = yaml.safe_load(ct.MASTER_PATH.read_text(encoding="utf-8"))
    roles = [r["id"] for r in data["experience"]]
    achievements = [a["id"] for r in data["experience"] for a in r.get("achievements") or []]
    return roles, achievements


@pytest.fixture
def valid_yaml(master_ids):
    roles, achievements = master_ids
    return yaml.safe_dump({
        "summary": "Senior SDET with nine years in FinTech.",
        "skills": [{"group": "Test Automation & Frameworks", "items": ["Cucumber"]}],
        "experience": [{"ref": roles[0], "achievements": [{"ref": achievements[0]}]}],
        "section_order": ["summary", "skills", "experience", "education", "certifications"],
    }, sort_keys=False)


# --- coverage figures ------------------------------------------------------
def test_coverage_reads_the_two_comparable_priority_figures():
    """The regression this exists for: master priority coverage and tailored
    PRIORITY coverage are the same measurement and must be read as such.

    Before the fix the tailored figure came from the all-term line (227 terms)
    while the master's came from the priority line (35), so a faithful draft
    reported 83% → 26% and looked like a catastrophe. The all-term line is
    still in the report; it must simply not be mistaken for the tailored
    headline."""
    report = "\n".join([
        "# Keyword & ATS gap report: X",
        "- **Priority-term coverage: 83%** — 29/35 requirement-level terms are evidenced.",
        "- **All-term coverage: 32%** — 72/227 extracted terms appear in your achievement bank.",
        "- **Tailored CV coverage: 26%** — 60/227 terms appear in the CV you're sending",
        "- **Tailored priority-term coverage: 77%** — 27/35 requirement-level terms made it into this CV.",
        "- **Priority gaps you can't currently evidence: 6** (4 hard skills, 2 other)",
        "- **Priority terms dropped in tailoring: 2** (api testing, cross-functional collaboration)",
        "- **In your master but dropped in tailoring: 12** — see below",
    ])
    got = ct.parse_coverage(report)
    assert got["master_coverage"] == 83.0
    assert got["tailored_coverage"] == 77.0, (
        "the tailored figure must be the priority-term one; 26.0 here means the "
        "all-term line was read instead")
    assert got["priority_gaps"] == 6
    assert got["dropped_priority"] == 2


def test_coverage_master_only_report_has_no_tailored_figure():
    """Before a draft exists the report has no tailored lines at all. The
    figure must be absent, not defaulted to something that looks measured."""
    got = ct.parse_coverage(
        "- **Priority-term coverage: 83%** — 29/35 requirement-level terms are evidenced.\n"
        "- **Priority gaps you can't currently evidence: 6** (4 hard skills, 2 other)\n")
    assert got["master_coverage"] == 83.0
    assert got["tailored_coverage"] is None
    assert got["priority_gaps"] == 6


# --- drafting --------------------------------------------------------------
def test_draft_parses_a_valid_yaml_document(valid_yaml):
    provider = StubProvider(_draft_result(valid_yaml))
    draft = ct.draft_cv({"company": "X", "title": "Y"}, "posting", "gaps", provider=provider)
    assert draft["data"]["summary"].startswith("Senior SDET")
    assert draft["provider"] == "stub"


def test_draft_strips_a_markdown_fence_the_model_added():
    """A fenced document is a near-miss, not a failure: the schema asked for a
    raw string and models add fences anyway."""
    fenced = "```yaml\nsummary: Hello there.\nexperience:\n  - ref: visa-cc\n```"
    data = ct.parse_drafted_yaml(fenced)
    assert data["summary"] == "Hello there."


def test_draft_retries_once_with_the_parse_error_fed_back():
    """A small model garbles indentation far more often than it garbles the
    selection, so the retry must tell it what was wrong — repeating the same
    prompt unchanged would just reproduce the same output."""
    bad = StubProvider(_draft_result("summary: [unclosed\n  bad: yaml"))
    good = _draft_result("summary: Fine.\nexperience:\n  - ref: visa-cc\n")

    class SequenceProvider(StubProvider):
        def complete(self, system, user, schema, max_tokens):
            self.calls.append({"system": system, "user": user})
            return (bad._result if len(self.calls) == 1 else good)

    provider = SequenceProvider(None)
    draft = ct.draft_cv({"company": "X", "title": "Y"}, "posting", "gaps", provider=provider)
    assert draft["data"]["summary"] == "Fine."
    assert len(provider.calls) == 2
    assert "does not parse" in provider.calls[1]["user"], (
        "the retry prompt must carry the parser's complaint")


def test_draft_raises_rather_than_returning_an_empty_cv():
    provider = StubProvider(None)  # provider.complete returns None for every attempt
    with pytest.raises(ct.TailorError) as exc:
        ct.draft_cv({"company": "X", "title": "Y"}, "posting", "gaps", provider=provider)
    assert "submit_tailored_cv" in str(exc.value)


def test_draft_rejects_a_document_with_no_experience():
    with pytest.raises(ct.TailorError, match="experience"):
        ct.parse_drafted_yaml("summary: A summary with no experience section.")


def test_a_truncated_draft_is_caught_here_not_two_stages_later():
    """A response cut off at max_tokens mid-document is very often STILL VALID
    YAML, so it parses cleanly and sails on to fail somewhere confusing.

    Real case, LEGO posting 2026-09-23: the model spent its budget on a
    2,200-character summary — it had pasted most of the master's summary
    variants together — was cut off inside the experience section, and the first
    complaint surfaced in gap.py as "achievement ref 'None' (role 'visa-cc') is
    not in master". The draft had already been reported as a success by then."""
    truncated = (
        "summary: A very long summary.\n"
        "skills:\n"
        "  - group: Test Automation\n"
        "    items: [Cucumber]\n"
        "experience:\n"
        "  - ref: visa-cc\n"
        "    achievements:\n"
        "      - ref: visa-1\n"
        "        text: >\n"
        "          A bullet whose text block was cut off mid-sen\n"
        "      - ref:\n"
    )
    with pytest.raises(ct.TailorError) as exc:
        ct.parse_drafted_yaml(truncated)
    message = str(exc.value)
    assert "ref" in message
    assert "cut off" in message, "the message must point at truncation, the actual cause"


def test_a_truncated_document_with_a_missing_role_ref_is_caught_too():
    """The other shape truncation takes: the role line itself is incomplete."""
    with pytest.raises(ct.TailorError, match="cut off"):
        ct.parse_drafted_yaml("summary: S\nexperience:\n  - ref:\n")


def test_an_absent_achievements_key_is_still_valid():
    """`achievements` ABSENT means "all of that role's achievements" in the
    engine's data model, so the structural check must not demand it — requiring
    it would reject a perfectly good minimal draft."""
    data = ct.parse_drafted_yaml("summary: S\nexperience:\n  - ref: visa-cc\n")
    assert data["experience"][0]["ref"] == "visa-cc"
    # An explicit EMPTY list is different from absent, and also valid.
    data = ct.parse_drafted_yaml("summary: S\nexperience:\n  - ref: visa-cc\n    achievements: []\n")
    assert data["experience"][0]["achievements"] == []


def test_a_skill_group_with_no_group_name_is_rejected():
    with pytest.raises(ct.TailorError, match="group"):
        ct.parse_drafted_yaml("summary: S\nskills:\n  - items: [Cucumber]\nexperience:\n  - ref: visa-cc\n")


# --- reference validation --------------------------------------------------
def test_validate_accepts_refs_that_exist(valid_yaml, master_ids):
    roles, achievements = master_ids
    data = yaml.safe_load(valid_yaml)
    assert ct._validate_against_master(data) is None
    # Sanity: the fixture really is citing real ids, not matching by accident.
    assert data["experience"][0]["ref"] in roles
    assert data["experience"][0]["achievements"][0]["ref"] in achievements


def test_validate_names_every_bad_ref_at_once(master_ids):
    """Naming refs one at a time would make the user fix a ref, re-run, and meet
    the next — so all of them are collected into one message."""
    roles, _ = master_ids
    data = {
        "summary": "S",
        "experience": [
            {"ref": roles[0], "achievements": [{"ref": "no-such-achievement"}]},
            {"ref": "no-such-role", "achievements": []},
        ],
    }
    problem = ct._validate_against_master(data)
    assert problem is not None
    assert "no-such-achievement" in problem
    assert "no-such-role" in problem


def test_validate_lists_the_valid_role_ids(master_ids):
    """The message has to be actionable — a model retrying needs the ids that
    exist, not just the one that does not."""
    roles, _ = master_ids
    problem = ct._validate_against_master(
        {"summary": "S", "experience": [{"ref": "wrong", "achievements": []}]})
    for role in roles:
        assert role in problem


# --- the master prompt -----------------------------------------------------
def test_master_prompt_keeps_every_id_and_metric():
    """The prompt is reshaped for a 32k window, but ids and metrics are the two
    things that must survive it: ids are what the model must cite, and an empty
    metrics list is how rule 2 ("no number exists") is actionable."""
    prompt = ct.master_for_prompt()
    data = yaml.safe_load(ct.MASTER_PATH.read_text(encoding="utf-8"))
    for role in data["experience"]:
        assert role["id"] in prompt
        for ach in role.get("achievements") or []:
            assert ach["id"] in prompt
    assert "metrics: []" in prompt, "an empty metrics list must be visible to the model"


def test_master_prompt_fits_the_local_model_window():
    """32768 tokens is the slot window. The master prompt must leave room for a
    posting, the gap report and a few KB of output — measured at ~4 chars per
    token, so this asserts the master stays a small fraction of the budget."""
    approx_tokens = len(ct.master_for_prompt()) / 4
    assert approx_tokens < 8000, (
        f"master prompt is ~{approx_tokens:.0f} tokens, which leaves too little of the "
        f"32768-token slot for the posting and the draft")


def test_master_prompt_is_much_smaller_than_the_raw_file():
    """The reshaping has to actually pay: comments and unused structure are
    what make cv/master.yaml 40 KB of prompt for 12 KB of facts."""
    raw = ct.MASTER_PATH.read_text(encoding="utf-8")
    assert len(ct.master_for_prompt()) < len(raw) * 0.7


# --- prompt honesty rules --------------------------------------------------
def test_draft_prompt_carries_the_gap_report_before_the_master():
    """Order is the point: the gap report is the task list, the master is the
    material. It goes first so the model reads the job as targets to close."""
    prompt = ct.build_draft_prompt({"company": "X", "title": "Y"}, "POSTING TEXT",
                                   "GAP REPORT TEXT", "MASTER TEXT")
    assert prompt.index("GAP REPORT TEXT") < prompt.index("MASTER TEXT")
    assert prompt.index("POSTING TEXT") < prompt.index("GAP REPORT TEXT")


def test_draft_prompt_truncation_is_announced_not_silent():
    """A truncated gap report must say so, or a thin draft cannot be traced
    back to the prompt it came from."""
    prompt = ct.build_draft_prompt({"company": "X", "title": "Y"}, "posting",
                                   "x" * 20000, "master")
    assert "truncated" in prompt


def test_verifier_is_told_to_treat_reordering_as_correct_and_widening_as_a_lie():
    """The distinction the whole guard exists for. If the prompt does not say
    that adopting the posting's noun is correct tailoring, a strict verifier
    rejects every good draft; if it does not say that widening is fabrication,
    it accepts every bad one."""
    system = ct.VERIFY_SYSTEM
    assert "CORRECT" in system or "correct" in system
    assert "fabrication" in system.lower()
    assert "metrics" in system.lower()
    assert "must-have" in system.lower() or "must-haves" in system.lower()


# --- the two-step contract -------------------------------------------------
def test_preflight_reports_a_missing_local_server_with_the_fix(monkeypatch):
    """A stopped llama-server must produce a sentence naming the command that
    starts it — this is the single most likely failure in practice."""
    monkeypatch.setattr(ct.llm_providers, "check_local_available",
                        lambda *a, **k: "No local model server on http://127.0.0.1:8080.")
    monkeypatch.setenv("CV_DRAFT_PROVIDER", "local")
    problems = ct.preflight(need_draft=True)
    assert any("No local model server" in p for p in problems)


def test_preflight_skips_the_local_check_when_drafting_in_the_cloud(monkeypatch):
    monkeypatch.setenv("CV_DRAFT_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-" + "a" * 40)
    monkeypatch.setenv("CV_VERIFY_PROVIDER", "deepseek")
    assert ct.preflight(need_draft=True) == []


# --- layout ----------------------------------------------------------------
def test_filing_path_is_date_then_company():
    assert ct.application_dir("23-09-26", "trading-212") == \
        ct.OUTPUT_ROOT / "23-09-26" / "trading-212"


def test_slugify_matches_the_cv_engines_own_rule():
    """Reused rather than reinvented, so a folder created by the button looks
    like one the original CLI would have created."""
    assert ct.slugify("Trading 212") == "trading-212"
    assert ct.slugify("LEGO Digital Play") == "lego-digital-play"


def test_the_day_directory_is_fixed_on_first_prepare_and_reused(monkeypatch):
    """Re-tailoring must not move an application into today's folder: the
    vendored renderer's own rule, and the reason the date is stored rather than
    recomputed. Getting this wrong scatters one application's files across two
    dates and makes 'what did I send them, and when?' unanswerable."""
    job = {"company": "Acme", "title": "SDET"}
    first_dir, first_slug = ct._day_and_slug(None, job)
    assert first_dir == ct.date.today().strftime(ct.DATE_DIR_FORMAT)

    # A record from an earlier day keeps that day, whatever today is.
    record = {"day_dir": "01-01-26", "company_slug": "acme"}
    later_dir, later_slug = ct._day_and_slug(record, job)
    assert later_dir == "01-01-26"
    assert later_slug == "acme"


def test_job_metadata_date_is_written_back_from_the_day_directory(tmp_path):
    """job.yaml's date is what the renderer would use to derive the output path,
    so it must agree with the folder this module chose — two sources of truth
    for one path is a bug waiting for the first hand-edited job.yaml."""
    ct._write_job_metadata(tmp_path, {"company": "Acme", "title": "SDET",
                                      "url": "https://x", "source": "board"}, "23-09-26")
    meta = yaml.safe_load((tmp_path / "job.yaml").read_text(encoding="utf-8"))
    assert meta["date"] == "2026-09-23"
    assert meta["company"] == "Acme"


@pytest.mark.parametrize("day_dir,expected", [
    ("23-09-26", "2026-09-23"),
    ("01-01-26", "2026-01-01"),
    ("31-12-99", "2099-12-31"),
])
def test_the_two_digit_year_expands_to_the_2000s(day_dir, expected):
    """`%y` maps 26 to year 26. A job.yaml dated 0026 would send the renderer's
    date logic to a folder that cannot exist — caught by the metadata test
    above, which is why it asserts a full ISO date and not a folder name."""
    assert ct._day_dir_to_iso(day_dir) == expected


def test_an_unparseable_day_directory_falls_back_to_today():
    """A hand-edited day_dir must not fail a render; it degrades to today."""
    assert ct._day_dir_to_iso("not-a-date") == ct.date.today().isoformat()
    assert ct._day_dir_to_iso("") == ct.date.today().isoformat()


# --- the database record ---------------------------------------------------
def test_tailoring_record_round_trips(tmp_path):
    db = tmp_path / "t.db"
    with dedup.connect(str(db)) as conn:
        conn.execute("INSERT INTO job_details (url, company, title) VALUES (?, ?, ?)",
                     ("u1", "Acme", "SDET"))
        dedup.save_tailoring(conn, "u1", status="draft", day_dir="23-09-26",
                             company_slug="acme", tailored_yaml="summary: x\n",
                             master_coverage=83.0)
        got = dedup.get_tailoring(conn, "u1")
        assert got["status"] == "draft"
        assert got["tailored_yaml"] == "summary: x\n"
        assert got["master_coverage"] == 83.0
        # updated_at must be set even though the insert did not name it
        assert got["updated_at"]

        # A second, partial save must not clear what the first wrote — this is
        # what lets render() record its paths without rewriting the YAML.
        dedup.save_tailoring(conn, "u1", status="rendered", pdf_path="/x/cv.pdf")
        got = dedup.get_tailoring(conn, "u1")
        assert got["status"] == "rendered"
        assert got["pdf_path"] == "/x/cv.pdf"
        assert got["tailored_yaml"] == "summary: x\n", "the draft was wiped by a partial save"
        assert len(dedup.iter_tailorings(conn)) == 1


def test_tailoring_rejects_an_unknown_field(tmp_path):
    """A typo'd column must raise, not look like a successful save that quietly
    dropped the value."""
    with dedup.connect(str(tmp_path / "t.db")) as conn:
        with pytest.raises(ValueError, match="Unknown cv_tailorings field"):
            dedup.save_tailoring(conn, "u1", status="draft", pdf_pathh="/x")


def test_a_failed_re_render_does_not_mark_a_good_cv_as_broken(tmp_path):
    """`status` answers "is there a usable CV here?", not "did the last attempt
    succeed?".

    Found by deliberately rendering a fabricated CV against a job that already
    had a good one: the refusal overwrote status to 'failed' while the PDF sat
    on disk, so the board reported a broken application. The error is still
    recorded — it just must not misrepresent what exists."""
    with dedup.connect(str(tmp_path / "t.db")) as conn:
        conn.execute("INSERT INTO job_details (url, company, title) VALUES (?, ?, ?)",
                     ("u1", "Acme", "SDET"))
        dedup.save_tailoring(conn, "u1", status="rendered", pdf_path="/x/cv.pdf",
                             tailored_yaml="summary: good\n")
        old = dedup.get_tailoring(conn, "u1")

        ct._record_failure(conn, "u1", old, error="ref 'nope' is not in the master")
        got = dedup.get_tailoring(conn, "u1")
        assert got["status"] == "rendered", "a good CV was reported as failed"
        assert got["pdf_path"] == "/x/cv.pdf"
        assert "nope" in got["error"], "the failure reason must still be recorded"
        assert got["tailored_yaml"] == "summary: good\n", (
            "a refusal must not discard the draft — regenerating costs ~90s and the "
            "fix is usually one bad ref")


def test_a_failure_with_no_rendered_cv_reports_failed(tmp_path):
    """The honest case: nothing usable exists, so say so."""
    with dedup.connect(str(tmp_path / "t.db")) as conn:
        conn.execute("INSERT INTO job_details (url, company, title) VALUES (?, ?, ?)",
                     ("u1", "Acme", "SDET"))
        dedup.save_tailoring(conn, "u1", status="draft", tailored_yaml="summary: draft\n")
        ct._record_failure(conn, "u1", dedup.get_tailoring(conn, "u1"), error="boom")
        got = dedup.get_tailoring(conn, "u1")
        assert got["status"] == "failed"
        assert got["error"] == "boom"
        assert got["tailored_yaml"] == "summary: draft\n"


# --- the CLI protocol ------------------------------------------------------
def test_cli_emits_one_json_object_and_nothing_else(tmp_path):
    """The web app parses stdout as JSON. Anything the vendored scripts print
    leaking into stdout would corrupt it — which is exactly the kind of bug
    that only shows on the error path."""
    proc = subprocess.run(
        [str(ct.VENV_PY), "-m", "app.cv_tailor", "master-status"],
        cwd=str(ct.ROOT), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)  # must parse as a whole
    assert payload["ok"] is True
    assert payload["achievements"] > 0
    assert payload["placeholder"] is False


def test_cli_reports_an_error_as_json_with_a_nonzero_exit(tmp_path):
    """A failure must still be machine-readable: the browser shows `error`
    verbatim, and a traceback on stdout would render as nothing at all."""
    proc = subprocess.run(
        [str(ct.VENV_PY), "-m", "app.cv_tailor", "preview", "--url", "https://not-on-the-board"],
        cwd=str(ct.ROOT), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert "not-on-the-board" in payload["error"]


# --- escalation: the local draft is rejected, so a stronger model re-drafts ----
# Real case, Kainos public-sector role, 2026-09-23: the local model relabelled
# nine years of FinTech compliance work as "experience in public sector domains"
# to match the posting's framing. The verifier rejected it, which is the system
# working — but the user was left with a rejected draft and no obvious next step.
# These tests pin the escalation that fixes it.

BAD = {
    "verdict": "reject",
    "required_fixes": "DELETE the public sector claim from the summary.",
    "fabricated_claims": "Draft claims public sector; master is FinTech only.",
    "unsupported_refs": "none",
    "invented_metrics": "none",
    "missing_must_haves": "Public sector experience — a real gap in the master.",
    "notes": "Otherwise a faithful subset.",
}
GOOD = {
    "verdict": "accept",
    "required_fixes": "none",
    "fabricated_claims": "none",
    "unsupported_refs": "none",
    "invented_metrics": "none",
    "missing_must_haves": "none",
    "notes": "Faithful.",
}


def _draft(model="local", provider="local"):
    return {"data": {"summary": "s"}, "report": "r", "provider": provider,
            "model": model, "yaml_text": "summary: s\nexperience:\n  - ref: visa-cc\n"}


class _StubProvider:
    """A stand-in cloud provider — used where the point is WHICH provider ran,
    not what it is."""

    def __init__(self, name="deepseek", model="deepseek-flash"):
        self.name = name
        self.model = model

    def complete(self, system, user, schema, max_tokens):
        raise AssertionError("the stub provider should not be called directly here")


class _SequenceVerifier:
    """Returns a different verdict on each successive call."""

    name = "verify"
    model = "verifier-model"

    def __init__(self, results):
        self._results = list(results)
        self.calls = 0

    def complete(self, system, user, schema, max_tokens):
        self.calls += 1
        return self._results.pop(0)


def test_critique_is_imperative_and_excludes_the_real_gaps():
    """`missing_must_haves` must NOT reach the drafter as something to fix.

    Those are gaps in the candidate's actual history, so "fix this" can only mean
    invent it — the one outcome the whole system exists to prevent. The verifier's
    own Kainos note said the gap "must be removed rather than restated"."""
    critique = ct._verifier_critique(BAD)
    assert "DELETE the public sector claim" in critique
    assert "CLAIMS THAT ARE NOT IN THE MASTER CV" in critique
    assert "Public sector experience — a real gap" not in critique
    assert "REWORDING or REMOVING" in critique

    # A perfectly clean verification yields nothing to say, so the caller can
    # tell "no critique" from "empty critique".
    assert ct._verifier_critique(GOOD) == ""


def test_escalation_replaces_a_rejected_draft_with_a_better_verified_one(monkeypatch):
    """The whole point: a rejected local draft is re-drafted in the cloud using
    the verifier's corrections, and the RESULT is verified again rather than
    trusted."""
    second = _draft(model="deepseek-flash", provider="deepseek")
    monkeypatch.setattr(ct.llm_providers, "build_provider",
                        lambda *a, **k: _StubProvider())
    monkeypatch.setattr(ct, "draft_cv", lambda *a, **k: second)
    verifier = _SequenceVerifier([GOOD])

    out = ct._escalate_draft({"company": "Kainos"}, "posting", "gaps", BAD,
                             _draft(), verifier, {"master_coverage": 80.0})
    assert out is not None
    assert out["draft"] is second, "the cloud draft should replace the rejected one"
    assert out["verification"]["verdict"] == "accept"
    assert "re-drafted with deepseek" in out["reason"]
    assert "accepted" in out["reason"], "the reason must read as English, not 'accept' + 'ed'"
    assert verifier.calls == 1, "the escalation must itself be verified"


def test_escalation_keeps_the_original_when_the_retry_is_no_better(monkeypatch):
    """A retry that verifies no better must not silently replace a
    rejected-but-fixable draft with a rejected-and-worse one — and the verdict
    shown must describe the document actually shown, or the display lies."""
    first = _draft()
    monkeypatch.setattr(ct.llm_providers, "build_provider",
                        lambda *a, **k: _StubProvider())
    monkeypatch.setattr(ct, "draft_cv", lambda *a, **k: _draft("deepseek-flash", "deepseek"))
    verifier = _SequenceVerifier([BAD])  # still rejected after the retry

    out = ct._escalate_draft({"company": "Kainos"}, "posting", "gaps", BAD,
                             first, verifier, {"master_coverage": 80.0})
    assert out["draft"] is first, "the original draft must be kept"
    assert out["verification"] is BAD
    assert "no better" in out["reason"]


def test_escalation_is_skipped_cleanly_without_a_cloud_key(monkeypatch):
    """No DEEPSEEK_API_KEY must produce a reason, not an exception: the local
    verdict is then final, and the user should know that rather than waiting for
    an escalation that will never come."""
    def no_key(*a, **k):
        raise ct.llm_providers.ProviderError("DEEPSEEK_API_KEY: missing")

    monkeypatch.setattr(ct.llm_providers, "build_provider", no_key)
    first = _draft()
    out = ct._escalate_draft({"company": "X"}, "posting", "gaps", BAD, first,
                             _SequenceVerifier([]), {"master_coverage": 80.0})
    assert out["draft"] is first
    assert "Not escalated" in out["reason"] and "DEEPSEEK_API_KEY" in out["reason"]


def test_escalation_survives_a_cloud_draft_failure(monkeypatch):
    """A cloud error mid-escalation must leave the user with the local draft and
    its findings, not a failed run."""
    monkeypatch.setattr(ct.llm_providers, "build_provider",
                        lambda *a, **k: _StubProvider())
    monkeypatch.setattr(ct, "draft_cv",
                        lambda *a, **k: (_ for _ in ()).throw(ct.TailorError("cloud 500")))
    first = _draft()
    out = ct._escalate_draft({"company": "X"}, "posting", "gaps", BAD, first,
                             _SequenceVerifier([]), {"master_coverage": 80.0})
    assert out["draft"] is first
    assert "its draft failed" in out["reason"]


def test_escalation_does_nothing_when_there_is_no_critique(monkeypatch):
    """An `unknown` verdict (the checker returned nothing) has no corrections to
    apply, so there is nothing to escalate with — escalating blind would just
    spend a cloud call re-rolling the dice."""
    monkeypatch.setattr(ct, "draft_cv",
                        lambda *a, **k: pytest.fail("must not re-draft without a critique"))
    assert ct._escalate_draft({"company": "X"}, "posting", "gaps",
                              {"verdict": "unknown", "required_fixes": "none"},
                              _draft(), _SequenceVerifier([]), {}) is None


# --- HTML structure preservation for the gap report --------------------------
# The bug this pins, found 2026-09-23 on the Kainos Workday advert: the posting
# is stored as HTML on ONE line (<h1>…</h1><ul><li>…</li></ul>), and
# filters.strip_html replaces every tag with a SPACE. keywords.split_sections
# finds headings only when they sit on their own line, so with the whole advert
# flattened onto one line NO heading was recognised, every term fell into
# "other", and the report claimed the posting had 3 requirement-level terms when
# it has 17. Coverage read 33% instead of 65%, and 2 priority gaps were reported
# instead of 6 — so the verifier under-reported real gaps AND the drafting prompt
# lost the requirement/gap split that makes it useful.

WORKDAY_HTML = (
    "<div><h1>Join us</h1><p>We are a technology company.</p>"
    "<h2>MAIN PURPOSE OF THE ROLE</h2><p>You will test things.</p>"
    "<h2>MINIMUM (ESSENTIAL) REQUIREMENTS:</h2><ul>"
    "<li>Experience working in Continuous Integration environment and using tools "
    "such as Jenkins and/or TeamCity</li>"
    "<li>Knowledge or experience of at least one type of Non-functional testing</li>"
    "<li>Demonstrable experience of using cloud architecture</li></ul>"
    "<h2>DESIRABLE:</h2><ul><li>Experience producing Assurance artefacts</li>"
    "<li>Experience testing APIs, microservices, databases and using SQL</li></ul>"
    "<p>Embracing our differences &#39;nbsp&#39; equality</p></div>"
)


def test_html_is_converted_with_block_structure_not_flattened():
    text = ct.html_to_structured_text(WORKDAY_HTML)
    # Headings must be on their own lines, which is the signal split_sections uses.
    assert "MINIMUM (ESSENTIAL) REQUIREMENTS:" in text.splitlines()
    assert "DESIRABLE:" in text.splitlines()
    assert len([l for l in text.splitlines() if l.strip()]) > 5, "structure was flattened"


def test_entities_are_decoded_and_whitespace_normalised():
    text = ct.html_to_structured_text(WORKDAY_HTML)
    assert "&#39;" not in text and "&#x27;" not in text
    assert "'" in text, "numeric/named entities must be decoded"
    assert "\u00a0" not in text, "non-breaking spaces must not survive"
    assert "\n\n\n" not in text, "blank runs must collapse — a blank line marks a heading"
    assert not text.endswith("\n")


def test_structured_html_gives_the_gap_engine_its_section_weighting_back():
    """The end-to-end consequence: requirement-level terms must be classified as
    requirements. Before the fix this reported 0-1 of several."""
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(ct.SCRIPTS))
    try:
        import keywords as kw
        sections = kw.split_sections(ct.html_to_structured_text(WORKDAY_HTML))
    finally:
        _sys.path.pop(0)

    assert len(sections["requirements"]) > 100, "essential requirements were not bucketed"
    assert len(sections["nice"]) > 50, "the desirable section was not bucketed"
    assert "jenkins" in sections["requirements"].lower()
    assert "sql" in sections["nice"].lower()


def test_plain_text_postings_are_untouched():
    """The converter must be a no-op for postings that are already plain text —
    most of the board — or it would rewrite headings that are already correct."""
    plain = "Requirements:\n\nJava and Ruby experience.\n\nBenefits:\n\nPension.\n"
    assert ct.html_to_structured_text(plain) == plain.strip()


def test_post_text_uses_the_structured_conversion():
    """_post_text is what gap.py actually receives; a regression there would
    leave the converter correct and unused."""
    job = {"description": WORKDAY_HTML, "company": "X", "title": "Y"}
    text = ct._post_text(job)
    assert "MINIMUM (ESSENTIAL) REQUIREMENTS:" in text.splitlines()


# --- the posting-context block: stopping the local model adopting the ---------
# --- employer's world --------------------------------------------------------
# The Kainos public-sector draft relabelled nine years of FinTech compliance work
# as "experience in public sector domains". Diagnosing it showed the information
# was already available — the master's Domain group is FinTech-only — but nothing
# told the model that a gap in the POSTING's context is not a licence to re-file
# the candidate's history under it. These pin the fix.

_REAL_MASTER = {
    "experience": [{"company": "Visa / CurrencyCloud"}, {"company": "Ten10 Group"}],
    "skill_groups": [
        {"group": "Domain", "items": ["FinTech", "Regulated Financial Services", "Payments"]},
        {"group": "Test Automation & Frameworks", "items": ["Cucumber"]},
    ],
}
_KAINOS_GAP_REPORT = """# Keyword & ATS gap report: Senior Test Engineer (Public Sector)

## Headline

- **Priority-term coverage: 65%** — 11/17 requirement-level terms are evidenced.

## Priority gaps: hard skills

| Term | Where it appears | Mentions |
| --- | --- | ---: |
| cypress | **requirements** | 1 |
| teamcity | **requirements** | 1 |

## Priority gaps: methods, domains and other phrasing

| Term | Where it appears | Mentions |
| --- | --- | ---: |
| public | **job title** | 1 |
| sector | **job title** | 1 |

## Well-evidenced priorities

| Term | Where it appears | Mentions |
| --- | --- | ---: |
| jenkins | **requirements** | 3 |
"""


def test_priority_gap_parser_reads_both_tables_and_stops_at_the_next_section():
    terms = ct._priority_gap_terms(_KAINOS_GAP_REPORT)
    assert terms == ["cypress", "teamcity", "public", "sector"], (
        "both priority tables must be read, and 'jenkins' from Well-evidenced "
        "priorities must NOT leak in as a gap")


def test_priority_gap_parser_tolerates_a_report_with_no_gaps():
    assert ct._priority_gap_terms("# Report\n\n## Headline\n\nnothing here\n") == []
    assert ct._priority_gap_terms("") == []


def test_context_block_names_the_candidate_employers_and_real_domains():
    """Fact 1 and 2 from the block's docstring: where they actually worked, and
    which domains the master can support."""
    block = ct.posting_context_block({"company": "Kainos"}, _REAL_MASTER, _KAINOS_GAP_REPORT)
    assert "Visa / CurrencyCloud, Ten10 Group" in block
    assert "The posting's employer is: Kainos" in block
    assert "has NOT worked there" in block
    assert "FinTech" in block and "Regulated Financial Services" in block


def test_context_block_names_the_ungovernable_gaps_explicitly():
    """Fact 3. 'public' and 'sector' must be named as gaps that stay gaps — the
    specific words the rejected draft turned into a claim."""
    block = ct.posting_context_block({"company": "Kainos"}, _REAL_MASTER, _KAINOS_GAP_REPORT)
    assert "cypress" in block and "public" in block and "sector" in block
    assert "GAPS. They stay gaps" in block
    assert "must not appear as a description of the candidate" in block


def test_context_block_says_a_domain_absent_from_the_list_cannot_be_claimed():
    block = ct.posting_context_block({"company": "X"}, _REAL_MASTER, "")
    assert "A domain term absent from the list above cannot be claimed" in block
    # No gaps in the report -> no gap sentence, rather than an empty one.
    assert "CANNOT\nevidence" not in block and "These are GAPS" not in block


def test_the_context_block_comes_before_the_posting_text_in_the_prompt():
    """ORDER IS ARGUMENT. A model that meets the constraint after 3,000 words of
    the employer's own marketing has already absorbed that framing, which is
    exactly how the Kainos draft went wrong."""
    prompt = ct.build_draft_prompt({"company": "Kainos", "title": "T"}, "POSTING TEXT HERE",
                                   _KAINOS_GAP_REPORT, "MASTER TEXT",
                                   master_data=_REAL_MASTER)
    assert prompt.index("POSTING CONTEXT") < prompt.index("POSTING TEXT HERE")
    assert prompt.index("POSTING CONTEXT") < prompt.index("MASTER TEXT")


def test_a_prompt_without_master_data_omits_the_block_rather_than_crashing():
    """The parameter is optional so the existing call sites and tests keep
    working; an absent master must degrade to the old prompt, not raise."""
    prompt = ct.build_draft_prompt({"company": "X"}, "posting", "gaps", "master")
    assert "POSTING CONTEXT" not in prompt
    assert "POSTING TEXT" in prompt


# --- required skill groups: enforcement, not a request ------------------------
# Regression, found 2026-09-23. The master has always carried the AI and
# local-LLM work, and the OLD repo's tailored CVs carried `Personal & Prototype
# Work` in four of their last six. My draft-prompt instruction — "list only the
# skill items that serve this posting; the selection is the tailoring" — is right
# about ITEMS and was applied by the local model to whole GROUPS, so the Kainos
# CV shipped with no Playwright, Python, Ollama, llama.cpp, Qwen or local-LLM
# content at all. The code now holds the line, because a model that ignores an
# instruction cannot omit them.

def test_required_groups_are_reinserted_at_master_order():
    master = {"skill_groups": [
        {"group": "Test Automation & Frameworks", "items": ["Cucumber"]},
        {"group": "AI-Assisted Engineering", "items": ["Claude Code"]},
        {"group": "Domain", "items": ["FinTech"]},
        {"group": "Personal & Prototype Work", "items": ["Playwright", "Python"]},
    ]}
    draft = {"summary": "s", "skills": [
        {"group": "Test Automation & Frameworks", "items": ["Cucumber"]},
        {"group": "Domain", "items": ["FinTech"]},
    ]}
    out, readded = ct.merge_required_groups(draft, master)
    assert readded == ["AI-Assisted Engineering", "Personal & Prototype Work"]
    # Master order, not appended: the two groups sit where the master puts them,
    # so the CV reads in the same sequence as every other CV.
    assert [g["group"] for g in out["skills"]] == [
        "Test Automation & Frameworks", "AI-Assisted Engineering",
        "Domain", "Personal & Prototype Work"]


def test_a_group_present_keeps_the_drafters_own_items_and_order():
    """Trimming irrelevant ITEMS inside a group is legitimate tailoring — only a
    whole missing group is a problem, so a present group must be left alone."""
    master = {"skill_groups": [
        {"group": "AI-Assisted Engineering", "items": ["Claude Code", "LLM-Assisted Development"]},
        {"group": "Personal & Prototype Work", "items": ["Playwright", "Python", "Ollama"]},
    ]}
    draft = {"summary": "s", "skills": [
        {"group": "AI-Assisted Engineering", "items": ["Claude Code"]},
    ]}
    out, readded = ct.merge_required_groups(draft, master)
    assert readded == ["Personal & Prototype Work"]
    assert out["skills"][0]["items"] == ["Claude Code"], "the drafter's own selection was overwritten"


def test_omitted_skills_means_every_group_is_emitted_so_nothing_is_added():
    """The engine reads an absent `skills` key as 'use every master group', so
    re-adding would duplicate the whole list."""
    master = {"skill_groups": [{"group": "Personal & Prototype Work", "items": ["Playwright"]}]}
    for empty in (None, []):
        draft = {"summary": "s", "skills": empty}
        out, readded = ct.merge_required_groups(draft, master)
        assert readded == []
        assert out["skills"] == empty


def test_a_draft_that_already_complies_is_returned_untouched():
    """A compliant draft must come back byte-identical, because the caller only
    re-serialises when something was re-added — and re-serialising drops the
    model's comments. A draft that needed no correction keeps them."""
    items = list(ct.REQUIRED_SKILL_ITEMS["Personal & Prototype Work"])
    master = {"skill_groups": [{"group": "Personal & Prototype Work", "items": items}]}
    draft = {"summary": "s", "skills": [{"group": "Personal & Prototype Work", "items": list(items)}]}
    out, readded = ct.merge_required_groups(draft, master)
    assert readded == []
    assert out["skills"] == draft["skills"]


def test_a_partially_compliant_group_is_still_completed():
    """The Trading 212 shape: group present, most items present, one missing. This
    is the case the earlier group-level-only check let through."""
    items = list(ct.REQUIRED_SKILL_ITEMS["Personal & Prototype Work"])
    master = {"skill_groups": [{"group": "Personal & Prototype Work", "items": items}]}
    partial = [i for i in items if i != "DeepSeek V4 Flash"]
    draft = {"summary": "s", "skills": [{"group": "Personal & Prototype Work", "items": partial}]}
    out, readded = ct.merge_required_groups(draft, master)
    assert readded, "a missing pinned item must be reported"
    assert "DeepSeek V4 Flash" in out["skills"][0]["items"]


def test_the_prompt_and_the_master_agree_on_which_groups_are_required():
    """The prompt names the groups in prose and the master supplies them by name.
    If the master ever renames a group, the prose silently stops matching and the
    enforcement re-adds a group under a name the prompt never mentioned — so the
    two are asserted against each other here rather than trusted."""
    master = ct.load_master_data()
    names = {g["group"] for g in master["skill_groups"]}
    for required in ct.REQUIRED_SKILL_GROUPS:
        assert required in names, f"REQUIRED_SKILL_GROUPS names a group the master does not have: {required}"
        assert required in ct.DRAFT_SYSTEM, f"the prompt does not name the required group {required}"


def test_the_required_groups_actually_carry_the_local_llm_work():
    """The point of requiring them. Named individually because 'the group is
    present' is not the same as 'the local-LLM work is present', which is the
    thing that went missing."""
    master = ct.load_master_data()
    items = {str(i).lower() for g in master["skill_groups"]
             if g["group"] in ct.REQUIRED_SKILL_GROUPS for i in g.get("items") or []}
    for expected in ("playwright", "python", "ollama", "llama.cpp", "qwen3-coder",
                     "deepseek v4 flash", "local llm deployment"):
        assert expected in items, f"'{expected}' is missing from the required groups"


def test_reserialising_preserves_every_value_the_renderer_needs():
    """Rebuilding the YAML must not quietly drop a reworded bullet, a ref, the
    summary or the section order — the whole document is re-serialised, so this
    is the check that the round trip is safe."""
    raw = (
        "summary: >\n  Senior SDET with nine years in FinTech.\n"
        "skills:\n  - group: Test Automation & Frameworks\n    items: [Cucumber]\n"
        "experience:\n  - ref: visa-cc\n    achievements:\n"
        "      - ref: visa-1\n        text: >\n          Reworded, same fact.\n"
        "      - ref: visa-2\n"
        "section_order: [summary, skills, experience, education, certifications]\n"
    )
    data = ct.parse_drafted_yaml(raw)
    data, readded = ct.merge_required_groups(data, ct.load_master_data())
    assert readded, "this fixture is meant to be missing the required groups"
    back = yaml.safe_load(ct._reserialise_with_groups(data, raw))

    assert "Reworded, same fact." in back["experience"][0]["achievements"][0]["text"]
    assert [a["ref"] for a in back["experience"][0]["achievements"]] == ["visa-1", "visa-2"]
    assert back["summary"].startswith("Senior SDET")
    assert back["section_order"][0] == "summary"
    assert ct._validate_against_master(back) is None


def test_claude_code_is_restored_into_a_group_the_drafter_kept():
    """The second half of the regression. The local model kept the
    `AI-Assisted Engineering` GROUP but trimmed `Claude Code` out of it — twice
    running, on a public-sector test role it judged AI-irrelevant. A group being
    present is not the same as the work being present, and Claude Code is the
    strongest item available because it is EMPLOYER work (visa-10)."""
    master = {"skill_groups": [
        {"group": "AI-Assisted Engineering",
         "items": ["Claude Code", "AI-Assisted Test Generation", "LLM-Assisted Development"]},
        {"group": "Personal & Prototype Work", "items": ["Playwright"]},
    ]}
    draft = {"summary": "s", "skills": [
        {"group": "AI-Assisted Engineering", "items": ["AI-Assisted Test Generation"]},
        {"group": "Personal & Prototype Work", "items": ["Playwright"]},
    ]}
    out, notes = ct.merge_required_groups(draft, master)
    items = [i for g in out["skills"] if g["group"] == "AI-Assisted Engineering" for i in g["items"]]
    assert "Claude Code" in items
    assert "AI-Assisted Test Generation" in items, "the drafter's own items must survive"
    assert any("Claude Code" in n for n in notes), "the correction must be reported"


def test_a_group_kept_with_no_items_is_filled_rather_than_left_empty():
    """An empty items list renders a group heading with nothing under it."""
    master = {"skill_groups": [
        {"group": "AI-Assisted Engineering", "items": ["Claude Code"]},
        {"group": "Personal & Prototype Work", "items": ["Playwright"]},
    ]}
    draft = {"summary": "s", "skills": [
        {"group": "AI-Assisted Engineering", "items": []},
        {"group": "Personal & Prototype Work", "items": ["Playwright"]},
    ]}
    out, notes = ct.merge_required_groups(draft, master)
    entry = [g for g in out["skills"] if g["group"] == "AI-Assisted Engineering"][0]
    assert entry["items"] == ["Claude Code"]
    assert notes


def test_the_prompt_names_every_required_item():
    """Prompt prose and enforcement must not drift: if the code starts pinning an
    item the prompt never mentions, the model is being failed for something it was
    not told — which is how a fix becomes a new bug."""
    for group, items in ct.REQUIRED_SKILL_ITEMS.items():
        for item in items:
            assert item in ct.DRAFT_SYSTEM, f"the prompt does not name the required item {item!r}"


def test_the_local_llm_stack_items_are_pinned_not_just_the_group():
    """Found on the Trading 212 CV: the drafter kept the group, kept eight items,
    and wrote `DeepSeek Harness` where the master says `DeepSeek V4 Flash`.

    Every group-level check passed while the CV named the wrong thing. `deepseek
    harness` IS a real master project tag, so this is not fabrication — the
    engine's own guard accepts it too (render.py matches tags) — which is exactly
    why it had to be caught here rather than downstream. The item Ed asked for
    had simply gone."""
    master = {"skill_groups": [
        {"group": "Personal & Prototype Work",
         "items": ["Playwright", "Python", "Ollama", "llama.cpp", "Qwen3-Coder",
                   "DeepSeek V4 Flash", "Local LLM Deployment"]},
    ]}
    draft = {"summary": "s", "skills": [
        {"group": "Personal & Prototype Work",
         "items": ["Playwright", "Qwen3-Coder", "DeepSeek Harness"]},
    ]}
    out, notes = ct.merge_required_groups(draft, master)
    items = out["skills"][0]["items"]
    assert "DeepSeek V4 Flash" in items, "the master's own item must be restored"
    assert notes, "the correction must be reported"


def test_the_real_master_pins_the_whole_local_llm_stack():
    """Against the live master, because the names are the point — a typo here
    silently pins nothing."""
    master = ct.load_master_data()
    group = [g for g in master["skill_groups"]
             if g["group"] == "Personal & Prototype Work"][0]
    for item in ct.REQUIRED_SKILL_ITEMS["Personal & Prototype Work"]:
        assert item in group["items"], (
            f"REQUIRED_SKILL_ITEMS pins {item!r}, which the master does not list — "
            f"the pin would silently do nothing")


def test_pinned_items_come_from_the_master_so_they_cannot_be_fabricated():
    """A pin that named something absent from the master would add a skill the
    candidate cannot support — turning a helper into a fabrication source. Every
    pinned item must exist in the master group it is pinned into."""
    master = ct.load_master_data()
    by_group = {g["group"]: [str(i) for i in g.get("items") or []]
                for g in master["skill_groups"]}
    for group_name, items in ct.REQUIRED_SKILL_ITEMS.items():
        assert group_name in by_group, f"{group_name} is not a master skill group"
        for item in items:
            assert item in by_group[group_name], f"{item!r} is not in master group {group_name!r}"
