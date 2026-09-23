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

LinkedIn cannot be fetched — it restricts job data to approved partners and
prohibits scraping, so no fetcher in this repo can see it. You paste it instead,
and it lands in the same SQLite store as everything else: same board, same
status/notes, same AI scoring. `app/add_job.py` does the writing.

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
have passed"). The verdict is information only: a pasted job is stored either
way, because choosing to paste it *is* the in-scope judgement.

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

### Only company / title / location are required

```bash
python -m app.add_job --url "..." --company X --title "Y" --permanent      # permanent (default is "unknown")
python -m app.add_job --url "..." --company X --title "Y" --contract       # contract: see below
python -m app.add_job --url "..." --company X --title "Y" --posted-at "2 days ago"
```

`--infer` guesses the fields from the paste (title on line 1, then
`Company · Location · 2 weeks ago`). A value read off an explicit label
(`Company: Monzo`) is used automatically; a **guessed** one is printed but not
written unless you add `--yes`. The guessers are unreliable by measurement, so
prefer passing the three fields by hand.

### Contract roles

```bash
python -m app.add_job --url "..." --company X --title "Y" --contract --file posting.txt
```

The day rate and IR35 status are read out of the pasted text either way — the
flag only sets the employment type, which an advert often does not state
plainly. Run it with `--dry-run` first and it prints the rate, the IR35 verdict
and what the rate is worth as a salary. A contract below the `contract.yaml`
floor is stored and flagged `Below floor`, not refused.

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
applied / interview / rejected and notes, and press **Tailor CV**.

Production build and other `DB_PATH` details: [`web/README.md`](web/README.md).

## Tailor a CV for one posting

```bash
make cv-setup            # once, and after any requirements.txt change
make cv-status           # what cv/master.yaml holds, and whether it is placeholder data
./.venv/bin/python -m app.cv_tailor preview --url '<posting url>'   # draft + verify, writes no CV
./.venv/bin/python -m app.cv_tailor render  --url '<posting url>'   # write + render the stored draft
make cv-inventory        # re-render the master full inventory — ON DEMAND ONLY
make cv-selftest         # the engine's 198 checks, incl. the fabrication guard
make cv-lessons          # what the drafter has been taught from its own proven mistakes
make cv-lessons ARGS="--clear"   # forget them all, or ARGS="--clear <mode>" for one
```

`preview` reports what the stored job text asks for and `cv/master.yaml` cannot
evidence — that gap list is what you edit `cv/master.yaml` from, by hand. A new
fact never enters a CV automatically; the button/CLI writes only what the master
already supports. If the Tailor CV button says the venv is missing, run
`make cv-setup`.

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
| Tailor CV button: "run make cv-setup" | `make cv-setup` — the rendering deps (`python-docx`, `fpdf2`, `pypdf`, `PyYAML`) are not installed system-wide. |
| The board is stale after adding a job | Refresh the page. `make web` reads the SQLite file per request; no restart needed. |
