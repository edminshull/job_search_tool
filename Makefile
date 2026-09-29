SHELL := /bin/bash
.DEFAULT_GOAL := run

.PHONY: run test test-web test-ui test-ui-headed ci dependabot-list dependabot-review dependabot-clean evaluate web cv-setup cv-selftest cv-inventory cv-status cv-lessons

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

# Reproduce CI exactly, locally.
#
# The lines below are the CI job steps from .github/workflows/ci.yml, in the same
# order and against the same venv layout — the workflow recreates this repo's own
# .venv, which is what VENV/PY above point at. A green `make ci` therefore means a
# green CI run, and a red one can be debugged here instead of in a runner log.
#
# QA_SKIP_DOCKER_TESTS=1 mirrors the workflow, and is why the three container
# tests are SKIPPED rather than run: they pull python:3.12-alpine and serve a mock
# upstream over a real socket, which CI avoids to stay fast and offline. So
# `make ci` does NOT mean "everything ran". `make test` runs those three locally
# when Docker is up — that is the difference between the two targets.
ci:
	QA_SKIP_DOCKER_TESTS=1 $(PY) -m pytest app/ -q
	$(PY) -m app.tests.test_pipeline
	$(PY) -m app.tests.test_discover_companies
	$(PY) -m app.tests.test_ai_evaluate
	npm --prefix web test --silent
	npm --prefix web run typecheck --silent
	npm --prefix web run test:ui --silent

# ---------------------------------------------------------------------------
# Reviewing a Dependabot PR.
#
#   make dependabot-list
#   make dependabot-review BRANCH=dependabot/pip/pypdf-6.0.0
#   make dependabot-clean
#
# The review runs in a worktree at $(REVIEW_DIR), OUTSIDE this repo, so none of
# your uncommitted work is touched. That is the whole point of the worktree: a
# bump rewrites requirements.txt / package-lock.json, and checking the branch out
# in place would fight whatever you have in flight.
#
# It builds a FRESH venv inside the worktree rather than reusing this checkout's.
# For a pip bump that is the entire point: `pip install -r requirements.txt` has
# to install the versions the PR proposes, and doing it into .venv would silently
# rewrite your working environment — which is the opposite of what a review is
# for. npm gets the same treatment: `npm ci` (not `install`) installs the PR's
# lockfile exactly, and `--cache` keeps it away from the root-owned ~/.npm cache,
# which otherwise fails with EPERM on this machine.
#
# cv/ is symlinked in so the CV-tailoring suite RUNS instead of skipping under its
# requires_real_master guard: that suite holds the fpdf2 / python-docx / pypdf
# tests, i.e. exactly the dependencies most likely to be the bump. Remove the
# `ln -sfn $(CURDIR)/cv ...` line if you would rather it skipped.
#
# Expect roughly 2-4 minutes: a fresh venv, a real npm ci, then the full suite.
# ---------------------------------------------------------------------------
REVIEW_DIR ?= /tmp/dependabot-review

dependabot-list:
	@git fetch -q origin '+refs/heads/dependabot/*:refs/remotes/origin/dependabot/*' 2>/dev/null || true
	@git branch -r --list 'origin/dependabot/*' | sed 's|^ *origin/||' | grep . || echo "(no Dependabot branches open)"

dependabot-review:
	@test -n "$(BRANCH)" || { echo "usage: make dependabot-review BRANCH=dependabot/pip/<dep>-<version>"; echo "       make dependabot-list    # to see what is open"; exit 2; }
	git fetch origin '+refs/heads/dependabot/*:refs/remotes/origin/dependabot/*'
	@git worktree remove --force $(REVIEW_DIR) 2>/dev/null || true
	@git worktree prune
	@rm -rf $(REVIEW_DIR)
	git worktree add --detach $(REVIEW_DIR) origin/$(BRANCH)
	python3 -m venv $(REVIEW_DIR)/.venv
	$(REVIEW_DIR)/.venv/bin/python -m pip install -q -r $(REVIEW_DIR)/requirements.txt
	ln -sfn $(CURDIR)/cv $(REVIEW_DIR)/cv
	npm ci --prefix $(REVIEW_DIR)/web --cache $(REVIEW_DIR)/.npm-cache
	$(MAKE) -C $(REVIEW_DIR) ci
	@echo
	@echo "=== $(BRANCH) reviewed in $(REVIEW_DIR) ==="
	@echo "clean up with: make dependabot-clean"

# Throw the review worktree away. Safe to run when there is nothing to remove.
dependabot-clean:
	@git worktree remove --force $(REVIEW_DIR) 2>/dev/null || true
	@git worktree prune
	@rm -rf $(REVIEW_DIR)
	@echo "removed $(REVIEW_DIR)"

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
