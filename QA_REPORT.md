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
