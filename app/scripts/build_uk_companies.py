"""Build a verified UK employer list for companies.yaml.

WHY: the audit (app/scripts/audit_companies_uk.py) showed the inherited
companies.yaml — 204 mostly North-American companies — yields exactly ONE
company posting a role that passes Ed's London/UK + SDET title gate. The list
needs building, not pruning. This does the building.

WHAT IT DOES
  Phase 1 — for each candidate UK employer name, guess ATS slugs and probe the
  five supported ATSes (greenhouse, ashby, lever, workable, smartrecruiters).
  Phase 2 — VERIFY IDENTITY before believing a hit.

Phase 2 is the part that matters, and it is not paranoia: the repo already has
two documented cases of a resolving slug being the wrong board (Coveo's
"coveodeven" was a live dev/test board with 1 fake job; Treewalk's first
guessed slug 404'd while a different slug worked). Guessing slugs for short
British company names is far worse than that — "wise", "tide", "curve", "plum",
"monzo", "cleo", "huma", "faculty" and "sky" are ordinary English words, and
boards-api.greenhouse.io will happily return SOME company's real postings for
them.

So a hit is only accepted when the postings themselves corroborate it:
  * the company's distinctive name token appears in the postings' own text
    (titles/descriptions, which the fetchers already download), AND
  * the board actually has UK/London postings.
Anything else goes to a review list instead of into companies.yaml.

Usage (run from the repo root):
    python -m app.scripts.build_uk_companies                 # report only
    python -m app.scripts.build_uk_companies --write         # append confirmed entries
    python -m app.scripts.build_uk_companies --workers 4 --verbose
"""
import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

from app import ats_clients

# Probe order matters: these are tried left to right and a candidate stops at
# its first resolving slug per ATS. Ordered by how common each ATS is among
# UK scaleups. (Banks and other large UK enterprises are mostly on
# Workday/Taleo/SuccessFactors, which this repo has NO client for — see the
# LIMITATIONS note in the report.)
ATS_TYPES = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]

# Candidate UK employers. Format: "Display Name" or "Display Name | alias1,alias2"
# where aliases are extra slug seeds (brand names, former names, domains).
# Deliberately weighted to companies that actually run QA/test-automation
# functions in the UK: fintech, payments, trading, consultancies, testing
# service providers, product/marketplace tech.
CANDIDATES = [
    # --- Fintech, payments, neobanks ---
    "Monzo", "Starling Bank | starling", "Revolut", "Wise | transferwise",
    "Checkout.com | checkout,checkoutcom", "GoCardless", "Tide", "Zopa", "Zego",
    "Marshmallow", "Cleo", "ThoughtMachine", "Form3", "Quantexa", "Featurespace",
    "ComplyAdvantage", "Onfido", "Napier | napierai", "TrueLayer", "Modulr",
    "Paddle", "Primer | primerapi", "Vitesse", "Ebury", "Codat", "Yapily",
    "ClearBank | clearbank", "Allica Bank | allica", "OakNorth", "Atom Bank | atombank",
    "Zilch", "Wagestream", "Curve", "Freetrade", "Plum", "Moneybox", "Nutmeg",
    "PensionBee", "Smart Pension | smartpension", "iwoca", "Funding Circle | fundingcircle",
    "Habito", "Molo", "Ravelin", "Fnality", "Banked", "Weavr", "OpenPayd",
    "PPRO", "Capital on Tap | capitalontap", "NewDay | newday", "Juro", "Liberis",
    "Shieldpay", "Ryft", "Paytrix", "Gr4vy | gr4vy", "Tandem Bank | tandembank",
    "Hampden & Co | hampdenandco", "Cynergy Bank | cynergybank",
    # --- Trading, exchanges, investment, insurance ---
    "IG Group | iggroup", "CMC Markets | cmcmarkets", "Plus500",
    "Hargreaves Lansdown | hargreaveslansdown", "AJ Bell | ajbell",
    "Interactive Investor | interactiveinvestor", "LSEG | lseg",
    "Cboe Europe | cboe", "Tradeweb", "TP ICAP | tpicap", "Marex", "Peel Hunt | peelhunt",
    "Investec", "Schroders", "abrdn", "M&G | mandg", "Legal & General | legalandgeneral",
    "Aviva", "Admiral | admiralgroup", "Direct Line | directline", "Bupa", "Vitality",
    "Howden", "Beazley", "Hiscox", "Rothesay | rothesaylife", "Just Group | justgroup",
    "Phoenix Group | phoenixgroup", "M&G Investments | mandginvestments",
    # --- Consultancies and testing service providers ---
    "Resillion", "Ten10 | ten10group", "Sogeti | sogetiuk", "BJSS", "Softwire",
    "Made Tech | madetech", "Kainos", "Thoughtworks", "Zuhlke", "Version 1 | version1",
    "Sparta Global | spartaglobal", "FDM Group | fdmgroup", "Qualitest", "Expleo",
    "TestingXperts", "nFocus | nfocus", "Testhouse", "Slalom", "Sabio",
    "Infosys", "Cognizant", "Capgemini", "Deloitte", "VML | vml", "NTT Data | nttdata",
    "Tata Consultancy Services | tcs", "Wipro", "Publicis Sapient | publicissapient",
    # --- Product, marketplace and consumer tech ---
    "Depop", "Deliveroo", "Just Eat Takeaway | justeattakeaway",
    "Ocado | ocadotechnology", "ASOS", "Farfetch", "Marks & Spencer | marksandspencer",
    "Tesco | tesco", "Sainsbury's | sainsburys", "John Lewis | johnlewispartnership",
    "Waitrose", "Trainline | thetrainline", "Citymapper", "Skyscanner", "Trustpilot",
    "Moonpig", "Bloom & Wild | bloomandwild", "Auto Trader | autotrader", "Rightmove",
    "Zoopla", "Compare the Market | comparethemarket", "MoneySuperMarket | moneysupermarket",
    "ManyPets | manypets", "Bumble", "On the Beach | onthebeach", "Loveholidays",
    "Jet2", "EasyJet | easyjet", "Octopus Energy | octopusenergy", "OVO Energy | ovoenergy",
    "E.ON Next | eonnext", "Centrica", "SSE | sse", "National Grid | nationalgrid",
    "Currys", "Boots | boots", "The Very Group | theverygroup", "N Brown | nbrown",
    "THG | thg", "Gymshark", "Made.com | made", "Cazoo", "Cinch | cinch",
    # --- Cybersecurity, data, AI, health ---
    "Darktrace", "Mimecast", "Egress | egress", "Immersive Labs | immersivelabs",
    "Red Sift | redsift", "Tessian", "Snyk", "NCC Group | nccgroup",
    "Huma", "Cera | cerahealth", "Accurx", "Doccla", "Zava", "Livi",
    "Synthesia", "ElevenLabs", "PolyAI", "Tractable", "Faculty", "Multiverse",
    "Beamery", "Signal AI | signalai", "Eigen Technologies | eigentechnologies",
    "Wayve", "Stability AI | stabilityai", "Isomorphic Labs | isomorphiclabs",
    "Graphcore", "Improbable",
    # --- Media, broadcast, publishing ---
    "Sky | skyuk", "ITV", "Channel 4 | channel4", "Guardian News | theguardian",
    "Financial Times | financialtimes", "The Economist | economist", "DAZN",
    "Bloomsbury | bloomsbury", "Pearson", "BBC | bbc",
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


_SUFFIX_WORDS = {
    "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "group", "holdings", "uk", "plc", "the", "and", "technologies",
    "technology", "tech", "labs", "bank", "banking", "services", "solutions",
}


def slug_variants(display_name: str, aliases: list[str]) -> list[str]:
    """Ordered, de-duplicated slug guesses for one company.

    Broader than app/scripts/try_companies_across_ats.py's candidate_slugs():
    that one only produced joined/hyphenated forms of the suffix-stripped name,
    which misses the two shapes UK boards actually use most — a bare brand word
    ("Starling Bank" -> "starling") and a squashed domain ("Checkout.com" ->
    "checkoutcom")."""
    out: list[str] = []

    def add(v: str):
        v = v.strip().strip("-")
        if v and v not in out and len(v) >= 3:
            out.append(v)

    for alias in aliases:
        add(_norm(alias))

    for part in [p.strip() for p in re.split(r"[()]", display_name) if p.strip()]:
        base = re.sub(r"\.(com|io|co|ai|net|org)\b", "", part.lower())
        base = base.replace("&", " and ")
        base = re.sub(r"[^\w\s-]", "", base)
        words = base.split()
        significant = [w for w in words if w not in _SUFFIX_WORDS] or words
        if not significant:
            continue
        joined, hyphened = "".join(significant), "-".join(significant)
        add(joined)
        add(hyphened)
        add("".join(words))            # keeps suffix words: "starlingbank"
        if len(significant) > 1:
            add(significant[0])        # bare brand: "starling"
            add("-".join(significant[:2]))
    return out[:5]                     # cap: each extra slug is 5 more requests


# --- identity verification -------------------------------------------------
# Ordinary English words that appear in job ads for reasons unrelated to the
# company. A single occurrence of one of these is NOT evidence: "next" matches
# "next steps" in essentially every JD, and "wise"/"tide"/"curve" have common
# non-brand senses ("otherwise", "learning curve"). For a company whose only
# name tokens are all in this set, we demand a multi-word/joined match instead.
AMBIGUOUS_TOKENS = {
    "next", "new", "one", "plus", "now", "wise", "tide", "curve", "sky",
    "core", "prime", "data", "digital", "global", "group", "energy", "health",
    "trust", "smart", "clear", "open", "circle", "star", "just", "eat",
}

_WORD_RE = re.compile(r"[^a-z0-9]+")


def _blobs(text: str) -> tuple[str, str]:
    """(word-separated, fully-normalized) lowercase forms. Word form is for
    \\b-anchored matching, normalized form for joined-brand matching."""
    low = (text or "").lower()
    return _WORD_RE.sub(" ", low), _WORD_RE.sub("", low)


def _name_patterns(display_name: str) -> tuple[list[str], list[str]]:
    """(strong, weak) evidence for "this board really is `display_name`".

    strong = distinctive multi-word evidence: the squashed brand
      ("marksandspencer", "justeattakeaway") or two adjacent name words
      ("just eat", "octopus energy"). Any one hit is convincing.
    weak = a single name word standing alone with word boundaries. "wise" no
      longer matches "otherwise", but needs corroboration (see verify()).
    """
    # Squash intra-word dots first, or "E.ON Next" splits into ["e", "on",
    # "next"], loses both short fragments to the length filter, and leaves only
    # the useless everyday word "next" as evidence.
    cleaned = re.sub(r"(?<=[a-z0-9])\.(?=[a-z0-9])", "", display_name.lower())
    words = [w for w in _WORD_RE.split(cleaned) if len(w) >= 3]
    words = [w for w in words if w not in _SUFFIX_WORDS]

    strong: list[str] = []
    if len(words) >= 2:
        strong.append("".join(words))                                   # squashed brand
        strong += [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)]  # adjacent pair
    weak = [w for w in words if len(w) >= 4]
    return strong, weak


def verify(name: str, jobs: list[dict]) -> dict:
    """Count what matters and decide whether this board is really `name`."""
    from app import filters

    strong, weak = _name_patterns(name)
    strong_re = [re.compile(r"\b" + re.escape(s) + r"\b") for s in strong if " " in s]
    weak_re = [re.compile(r"\b" + re.escape(w) + r"\b") for w in weak]

    uk = london = gate = 0
    strong_hits = weak_hits = 0
    for j in jobs:
        loc = j.get("location") or ""
        title = j.get("title") or ""
        desc = j.get("description") or ""
        if filters.location_is_allowed(loc):
            uk += 1
        if re.search(r"\blondon\b", loc, re.I):
            london += 1
        if filters.title_is_relevant(title) and filters.location_is_allowed(loc):
            gate += 1

        words_blob, norm_blob = _blobs(f"{title} {desc}")
        s_hit = any(s in norm_blob for s in strong) or any(r.search(words_blob) for r in strong_re)
        w_hit = any(r.search(words_blob) for r in weak_re)
        if s_hit:
            strong_hits += 1
        elif w_hit:
            weak_hits += 1

    # Corroboration rule: one strong hit, OR at least two weak hits from a
    # token that isn't an everyday English word. Anything less is not enough to
    # claim this is the right company's board.
    unambiguous_weak = [w for w in weak if w not in AMBIGUOUS_TOKENS]
    if strong_hits >= 1:
        identity_ok, why = True, f"{strong_hits} multi-word name match(es)"
    elif weak_hits >= 2 and unambiguous_weak:
        identity_ok, why = True, f"{weak_hits} standalone name match(es)"
    else:
        identity_ok, why = False, (
            f"strong={strong_hits} weak={weak_hits}"
            + ("" if unambiguous_weak else " (name is all everyday words)")
        )

    return {
        "total": len(jobs), "uk": uk, "london": london, "gate": gate,
        "strong_hits": strong_hits, "weak_hits": weak_hits,
        "identity_ok": identity_ok, "identity_why": why,
        "token": (strong or weak or [""])[0],
    }


_lock = threading.Lock()

# Authoritative board-name endpoints. Where an ATS exposes one, we use the name
# the ATS itself reports instead of inferring identity from posting text: that
# turns "probably the right company" into "the board says it is this company".
# Ashby and Lever have no such endpoint, so they fall back to the text
# heuristic in verify() (which held up well: Zopa 34/34 postings matched,
# Trainline 29/29).
_META_URLS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
}

# An ATS that has rate-limited us hard is disabled for the REST OF THE RUN.
#
# This is not a micro-optimisation: it is the difference between a 3-minute run
# and a 45-minute one. Workable answers a 429 with `Retry-After: 85689` — about
# 23.8 hours, i.e. an IP-level ban rather than a nudge — and once it starts it
# does so for EVERY slug, including slugs that resolved minutes earlier. The
# first version of this script treated all 429s alike: sleep 3s, retry once.
# Against an 86,000-second ban that wasted ~6 seconds on each of 130 companies,
# and kept re-attempting Workable for every slug of every candidate. Honouring
# the header and abandoning that host turns all of it into zero-cost skips.
_DISABLED_ATS: dict[str, str] = {}
_DISABLE_AFTER_SECONDS = 60  # a longer Retry-After ends this run's use of that ATS


def _disable_ats(ats: str, reason: str) -> None:
    with _lock:
        if ats not in _DISABLED_ATS:
            _DISABLED_ATS[ats] = reason
            print(f"    !! {ats} disabled for the rest of this run: {reason}",
                  file=sys.stderr, flush=True)


def _retry_after(exc) -> float | None:
    """Seconds from a 429's Retry-After header, or None if absent/unparseable."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        raw = resp.headers.get("retry-after")
    except Exception:
        return None
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None  # HTTP-date form; not worth parsing here


def board_identity(ats: str, slug: str) -> str | None:
    """The company name the ATS itself reports for this board, or None if the
    ATS has no such endpoint / the lookup failed."""
    import httpx

    url = _META_URLS.get(ats)
    if not url or ats in _DISABLED_ATS:
        return None
    try:
        resp = httpx.get(url.format(slug=slug),
                         headers={"User-Agent": ats_clients.USER_AGENT},
                         timeout=ats_clients.TIMEOUT)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None
    if ats == "smartrecruiters":
        try:
            return (data["content"][0].get("company") or {}).get("name")
        except Exception:
            return None
    name = data.get("name") if isinstance(data, dict) else None
    return name or None


def names_agree(candidate: str, board_name: str | None) -> bool | None:
    """True/False if the ATS told us its name and we can compare; None if we
    have nothing authoritative to compare (fall back to text evidence).

    Deliberately loose on the board side, because boards are often titled
    differently from the company: greenhouse/tide reports "Careers at Tide"."""
    if not board_name:
        return None
    c_norm, b_norm = _norm(candidate), _norm(board_name)
    if c_norm and (c_norm in b_norm or b_norm in c_norm):
        return True
    _strong, weak = _name_patterns(candidate)
    return any(t in b_norm for t in weak) if weak else False


def probe_one(display_name: str, aliases: list[str], verbose: bool = False,
              slug_cap: int = 5) -> dict:
    """Try every slug variant against every ATS, stopping at the first
    resolving slug per ATS. Returns the best hit plus any throttled boards."""
    hits, throttled = [], []
    for slug in slug_variants(display_name, aliases)[:slug_cap]:
        for ats in ATS_TYPES:
            if any(h["ats"] == ats for h in hits):
                continue  # already found this company's board on this ATS
            if ats in _DISABLED_ATS:
                continue  # banned for the rest of the run — see _DISABLED_ATS
            fetcher = ats_clients.FETCHERS.get(ats)
            if fetcher is None:
                continue
            jobs = None
            for attempt in (1, 2):
                try:
                    jobs = fetcher(display_name, slug)
                    break
                except Exception as exc:
                    msg = str(exc)
                    if "429" not in msg and "Too Many Requests" not in msg:
                        break  # 404s and timeouts are ordinary misses
                    retry_after = _retry_after(exc)
                    if retry_after and retry_after > _DISABLE_AFTER_SECONDS:
                        _disable_ats(ats, f"429 with Retry-After {retry_after:.0f}s")
                        throttled.append(f"{ats}:{slug} (banned {retry_after:.0f}s)")
                        break
                    if attempt == 1:
                        time.sleep(3.0)  # short 429: back off, then retry once
                        continue
                    throttled.append(f"{ats}:{slug}")
                    break
            if not jobs:
                continue
            stats = verify(display_name, jobs)
            board_name = board_identity(ats, slug)
            agree = names_agree(display_name, board_name)
            if agree is not None:
                stats["identity_ok"] = agree
                stats["identity_why"] = (
                    f"ATS reports board name {board_name!r}"
                    + ("" if agree else " — MISMATCH")
                )
            stats["board_name"] = board_name
            hits.append({"name": display_name, "ats": ats, "slug": slug,
                         "titles": [j.get("title", "") for j in jobs[:4]], **stats})
            if verbose:
                with _lock:
                    print(f"    hit {ats}/{slug}: {stats['total']} jobs, "
                          f"{stats['uk']} UK, {stats['gate']} gate-pass, "
                          f"board_name={board_name!r}", flush=True)
            time.sleep(0.1)
    hits.sort(key=lambda h: (h["gate"], h["uk"], h["identity_ok"]), reverse=True)
    return {"name": display_name, "hits": hits, "throttled": throttled}


def scan(candidates: list[str], workers: int, verbose: bool, slug_cap: int = 5) -> list[dict]:
    parsed = []
    for c in candidates:
        if "|" in c:
            name, alias_blob = c.split("|", 1)
            parsed.append((name.strip(), [a.strip() for a in alias_blob.split(",") if a.strip()]))
        else:
            parsed.append((c.strip(), []))

    results, done = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe_one, n, a, verbose, slug_cap): n for n, a in parsed}
        for fut, name in futures.items():
            try:
                results.append(fut.result())
            except Exception as exc:
                # NOT the same thing as throttling. Conflating the two is how
                # the first version of this report ended up claiming 120
                # companies had been rate-limited when a controlled burst test
                # (60 rapid requests, all clean 404s) showed no rate limiting
                # at all. Keep the reason and print it.
                results.append({
                    "name": name, "hits": [], "throttled": [],
                    "error": f"{type(exc).__name__}: {exc}",
                })
            done += 1
            print(f"[{done:3d}/{len(parsed)}] {name}", file=sys.stderr, flush=True)
    return results


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true",
                    help="append identity-verified hits to companies.yaml")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent company probes (default 4 — be polite to shared ATS hosts)")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--slug-cap", type=int, default=5,
                    help="max slug guesses per company (default 5). Lower it for "
                         "large re-check runs: each slug costs up to 5 requests, and "
                         "the first two variants cover the joined and bare-brand shapes.")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only-file",
                    help="path to a file of display names (one per line) to probe "
                         "instead of the full CANDIDATES list — used to re-check the "
                         "entries a throttled run left unresolved")
    args = ap.parse_args()

    candidates = CANDIDATES
    if args.only_file:
        wanted = {ln.strip().lower() for ln in open(args.only_file) if ln.strip()}
        candidates = [c for c in CANDIDATES
                      if c.split("|")[0].strip().lower() in wanted]
        missing = wanted - {c.split("|")[0].strip().lower() for c in candidates}
        if missing:
            print(f"WARNING: {len(missing)} name(s) in {args.only_file} are not in "
                  f"CANDIDATES and will be skipped: {sorted(missing)[:6]}", file=sys.stderr)
    if args.limit:
        candidates = candidates[: args.limit]
    print(f"Probing {len(candidates)} candidate UK employers across "
          f"{', '.join(ATS_TYPES)}...\n", file=sys.stderr)

    results = scan(candidates, args.workers, args.verbose, args.slug_cap)

    trusted, review, no_uk, none_found, throttled_names, errored = [], [], [], [], [], []
    for r in results:
        if r.get("throttled"):
            throttled_names.append(r["name"])
        if r.get("error"):
            errored.append(r)
            continue
        if not r["hits"]:
            none_found.append(r["name"])
            continue
        best = r["hits"][0]
        if best["uk"] == 0:
            no_uk.append(best)
        elif best["identity_ok"]:
            trusted.append(best)
        else:
            review.append(best)

    print("\n" + "=" * 100)
    print(f"TRUSTED — board verified by name-in-postings AND has UK roles: {len(trusted)}")
    print("=" * 100)
    for h in sorted(trusted, key=lambda h: (-h["gate"], -h["uk"])):
        print(f"  {h['name'][:30]:30} {h['ats']:15} slug={h['slug'][:20]:20} "
              f"{h['total']:4d} jobs  {h['uk']:3d} UK  {h['gate']:2d} CANDIDATE  "
              f"identity={h['identity_why'][:34]}")

    if review:
        print(f"\n--- NEEDS A HUMAN LOOK — UK roles but the postings never mention "
              f"the company name ({len(review)}): possible wrong-company collision ---")
        for h in review:
            print(f"  {h['name'][:30]:30} {h['ats']:15} slug={h['slug'][:20]:20} "
                  f"{h['total']:4d} jobs  {h['uk']:3d} UK  e.g. {h['titles'][:2]}")

    print(f"\n--- Board found, but NO UK roles right now ({len(no_uk)}) — skipped ---")
    for h in sorted(no_uk, key=lambda h: h["name"]):
        print(f"  {h['name'][:30]:30} {h['ats']:15} slug={h['slug'][:20]:20} {h['total']:4d} jobs")

    if none_found:
        print(f"\n--- No board found on the 5 supported ATSes ({len(none_found)}) ---")
        print("    (Banks/enterprises are usually on Workday/Taleo, which this repo")
        print("     cannot read — Adzuna is the only way those get in.)")
        for n in none_found:
            print(f"  {n}")
    if _DISABLED_ATS:
        print("\n--- ATS HOSTS BANNED FOR THE REST OF THIS RUN ---")
        for ats, reason in _DISABLED_ATS.items():
            print(f"  {ats:16} {reason}")
        print("  => Every 'not found' result below is UNRELIABLE for these hosts, and")
        print("     'no UK roles' for a company that only exists there is meaningless.")

    if errored:
        print(f"\n--- CRASHED while probing ({len(errored)}) — 'not found' is NOT reliable for these ---")
        for r in errored:
            print(f"  {r['name'][:30]:30} {r['error'][:90]}")
    if throttled_names:
        print(f"\n--- Genuinely rate-limited (429) — 'not found' is NOT reliable for these ({len(throttled_names)}) ---")
        for r in results:
            if r.get("throttled"):
                print(f"  {r['name'][:30]:30} {', '.join(r['throttled'][:4])}")

    if args.write:
        if not trusted:
            print("\nNothing verified to write.")
            return
        from app import discover_companies
        added = discover_companies.append_new_companies(
            [{"name": h["name"], "ats": h["ats"], "slug": h["slug"]} for h in trusted],
            source_note="UK list built 2026-09 via app/scripts/build_uk_companies.py "
                        "(slug probed + identity-verified)",
        )
        print(f"\nWrote {len(added)} new entries to companies.yaml.")
    else:
        print("\n(report only — re-run with --write to append the TRUSTED list)")


if __name__ == "__main__":
    main()
