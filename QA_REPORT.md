# QA report — component and UI test suites

Twenty tests, in two suites, over the two halves of this tool:

| Suite | Count | What it drives | Needs Docker? | Needs a model? |
|---|---|---|---|---|
| `app/tests/test_qa_component.py` | 7 | the Python pipeline in-process | no | no |
| `app/tests/test_qa_component_containers.py` | 3 | the HTTP clients over a real socket, against a containerised upstream | yes (skips cleanly without) | no |
| `web/e2e/*.spec.ts` | 10 | the Next.js board in Chrome | no | no |

Every test is labelled with its category, and the four categories the brief asked
for are covered by both suites:

| | Python | Playwright |
|---|---|---|
| **P**ositive | 4 | 4 |
| **N**egative | 2 | 2 |
| **B**oundary | 2 | 2 |
| **E**rror handling | 2 | 2 |

Nothing here calls a live ATS, a live model, or the real
`data/seen_jobs.sqlite3`. The suites are runnable on a fresh clone.

---

## Running them

```bash
# --- the whole Python suite, including the 7 non-container QA tests ---------
make test                      # from the repo root; pytest app/ picks both QA files up

# --- just the QA component tests -------------------------------------------
.venv/bin/python -m pytest app/tests/test_qa_component.py -v
.venv/bin/python -m pytest app/tests/test_qa_component_containers.py -v

# --- the UI suite ----------------------------------------------------------
make test-ui                   # headless Chrome, self-contained
make test-ui-headed            # a real visible Chrome window, for watching

cd web
npm run test:ui                # headless  (10 tests, ~13s)
npm run test:ui:headed         # headed
npm run test:ui:all            # BOTH projects, 20 runs
npm run test:ui -- --ui        # Playwright's interactive UI mode
QA_SLOWMO=300 npm run test:ui:headed   # slow the headed run down to watch it
npm run test:ui:report         # open the last HTML report
```

`make test-ui` starts everything itself — it resets the fixture database, starts
its own `next dev` on port 3100, waits for readiness, warms the routes, and tears
the server down afterwards. No setup step, and it does **not** disturb a board you
already have running on `:3000` (see "Two design constraints" below).

### Docker is optional

Only the three container-backed tests need it. With no daemon reachable they
**skip with a reason** rather than failing, so the suite is still meaningful on a
machine without Docker:

```
3 skipped — needs a Docker daemon (or QA_UPSTREAM_URL pointing at a mock already
running) and neither is available — start Docker Desktop, or run
`python app/tests/qa/mock_upstream.py --port 8123` and set QA_UPSTREAM_URL=...
```

That second route is also the fast one for iterating on those tests: the same
mock server runs as a plain local process, so you can edit an assertion and re-run
in under a second without an image pull and a container start.

---

## The Python suite

### `app/tests/test_qa_component.py` — 7 tests, no Docker

| | Test | Covers | The case that matters |
|---|---|---|---|
| P | `test_positive_pipeline_stores_a_matching_job_and_dedupes_the_next_run` | `main.process_jobs`, `dedup`, `filters` | Driven through `process_jobs` rather than the pieces in turn, because the *order* inside it is the contract (`mark_seen` before filtering, `apply_to_job` before `passes_filters`). Asserts the duplicate URL collapses, the non-QA posting is stored with `passed_filters=0` rather than discarded, and a re-fetch yields nothing new but stamps `last_listed_at`. |
| P | `test_positive_add_job_canonicalises_the_url_and_normalises_a_paste` | `add_job`, `jd_text`, `dedup` | Three shapes of one LinkedIn URL reduce to one key; an explicitly labelled paste is authoritative and config-independent; re-pasting is refused with the override named; a different URL for the same role is *allowed but flagged*. |
| N | `test_negative_postings_that_must_never_reach_the_board` | `filters` | A Python/pytest/Playwright-only JD is a `mismatch` and the rejection reason **names the words that matched**. Then the escape hatch: naming Java alongside Python makes it a Java role. Then clearance — `blocked` dropped, `eligible`/`BPSS`/`"would be a benefit"` kept and flagged, checked on clearance *alone*. |
| N | `test_negative_contract_below_the_floor_is_refused_but_a_missing_rate_is_not` | `contract_rates`, `filters` | The refusal, and both deliberate escapes: no rate stated (kept) and a rate only Adzuna *estimated* (never acted on). Getting these the wrong way round is the difference between a shortlist and an empty page, and neither shows up as an error. |
| B | `test_boundary_day_rate_exactly_on_the_threshold` | `contract_rates` | Exactly at the floor passes; a penny under fails; between the inside and outside thresholds with IR35 unstated is `review`, not `fail`; no rate is `unknown`. Thresholds are **read from the configured model**, so this keeps testing the edge after `contract.yaml` is retuned. |
| B | `test_boundary_language_classification_and_dedup_key_normalisation` | `filters`, `dedup` | Silence (`none`) is not a mismatch and ranks below a named language but above a mismatch. The dedup key normalises case/punctuation/whitespace, but the location is part of it, so the same title in two cities is two jobs. |
| E | `test_error_malformed_config_and_old_schema_never_answer_silently` | `filters`, `dedup`, `add_job` | An empty keyword list would compile to `()` and match *every* title — asserted to raise instead. A typo'd column name raises rather than being dropped. The CLI refuses to run with no URL. A job with no description is handled everywhere without rejecting on stack alone. Finally, an old-shaped database is migrated in place with its rows intact. |

### `app/tests/test_qa_component_containers.py` — 3 tests, container-backed

| | Test | Covers |
|---|---|---|
| P | `test_positive_every_fetcher_parses_a_real_response` | Greenhouse, Ashby, Workable, Lever, SmartRecruiters and Remotive, each against a real HTTP response, **with the request verified** (`content=true`, the pagination offsets) — a fetcher that stopped sending `?content=true` would still parse a mock's response while going blind in production. Both pagination branches are covered: a short page stops the loop, a full page advances the offset. |
| P | `test_positive_llm_provider_completes_a_forced_tool_call` | The forced-tool-call protocol end to end, through the real `ai_evaluate.evaluate_one`, against a containerised OpenAI-compatible endpoint. Asserts the *payload*: `tool_choice` forced, `temperature: 0`, `thinking: disabled`, the schema, and the screening signals handed to the model rather than re-derived. |
| E | `test_error_upstream_failures_are_loud_and_never_a_silent_empty_result` | 500 → `HTTPStatusError`; a truncated 200 body → a JSON decode error; Adzuna 401 → the credential-advice message; a non-401 error body **echoing the app_key** → redacted to `[REDACTED]`; a 500 from the LLM → `ProviderError` naming `DEEPSEEK_API_KEY`; a response with no usable tool call → `None` plus a diagnosis that reaches the operator. |

**Why containers here, and nowhere else.** The clients' existing unit tests
replace `httpx.get` with a `MagicMock`, which verifies parsing and nothing about
the transport. A mock cannot produce any of the failures that actually break a
scheduled run: a 500 with an HTML body, a 200 with a truncated body, a 401 that
echoes your credentials. So `app/tests/qa/mock_upstream.py` is a real HTTP server
on `python:3.12-alpine`, holding no dependency but the standard library — no
image build to keep in sync with `requirements.txt`, and a container that starts
in under a second.

The client's hard-coded host is redirected by `app/tests/qa/upstream.py`, which
rewrites the URL's scheme and authority and **nothing else**: path, query,
headers, timeout and body are the client's own, and `httpx` opens a real socket.
The alternative — adding a `base_url` parameter to six fetchers — would put a
test-only seam into the production code and invite exactly the "configurable in
tests, wrong in production" drift this repo refuses elsewhere.

The URL shim also carries a scenario prefix, so one container serves both the
happy path and every failure mode with no control-plane API:

```
/ok/greenhouse/v1/boards/<slug>/jobs      200 + well-formed JSON
/err500/...                               500 + an HTML error page
/badjson/...                              200 + a body that is not JSON
/authfail/adzuna/...                      401 + Adzuna's AUTH_FAIL shape
/nofn/openai/chat/completions             200, no tool call, no parseable content
```

---

## The Playwright suite

Ten tests over five of the journeys, in `web/e2e/`, all against a seeded fixture
board — never `data/seen_jobs.sqlite3`.

| | Test | Journey |
|---|---|---|
| P | `board.spec.ts` › P1 | Board loads; then every toolbar filter narrows it (Type, Language, AI status, My status), filters **combine**, and a combination matching nothing says so. |
| P | `board.spec.ts` › P2 | Search on company *and* title, case-insensitively; then clicking Score and Posted reorders — including that unknowns sink, and that the top-scoring row stops being first once you sort by date. |
| P | `status.spec.ts` › P3 | The inline per-row status select writes for real, does **not** open the detail panel, survives a Refresh, is findable under the new status filter, and can be cleared. |
| P | `tailoring.spec.ts` › P4 | The whole CV journey: panel → draft → in-progress → review → **edit the YAML** → approve → rendered links. Asserts the text sent to `/render` is what the user edited, not what the model returned. |
| N | `board.spec.ts` › N1 | A no-match search shows the empty state — and does *not* offer the date-window escape, because that would imply a fix that does nothing. Then the case where the window **is** the cause, with the escape actually working. |
| N | `status.spec.ts` › N2 | Four invalid `my_status` values are each rejected with 400 and a message naming the value and the accepted set; a missing and an unknown URL are rejected for their own reasons; a forced 500 is surfaced in the browser rather than looking like a mis-click; and the refusal is not persisted. |
| B | `board.spec.ts` › B1 | The date window's closed edge: exactly 30 days old is visible, 31 days is hidden, undated is always kept, the announcement count tracks the window, "Show all dates" rescues everything, and the choice is sticky across a Refresh. |
| B | `board.spec.ts` › B2 | A two-year-old posting that is still listed stays visible and says so, with both dates in its tooltip; and the two never-re-listed old rows get no such rescue and no chip. |
| E | `states.spec.ts` › E1 | A slow fetch shows `Loading...`; an unreadable one shows the error, names the server's reason, suggests the fix, and renders **no table and no empty state** (an empty board must never look like a broken one); the overlay shows `Loading the stored CV…` instead of guessing the wrong phase; and a refused render leaves the overlay open quoting the pipeline verbatim, with both the fabrication-specific and the machinery-failure branches asserted. |
| B | `tailoring.spec.ts` › B2 | A stored draft opens straight into review with **no model call**, showing the stored YAML, verdict and findings; and a record claiming `rendered` whose PDF is gone says "CV file missing — re-render" and offers **no dead link**. |

### The model is stubbed in the UI tests, deliberately

`/api/tailor/preview` and `/api/tailor/render` shell out to `app/cv_tailor.py`,
which makes a ~90-second local-LLM call plus a DeepSeek verification call.
Driving that from a UI test would make the suite slow, non-deterministic,
dependent on model credentials, and liable to fail for reasons unrelated to the
interface. So those two routes are intercepted with schema-shaped responses, and
the assertions are about the *interface's* contract — the phases, the text that
goes back for rendering, and the honesty of the failure states. `/api/jobs`,
`/api/jobs/tailoring` and `/api/status` are **not** stubbed (except where a
failure is being forced), so every assertion about stored state is reading a real
database through the real route.

### The fixture board

`web/e2e/scripts/make-fixture-db.mjs` seeds twelve postings, each chosen so a
test's expectation is an exact number rather than a range, and each documented in
that file with the reason it exists: a contract IR35 role with Java and a day
rate; a JS/Node secondary; a silent-stack role; the stale-but-still-listed one
(and the highest scorer, so the sort test can prove it moves); one posted exactly
30 days ago and one 31 days ago; an undated one; one never evaluated and never
tailored; one whose record says rendered with no PDF; one with a stored draft and
the verifier's findings; one with obtainable clearance; and a 200-day-old one in
epoch-millis form.

Four *date formats* appear across them (ISO with `Z`, SQLite's local
`CURRENT_TIMESTAMP`, a relative phrase, epoch millis) because `lib/dates.ts`
exists precisely because the sources disagree, and a single-format fixture would
never exercise the other three.

Reset is in-place `DELETE`/`INSERT` rather than a new file, because `next dev`
holds an open handle on the path for the life of the server — recreating the file
underneath it would leave the server reading an unlinked inode, and a per-test
reset would appear to do nothing. `workers: 1` plus a reset hook per spec makes
each test's starting state a fact rather than a race; two tests write to the
board, so this matters more than the seconds parallelism would save.

---

## Two design constraints worth knowing about

**1. Next 16 refuses a second `next dev` for the same directory.** It takes an
exclusive lock at `<distDir>/lock` and exits with "Another next dev server is
already running" — right for a human, and fatal for a test server when you have
the board open on `:3000`. So `web/next.config.js` gained one line:

```js
distDir: process.env.NEXT_DIST_DIR || ".next",
```

Default unchanged; only the E2E wrapper sets it. The test server therefore has its
own build directory and its own lock, and the two run concurrently without
touching each other's output. `web/tsconfig.json` gained the matching `include`
entries so Next stops rewriting that file on every run, and a Playwright
`globalTeardown` (`web/e2e/global-teardown.ts`) puts the auto-generated
`next-env.d.ts` back to the `.next` build directory afterwards — without it, every
UI run leaves a tracked file modified and points `tsc` at a directory a later
`rm -rf .next-e2e` would remove.

**2. The board's two overlays share DOM ids.** `JobDetailPanel` and
`TailoringPreview` both render `id="overlay"`, `id="panel"`, `id="panel-header"`,
`id="panel-body"` and `id="panel-close"`, and both are mounted at the same time
while a CV is being drafted. The specs scope around it (`#panel:not(.tailor-panel)`,
`.tailor-panel`), but it is a real defect — see finding 5 below.

---

## Findings

Four issues surfaced while writing these tests. The first three are defects or
gaps; each is stated with the evidence that produced it.

### 1. `add_job(url="")` stores an empty primary key, then swallows other postings — **defect**

`add_job.add_job(conn, url="", company="Co1", title="Role 1", …)` stores a row
whose primary key is the empty string. The consequence is not cosmetic: the next
call with a *different* company and title is reported as `Already stored (Co1 —
Role 1)` and refused, because `dedup.get_details_by_url("")` matches the first
row. One bad call silently swallows every subsequent URL-less posting.

Verified directly against a temporary database:

```
add_job empty url #1: True  Added Co1 — Role 1
add_job empty url #2: False Already stored (Co1 — Role 1). Re-run with --update to overwrite it.
```

That this is a defect rather than a design choice is settled by the codebase's own
precedent: `ats_clients.fetch_smartrecruiters` skips a posting rather than storing
`url=""` for exactly this reason, in as many words. The CLI is safe — `--url` is
required, and that *is* asserted — so the gap is reachable through the Python API,
which is how the pipeline and any future caller would use it. Recorded in the
module docstring of `app/tests/test_qa_component.py`; it has no test slot because
that module is held to ten cases by the test plan.

### 2. An epoch timestamp stored as a REAL defeats the date parsing — **robustness gap**

`lib/dates.ts` handles epoch millis as a JS number and as a clean digit string
(`^\d{10,13}$`), and `lib/dates.test.ts` covers both. It does **not** handle
`"1772884800000.0"` — which is what SQLite's TEXT affinity produces when a *float*
is written to `posted_at`:

```
Python int   1772884800000   -> stored "1772884800000"     parsed fine
Python float 1772884800000.0 -> stored "1772884800000.0"   NOT parsed
```

The string fails the regex, `Date.parse` fails, and the posting is reported as
`no date`. Because `withinWindow` always keeps undated rows, the effect is that
**the posting becomes permanently visible and the date window can never hide it** —
a silent wrong answer of exactly the kind the module's own comments are written to
prevent.

Reachable: `ats_clients.fetch_lever` uses Lever's `createdAt`, documented in place
as "epoch millis". If that ever arrives as a JSON float, this is the path. The fix
is one of `int(...)` on the way in, or allowing an optional `.\d+` in the regex.

Found because the first version of the fixture wrote the value with
better-sqlite3, which binds a JS number as a double — the fixture now writes the
digit-string form, which is what Python produces, and the gap is recorded here
rather than encoded as expected behaviour.

### 3. The README says Go is a dealbreaker; `filters.yaml` matches only "golang" — **documentation mismatch**

`README.md` lists the stack dealbreakers as "C#/.NET, C++, Go, PHP, Kotlin, Swift,
Scala, Python and Playwright", but the configured pattern is `\bgolang\b`. So a
posting saying "We use Go and Kubernetes" is classified `none` (kept, ranked low)
rather than `mismatch` (dropped):

```
We write Golang and deploy to Kubernetes.  -> mismatch
We go fast and deploy to Kubernetes.      -> none
Golang and JavaScript.                    -> secondary
```

The narrow pattern is the *right* call — `\bgo\b` is an English verb and would
classify a large slice of unrelated postings as Go shops, and dropping on a guess
is worse than ranking down — so the README's "Go" should be read as shorthand for
Golang. Worth a parenthetical there, since a reader who trusts the list would
expect a "Go developer" advert to be filtered out. Asserted from both sides in the
boundary test so the trade-off is visible rather than accidental.

### 4. `add_job.parse_paste` withholds a guess outside the configured market — **behaviour worth documenting**

The "Company · Location · age" middot line is the most reliable structure in a
LinkedIn paste, but the guess it produces is gated on
`filters.location_is_allowed`. With a market that refuses the paste's location,
`--infer` returns no company and no location at all — the guess is withheld rather
than offered as a maybe. That is defensible, but it means `--infer` can appear to
do nothing on a posting from outside the configured market with no explanation.
Asserted from both sides in the positive test, with a stubbed allowlist, which
also keeps that test independent of whichever market `filters.yaml` points at.

### 5. The two overlays share DOM ids — **defect, minor**

While a CV is being drafted, `#overlay`, `#panel`, `#panel-header`, `#panel-body`
and `#panel-close` each exist twice in the document. Duplicate ids are invalid
HTML, break `document.getElementById` and any `label for=`, and are invisible to
the eye. The specs scope their locators around it; the app should give the
tailoring overlay its own ids.

One further robustness note, not a defect: the board's `/api/status` answers GET
with 405 rather than 404, which made Playwright's webServer readiness probe
(`webServer.url`) time out while the board was serving perfectly well. The config
now probes `/api/jobs` — a GET route — and carries a comment saying why.

---

## What is deliberately not covered

- **Real model calls.** No test reaches DeepSeek, Anthropic or a local
  llama-server; the LLM is exercised against a containerised
  OpenAI-compatible server, which validates the protocol, not the answers.
- **A real CV render.** `fpdf2`/`python-docx` output is not produced; the two
  tailoring routes are stubbed. The vendored engine's own 198 checks cover that
  (`make cv-selftest`).
- **Real ATS/aggregator network calls.** The clients are driven against the
  containerised upstream. Confirming a *specific* company's slug still needs
  `python -m app.main --company <slug>`.
- **Browsers other than Chrome.** The brief asked for Chrome, headless and
  headed. Firefox and WebKit would need `npx playwright install` and a channel
  change; the assertions are engine-independent, so the projects are the only
  change.
- **Mobile viewports and an accessibility audit.** `viewport: 1440×900` is set
  because the board is a nine-column desktop table, and a phone layout would be
  testing something that does not exist. No axe/ARIA audit is included — finding
  5 is the kind of thing one would surface.

## Extending the suites

The two seams are deliberately narrow. To add a Python case, add a function to
`test_qa_component.py` (mark it `@pytest.mark.real_filters` only if the assertion
is about the shipped filter or contract *policy*; otherwise it runs against the
pinned test config and survives a retune). To add an upstream failure, add a
scenario to `mock_upstream.py`'s dispatch and its `SCENARIOS` tuple, then select
it with the shim. To add a UI case, add a row to the fixture cast and a spec —
the expected counts all live in `web/e2e/fixtures.ts`, so a changed cast cannot
leave one spec asserting yesterday's arithmetic.

---

# CI report — how these tests actually run on a pull request

## First correction: it is GitHub Actions, not Jenkins

There is no Jenkins here, and nothing to configure on one. Every automatic test
run for this repository is a **GitHub Actions** workflow called `CI`, defined in
`.github/workflows/ci.yml` (197 lines, comments included). A pull request shows
three check rows — `Python tests`, `Web tests`, `UI tests` — and those are the
three jobs of that one workflow.

The `Dependabot Updates` workflow you will also see in the Actions tab is a
different thing: it is Dependabot's own dynamic workflow, and it is what *creates*
the branch and the pull request. `CI` is what then runs on the resulting pull
request. On PR #7 both are visible in the run list — `pip in / for fpdf2 -
Update #1604840815` (the producer) followed a minute later by `CI`.

All figures below are taken from the real run for
[PR #7](https://github.com/edminshull/job_search_tool/pull/7),
[run 37023149803](https://github.com/edminshull/job_search_tool/actions/runs/37023149803),
on the `dependabot/pip/fpdf2-gte-2.8.8` branch.

## What triggers a run

| Trigger | Branches | Effect |
|---|---|---|
| `pull_request` | any branch, any PR (including forked and Dependabot PRs) | the three jobs run against the PR's merge result |
| `push` | `main` only | the same three jobs re-run on `main` after a merge |

Three workflow-level settings do real work and are easy to miss:

- `permissions: contents: read` — the run cannot write to the repository. That is
  deliberate: nothing in the workflow needs to, and it keeps a compromised
  dependency in a test path from being able to push.
- `concurrency: group: ci-${{ github.ref }}` with `cancel-in-progress: true` — a
  new push to the same branch kills the previous run. So when Dependabot rebases
  a branch, the old run goes grey (`cancelled`) rather than lingering as a stale
  red or a stale green. **A cancelled check is not a pass.**
- `on.pull_request` with **no** `pull_request_target` anywhere. That is the
  correct choice and the file header says why: a `pull_request` run gets no
  repository secrets at all, so there is nothing for a Dependabot-authored branch
  to exfiltrate. Using `pull_request_target` here would hand untrusted code a
  token for no benefit.

## The three jobs

| Check name (job) | Runner | Timeout | Real duration on PR #7 |
|---|---|---|---|
| `Python tests` | `ubuntu-latest` | 15 min | **26 s** |
| `Web tests` | `ubuntu-latest` | 15 min | **25 s** |
| `UI tests` | `ubuntu-latest` | 20 min | **1 m 11 s** |

The whole run took **1 m 15 s**. The jobs are independent and run in parallel, so
wall-clock time is the slowest job, not the sum.

### `Python tests`

1. `actions/checkout@v7`
2. `actions/setup-python@v7` with **Python 3.14**, pip cache keyed on
   `requirements.txt`
3. `python -m venv .venv` — a *real* venv at the repo's own documented path, not
   an install into the system interpreter. `Makefile` defines
   `VENV := $(CURDIR)/.venv`, and `app/cv_tailor._run_script` invokes the
   tailoring engine as a **subprocess** with that interpreter, so CI reproduces
   the layout the code assumes rather than a convenient approximation of it.
4. `pip install --upgrade pip` then `pip install -r requirements.txt`
5. `QA_SKIP_DOCKER_TESTS=1 .venv/bin/python -m pytest app/ -q`
6. the three standalone `main()`-style sanity scripts:
   `app.tests.test_pipeline`, `app.tests.test_discover_companies`,
   `app.tests.test_ai_evaluate`

Observed output for steps 5–6 on PR #7:

```
352 passed, 140 skipped in 3.49s
7/16 passed title+location+stack filters
All assertions passed.
All assertions passed.
All assertions passed.
```

### `Web tests`

1. `actions/setup-node@v7` with **Node 26**, npm cache keyed on
   `web/package-lock.json`
2. `npm ci --prefix web` — `ci`, never `install`, so the PR's **own lockfile** is
   what gets installed and therefore tested
3. `npm --prefix web test` → `node --test lib/*.test.ts` (Node ≥ 22.18 strips the
   TypeScript types itself; that is why this repo needs no jest/vitest and no
   transpile step)
4. `npm --prefix web run typecheck` → `tsc --noEmit`

Observed: `tests 33 / pass 33 / fail 0`, then a silent successful typecheck.

### `UI tests`

1. `npm ci --prefix web`
2. `npm --prefix web run test:ui` → `playwright test --project=chrome-headless`
3. on failure only, `actions/upload-artifact@v7` uploads
   `web/playwright-report/` with 7-day retention

Observed: `Running 21 tests using 1 worker` … `21 passed (49.0s)`.

Two details make this job fragile in a way the other two are not:

- **There is no `playwright install` step, on purpose.** `playwright.config.ts`
  sets `channel: "chrome"`, i.e. the Google Chrome already baked into the runner
  image, rather than a downloaded Chromium build. If a runner image ever ships
  without Chrome, this job fails with a browser-launch error that looks nothing
  like a test failure.
- **`retries: 1` in CI** (`retries: process.env.CI ? 1 : 0`). A flaky UI test is
  retried once before the job goes red, so a green `UI tests` check can hide one
  flake. The log says so; the check colour does not.

The suite needs no setup step: `web/e2e/scripts/serve-fixture-board.mjs` resets
its own fixture database, starts `next dev` on port 3100 against it, warms the
routes and tears it down. `data/seen_jobs.sqlite3` is never touched, which is why
this job can run on a pull request from a fork.

## The numbers, and why 140 tests are skipped

CI collects **492** Python tests and reports **352 passed, 140 skipped**. None of
the 140 is an accident, and none is hidden behind an `--ignore`:

| Skipped | Reason |
|---|---|
| 137 | `app/tests/test_cv_tailor.py`, guarded by `requires_real_master`. That suite deliberately tests the real `cv/master.yaml` ("a fixture master would let a real dangling reference pass"), and `cv/` is gitignored because it is a real person's CV. CI has no such file, so the module skips naming the reason instead of producing 16 failures and 11 errors. |
| 3 | `app/tests/test_qa_component_containers.py`, behind the repo's own `QA_SKIP_DOCKER_TESTS` opt-out. They pull `python:3.12-alpine` and serve the mock upstream over a real socket — fine locally, slow and network-dependent in CI. |

The skip guard is load-bearing rather than cosmetic: a GitHub runner **has**
Docker but **not** the `testcontainers` package (deliberately absent from
`requirements.txt`), so a Docker-only guard used to pass and the three tests
*errored on import*. The guard now checks for the package as well.

**The same suite, run here, reports `489 passed, 3 skipped in 3.41s`.** The
arithmetic reconciles exactly: `489 − 352 = 137` and `140 − 3 = 137`. The only
difference between local and CI is that this machine has `cv/master.yaml` and no
reachable Docker daemon. Nothing is silently skipped in one place and run in the
other.

## How CI and the local targets line up

| Target | Runs | Equivalent to |
|---|---|---|
| `make test` | pytest `app/` + the 3 scripts + `npm --prefix web test` | most of CI, **plus** the container tests when Docker is up, **minus** typecheck and UI |
| `make test-web` | web unit tests + typecheck | the whole `Web tests` job |
| `make test-ui` | Playwright headless | the whole `UI tests` job |
| `make ci` | all of the above in CI's order, with `QA_SKIP_DOCKER_TESTS=1` | a CI run, minus `actions/checkout` and the artifact upload |

So a green `make ci` means a green CI, with one honest caveat the Makefile states
itself: it does **not** mean everything ran, because the three container tests are
skipped in exactly the way CI skips them.

### Reviewing a Dependabot PR locally

The repository already ships the workflow for this, and it is the reason a bump
never has to be checked out in place:

```bash
make dependabot-list                                    # what is open
make dependabot-review BRANCH=dependabot/pip/fpdf2-gte-2.8.8
make dependabot-clean
```

`dependabot-review` creates a detached worktree at `/tmp/dependabot-review`,
builds a **fresh** venv there (so installing the PR's `requirements.txt` cannot
rewrite your working `.venv`, which would defeat the point of a review), symlinks
`cv/` in so the tailoring suite *runs* rather than skips, runs `npm ci` inside
`web/`, then runs the same `make ci` target. Expect 2–4 minutes.

The `ci.yml` header documents the same idea for a single dependency without the
full target, and adds one warning worth repeating: the `ln -s "$PWD/.venv"
/tmp/db-review/.venv` trick works for Python, but **do not symlink
`web/node_modules` the same way** — Turbopack rejects a `node_modules` symlink
pointing outside the project root and `next dev` will not start. Run a real
`npm ci --prefix web` instead.

## What Dependabot changes about the run

This matters more than anything else on this page, because it is what makes CI
safe on Dependabot PRs and what makes some automations impossible:

- **The workflow definition is taken from the BASE branch.** A `ci.yml` that
  exists only on the PR branch is never executed. That is why the file header
  says it must be merged to `main` *before* Dependabot's first PR will be tested.
- **The `GITHUB_TOKEN` is read-only and Actions secrets are unavailable** for
  runs triggered by Dependabot (`github.actor == 'dependabot[bot]'`) on
  `pull_request`, `push`, `create` and similar events. This workflow needs
  neither, so it runs unmodified — and the header flags the trap: if a future job
  needs a credential, it will silently get nothing on a Dependabot PR.
- **`pull_request_target` does not rescue that.** GitHub's Dependabot-on-Actions
  reference states that when the base ref of a pull request was created by
  Dependabot, the token is read-only and secrets are unavailable on
  `pull_request_target` as well, and that this applies even if the workflow is
  re-run by a different actor. The consequence worth writing down: **a merge
  workflow triggered by Dependabot's own `pull_request` events cannot merge the
  PR.** Only a *scheduled* run — or any run not initiated by Dependabot, such as
  one on your own machine with your own `gh` token — gets a writable token.

## Reading a run for a pull request

```bash
# What the checks say. All three must be `pass`; `pending` is not a pass.
gh pr checks 7 --repo edminshull/job_search_tool

# The run itself, down to one job's log.
gh run view 37023149803 --repo edminshull/job_search_tool --log --job 110891024350
```

Two traps specific to this checkout, both hit while writing this report:

- **Pass `--repo` explicitly.** `gh` resolves the default repository to
  `fedorchenko-juli/job_search_tool` (the upstream this is a fork of), so a bare
  `gh pr view 7` fails with *"Could not resolve to a PullRequest with the number
  of 7"* while `gh pr list` quietly shows the upstream's pull requests. No
  `remote.origin.gh-resolved` is set. Any script or cron job must pass
  `--repo edminshull/job_search_tool` on every call.
- **`gh run view --log` fails under a restricted cache directory** with
  `failed to get run log: creating cache directory: mkdir ~/.cache: operation not
  permitted`. Under a sandboxed or CI-like environment, fetch logs through the
  API instead — `gh api --allow-escape-sequences
  repos/edminshull/job_search_tool/actions/jobs/<job-id>/logs` — which needs no
  cache write.

Useful extras:

```bash
gh run rerun <run-id> --repo edminshull/job_search_tool     # after a known flake
gh run watch <run-id> --repo edminshull/job_search_tool     # block until it settles
gh pr view 7 --repo edminshull/job_search_tool \
  --json mergeStateStatus,mergeable,statusCheckRollup       # machine-readable state
```

## Findings

### 1. `main` is not protected, so nothing currently stops a red merge — **process gap**

`gh api repos/edminshull/job_search_tool/branches/main/protection` returns
`404 Branch not protected`. The three CI checks are advisory: any merge — by
hand, by a script, or by a later automation — can land code whose checks are red,
pending, or still running. Requiring `Python tests`, `Web tests` and `UI tests`
on `main` is the single change that turns this CI from a report into a gate.

### 2. Auto-merge is switched off at the repository level — **configuration gap**

`allow_auto_merge: false`, so `gh pr merge --auto` fails today and "merge once
green" has to be a polling loop. Switching it on would let GitHub itself hold a
pull request in a queue until its checks pass — strictly more reliable than
anything polling from outside, because the wait happens on GitHub's side.

### 3. This report undercounts the UI suite — **documentation drift**

The summary table at the top of this file says `web/e2e/*.spec.ts` is **10**
tests, and `web/playwright.config.ts` still says "All ten tests share one board"
in its `workers: 1` rationale. The suite is **21** tests across five spec files
(`board` 5, `cvscan` 8, `duplicates` 5, `states` 1, `status` 2), and both
`Makefile` and the CI log already say 21. Same drift, three files; the config and
this report are the two that are stale.

### 4. CI installs the newest version its lower bounds allow, not the one the PR names — **behaviour worth knowing**

`requirements.txt` uses lower-bound ranges. On PR #7 the diff is one line,
`fpdf2>=2.7` → `fpdf2>=2.8.8`, and CI installed **fpdf2-2.8.9** (with
`pytest-9.1.1`, `pypdf-6.19.0`, `pyyaml-6.0.3`, `python-docx-1.2.0` for good
measure). That is correct behaviour for a range-based requirements file, but it
means a green run on a bump PR is evidence about the resolver's choice that day,
not about the version in the PR title. Two consequences: the same PR can go green
today and red tomorrow when a newer patch is published, and a green fpdf2 PR is
not proof that fpdf2 2.8.8 specifically works.

### 5. `ubuntu-latest` is migrating to Ubuntu 26 on 19 October 2026 — **scheduled risk**

Every run currently carries the annotation *"The ubuntu-latest label will migrate
to Ubuntu 26 beginning October 19, 2026"* on all three jobs. Nothing here pins a
runner image, so when that migration lands the job to watch is `UI tests` — it
depends on the image's preinstalled Google Chrome rather than on a
Playwright-managed browser.

## What this CI does not cover

- **The container-backed QA tests.** Skipped in CI by design; `make test` with
  Docker up is the only place they run.
- **The CV-tailoring suite.** Skipped unless `cv/master.yaml` exists. For a bump
  to `fpdf2`, `python-docx` or `pypdf` — precisely the dependencies that suite
  exercises — a green CI is *not* sufficient evidence. `make dependabot-review`
  exists for that reason and symlinks `cv/` in.
- **Anything needing a secret or the network.** By construction: no live ATS, no
  live model, no live Google Doc. That is what makes the workflow safe on a
  Dependabot PR, and it is also why a green run says nothing about those paths.
- **A browser other than Chrome**, and no mobile viewport (as the earlier section
  of this report already notes).
- **Branch protection or merge gating.** Finding 1: the checks report, they do
  not block.

---

# Automating the Dependabot queue

Clearing the Dependabot pull requests by hand every morning is the tedium that
`scripts/dependabot_merge.py` and `.github/workflows/dependabot-merge.yml`
remove. The interesting part is not the automation, it is the list of things it
refuses to do — every one of them a place where "the tests passed" is not the
same as "this is safe".

## The division of labour

| Work | Owner |
|---|---|
| Green patch/minor bumps, unattended, 07:30 / 11:00 / 15:00 UTC | `.github/workflows/dependabot-merge.yml` |
| Rebasing bumps that fell behind `main` | the same workflow, via `@dependabot rebase` |
| Green bumps of fpdf2, python-docx, pypdf | a local `make dependabot-review`, then an explicit `--ci-blind allow` |
| Major bumps | a person, on purpose |
| Anything with a red check | nobody. Reported on the PR and left open |

## Three rules that make it safe

1. **The verdict binds to a commit, not to a pull request.** Checks are read from
   `/repos/{repo}/commits/{head-sha}/check-runs` — the exact commit whose three
   check names must all be `completed`/`success` — and the merge is issued with
   `gh pr merge --match-head-commit <sha>`, so GitHub refuses it if the branch
   moved in between. Without that pair, a force-push between "green" and "merge"
   is a real hole.
2. **Missing, pending and cancelled are all not-passes.** `pending` is never a
   pass; a `cancelled` check is never a pass (this workflow's `concurrency`
   cancels the previous run on every push, so `cancelled` is routine); and a
   required check that is absent entirely is treated as a *failure*, not as an
   absence of evidence. If `ci.yml` were deleted, nothing would merge.
3. **Majors need a person.** `classify()` reports `major` for a major version
   change, for a `0.x` minor (semver promises nothing below 1.0), for a grouped
   update whose group name contains `major`, and for any title it cannot parse.
   None of those merge without `--include-majors`, typed deliberately.

## The refusal that matters most here: CI-blind dependencies

For **fpdf2, python-docx and pypdf** the tool will not merge on a green CI, and
that is not caution — it is a fact about this repository. The suite that
exercises them, `app/tests/test_cv_tailor.py`, deliberately tests the real
`cv/master.yaml` because "a fixture master would let a real dangling reference
pass", and `cv/` is gitignored. In CI that module skips: **137 tests**. The
`ci.yml` header says so directly — *"For a dependabot bump to fpdf2 /
python-docx / pypdf — the dependencies that suite actually exercises — run it
locally on the PR branch."*

So those bumps come back as `LOCAL_REVIEW` with the exact command to run, and
become mergeable only after `make dependabot-review` has been green on that
branch. Notably, this catches **PR #7** (fpdf2 2.7 → 2.8.8), which is a green,
mergeable, minor bump that CI alone cannot vouch for — the case that motivated
writing this down.

A grouped pip PR is checked for these names too: `touched_blind_deps()` reads the
diff when the title is a group title (`Bump the python-minor-patch group with 4
updates`), because with the grouping in `.github/dependabot.yml` a grouped pip PR
will regularly contain one of the three.

## Why the workflow is triggered by `schedule`, not by `pull_request`

This is the single most counter-intuitive thing in the design, and it is why the
usual "auto-merge on the PR event" recipe cannot work in this repository. A
workflow triggered by Dependabot's own `pull_request` events gets a **read-only
`GITHUB_TOKEN`**, so it cannot merge. GitHub's Dependabot-on-Actions reference
also rules out the workaround: on `pull_request_target`, when the base ref was
created by Dependabot, the token is read-only there too, "even if the workflow is
re-run by a different actor".

A **scheduled** run is not Dependabot-initiated, so it gets the token its
`permissions:` block asks for (`contents: write`, `pull-requests: write`) and can
merge. Two consequences follow from that, both documented in the workflow header
because both are silent failures:

- **This repository is a public fork and scheduled workflows are disabled by
  default in forks.** Until it is enabled, the file does nothing at all — no run,
  no error, no warning.
- **GitHub disables scheduled workflows after 60 days of repository inactivity.**

`concurrency: {group: dependabot-merge, cancel-in-progress: false}` is the other
non-obvious setting: queued rather than cancelled, because cancelling mid-run
would abandon a half-decided queue, and two concurrent runs would each be judging
pull requests against a `main` the other one is moving.

## Settings this depends on

The script fails closed on its own, but two repository settings decide how much
of GitHub's own machinery backs it up — see findings 1 and 2 above:

- **Branch protection** with `Python tests`, `Web tests`, `UI tests` as required
  checks. This is the only control that still holds when the script is wrong.
- **Auto-merge enabled** (`allow_auto_merge`), which is what lets `--auto` hand
  "wait for the checks" to GitHub instead of polling. Auto-merge only truly waits
  for checks that branch protection marks *required*, so the two go together.

## Running it by hand

```bash
cd job_search_tool
python3 scripts/dependabot_merge.py                     # dry run — changes nothing
python3 scripts/dependabot_merge.py --apply --auto      # the routine case
gh workflow run dependabot-merge --repo edminshull/job_search_tool   # the workflow, dry
```

The dry run is the default and its stdout *is* the report — the same markdown the
workflow appends to `$GITHUB_STEP_SUMMARY`. Nothing is mutated without `--apply`,
which matters because a merge is not reversible.

`--max-merges` defaults to **1**: each merge moves `main`, so every later pull
request in the same run would be judged against a base that no longer exists.

