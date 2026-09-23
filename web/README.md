# Job Search Board (Next.js)

A local web UI over `job_search_pipeline`'s SQLite store (`../data/seen_jobs.sqlite3`).
Reads and writes the database directly — no export/import step, no separate JSON file
to keep in sync. This is now the recommended way to browse jobs and track your own
`applied / interview / rejected / skipped / silence` status with notes.

## Setup

```bash
cd web
npm install
npm run dev
```

Then open http://localhost:3000. It reads `../data/seen_jobs.sqlite3` relative to the
`web/` folder — i.e. it expects `job_search_pipeline/data/seen_jobs.sqlite3` to exist,
which means you should run the Python pipeline (`main.py`, then `ai_evaluate.py`) at
least once first. If the file doesn't exist yet, the app still starts and creates the
tables it needs, you'll just see an empty board.

If your `job_search_pipeline` checkout lives somewhere other than the parent of `web/`,
point at the database explicitly:

```bash
DB_PATH=/path/to/seen_jobs.sqlite3 npm run dev
```

From the repo root, `make web` does the same thing with the absolute `DB_PATH`
already set.

## Troubleshooting

**`npm install` succeeds, then the app dies with "Could not locate the bindings file".**
This is `better-sqlite3`'s native addon failing to build, and it does not fail loudly:
npm prints `up to date` and exits 0. Two things cause it (both hit in Sept 2026):

1. **npm 12 blocks install scripts by default.** Because `better-sqlite3` has a
   `binding.gyp` and no explicit install script, npm synthesises `node-gyp rebuild`
   for it and then refuses to run it, emitting only a warning:

   ```
   npm warn install-scripts 1 package had install scripts blocked because they are
   not covered by allowScripts:
   npm warn install-scripts   better-sqlite3@13.0.3 (install: node-gyp rebuild)
   ```

   With `better-sqlite3@13` this warning is **harmless**: v13 ships prebuilt binaries
   in `prebuilds/` (darwin-arm64, darwin-x64, linux-x64, linux-arm64, linuxmusl-x64,
   linuxmusl-arm64, win32-x64, win32-arm64) and loads one directly, so nothing needs
   compiling. On a platform with no prebuild you must approve the build once:

   ```bash
   npm install-scripts approve better-sqlite3   # writes an allowScripts entry
   npm rebuild better-sqlite3 --foreground-scripts
   ```

2. **The old `^11` pin could not work on modern Node at all.** `better-sqlite3@11`
   predates Node 26, so `prebuild-install` found no binary and `node-gyp rebuild`
   died with 6 C++ errors against Node 26's V8 headers. If you ever pin this back to
   v11, you must also run the app on an older Node. v13 declares `engines: node >= 22`.

To check the addon independently of Next:

```bash
node -e "const D=require('better-sqlite3');new D('../data/seen_jobs.sqlite3',{readonly:true});console.log('ok')"
```

**The board loads but shows `0 / 0`.** The page reads `passed_filters = 1` rows only, so
an empty board almost always means the Python pipeline has not been run against this
database yet — not a web-app fault. Run `python -m app.main`, then
`python -m app.ai_evaluate`. You can confirm what the server sees directly:

```bash
curl -s localhost:3000/api/jobs | head
```

## What it shows

Only jobs that passed the deterministic filter (`passed_filters = 1`). Each row has:

- **Posted** — the age of the posting ("3 days ago", "4 months ago"), with the exact
  date and day count on hover. Coloured from green (this week) through to faded with a
  ⚠ past 45 days, so a stale advert is obvious without reading the date.
- **Lang** — the language tier, computed from the words the advert uses. `Java/Ruby`
  (green) is the priority; `JS/Node` is acceptable; `no language` means the text we
  hold names none at all, which is **not** a mismatch and is not penalised. It is the
  largest group, because where the full advert could not be fetched the stored text is
  Adzuna's 500-character snippet and snippets rarely name a language. Hover for the
  words that matched. Sortable, best first.
- **clearance?** chip — the posting mentions security clearance but as something the
  successful candidate obtains ("must be eligible for SC", BPSS). A posting that
  requires clearance Ed would already have to hold never reaches the board at all —
  it is dropped by the filter. Hover for the clause it was read from.
- **Rate** — for contract roles, the day rate plus a verdict badge
  (`Meets floor` / `Check IR35` / `Below floor` / `No rate stated`). A figure recovered
  from Adzuna's annualised salary rather than quoted in the advert is marked `~derived`
  so it is never mistaken for one the advert stated.
- **Contract chip** on the title, with the IR35 status where the advert states one.
- AI status (`apply` / `consider` / `skip` / `not evaluated`) — from `ai_evaluate.py`
- Your own status (`applied` / `interview` / `rejected` / `skipped` / `silence`) —
  set inline in the table or from the detail panel. Deliberately separate from the AI's
  suggestion, since one is "should I apply" and the other is "what actually happened".
- Notes — free text, editable from the detail panel.

The detail panel adds a **Screening** block (language tier and the words that matched,
plus the clearance finding and the clause it came from) and a **Contract terms** block
for contract roles: the IR35 status and the phrase it was read from, the day rate and
where that figure came from, the equivalent permanent salary, and the floor it was
judged against.

### The language filter

The toolbar's **Language** control is deliberately asymmetric, because the tiers are not
equally trustworthy:

- **Java/Ruby only** — the tightest, and the one to work from.
- **Java/Ruby & JS/Node** — adds the acceptable tier.
- **Any language named** — drops the postings that name no language at all. Unlike the
  other two, this one *excludes* things rather than narrowing to them.

A posting in the `no language` tier is never swept into a Java/Ruby filter: it is not
evidence of a match, just an absence of evidence.

### The posting-date window

The board **defaults to postings from the last month**. That is a default, not a limit:
the toolbar's "Posted within" control opens it to 7 days / 3 months / 6 months / all
dates, and whenever the window is hiding rows the board says so with a one-click
"Show all dates". Nothing is ever dropped silently.

Two deliberate details:

- **An undated posting is always shown**, marked "no date". `posted_at` is not reliably a
  timestamp — it is whatever each source supplied, and the sources disagree (Adzuna gives
  ISO, SmartRecruiters gives epoch milliseconds, and `add_job.py` stores whatever the
  advert said, e.g. "2 weeks ago"). `lib/dates.ts` handles all of those and returns `null`
  rather than an `Invalid Date` for anything it cannot read. Hiding a job because its
  advert omitted a date would be worse than showing it with a marker.
- **The Contract filter is exact; Permanent is not.** ATS boards rarely report an
  employment type, so filtering to "Permanent" would hide most of the board, whereas
  filtering to "Contract" is reliable. Permanent therefore means "not declared contract".

Filter by status/type/date, search by company/title, sort any column, click a row to open
the full JD plus the AI's reasoning (gaps / strengths / risk factors) and edit
status+notes.

## Tests

```bash
npm test          # node --test — covers lib/dates.ts
npm run typecheck
```

Node ≥ 23 strips the TypeScript types itself, so this needs no jest/vitest and no
transpile step.

## Production build

```bash
npm run build
npm run start
```
