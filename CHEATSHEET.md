# Cheat sheet — what to run, in what order

Every command runs from the **repository root** (`job_search_tool/`).

`README.md` explains *why* each step works the way it does; this file is just the
commands. If you only read one section, read
[Add a job from LinkedIn](#add-a-job-from-linkedin) — it is the step that is not
part of the automatic pipeline, so there is no command to guess.

**Two interpreters, on purpose.** Everything except the CV pipeline runs on the
interpreter that has `requirements.txt` installed (plain `python` in this
checkout). The CV pipeline needs its own venv — `make cv-setup` builds it — and
its commands below say `./.venv/bin/python` explicitly.

## The whole loop, in order

```bash
python -m app.main                    # 1. fetch + filter (free)      -> data/candidates.csv
#   ... paste any LinkedIn finds in here — see the next section ...
python -m app.ai_evaluate --dry-run   # 2. what is queued? (free, no API calls)
python -m app.ai_evaluate --limit 5   #    score 5 first: check quality and cost
python -m app.ai_evaluate             #    score the rest (COSTS MONEY)
make web                              # 3. board at http://localhost:3000
```

`make run` and `make evaluate` are steps 1 and 2 with interactive prompts
instead of flags. `make` with no target is `make run`.

## One-time setup (once per clone)

```bash
pip install -r requirements.txt          # needed by fetch / add_job / ai_evaluate
cp .env.example .env                     # then put your API key(s) in it
cp profile.example.yaml profile.yaml      # then describe your own background
cp contract.yaml.example contract.yaml    # then set target_permanent_salary_gbp
make cv-setup                            # .venv + rendering deps, for the CV pipeline
npm --prefix web install                 # the board's own dependencies
```

## Add a job from LinkedIn

LinkedIn is not one of this repo's sources — it restricts job data to approved
partners — but `app/add_job.py` puts a posting you found there into the same
SQLite store as everything else: same board, same status/notes, same AI scoring.

**Just the URL.** It reads the posting's public page and takes the company,
title, location, "posted" age and full advert text from it:

```bash
python -m app.add_job --url "https://www.linkedin.com/jobs/view/4471638473"

  Company      : Haley Bridge
  Title        : Senior Quality Assurance Automation Engineer
  Location     : London Area, United Kingdom
  Description  : 1853 characters (read from the LinkedIn page)
  fetched  company/title/location/posted_at: from the LinkedIn page (job 4471638473)
  Filters      : would have passed the deterministic filters
  Result       : Added Haley Bridge — Senior Quality Assurance Automation Engineer
```

Add `--dry-run` to see that without writing. Values read from the page are
LinkedIn's own structured data rather than a guess, so they are stored without
`--yes`; an explicit flag still beats them. `--no-fetch` never touches the
network, and passing `--company`/`--title` yourself also skips it.

If the fetch cannot work — the advert is down (404), an auth wall, a rate-limit —
it says which, and you paste instead. That is the next section, and it is the
only path for a posting with no reachable URL.

### Pasting it, when the URL will not do

```bash
# 1. On the LinkedIn job page: select the posting text, copy it (⌘A ⌘C works).
#    Save it to a file, or just leave it on the clipboard for step 3.

# 2. Check what would be stored. --dry-run writes NOTHING:
python -m app.add_job --url "https://www.linkedin.com/jobs/view/4467610510/" \
    --company "Savant Recruitment Experts" --title "Quality Assurance Engineer" \
    --location "London Area, United Kingdom" \
    --file data/postings/4467610510-savant-qa-engineer.txt \
    --permanent --dry-run

# 3. Add it for real — same command, drop --dry-run.
```

Read the block step 2 prints before you run step 3. It shows the fields, the
description length, and the filter verdict ("would have passed" / "would NOT
have passed"). The verdict is information only: a hand-added job is stored either
way, because choosing to add it *is* the in-scope judgement.

Optional, but it is what makes the row useful later: save the paste text under
`data/postings/` (git-ignored with the rest of `data/`) and point `--file` at
it, rather than pasting from the clipboard and having no record of the advert
once the posting comes down.

### The same thing, from the clipboard instead of a file

```bash
pbpaste | python -m app.add_job --url "https://www.linkedin.com/jobs/view/4467610510/" \
    --company X --title "Y" --location "London (hybrid)"        # piped stdin is read automatically

python -m app.add_job --url "..." --clipboard \
    --company X --title "Y" --location "London (hybrid)"        # or let it read the clipboard itself
```

### Working out company / title / location

```bash
python -m app.add_job --url "..." --fetch                                  # force the URL read
python -m app.add_job --url "..." --no-fetch                               # never touch the network
python -m app.add_job --url "..." --company X --title "Y" --permanent      # permanent (default is "unknown")
python -m app.add_job --url "..." --company X --title "Y" --contract       # contract: see below
python -m app.add_job --url "..." --company X --title "Y" --posted-at "2 days ago"
```

Order of authority: an explicit flag, then the URL fetch, then a labelled line in
a paste (`Company: Monzo`), then `--infer`. `--infer` guesses from the paste
(title on line 1, then `Company · Location · 2 weeks ago`); a **guessed** value is
printed but not written unless you add `--yes`. The guessers are unreliable by
measurement, so prefer the URL or passing the fields by hand.

### Contract roles

```bash
python -m app.add_job --url "..." --contract --file posting.txt
```

The day rate and IR35 status are read out of the advert text either way — the
flag only sets the employment type, which an advert often does not state plainly.
A URL-only run does not even need the flag when LinkedIn states a contract
employment type: it is read off the page. Run with `--dry-run` first and it
prints the rate, the IR35 verdict and what the rate is worth as a salary. A
contract below the `contract.yaml` floor is stored and flagged `Below floor`,
not refused.

### Fix, list, or remove what you added

```bash
python -m app.add_job --list                 # everything added by hand, newest first
python -m app.add_job --list --pending       # only ones not scored yet
python -m app.add_job --url "..." --company X --title "Y" --update    # overwrite an existing row
python -m app.add_job --remove "https://www.linkedin.com/jobs/view/4467610510/"
```

Adding the same URL twice is refused with "Already stored" — that message tells
you to re-run with `--update`. The URL is canonicalised (`…/jobs/view/<id>/`
regardless of tracking parameters or the slug form), because the URL is the
key that notes, status and scores all hang off.

## The same job advertised twice

Agencies re-advertise the job they are still trying to fill, under a **new advert
id**, with the title and the office address nudged. That defeats both dedup keys —
the URL differs and so does `company::title::location` — so the copy arrives on the
board as fresh, un-actioned work. Real example from this board:

```
Experis — "AI Automation Tester X2"  @ Farringdon, Central London   → applied (9 Sept)
Experis — "AI Automation Tester"     @ Fleet Street, Central London → looked new (24 Sept)
```

```bash
python -m app.find_duplicates            # CHECK: what is duplicated on the board? writes nothing
python -m app.find_duplicates --all      # ...including rows the filters dropped
python -m app.find_duplicates --apply    # STOP: link them and carry the status over
```

`--apply` records `duplicate_of` (so the board can show a re-advert as one) and
copies a decision you had **already** made onto the copies that have none, marked
as inherited. Safe to re-run — it is idempotent, it never deletes or hides a row,
and it never overwrites a status you set yourself. Running it after you apply to
something is also how the *other* adverts of that job get updated.

A match needs all three of: the same company (legal suffixes ignored), an
**identical set** of title words (with `X2`, `Contract`, `Remote`, "6 month" and
similar filler dropped), and compatible locations. It is deliberately strict: a
wrong match marks a real job as one you have already dealt with. Measured on this
board, every true duplicate sat at a title-word overlap of 1.00 while Capco's
genuinely different Principal/Senior SDET pair sat at 0.75.

On the board, a re-advert carries a grey **re-advert** chip, and the panel says
which other advert it is and where an inherited status came from. Saving a status
on that row yourself makes it yours and the "inherited" label goes away.

## Score the jobs (step 2 — costs money)

```bash
python -m app.ai_evaluate --list-models       # once per key: which model IDs work
python -m app.ai_evaluate --dry-run           # what is queued, no API calls, no cost
python -m app.ai_evaluate --limit 5           # sanity-check quality and cost
python -m app.ai_evaluate                     # score everything unscored
python -m app.ai_evaluate --rescore           # re-score the WHOLE board, overwriting old scores
```

A job already scored is never paid for twice. `--rescore` is the exception and
is what to run after the prompt, `profile.yaml`, or a stored job description has
changed — an old score and a new score are not comparable. Provider is
auto-detected from the key in `.env` (DeepSeek wins if both are set); force it
with `LLM_PROVIDER=deepseek|anthropic` or `--provider`. `--db <path>` scores a
scratch database instead of the real one.

## Open the board (step 3)

```bash
make web                 # DB_PATH already pointed at data/seen_jobs.sqlite3
                         # -> http://localhost:3000
```

`make web` is `npm --prefix web run dev` with `DB_PATH` set. The board reads and
writes `data/seen_jobs.sqlite3` directly — no export or import step — so a job
you just added is on the next refresh. From a row you can set
applied / interview / rejected and notes, and press **Scan CV** — which checks
the CV you already have against that posting and tells you what it is missing.

Production build and other `DB_PATH` details: [`web/README.md`](web/README.md).

## Scan your CV against one posting

The CV is ONE Google Doc (`MASTER_CV_DOC_URL` in `.env`); `cv/master.yaml` is the
fact bank behind it. The scan compares the posting against both and reports
whether a keyword screen would find what the advert asks for, plus any real
experience the CV is not currently showing.

```bash
make cv-doc              # is the Doc reachable, and which copy (private / link-shared / cache)?
make cv-scan URL='<posting url>'          # the ATS scan, as JSON. Free: no model call, and no
                                          # network call while the cached Doc copy is fresh
make cv-scan URL='<posting url>' ARGS='--refresh-master'   # re-read the Doc first
```

No model call, no cost, ~150ms, and the same inputs always give the same answer.

**The verdict is a rule, not a score.** `fail` means the posting demands a hard
skill under a *Requirements* heading that the CV does not mention. `borderline`
means no requirement is missing but priority-term coverage is under 85%. `pass`
means neither. `unknown` means the posting could not be read well enough to
judge — a **snippet** (Adzuna returns a 500-character teaser; the median stored
description on this board is 500 chars) or **no headings** for the weighting to
work against. `unknown` is never a pass.

**Suggestions are always verbatim lines from `cv/master.yaml`** — never generated
prose — and each is classed `add` (not on the CV at all) or `reword` (already
there, in different words). Only *priority* gaps produce them, and anything the
master cannot evidence is listed separately as **not closable**: those are the
real gaps.

### The per-posting tailoring engine (CLI only)

Retired from the board on 2026-10-01 — it produced a different CV per
application, which is a CV you will not maintain. It still works from the
command line, and it produced everything already under `cv_output/`.

```bash
make cv-setup            # once, and after any requirements.txt change
make cv-status           # what cv/master.yaml holds, and whether it is placeholder data
./.venv/bin/python -m app.cv_tailor preview --url '<posting url>'   # draft + verify, writes no CV
./.venv/bin/python -m app.cv_tailor preview --url '<posting url>' --provider deepseek
                         # same thing drafted in the cloud: ~15s instead of ~90s, a PAID call.
                         # Also works when llama-server is not running, which the default does not.
./.venv/bin/python -m app.cv_tailor render  --url '<posting url>'   # write + render the stored draft
make cv-inventory        # re-render the master full inventory — ON DEMAND ONLY
make cv-selftest         # the engine's 202 checks, incl. the fabrication guard
make cv-lessons          # what the drafter has been taught from its own proven mistakes
make cv-lessons ARGS="--clear"   # forget them all, or ARGS="--clear <mode>" for one
```

`preview` reports what the stored job text asks for and `cv/master.yaml` cannot
evidence — that gap list is what you edit `cv/master.yaml` from, by hand. A new
fact never enters a CV automatically; the CLI writes only what the master
already supports. If a CV command says the venv is missing, run `make cv-setup`.

`preview` needs `--provider` only when you want to move OFF the default: a draft
the verifier did not accept is re-drafted by hand with `--provider deepseek`. Set
a standing preference with `CV_DRAFT_PROVIDER=deepseek` in `.env` — that is what
an unflagged run reads, since `cv_tailor.py` loads `.env`.

## Maintenance and checking

```bash
python -m app.refilter                       # re-run current filters over everything stored
                                             #   -> then re-run ai_evaluate, then refresh the board
python -m app.inspect_job <job url>          # a job's full stored record, incl. description
python -m app.inspect_job --rejected 20      # last 20 rejections, with the reason for each
python -m app.main --company <slug>          # one company only, for debugging a fetcher
make test                                    # pytest + sanity scripts + the web app's tests
make test-web                                # just the web tests and typecheck
npm --prefix web run test:ui                 # Playwright UI tests (uses a fixture board, NOT data/)
```

`refilter` is the one to run after editing `filters.yaml`: it re-judges every
job already stored, including the ones that were filtered out, so a newly
broadened rule can rescue them without re-fetching anything.

## Troubleshooting

| Symptom | What to do |
|---|---|
| `python -m app.main` returns no candidates | `filters.yaml` → `location_allow_patterns` is the usual cause: if nothing matches, every job is dropped. Note it also drops postings with a blank location. |
| Add command says "Already stored" | You are re-adding the same LinkedIn job (or the same URL with different tracking parameters). Re-run with `--update` to overwrite. |
| A job is on the board with no description | The paste was empty. Check the run printed `Description  : N characters`; a piped `pbpaste` pipe is read automatically, and `--stdin`, `--clipboard` and `--file` all still work. |
| AI step says 401 / rejects the model | `python -m app.ai_evaluate --list-models`. A placeholder key in `.env` is detected, but a model ID can also have been retired — the error names the fix. |
| Score looks wrong after editing `profile.yaml` | Re-score that board with `python -m app.ai_evaluate --rescore`; scores from different inputs are not comparable. |
| A CV command or the panel says the venv is missing | `make cv-setup` — the rendering deps (`python-docx`, `fpdf2`, `pypdf`, `PyYAML`) are not installed system-wide. |
| Scan CV says it could not read the master CV | `make cv-doc`. It separates "no Doc configured" from "the service account was not given access" from "the network is down". |
| Scan CV says **Cannot tell** | The stored advert is a snippet or has no headings. Try the fuller copy the panel offers, or re-fetch the advert. Nothing is wrong with the CV. |
| The board is stale after adding a job | Refresh the page. `make web` reads the SQLite file per request; no restart needed. |
