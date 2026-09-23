SHELL := /bin/bash
.DEFAULT_GOAL := run

.PHONY: run test test-web test-ui test-ui-headed evaluate web cv-setup cv-selftest cv-inventory cv-status cv-lessons

# The CV tailoring pipeline runs its vendored scripts with this interpreter, so
# the rendering dependencies have to live here rather than only in whatever
# python happens to be on PATH. See `cv-setup`.
VENV := $(CURDIR)/.venv
PY := $(VENV)/bin/python

# Interactive wrapper around `python -m app.main` — asks the questions
# instead of you having to remember the argparse flags.
run:
	@companies_flag=""; aggregators_flag=""; discovery_flag=""; company_flag=""; \
	read -rp "Fetch from companies.yaml? [Y/n] " ans; \
	[[ "$$ans" =~ ^[Nn] ]] && companies_flag="--skip-companies"; \
	read -rp "Fetch from aggregators.yaml? [Y/n] " ans; \
	[[ "$$ans" =~ ^[Nn] ]] && aggregators_flag="--skip-aggregators"; \
	read -rp "Auto-discover new companies from Adzuna results? [Y/n] " ans; \
	[[ "$$ans" =~ ^[Nn] ]] && discovery_flag="--skip-discovery"; \
	read -rp "Only one company slug? (blank = all) " slug; \
	[[ -n "$$slug" ]] && company_flag="--company $$slug"; \
	$(PY) -m app.main $$companies_flag $$aggregators_flag $$discovery_flag $$company_flag

# Automated test suite: pytest-style tests plus the standalone main()-style
# sanity scripts that print their own pass/fail summary, plus the web app's
# date-parsing tests (Node's built-in runner — Node >= 23 strips the
# TypeScript types itself, so there is no transpile step or jest/vitest).
test:
	$(PY) -m pytest app/ -q
	$(PY) -m app.tests.test_pipeline
	$(PY) -m app.tests.test_discover_companies
	$(PY) -m app.tests.test_ai_evaluate
	npm --prefix web test --silent

# Just the web app's tests + typecheck, for when you are only touching web/.
test-web:
	npm --prefix web test --silent
	npm --prefix web run typecheck --silent

# The board's UI tests (Playwright, headless Chrome). Self-contained: Playwright
# resets the fixture database, starts its own `next dev` on port 3100 against it,
# warms the routes and tears the server down — so this needs no setup step and
# does NOT disturb a board you already have open on :3000 (see the NEXT_DIST_DIR
# note in web/next.config.js). It never touches data/seen_jobs.sqlite3.
#
# Kept out of `make test` on purpose: it is ~13s and needs Google Chrome, whereas
# `make test` is seconds and needs nothing but the venv.
test-ui:
	npm --prefix web run test:ui --silent

# The same ten tests in a real, visible Chrome window — for watching a journey
# happen, or demoing it. QA_SLOWMO=300 make test-ui-headed slows each action
# down enough to follow.
test-ui-headed:
	npm --prefix web run test:ui:headed --silent

# Interactive wrapper around `python -m app.ai_evaluate` — asks the questions
evaluate:
	@dry_run_flag=""; limit_flag=""; \
	read -rp "Do you want a dry run? [Y/n] " ans; \
	[[ ! "$$ans" =~ ^[Nn] ]] && dry_run_flag="--dry-run"; \
	read -rp "Do you want to set a limit? (blank = all) " limit_val; \
	[[ -n "$$limit_val" ]] && limit_flag="--limit $$limit_val"; \
	$(PY) -m app.ai_evaluate $$dry_run_flag $$limit_flag

web:
	DB_PATH=$(CURDIR)/data/seen_jobs.sqlite3 npm --prefix web run dev

# --- CV tailoring -----------------------------------------------------------

# One-time (and after any requirements.txt change): create the venv the CV
# pipeline runs in. Without it the Tailor CV button fails with an instruction to
# run this, rather than an ImportError from three frames deep.
cv-setup:
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

# The vendored engine's own test suite: 198 checks on the tailoring pipeline,
# including that it hard-fails on a fabricated skill. Run after touching
# anything under scripts/ — the fabrication guard is the reason the output can
# be trusted, so it is worth knowing it still works.
cv-selftest:
	$(PY) scripts/selftest.py

# Re-render the master full inventory at the ROOT of cv_output/.
#
# ON DEMAND ONLY, by design — a tailoring run never triggers this. Update it when
# a new skill or achievement genuinely exists (Ed, 2026-09-23). The tailoring
# preview lists the posting terms cv/master.yaml cannot evidence, which is the
# evidence needed to decide; the master itself is edited by hand, because only a
# person can say whether a fact is real.
cv-inventory:
	$(PY) -m app.cv_tailor master-inventory

# What cv/master.yaml currently holds, and whether it is still placeholder data.
cv-status:
	$(PY) -m app.cv_tailor master-status

# What the local drafter has been TAUGHT from its own proven mistakes: the
# failure modes counted so far, how often, and which have recurred enough to be
# stated on every future draft. `--clear <mode>` is the veto — a lesson that is
# wrong, or that has stopped being true, must be removable without hand-editing
# the database.
cv-lessons:
	$(PY) -m app.cv_tailor lessons $(ARGS)
