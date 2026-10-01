# AI-Powered Job Search Automation


An automated pipeline for a targeted job search: fetch open roles directly
from companies' ATS APIs and job aggregators, filter out anything
irrelevant, deduplicate against everything already seen, optionally score
each candidate's fit against your own experience with Claude, and review
the results in a local web board.

Nothing here talks to any service other than the job sources you configure
and (for the optional AI step) the Anthropic API. All state — scraped
jobs, scores, your own notes — lives in a local SQLite database; nothing
is sent to a third party beyond fetching the postings themselves.

**In a hurry?** [`CHEATSHEET.md`](CHEATSHEET.md) lists the commands for each
task in the order you run them — fetch, add a LinkedIn find, score,
open the board, tailor a CV. This file is the reasoning behind them.

<img width="2532" height="1215" alt="image" src="https://github.com/user-attachments/assets/679cea89-3156-4e46-af3c-74d3e0d9b30c" />

## What is in scope

Beyond title and location, two deterministic screens decide what reaches the
board. Both are configured in `filters.yaml` and both are reported on the row,
with the words that matched, so a decision can be checked rather than trusted.

### Language fit — Java and Ruby first

Every posting is put in one of four tiers, from the words it actually uses:

| tier | meaning | effect |
|---|---|---|
| **priority** | names Java or Ruby (plus Rails/RSpec/Capybara/Groovy/Spock/Maven/JUnit/Spring) | ranked top, never rejected for language |
| **secondary** | names JavaScript or Node only | kept, ranked below priority |
| **none** | names **no** language at all | kept — see below |
| **mismatch** | names a dealbreaker and none of the above | **dropped** |

`mismatch` is only reached when nothing from the priority or secondary tiers is
named anywhere in the posting, because "Java, Python or JavaScript" is a Java
role. That escape hatch is what makes it safe to be strict about the rest.

The dealbreaker list is C#/.NET, C++, Go, PHP, Kotlin, Swift, Scala, **Python
and Playwright**. The last two were added on request. They are a deliberate
exception to the rule that tools do not belong in this list — the rule exists
because Kubernetes and Docker are named in *every* JD and so cancelled every
dealbreaker (a "Golang with Kubernetes" role once sailed through as a match).
Python is a language, not a tool; and Playwright, while it has Java bindings,
is a strong marker of a Node/TypeScript or Python shop when no language is
named alongside it.

Measured before adding: this rejects 10 of the 92 postings then on the board,
every one of them a genuine mismatch — Python/Playwright-only shops, plus a
Dynamics 365 low-code role that names no language at all.

**`none` is deliberately not a mismatch.** 37 of the 72 postings on the board
name no language (short aggregator snippets, or JDs that talk about "automation
frameworks" generically). Rejecting silence would throw away real roles, so
they are kept and ranked below the ones with a named language.

### Security clearance

Ed holds none, so the distinction that matters is *must already have* versus
*can obtain*:

| status | example | effect |
|---|---|---|
| **blocked** | "Active SC clearance required", "SC Cleared Lead Automation Engineer", "candidates must hold current SC", "eSC or eDV Clearance Required" | **dropped** |
| **possible** | "must be **eligible** for Security Clearance (SC)", "BPSS to start, with eligibility for Home Office SC", "Clearance: SC-cleared / Non-SC", "Existing SC Clearance would be a benefit" | kept and **flagged** |
| **none** | never mentions clearance | — |

Those example phrases are real advert text, not invented — the patterns were
derived from the live corpus and are pinned by tests in
`app/tests/test_screening.py`.

Three details worth knowing:

- **BPSS is not a blocker.** It is pre-employment screening, not clearance, and
  is routinely arranged for the successful candidate.
- **The title counts.** "SC Cleared Lead Automation Engineer" and "Test Engineer
  (SC)" carry the requirement in the title alone, so the check reads title and
  description together.
- **A clearance mention alone is not enough.** The check only applies once the
  posting actually mentions clearance; before that gate existed, ordinary
  boilerplate ("ability to work independently", "bonus scheme") classified 85
  postings — including *Freelance Copywriter* — as clearance-related.

Set `clearance.reject_blocked: false` in `filters.yaml` to keep the blocked
postings on the board as flagged rows instead of dropping them.

### Both screens feed the AI scoring too

The AI step is handed the deterministic findings rather than left to re-derive
them, so the same posting cannot score as a Java role on one run and a Python
one on the next. `profile.yaml` states the same policy in prose: Java/Ruby are
the priority, Python/Playwright are gaps to name, and clearance Ed does not hold
is either disqualifying or a question to answer early.

To re-apply changed rules to postings already in the database — no re-fetch:

```bash
python -m app.scripts.backfill_derived_fields --dry-run
python -m app.scripts.backfill_derived_fields                        # refill the columns
python -m app.scripts.backfill_derived_fields --apply-rejections     # and enforce the policy
python -m app.refilter                                               # same, but also rescues
```

## When the description is not the advert

Worth knowing about, because it silently corrupted real data until it was
caught on 2026-09-21 — and because it limits how much the language screen can
see.

Adzuna's search API returns only a short snippet, so
`aggregator_clients.fetch_full_description` follows the listing's redirect to
get the real advert text. That page turned out to carry **the advert *and* the
site's own furniture**: navigation, a "Popular Jobs" sidebar, and other
companies' listings. Storing the whole page as the description meant 79 of 336
postings — 53 of the 72 then on the board — had *other people's adverts* as
their text.

The damage was concrete, not cosmetic:

- **A wrong day rate.** "£350 to £550 per day", belonging to an *NTT DATA /
  INNOVATIVE TECH PEOPLE* advert, was stored against Capgemini's job as its own
  **stated** rate.
- **A signature that gave it away.** Four unrelated postings (two IBM, Anson
  Mccade, ITV) reported the identical language hit list, which no single advert
  would.
- **AI scoring on the wrong text**, since the same description is what gets
  sent to the model.

### The fix: extract the advert, don't discard the page

The advert really is on that page, above the footer — so `app/jd_text.py`
**extracts** it rather than throwing the page away. The body runs from
`Apply for this job` to whichever comes first: the site footer, or the next
listing's apply button (one advert has one apply button; a second one is the
next job down).

Discarding instead was tried first and was worse: Adzuna's snippet is the only
other source, and it rarely names a language, so 65 of 69 postings ended up
with no language signal at all. Extraction recovered 2,300–9,500 characters per
posting, and the language hits became **specific to each advert** — eight
distinct lists across the top ten, where four unrelated rows had previously
shared one.

Everything reads through `jd_text.advert_text`, so the same rule applies to the
language screen, the clearance screen, the day-rate parser and the AI prompt.
When no body can be extracted it falls back to the **title**, which is always
this advert's own — that is what keeps *Test Engineer (SC)* blocked on
clearance even with an unusable body.

### One thing to know about the leftovers

Where the full advert could not be fetched at all, the stored text is Adzuna's
**500-character snippet**. That is real text about the right job, but it often
does not name a language, which is why `language_tier: none` is the largest
group on the board. `none` is not a mismatch — it is an absence of evidence,
kept and ranked below the rows that do name a language.

To repair rows already stored — clears the dedup key so the next run re-fetches
them; nothing is deleted, and statuses, notes and AI scores survive:

```bash
python -m app.scripts.queue_polluted_descriptions --dry-run
python -m app.scripts.queue_polluted_descriptions
python -m app.main --skip-companies                                  # re-fetch them
python -m app.scripts.backfill_derived_fields --clean-descriptions   # extract in place
```

`--clean-descriptions` needs no network at all: the advert is already inside
the stored page, so the backfill rewrites it to just the body.

## How it works

1. **Fetch** (`app/main.py`) — pulls open roles from:
   - `companies.yaml` — known companies queried directly via their ATS
     (Greenhouse, Ashby, Workable, Lever, SmartRecruiters) — precise, low
     noise.
   - `aggregators.yaml` — broad keyword+location search across many
     employers at once (Adzuna, Remotive) — wider reach, more noise. Adzuna
     entries carry `max_days_old` (a server-side recency window) and
     `contract: 1` for the contract-only searches.
2. **Filter** (`app/filters.py`) — drops anything that isn't an
   engineering-shaped title, isn't in an allowed location, names a language
   you don't work in, or requires security clearance you don't hold. A
   **contract** posting whose day rate cannot reach the salary-equivalent
   floor is dropped here too. See
   [What is in scope](#what-is-in-scope) and
   [Contract roles and day rates](#contract-roles-and-day-rates).
3. **Dedup** (`app/dedup.py`) — everything fetched is stored in a local
   SQLite database (`data/seen_jobs.sqlite3`), keyed by URL and by
   normalized company+title, so re-runs only ever surface genuinely new
   postings.
4. **AI evaluation** (`app/ai_evaluate.py`, optional, costs money) — sends
   each filtered candidate plus your `profile.yaml` to Claude, which scores
   fit (0-100) and returns concrete gaps, transferable strengths, risk
   factors, and an apply/consider/skip recommendation.
5. **Review** (`web/`) — a local Next.js app reading/writing the same
   SQLite database directly, for browsing results and tracking your own
   `applied / interview / rejected / skipped / silence` status and notes.
   Defaults to postings from the **last month**, with the window adjustable
   in the toolbar.

## Contract roles and day rates

A £60,000 contract is not a £60,000 salary, and which way it differs depends
entirely on IR35 — so "the equivalent day rate" resolves to **two different day
rates**, not one:

| | why the rate differs |
|---|---|
| **Inside IR35** | Taxed as an employee, but the assignment rate the agency pays must first cover employer National Insurance, the apprenticeship levy, pension and the umbrella's margin. The rate is grossed **up**. |
| **Outside IR35** | You run a limited company: corporation tax and dividends replace employee NIC, which is cheaper for the same take-home. The headline rate can be **lower**. |

Print them, with the full arithmetic:

```bash
python -m app.contract_rates            # the tables and the workings
python -m app.contract_rates --json     # for scripting
python -m app.contract_rates --days 260 --salary 80000
```

For the 2026/27 tax year, over 220 billable days, matching the £60,000 example
salary in `contract.yaml.example`:

| billable days/yr | INSIDE IR35 | OUTSIDE IR35 |
|---|---|---|
| 200 | £355/day | £301/day |
| **220 (default)** | **£323/day** | **£274/day** |
| 240 | £296/day | £251/day |
| 260 | £273/day | £231/day |

Billable days is the biggest lever: 260 assumes no holiday and no gap
between contracts. 220 allows ~6 weeks off plus bench time. Set your own in
`contract.yaml`.

**Every assumption is an editable input in `contract.yaml`** — the tax
rates, the umbrella margin, the accountant's fee, the target salary. The
rates were verified against gov.uk on 2026-09-21 for 2026/27. Note in
particular that **dividend tax rose to 10.75%/35.75%** from April 2026: any
outside-IR35 figure you find quoted from 2025 used 8.75%/33.75% and is now
too optimistic. Sources:

- [Income Tax rates and Personal Allowances](https://www.gov.uk/income-tax-rates)
- [Rates and thresholds for employers 2026 to 2027](https://www.gov.uk/guidance/rates-and-thresholds-for-employers-2026-to-2027) (employer NIC 15% above £5,000; apprenticeship levy)
- [Tax on dividends](https://www.gov.uk/tax-on-dividends)
- [Corporation Tax rates](https://www.gov.uk/corporation-tax-rates) (19% / 25% with Marginal Relief)
- [Workplace pensions: what you, your employer and the government pay](https://www.gov.uk/workplace-pensions/what-you-your-employer-and-the-government-pay) (3% employer minimum on £6,240–£50,270)

### How a posting is judged

Each job is labelled with an employment type, an IR35 status read from its
own text, and a day rate, and then given a verdict:

| verdict | meaning |
|---|---|
| **pass** | meets the floor for its IR35 status |
| **review** | clears the outside-IR35 floor but not the inside one, and the advert does not say which applies — worth one question to the agent, not an automatic no |
| **fail** | below both floors; **dropped**, but only when the rate is stated |
| **unknown** | no rate stated anywhere in the posting |

Where a rate is found, in order of trustworthiness:

1. **stated** — a figure the advert itself quotes (`£500 per day`, `£450-£550/day`, `£475pd`).
2. **derived_from_advertised_salary** — Adzuna reports contract pay
   *annualised*; dividing by 260 recovers the day rate (verified empirically:
   for every contract ad quoting both, `salary_max / stated rate` was exactly
   260). Shown as `~derived` on the board, never as if the advert said it.
3. **estimated_by_adzuna** — Adzuna's own guess from similar ads.

Most contract adverts quote no rate at all. Those are **kept** and shown as
`unknown` rather than dropped, because they are exactly the roles worth
asking about — silently discarding them would empty the board of contract
work entirely. Only a *stated* rate below both floors is a hard rejection.

To backfill or re-evaluate postings already in the database after changing
`contract.yaml`:

```bash
python -m app.scripts.backfill_derived_fields --dry-run
python -m app.scripts.backfill_derived_fields          # writes the fields
python -m app.scripts.backfill_derived_fields --apply-rejections   # also enforces the floor on stored rows
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in .env with the keys you need — see the comments in that file
```

Create your experience profile from the template (this file is gitignored
— it's never committed, since it holds your personal experience):

```bash
cp profile.example.yaml profile.yaml
# then edit profile.yaml with your own background
```

`profile.example.yaml` is a blank template — it tells you the shape but
not the bar. For a fully worked (fictional) example showing the level of
specificity/quantification each evidence bullet should actually have, see
[`profile.sample.yaml`](profile.sample.yaml).

Then the contract-role settings, which are gitignored for the same reason —
the target salary in them is personal:

```bash
cp contract.yaml.example contract.yaml
# then set target_permanent_salary_gbp to your own floor
```

Without this, `app/contract_rates.py` falls back to reading
`contract.yaml.example` directly: a fresh clone still runs, but it judges every
contract posting against the **example** salary rather than yours. Copy it
across before you rely on a day-rate verdict.

`ANTHROPIC_API_KEY` is only needed for the AI evaluation step. Adzuna
(`ADZUNA_APP_ID`/`ADZUNA_APP_KEY`) is only needed if you keep an Adzuna
entry in `aggregators.yaml` — register a free key at
[developer.adzuna.com](https://developer.adzuna.com). Remotive needs no
auth but only covers remote roles.

### Customize for your own search

`filters.yaml` and `aggregators.yaml` have since been retuned for London/UK
and senior SDET / test-automation roles, so items 1-2 below are already done
for this checkout — they are kept here because they are the steps that matter
for any other market. `companies.yaml` has NOT been pruned: it is still the
original author's list (see item 3).

**Before your first run, edit these three files** or you'll get zero
candidates, or candidates that don't match your actual stack:

1. **`filters.yaml` → `location_allow_patterns`** — regex patterns for
   locations to keep. Was Canada/Alberta-only; now London/UK. Replace with
   your own country/region/cities, or delete entries to broaden it. This is
   the #1 reason a first run returns nothing — if nothing you fetch ever
   matches these patterns, `location_is_allowed()` rejects every job.
   Note it also drops any posting with a BLANK location string, so "location
   not stated" job ads are filtered out even when they're a good fit.
2. **`filters.yaml` → `stack_dealbreakers` / `stack_core`** — a JD is
   rejected if it mentions a `stack_dealbreakers` language and none of
   `stack_core`. Was Python/JS with Java as a dealbreaker; now Java/Ruby, for
   Ed. Keep `stack_core` to LANGUAGE-level entries only: anything named in
   nearly every JD (Kubernetes, Docker, Jenkins) cancels every dealbreaker if
   you put it there.
3. **`companies.yaml`** — the company registry. Still the original author's
   target list; add/remove companies to match who you're actually applying
   to (see the file's header comment for the format). Two scripts do this
   for you, and neither costs anything to run:

   ```bash
   # Which of the companies in the file actually post roles in MY market?
   # One list request each; reports London postings, UK postings, and how many
   # postings would pass your title+location filter.
   python -m app.scripts.audit_companies_uk
   python -m app.scripts.audit_companies_uk --csv out.csv

   # Build a list for a new market: guess ATS slugs for a curated set of
   # employers, then VERIFY each hit before believing it (the ATS's own board
   # name where available, otherwise the company name inside the postings).
   python -m app.scripts.build_uk_companies            # report only
   python -m app.scripts.build_uk_companies --write    # append the verified ones
   ```

   `build_uk_companies.py` is deliberately conservative: a slug that merely
   resolves proves nothing. This repo has already been caught twice by a live
   but wrong board (Coveo's `coveodeven` dev board, which is still in this
   file — see the note below), and short British names make it worse, since
   `wise`, `tide`, `curve`, `plum`, `cleo` and `sky` are ordinary English
   words. Anything it cannot corroborate goes to a review list instead of
   into the file.

   **The registry was pruned on 2026-09-23** (256 → 226 entries) after an audit
   of every board. What the pruning removed, and why:

   | removed | reason |
   |---|---|
   | 9 | exact duplicate entries — the same board fetched twice per run |
   | 16 | boards that return HTTP 200 with `"jobs": []` (empty) |
   | 3 | boards that now 404 |
   | 2 | `ats: bamboohr`, for which no fetcher exists |
   | 3 | the WRONG company: a namesake board (Maxima) and a US Relay on Lever while the UK one is on Ashby |
   | 1 | a dev/test board carrying mock jobs (Coveo's `coveodeven`) |
   | 2 | boards with no UK-located posting at all (Doppel, Outschool — see below) |

   **Worth knowing before adding more companies.** An audit of all 244 boards
   found that only **7 had ever produced a job that passed the filters**, and
   those seven are worth keeping precisely because they did: Kainos (7.1% of its
   postings pass), Gymshark (8.0%), Version 1, Trading 212, Graphcore,
   ElevenLabs and Jobgether. Every other registered board had contributed
   nothing. Adzuna, meanwhile, supplies **73 of the 87 postings on the board
   (84%)**; all 226 company boards together supply 14.

   That is not an argument against the registry — an ATS board catches *every*
   posting the employer makes, including ones Adzuna never indexes or misspells,
   and a company that has hired QA before will hire QA again. But it does mean
   **adding companies is a low-yield way to broaden the search**. Extending
   `aggregators.yaml` (more keyword and location searches) has consistently been
   worth more. Add companies when you have a specific reason — you saw a role
   there, or they are a known QA employer — not to make the number bigger.

   Two traps this section exists to record, both hit in practice:

   * **A 200 with an empty list looks fully configured.** Kainos exist on
     Workable *and* SmartRecruiters, both returning HTTP 200 — with zero jobs.
     Trading 212's Personio feed returns 200 and holds a single "General
     Application" from 2018. Probe for actual postings, never for a status code.
   * **A company on two ATSes is normal, and one is often a namesake.** Wayve
     genuinely runs boards on both Greenhouse and Ashby with the same postings
     (register one, not both). Relay's Ashby board is the UK company
     (London, Nottingham); its Lever board is a San Diego namesake. Maxima's
     SmartRecruiters board is a Russian industrial firm.


   **Known issue in the inherited list:** `Coveo` appears twice, and the
   second entry is `slug: coveodeven` — the dev/test board that the comment at
   line 25 says was replaced for being a test board with fake jobs. The stale
   duplicate was never removed, so it is still fetched on every run. ~12 other
   entries are duplicates too (7 exact, 5 the same company on two ATSes), and
   the audit shows 134 of the inherited entries have no UK postings at all.

   **Workable rate-limits hard, and it is worth knowing before you bulk-probe.**
   `apply.workable.com` answers a `429` with `Retry-After: 85689` — roughly
   24 hours, an IP-level ban rather than a nudge — and once it starts it does so
   for every slug, including ones that resolved minutes earlier. A few full
   scans in quick succession is enough to trigger it (measured 2026-09). While
   banned, every Workable-hosted company in `companies.yaml` (12 of them)
   returns nothing, and a long-running probe script that retries 429s blindly
   will burn its whole runtime waiting. `build_uk_companies.py` disables a host
   for the rest of the run when it sees a Retry-After that long; `main.py`
   fails that company for the run and moves on.

   `aggregators.yaml` is location-specific too and has been retuned — its
   Adzuna entries take a `country` param (a two-letter code; `gb` for the
   UK, and `uk` is accepted as an alias for it) because the country used to
   be hardcoded to `ca` in `aggregator_clients.py`.

## Usage

> **Just want the commands?** Every task, in the order you run it, is in
> [`CHEATSHEET.md`](CHEATSHEET.md) — including
> [adding a job you found on LinkedIn](CHEATSHEET.md#add-a-job-from-linkedin),
> which is the one step that is deliberately *not* automatic (see 2b below).

### 1. Fetch + filter (free)

```bash
python -m app.main                    # all companies + aggregators
python -m app.main --company affirm   # just one company, for debugging a fetcher
python -m app.main --skip-aggregators # companies.yaml only
python -m app.main --skip-companies   # aggregators.yaml only

make run                              # same thing, interactive prompts instead of flags
```

Output: `data/candidates.csv`, appended to on every run.

### 2. AI evaluation (optional, costs money)

```bash
python -m app.ai_evaluate --list-models   # what models does my key offer? (free)
python -m app.ai_evaluate --dry-run       # see what's queued, no API calls, no cost
python -m app.ai_evaluate --limit 5       # score just 5, to sanity-check quality/cost
python -m app.ai_evaluate                 # score everything unscored

make evaluate                             # interactive prompts instead of flags
```

Results are stored in SQLite (so re-running never re-pays for a job
already scored) and written to `data/scored_candidates.csv`, sorted
best-match-first.

**Provider.** DeepSeek by default; Claude (Anthropic) is still supported. The
provider is auto-detected from whichever API key is in `.env` (DeepSeek wins if
both are set), and can be forced with `LLM_PROVIDER=deepseek|anthropic` or
`--provider`. Only the wire format differs between them — the prompt, the
schema, the validation, the retries and the stored results are identical, so
swapping providers does not change how a score is produced. See
`app/llm_providers.py`.

Two things to know, both learned the hard way:

- **Run `--list-models` once before your first real run.** DeepSeek's model IDs
  change between releases: `deepseek-chat` — the long-standing alias, and this
  repo's original default — is no longer served, and a key issued in 2026-09
  reports `deepseek-flash` and `deepseek-v4-pro`. A rejected model name now
  produces an error that tells you to run `--list-models` rather than a bare 400.
- **A placeholder key in `.env` is not harmless.** Anything that merely checks
  whether a variable is non-empty treats it as configured, so the run picks a
  provider and dies later with a 401. `app/llm_providers.py` detects obvious
  placeholders (and too-short values) and says which key is bogus. The chosen
  provider and model are printed on every run, because this is the step that
  spends money.

### 2b. Adding a job you found on LinkedIn

LinkedIn is not one of the sources above: it restricts job data to approved
partners, so companies.yaml, Adzuna and Remotive are structurally blind to it.
Since LinkedIn is where a lot of real postings come from, `app/add_job.py` puts
one into the same SQLite store — after which it behaves exactly like a fetched
job: it appears on the board, carries your applied/interview/rejected status and
notes, and gets scored by the AI step.

The short form takes the URL and nothing else. It reads the posting's public
page and takes the company, title, location, "posted" age and the full advert
text from it:

```bash
python -m app.add_job --url "https://www.linkedin.com/jobs/view/4471638473"

  About to store:
    Company      : Haley Bridge
    Title        : Senior Quality Assurance Automation Engineer
    Location     : London Area, United Kingdom
    URL          : https://www.linkedin.com/jobs/view/4471638473/
    Description  : 1853 characters (read from the LinkedIn page)
    fetched  company/title/location/posted_at: from the LinkedIn page (job 4471638473)
    Criteria     : Seniority level: Mid-Senior level; Employment type: Full-time; …
    Filters      : would have passed the deterministic filters
    Result       : Added Haley Bridge — Senior Quality Assurance Automation Engineer
```

Add `--dry-run` to see that block without writing anything. Fields read from the
page are LinkedIn's own structured data, not a guess, so they are stored without
the confirmation a `--infer` guess needs — and an explicit flag beats them if you
want to correct one (a role posted by an agency, say). `--no-fetch` never touches
the network.

**What the fetch will not do.** It stays anonymous — no login, no session cookie,
no attempt to defeat a block — and it is best-effort by nature. A taken-down
advert answers 404, and an auth wall or a rate-limit is reported as itself, with
the paste workflow named as the way through:

```bash
python -m app.add_job --url "https://www.linkedin.com/jobs/view/9999999999"
Could not read the posting from LinkedIn: LinkedIn has no posting for that job id
(404). It has probably been taken down, filled, or expires — check the link in a
browser. If it opens fine for you, paste the text instead (--clipboard or --file).
Missing required field(s): --company, --title. Reading the posting from its URL
did not work (see above) — paste it instead (--clipboard or --file) and add
--infer, or pass them.
```

So the paste workflow is unchanged and still the fallback, and the only path for
a posting that cannot be reached by URL at all:

```bash
# explicit fields — the reliable path. Run it once with --dry-run first: it
# writes nothing and prints the fields, the filter verdict and, for a contract
# role, the day rate and IR35 verdict.
python -m app.add_job --url "https://www.linkedin.com/jobs/view/123456" \
    --company Resillion --title "Senior Test Automation Analyst" \
    --location "London (hybrid)" --file ~/Downloads/posting.txt

pbpaste | python -m app.add_job --url "..." --company X --title "Y"   # from clipboard:
                                                    # a piped stdin is read automatically
python -m app.add_job --url "..." --infer --file posting.txt   # infer fields (see below)
python -m app.add_job --list [--pending]                       # what have I added?
python -m app.add_job --remove "https://..."                   # undo one
python -m app.add_job --url "..." --company X --title Y --dry-run   # write nothing
```

Order of authority, most to least: an explicit flag, then the URL fetch, then a
labelled line in a paste (`Company: Monzo`), then an `--infer` guess (which
needs `--yes`). Passing `--company` and `--title` makes the command network-free,
which is what the recipe above relies on.

Keeping the paste in `data/postings/` (git-ignored along with the rest of
`data/`) rather than relying on the clipboard is worth the extra step: it is the
only record of the advert once the posting comes down, and `--file` is the same
command either way. [`CHEATSHEET.md`](CHEATSHEET.md#add-a-job-from-linkedin) has
the full ordered recipe and a worked example of two real postings.

For a **contract** role, add `--contract` — unless the fetch read it off the
page, which it does whenever LinkedIn states a contract employment type, in which
case the board gets the Contract chip with no flag at all. The day rate and IR35
status are read out of the advert text either way and reported before the job is
written:

```bash
python -m app.add_job --url "https://www.linkedin.com/jobs/view/123456" \
    --clipboard --infer --yes --contract

  Day rate     : £450–£500 per day (£500/day clears the £325/day needed for outside IR35.)
  IR35         : outside — read from "Outside IR35"
  Equivalent   : about £102,126 as a permanent salary
```

A contract that falls below the floor is still stored, flagged `Below floor`
rather than refused: adding a job is the in-scope judgement, exactly as it is
for the filter verdict above.

It is written with `passed_filters = 1` on purpose — you have already made the
"is this in scope" judgement by choosing to add it — and it is recorded as
seen, so a later pipeline run will not duplicate it. It is also given the same
`filters.screening_signals` every pipeline job gets, so the language and
clearance chips and the board's language filter work on it exactly as they do on
a fetched posting.

### 2c. The same job advertised twice

Agencies re-advertise the role they are still trying to fill, under a **new advert
id**, with the title and the office address nudged. That defeats both dedup keys —
the URL differs and so does `company::title::location` — so the copy lands on the
board as fresh, un-actioned work. Measured on the real board, the case that
prompted this was Experis's *AI Automation Tester*, applied to on 9 September and
re-advertised on the 24th from a different London address; the only way to tell was
to open it on LinkedIn and read "Applied". Twelve such pairs existed on a board of
102 rows, three of them with one copy un-actioned.

```bash
python -m app.find_duplicates            # check: what is duplicated. Writes nothing.
python -m app.find_duplicates --all      # include rows the filters dropped
python -m app.find_duplicates --apply    # link them and carry your decision over
```

`--apply` is the "stop" half and does exactly two things per pair: records
`job_details.duplicate_of`, and copies a decision you had **already** made —
applied, skipped, rejected — onto the copies that have none of their own, marked
with `user_status.inherited_from` so a derived value is never mistaken for one you
chose. Three rules keep that honest, and all three are asserted in
`app/tests/test_duplicates.py`:

- a status **you** set is never overwritten — a derived value never outranks a real
  decision;
- when a cluster holds a decision, the copy comes from the **earliest** advert that
  carries one, so the provenance is the first time you judged this job. That is not
  always the original advert: Red Badger's pair has the "skipped" on the *newer*
  copy, so reading only the original would have left the copy you had not judged
  still looking like fresh work;
- an inherited status stays **correctable** (a later decision replaces it) while one
  you set yourself does not.

Nothing is deleted or hidden: a re-advert stays on the board, readable, with the
original one click away. The judgement that two adverts are one job is a heuristic,
and a heuristic that silently removes a posting is a worse failure than the
duplicate it was fixing.

**What counts as a match.** Three conditions, all required, chosen for precision
because a wrong match marks a real job as one you have already dealt with:

1. the same company, after legal suffixes (`Hackajob Ltd` == `Hackajob`);
2. an **identical set** of title content words, with the filler a re-advert churns
   dropped — `X2`, `Contract`, `Remote`, "(Fixed Term)", "6 month". Equality, not a
   similarity threshold: measured on the real board every true duplicate sat at
   1.00 while Capco's genuinely different *Principal*/*Senior* SDET pair sat at
   0.75, so any threshold in between buys nothing and risks that;
3. compatible locations — a city in common (`Farringdon, Central London` and
   `Fleet Street, Central London` are both London), or one advert naming no
   particular place at all, which is how a remote-first role's repost can say `UK`
   where the original said London.

That last clause is bounded on purpose. An earlier version treated "names no city I
recognise" as "names no place", which merged 33 SumUp *Field Sales Executive* rows
from 33 different countries into one cluster; vagueness has to be a property of the
string, not of the module's ignorance.

On the board, a re-advert carries a grey **re-advert** chip, and the panel names the
other advert with a link to it and says when a status was inherited rather than
chosen. Saving a status on that row yourself clears the label.

The pipeline applies all of this to new postings automatically (`main.process_jobs`),
so a repost of something you applied to arrives already marked. `--apply` is for the
rows already stored — and, being idempotent, it is also what to re-run after you
action a job so its other adverts follow.

**About `--infer`.** On a genuine LinkedIn clipboard paste (title on line 1,
then `Company · Location · 2 weeks ago`) it recovers all four fields, verified.
On the curated multi-page documents in the CV repo it gets the title right about
half the time, because those keep the title in `job.yaml` rather than the pasted
text. So a value taken from an explicit label (`Company: Monzo`) is used
automatically, while a *guessed* one is never written without `--yes` — measured
against the real postings, the guessers twice produced confident nonsense
(`company="Senior Level"`, `location="GBP75,000 + Benefits"`) and both would
have gone straight into the AI prompt and the board.

### 3. Review results

```bash
cd web && npm install    # first time only
cd ..
make web                              # starts the board at http://localhost:3000
```

Reads/writes `data/seen_jobs.sqlite3` directly — no export/import step.
See [`web/README.md`](web/README.md) for details (custom `DB_PATH`,
production build, etc).

## Scanning your CV against a posting

Every job on the board has a **Scan CV** button in its detail panel. It answers
two questions about the ONE CV you actually send, and it answers them with
keyword arithmetic rather than a model call — so it is free, it takes about
150ms, and the same inputs always give the same answer:

1. **Would this CV pass this posting's screening?** A verdict, the coverage
   figure behind it, and the specific requirement-level terms the CV does not
   mention.
2. **Is there real experience I hold that the CV is not showing?** Concrete
   lines from `cv/master.yaml`, quoted verbatim, that the posting asks for.

```
             cv/master.yaml  ──┐
  (the fact bank: every        │   the DIFFERENCE between these two
   achievement you could       ├─► is exactly "experience you have
   legitimately claim)         │   but are not showing" — which is the
                               │   only thing the scan ever suggests
   Google Doc (the CV) ────────┘   adding.
```

Nothing is written to the CV, and nothing is written to `cv/master.yaml`. The
suggestions are copy-paste blocks: the scan's job is to say what is missing, and
yours is to decide whether it belongs on the CV.

### Why this replaced per-posting tailoring

Until 2026-10-01 the button **Tailor CV** drafted a rewritten CV for every
posting: it selected from the master, reworded into that advert's vocabulary,
and filed a PDF under `cv_output/<date>/<company>/`. The engine that did it is
still here and still works (see *The per-posting tailoring engine* below), but
it answered the wrong question. It produced a *different CV per application*,
and a CV you have to regenerate for every job is a CV you will not maintain. The
document that actually gets sent is one Google Doc. So the per-posting work
became: check the CV you have, and improve it where the evidence says you
can — which is what this scan does.

### Where the master CV lives

The CV is a **Google Doc**, configured with `MASTER_CV_DOC_URL` in `.env` (paste
the whole address-bar URL; the document id is extracted for you). `cv/master.yaml`
is *not* the CV — it is the fact bank, the complete record of everything you
could legitimately claim, and it is what the scan draws suggestions from. The two
are compared, never merged automatically.

There are two ways to read the Doc, and the supported one is the second:

| Method | Setup | Access model |
|---|---|---|
| **Text export** (default) | None — paste the URL | Works only because the Doc is readable by *anyone holding the link*. Fine to start with; wrong for a document carrying your name, phone number, email and full employment history. |
| **Service account** (recommended) | A Google Cloud project with the Docs API enabled, a service-account JSON key, and the Doc shared with that account as a **Viewer** | The Doc stays private. Only the robot account can read it. |

To switch to the service account:

1. Create a Google Cloud project and enable the **Google Docs API**.
2. Create a service account and download its JSON key.
3. Share the Doc with the service account's `client_email` address — **Viewer**
   is enough.
4. Set `GOOGLE_SERVICE_ACCOUNT_FILE` in `.env` to the key's path.
5. `./.venv/bin/python -m pip install google-auth`

The key file is git-ignored by pattern (`*-service-account*.json`), and the
fetched CV text is cached at `data/master_cv.json`, inside the already-ignored
`data/`. Check the whole path with:

```bash
./.venv/bin/python -m app.cv_tailor master-doc
```

It reports which method is in use, when the copy was taken, and — for a service
account that has not been given access — the difference between "no such
document" and "not shared with you", which Google reports identically as a 404.

A fresh cached copy is used without touching the network. If a live fetch fails,
the scan falls back to the cache, marks the result stale, and says so in the
panel: a scan against a two-day-old CV is worth having, but only if it says so.

### How the verdict is decided

A deterministic rule with three bands, not a weighted score out of 100. A made-up
score cannot be argued with or checked; a rule can be, and the scan returns the
counts and the threshold behind every band, so a wrong verdict is a bug report
rather than a matter of taste.

| Verdict | When |
|---|---|
| **Would pass** | No requirement-level hard skill is missing, and priority-term coverage is at or above the threshold (85%). |
| **Borderline** | Every requirement-level hard skill is covered, but coverage is below the threshold. |
| **Would be filtered out** | The posting demands a hard skill, under a *Requirements* heading, that the CV does not mention. One is enough — a keyword filter looking for Kubernetes does not care that you are strong on everything else. |
| **Cannot tell** | The posting could not be read well enough to judge. See below. |

Coverage is the **priority-term** figure, the same measurement `scripts/gap.py`
produces: terms are weighted by the section they appear under (3.0 for a
requirement, 1.5 for a duty), matched on stems and variants, and the employer's
own name is suppressed. It is the one figure comparable between two postings of
different lengths.

### "Cannot tell" is a real answer, and it is not a pass

Measured on this board on 2026-10-01, against the text the scan actually reads
(the pipeline's own `_post_text`, not the raw column): of 127 postings that
passed the filters, the **median stored description was 500 characters**, 65 were
under 800 — two of them with no advert text at all — and only 43 were over
3,000. Adzuna returns a teaser rather than the advert, and recovering
the full body does not always succeed (see *When the description is not the
advert*, above).

A verdict computed from 500 characters of prose is noise wearing a percentage
sign, so the scan refuses to give one. It says which of the two problems it hit,
because they have different fixes:

* **a snippet** — the stored text is a teaser. If the same job is also on the
  board from the employer's own ATS, the panel offers a button to scan that copy
  instead. Otherwise the advert needs re-fetching.
* **no headings** — the advert is long enough but has no *Requirements* /
  *Duties* headings for the section weighting to work against.

The first version of this scan did not have that gate and reported **"PASS —
priority-term coverage 100% (4/4)"** on Lendable's Adzuna copy, whose stored text
is 8,500 characters of flat prose with no headings at all. Four terms and a green
tick. That is the single most damaging thing this screen could do — it would send
you to an interview believing you had passed a filter that was never applied — so
"cannot tell" is a first-class outcome, and the UI never renders it in the pass
colour.

### What the suggestions are, and what they can never be

Every suggestion is a line from `cv/master.yaml`, **quoted verbatim**. Nothing
here is written by a model, so the scan cannot invent experience — it can only
point at experience you already recorded and are not currently showing. Each one
is classed as one of two things, and the distinction is not cosmetic:

* **Add to the CV** — the line is not on the CV at all. This is the interesting
  case, and a whole role can be missing. On this repo's own data an earlier
  commercial role is in the master and absent from the Doc, and the scan reports
  a missing role separately from the term-level gaps.
* **Already on the CV — name it in their words** — the fact is already printed,
  but not in this posting's vocabulary. Suggesting you add a bullet you already
  have would be a small lie about your own CV, and a detectable one, so the scan
  compares each achievement against the CV line by line and says *reword* when
  the line is there. It checks per LINE rather than against the whole document: a
  document-wide word set scored an absent role's bullet at 0.8, because "test",
  "performance" and "software" are in every QA bullet there is.

Whatever is left over is listed as **not closable** — the posting asks for it and
nothing in the master evidences it. That list is the honest one, and it is the
one worth taking into an interview.

### The per-posting tailoring engine

None of the following is reachable from the board any more — there is no
**Tailor CV** button. It all still works from the command line, and it is what
produced the CVs already filed under `cv_output/`:

```bash
./.venv/bin/python -m app.cv_tailor preview --url '<posting url>'  # draft + verify, writes no CV
./.venv/bin/python -m app.cv_tailor render  --url '<posting url>'  # write + render the stored draft
```

Both emit a single JSON object on stdout, so the pipeline's own error messages can
be shown verbatim, and the detail panel still links to a PDF for any job that has
one.

#### Two models, and why it is two

Stage 1 **drafts** with the local Qwen3-Coder-30B on `127.0.0.1:8080`. Stage 2
**verifies** that draft with DeepSeek. The split is not redundancy: selecting and
rewording facts is mechanical work the local model does well and for free, while
deciding whether a rewording is still *the same fact* is the judgement call this
whole system exists to get right, and that is not handed to a 30B model.

Measured on this machine (M4, 32 GB, `UD-Q3_K_XL`): the local model honours a
forced tool call and returns valid JSON, drafting a CV in **~80–90 seconds** at
~35 tokens/sec; the DeepSeek verification adds **~5 seconds**. So roughly 60–120
seconds per CV, and the drafting half costs nothing but wall-clock time.

The local model's binding constraint is its **32,768-token slot**, not its
capability. `cv/master.yaml` is ~40 KB, so the master is reshaped for the prompt
— every id, achievement, tag and metric kept, human-facing comments dropped —
and the deterministic gap report is passed in alongside it. That converts "write
a CV" into "close these specific gaps", which is both a smaller prompt and a far
better task for a small model. A long summary does not fit in the output budget,
which is why the prompt asks for 3–5 lines.

#### Stopping it adopting the employer's world

The mistake a small model makes is not random, so the prompt is built around it.
A CV claims what the candidate HAS DONE; a posting describes what the employer
IS. Rewording is vocabulary — choosing the posting's noun for work already done.
Attribution is moving that work into the employer's world, and only one of those
is true. The Acme draft did the second: it kept every genuine activity and
silently attached them to a sector Ed has never worked in.

Four layers now guard it, each doing a different job:

1. **A standing rule** distinguishing vocabulary from attribution, with the real
   before/after as a worked negative example — a concrete failure is worth more
   to a 30B model than an abstract instruction.
2. **A `POSTING CONTEXT` block**, first in the prompt, rendered deterministically
   from the master rather than left to the model to infer. It names the
   candidate's actual employers, states plainly that they have not worked for
   this one, lists the ONLY domains the master evidences, and names the
   requirement-level terms that cannot be evidenced — *"these are GAPS. They stay
   gaps."* Order is argument: constraints met after 3,000 words of the employer's
   marketing have already lost.
3. **The gap report**, which already classified `public` and `sector` as
   priority gaps — available, but not previously legible as a boundary.
4. **The verifier plus automatic escalation**, which is what caught it.

Measured on the same Acme advert: the local model went from **reject** (a
fabricated public-sector claim) to **revise** (no sector claim anywhere, summary
correctly reading "FinTech… regulated financial services") at the same ~66s.

Both halves are movable:

```bash
CV_DRAFT_PROVIDER=deepseek   # draft in the cloud instead of locally
CV_VERIFY_PROVIDER=local     # verify locally instead
LOCAL_LLM_BASE_URL=http://127.0.0.1:8080
LOCAL_LLM_MODEL=qwen3-coder-30b-a3b
```

If the local server is not running, `preview` says so — naming
`bootstrap_local_ai.sh` — rather than surfacing a connection error.

#### When the local draft does not pass

The local model stays the default; the cloud is the **fallback**, and it fires in
two ways.

* **Automatically** when the verifier returns `reject` or `revise`: the draft is
  retried on DeepSeek *with the verifier's corrections*, and that retry is kept
  only if it verifies strictly better (`_escalate_draft`). A retry that fared no
  better is discarded, and the discarded attempt is still shown, so a paid call
  made on your behalf never becomes invisible.
* **By hand**, with `--provider deepseek` on the run below. Same provider, same
  verification, but the decision is yours, and `--feedback` passes your own
  corrections to it. The panel that used to withhold this on an accepted draft is
  gone, so a paid call that cannot improve a clean result is now simply one you
  have to ask for.

It is a flag, for the runs where the local model is slow, busy, or not running at
all:

```bash
./.venv/bin/python -m app.cv_tailor preview --url '<posting url>' --provider deepseek
```

`--provider` names the drafter for that run only — `local`, `deepseek` or
`anthropic`. It beats `CV_DRAFT_PROVIDER`, and because it is also what the
preflight checks, passing it means a stopped llama-server no longer blocks a
cloud draft. Leave it off and the configured default stays in charge.

Measured on the CACI *Senior Test Engineer* posting (2026-09-24): the same draft
took **14s** on `deepseek-flash` against **~90s** locally, and verified `accept`
with 100% → 100% priority coverage. Drafting is the only part that gets faster —
verification was already DeepSeek.

#### Preview, then approve

`preview` **drafts first and writes nothing**. You read the YAML, the verifier's
findings and the coverage figures, and can edit the YAML or regenerate with
`--feedback`. `render` is what creates files. Re-rendering archives the previous
version into `archived/` rather than overwriting it, so "what did I actually
send them?" stays answerable.

#### What stops it lying about you

Three independent gates, in order:

1. **The verifier** (DeepSeek) checks that every `ref` exists, that each reworded
   bullet still describes the master fact it cites, that no number is invented,
   and that the posting's must-haves the master cannot evidence are named
   plainly. Its verdict is advisory — you decide what to do with it.

   It is handed the draft **resolved**, not raw: bullets the draft left as a bare
   `- ref:` (which the renderer fills in from the master) are spelled out, and
   each bullet is labelled *verbatim from master* or *reworded by the draft*.
   Verifying the raw YAML instead means the most faithful draft possible — every
   bullet the master's own words, nothing rewritten — reads as a CV with no
   bullets in it, gets flagged for it, and sends the escalation chasing a defect
   that does not exist. See `resolve_draft_for_verification`.
2. **Reference validation** refuses a draft whose `ref` is not in the master,
   naming every bad ref at once and listing the valid ones.
3. **`render.py`'s fabrication check** is the hard gate. A skill the master
   cannot support makes it exit 3 and write *nothing*. That is not a bug to work
   around; it is the reason the output can be trusted.

A drafted response can also be cut off at `max_tokens`, and a truncated YAML
document is very often still *valid* YAML — so structural completeness is
checked explicitly, and a missing `ref` is reported as truncation rather than
left to surface two stages later as a dangling reference.

#### Two skill groups are mandatory

`AI-Assisted Engineering` and `Personal & Prototype Work` appear on **every**
tailored CV, whether or not the posting is about AI. `Claude Code` is pinned
inside the first, and the local-LLM stack — Playwright, Python, Ollama,
llama.cpp, Qwen3-Coder, DeepSeek V4 Flash, Local LLM Deployment — inside the
second.

This is enforced in code (`REQUIRED_SKILL_GROUPS` / `REQUIRED_SKILL_ITEMS` in
`app/cv_tailor.py`), not merely requested in the prompt, and it is a correction
of a real regression: an over-tuned instruction to "list only the skill items
that serve this posting" was applied by the local model to whole *groups*, so a
CV shipped with none of that content. Relevance decides which items lead within
a group; it does not decide whether the group exists. Every other group is still
dropped freely when it does not serve the posting.

The distinction that matters: `Claude Code` is **employer** work (AI-assisted
test generation adopted in a commercial role), while the local-LLM
stack is **personal** time. They live in different groups for that reason, and
the master's comments say not to move them.

#### Every role and every bullet is mandatory too

The experience section is a **presentation of the master, not a selection from
it**. Every role appears, and every achievement under each role appears. What the
draft decides is the *order* — the bullet that best answers the posting leads —
and the *wording*, where the posting's own noun is a truer name for work the
candidate already did.

The prompt said the opposite until 2026-09-25 ("Select AGGRESSIVELY ... Drop
roles and bullets that do not serve this application"), and the local drafter
obeyed it. Measured over the first twelve tailored CVs: the cloud drafter kept
all 19 of the master's achievements in 3 of 3 drafts, the local model in only 3
of 9, otherwise dropping between 4 and 9. The RevTech draft kept 10 of 19 — and
of the nine it lost, three carried the master's *only* numbers (6 months, 54
repositories, three-week sprints) and one was its only leadership bullet.

So `merge_required_experience()` restores what the drafter left out, for the same
reason `REQUIRED_SKILL_GROUPS` exists: a request in a prompt is not enforcement,
and a small model's judgement about what to leave off is systematically wrong —
it selects on whether a bullet names a tool the posting already mentioned, not on
which facts are worth keeping. The drafter's **order is never touched**, and
restored bullets are inserted where the master puts them relative to the bullets
the drafter kept. A restored role carries no explicit bullet list, because an
absent `achievements` key already means "every achievement for this role".

Why items may still be trimmed inside a skill group but not from experience: a
group is a *catalogue*, where an undifferentiated list dilutes the items that
matter, so choosing between items is real editorial work. A bullet is a discrete
*fact*, so omitting one does not sharpen the CV — it deletes a claim the
candidate was entitled to make.

Everything the code restores is named in the preview's `readded` list
(`Claude Code (re-added to their group)`), so a code-level correction never
passes as the model's own judgement.

#### Coverage figures

Two numbers are shown, and they are **the same measurement on both sides**:
requirement-level terms the posting names that the CV evidences.

```
master CV 80%  →  this tailored CV 80%
```

A tailored CV scoring a little below the master is normal — it drops skill items
and background prose on purpose. (An earlier version of the report compared the
master's *priority-term* coverage against the tailored CV's *all-term* coverage,
which made a sound draft read as an 83% → 26% collapse. The comparable figure is
now reported by `gap.py` itself.)

#### When the local model gets it wrong

The local 30B model has one characteristic failure, and it is worth knowing
because it is not random: **it adopts the posting's framing as if it were your
history.** On the Acme public-sector role (2026-09-23) it relabelled nine years
of FinTech compliance work as *"experience in public sector domains"* — the exact
kind of claim that wins a screening and collapses at interview.

The verifier caught it and returned `reject`, which is the system working. But a
rejection on its own left nothing to do with it, so:

**A rejected or flagged draft is automatically re-drafted by DeepSeek, using the
verifier's own corrections.** The escalation is not blind — it is handed the
verifier's `required_fixes` and quoted evidence, and the result is verified
again before being shown. If the retry does not verify better than the original,
the original is kept.

- Reported real result on the Acme role: `local` → **reject** → `deepseek` →
  **revise**, in 67 seconds, with the fabricated claim gone.
- The preview lists every attempt and why each was set aside, so a
  cloud-written CV never reads as the local model's work. That provenance is
  **stored** (`cv_tailorings.attempts`), not re-derived from `draft_model` —
  which names only the draft that survived, so an escalation that was made and
  then beaten on verdict would otherwise leave no trace at all.
- `missing_must_haves` is deliberately **excluded** from that handover. Those are
  genuine gaps in your history, so "fix this" could only mean invent it — the one
  outcome the whole system exists to prevent.
- No `DEEPSEEK_API_KEY`, or a cloud failure mid-escalation, degrades cleanly: you
  keep the local draft and its findings, and the reason says why.

To change the balance: `CV_DRAFT_PROVIDER=deepseek` drafts in the cloud from the
start (best quality, every CV costs money). The escalation default spends cloud
calls only on drafts that actually need them.

A `reject` is never a dead end — the YAML is editable in the preview, and the
verifier's findings name the exact sentence to remove.

#### The drafter is taught its own proven mistakes

An escalation that works is evidence: the verifier flagged the local draft, a
stronger model re-drafted it, and the result verified **strictly better**. That
is proof the local draft was wrong, and it is the only thing this loop treats as
proof. Once a failure mode has been proven **twice**, every subsequent draft is
told about it.

Three modes, a closed set — citing references that are not in the master,
inventing numbers, and rewriting a bullet into something the master does not say:

```
python -m app.cv_tailor lessons                      # what it has learned, and how often
python -m app.cv_tailor lessons --clear <mode>       # stop teaching one of them
python -m app.cv_tailor lessons --clear              # forget everything
```

**The model never writes the prompt.** Only the remedy text in
`cv_tailor.FAILURE_MODES` reaches the drafter, and it is written by hand. The
verifier's own wording is stored alongside each count so you can audit what
produced it, and is never injected. This is the property that makes the loop safe
to leave switched on: a loop that taught the drafter whatever the verifier said
would have taught it the Savant false positive — that ref-only drafts are broken
— permanently, which is the exact reverse of true.

The other two gates matter as much. **A flag alone teaches nothing**: a retry that
verifies no better is not evidence of a drafting error, which is precisely the
Savant case. And **`missing_must_haves` is never consulted** — those are real gaps
in your history, and a remedy drawn from one could only read "close this gap",
which can only mean inventing it.

The honest limit: this only helps with **recurring, generalizable** failures. A
one-off posting-specific judgement call is not learnable, so it reduces cloud
escalations without eliminating them.

#### Updating the master, and why it is manual

`cv/master.yaml` is the only file holding facts, and it is edited **by hand**.
Nothing in a tailoring run writes to it.

The master full inventory at the root of `cv_output/` is **on demand only** —
`make cv-inventory`. It is *not* regenerated by a tailoring run, deliberately:
updating it is a human judgement that a new skill or achievement is real, and
regenerating it automatically would make the inventory look current whenever it
merely looked recent.

The preview is what tells you whether to update it: the verifier lists the
posting's requirement-level terms that the master cannot evidence. If one of
them describes work you have actually done, add it to `cv/master.yaml` by hand
and press `make cv-inventory`. If it does not, it stays a gap — and the
interview prep helps you answer for it honestly. Wiring those suggestions
straight into the master is exactly the failure the fabrication guard exists to
prevent.

#### CV commands

| Command | What it does |
|---|---|
| `make cv-setup` | Create `.venv` and install the rendering dependencies (one-time, and after any `requirements.txt` change). |
| `make cv-status` | What `cv/master.yaml` holds, and whether it is still placeholder data. |
| `make cv-inventory` | Re-render the master full inventory into `cv_output/`. On demand only. |
| `make cv-selftest` | The vendored engine's own 202 checks, including that it hard-fails on a fabricated skill. |
| `make cv-doc` | Check the master CV Google Doc is reachable, and which copy of it a scan would read. |
| `make cv-scan URL='…'` | ATS-scan the master CV against one posting. Free and deterministic, with no network call while the cached copy of the Doc is fresh — and the way to see the raw JSON behind a panel verdict. |

The pipeline is also runnable directly. The board drives `scan` and nothing else;
`preview` and `render` are done by hand, over the same one-JSON-object protocol:

```bash
./.venv/bin/python -m app.cv_tailor preview --url '<posting url>'  # draft + verify, writes no CV
./.venv/bin/python -m app.cv_tailor render  --url '<posting url>'  # write + render the stored draft
```

Both emit a single JSON object on stdout, so the pipeline's own error messages can
be shown verbatim.

#### Setup note

The CV pipeline runs in this repo's own venv — `python-docx`, `fpdf2`, `pypdf`
and `PyYAML` are not installed system-wide. `make cv-setup` creates it, and a CV
command tells you to run it if it is missing.

#### Privacy

Everything derived from the CV is git-ignored: `cv/` (the fact base holds your
name, email, phone number and full employment history) and `cv_output/` (every
rendered document). The vendored *code* — `scripts/` and `templates/` — is
deliberately not ignored; it is machine, not facts. `context/` is the exception:
it was read as machine documentation until the fork went public, and its
data-model note turns out to carry the same facts as `cv/master.yaml`, so it is
ignored at the directory level too (see `.gitignore`). `git status` stays clean
of CV data, which is what makes the repository safe to push if you ever want to.

## Makefile commands

Thin wrappers over the commands above — run from the repo root:

| Command        | Equivalent to                    | What it does |
|-----------------|----------------------------------|--------------|
| `make run`      | `python -m app.main`             | Fetch + filter (step 1). Interactively asks whether to skip companies/aggregators/discovery and whether to limit to one company slug, instead of you remembering the flags. |
| `make evaluate` | `python -m app.ai_evaluate`       | AI evaluation (step 2). Interactively asks for `--dry-run` and an optional `--limit`. |
| `make web`      | `npm --prefix web run dev`       | Starts the Next.js review board, with `DB_PATH` already pointed at `data/seen_jobs.sqlite3`. |
| `make test`     | `pytest app/` + the standalone sanity-check scripts | Runs the full test suite (see the Tests section below). |
| `make test-ui`  | `npm --prefix web run test:ui`   | The board's 21 Playwright UI tests, headless Chrome. Self-contained: it seeds its own fixture database and starts its own server on port 3100, so it neither touches `data/seen_jobs.sqlite3` nor disturbs a board you already have open on `:3000`. |
| `make test-ui-headed` | `npm --prefix web run test:ui:headed` | The same 10 tests in a real, visible Chrome window — for watching a journey happen, or demoing it. `QA_SLOWMO=300 make test-ui-headed` slows each action down enough to follow. |
| `make cv-setup` | `python3 -m venv .venv` + `pip install -r requirements.txt` | One-time: creates the venv the CV pipeline runs in. |
| `make cv-status` | `python -m app.cv_tailor master-status` | What `cv/master.yaml` holds, and whether it is placeholder data. |
| `make cv-inventory` | `python -m app.cv_tailor master-inventory` | Re-renders the master full inventory. On demand only — never automatic. |
| `make cv-selftest` | `python scripts/selftest.py` | The vendored engine's 202 checks, including the fabrication guard. |
| `make cv-doc` | `python -m app.cv_tailor master-doc` | Is the master CV Google Doc reachable, and which copy would a scan read? |
| `make cv-scan URL=…` | `python -m app.cv_tailor scan --url …` | ATS-scan the master CV against one posting. No model call, no cost. |

`make` with no target runs `make run` (the default goal).

These wrappers cover the pipeline, the board and the CV engine. Two things they
deliberately do **not** cover, because they take long free-text arguments:
`python -m app.add_job` (adding a LinkedIn find by URL or paste — see 2b) and the
one-off maintenance tools (`app.refilter`, `app.inspect_job`). Both, plus the
exact ordering of a normal run, are in [`CHEATSHEET.md`](CHEATSHEET.md).

## Configuration

- **`companies.yaml`** — the company registry (name, ATS type, slug). Add
  a company here once you've identified its ATS.
- **`aggregators.yaml`** — aggregator search config (keywords, location,
  pagination limits).
- **`filters.yaml`** — title allowlist/exclusion keywords, location
  allowlist patterns, the three language tiers (priority / secondary /
  dealbreakers), and the security-clearance rules. Edit this directly as you
  refine what counts as in-scope for you — no code changes needed (matching
  logic lives in `app/filters.py`; see
  [What is in scope](#what-is-in-scope)).
- **`contract.yaml`** — the contract-role settings: the target permanent
  salary, billable days per year, every 2026/27 tax rate, the umbrella
  margin, the accountant's fee, and the policy for inside/outside/unknown
  IR35. This is where the day-rate thresholds come from — see
  [Contract roles and day rates](#contract-roles-and-day-rates). It is
  **gitignored**, because the target salary in it is personal: the committed
  template is `contract.yaml.example`, and `contract_rates.load_config()`
  falls back to reading that when `contract.yaml` is absent.
- **`jd_text.py`** — not config, but the module that decides whether a stored
  description is the advert or a listing page; see
  [When the description is not the advert](#when-the-description-is-not-the-advert).
- **`profile.yaml`** — your experience profile fed to the AI evaluation
  step (see `profile.example.yaml` for the blank template and
  `profile.sample.yaml` for a fully worked example).

## Project layout

```
app/
  main.py                 orchestrates fetch -> filter -> dedup -> candidates.csv
  ats_clients.py           one fetch function per ATS
  aggregator_clients.py    one fetch function per aggregator
  add_job.py               adds a LinkedIn posting by URL or paste (see 2b and
                           CHEATSHEET.md); the row behaves like any fetched job afterwards
  linkedin_fetch.py        reads a posting's public LinkedIn page for add_job — anonymous,
                           no login or session cookie, and no attempt to defeat a block
  duplicates.py            decides when two adverts are the same job re-advertised, and
                           carries a decision you already made across to the copies
  find_duplicates.py       CLI: report the duplicates on the board, and --apply the links
  contract_rates.py        IR35 day-rate equivalence, day-rate/IR35 parsing, the printed report
  filters.py               loads and applies filters.yaml's rules
  jd_text.py               decides whether a stored description is the advert or a listing page
  dedup.py                 SQLite store (seen_jobs, job_details)
  discover_companies.py    auto-appends newly-resolved companies to companies.yaml
  ai_evaluate.py           stage 2: Claude-based fit scoring
  inspect_job.py           CLI to look up a stored job or list recent rejections
  refilter.py              re-runs current filters.py against already-fetched jobs
  scripts/                 one-off diagnostic/maintenance scripts, not part of the pipeline
                           (see backfill_derived_fields.py, queue_polluted_descriptions.py)
  tests/                   pytest + standalone sanity-check scripts
                           test_qa_component.py            QA component suite (7 tests)
                           test_qa_component_containers.py QA suite over a containerised
                                                           upstream (3 tests, skipped without Docker)
                           qa/                             the mock upstream + the testcontainers
                                                           fixture and URL shim those two share
web/                       Next.js review board (see web/README.md)
  e2e/                     Playwright UI suite (10 tests) + the fixture board it runs against
CHEATSHEET.md              the commands for each task, in the order you run them
QA_REPORT.md               the QA test plan, how to run both suites, and the findings
companies.yaml             company -> ATS registry
aggregators.yaml           aggregator search config
filters.yaml               title/location/stack filter rules
contract.yaml.example      template for contract.yaml (the real one is gitignored)
profile.example.yaml       blank template for profile.yaml (your real profile, gitignored)
profile.sample.yaml        fully worked (fictional) example of a filled-in profile
```

## Tests

```bash
make test          # python suite + the web app's date-parsing tests
make test-web      # just the web tests and typecheck
make test-ui       # the board's 21 Playwright UI tests (headless Chrome)
```

`make test` runs the pytest suite, the standalone sanity-check scripts
(`test_pipeline.py`, `test_discover_companies.py`, `test_ai_evaluate.py`),
and the web app's tests via Node's built-in runner (Node ≥ 23 strips the
TypeScript types itself, so there is no jest/vitest and no transpile step).
None of them call live external APIs.

`app/tests/test_screening.py` is the one to read before changing the language
or clearance rules: its fixtures are real advert text, and it asserts as much
about what must NOT be rejected (a Java role mentioning Python; a posting that
names no language; "eligible for SC") as about what must be.
`app/tests/test_contract_rates.py` is the one to read before changing
`contract.yaml`: it pins every rate to the value verified against gov.uk for
2026/27, and because the day-rate thresholds decide whether a contract is
worth applying to, a changed digit there is a decision someone should have to
make on purpose rather than a silent drift. The day-rate arithmetic is
asserted at a fixed `PINNED_SALARY` rather than at whatever the local
`contract.yaml` holds, so the suite is still green on a fresh clone that has
only the example salary.

### The QA suites — positive, negative, boundary and error handling

Beyond the module-level tests above there are two suites organised as a test
plan, twenty tests in total, covering the pipeline (in `app/tests/`) and the
board's journeys (in `web/e2e/`):

| | Component (Python) | UI (Playwright) |
|---|---|---|
| Positive | `app/tests/test_qa_component.py` | `web/e2e/board.spec.ts`, `status.spec.ts`, `tailoring.spec.ts` |
| Negative | same file | `board.spec.ts`, `status.spec.ts` |
| Boundary | same file | `board.spec.ts` |
| Error handling | same file + `test_qa_component_containers.py` | `states.spec.ts` |

Three of the Python tests drive the real HTTP clients against a mock upstream
running in a **Docker container** (`python:3.12-alpine`, standard library only),
because a `MagicMock` in place of `httpx.get` can verify parsing but can never
produce the failures that actually break a run — a 500 with an HTML body, a 200
with a truncated body, a 401 that echoes your API key back. Without a Docker
daemon those three *skip with a reason* rather than failing, and the other
seventeen still run.

The UI tests are stubbed at exactly two points — the CV draft and render routes,
which make ~90-second model calls — and unstubbed everywhere else, so every
assertion about stored state reads a real database through a real route. They run
against a seeded fixture board, never `data/seen_jobs.sqlite3`, and `make test-ui`
brings up everything they need by itself.

**[`QA_REPORT.md`](QA_REPORT.md)** has the full detail: what each of the twenty
tests protects and why, how the mock upstream and the fixture board are built,
the two Next.js constraints the harness works around, and the five findings the
exercise produced — including an `add_job(url="")` key collision, and an epoch
timestamp stored as a float that is read as "no date", which makes an old posting
permanently **visible** to the date window instead of hideable by it.

## Notes on scope

- ATS fetchers for Greenhouse, Ashby, Workable, and Lever were each
  validated against real live responses. SmartRecruiters support is built
  from documentation and third-party corroboration only — verify a new
  SmartRecruiters company with `python -m app.main --company <slug>`
  before trusting it in a real run.
- If a particular company's fetch fails, `main.py` logs a warning and
  continues with the rest rather than crashing the whole run.

## Possible next steps

- Workday support (`clio.wd3.myworkdayjobs.com`-style boards) — its
  job-search API is POST-based with a different pagination shape than the
  ATSes currently supported.
- Company-name normalization for aggregator-sourced dedup (the same
  posting sometimes comes back under slightly different company name
  strings, e.g. "Acme Corp" vs. "Acme").
- A digest output (email/Sheet) beyond the current CSV/SQLite/web-board
  review flow.
- Scheduling: once you're happy with a full local run, a daily cron entry
  like `0 7 * * * cd /path/to/job_search_pipeline && python -m app.main && python -m app.ai_evaluate`.

## License

[MIT](LICENSE)
