#!/usr/bin/env python3
"""Self-test for the CV tailoring pipeline.

Runs the whole thing against a fictional fixture job and asserts the behaviour
that actually matters:

  - the master loads and validates
  - dangling references are rejected rather than silently dropped
  - keyword extraction does not surface job-post filler as a skill gap
  - plurals and inflections don't invent gaps
  - sentence-final punctuation doesn't break matching
  - the fabrication guard catches skills with no master backing
  - DOCX output stays ATS-safe (no tables, images, headers or footers)
  - PDF output is single-column and text-extractable

Everything uses scripts/testdata/ — never your real CV, so the tests stay
meaningful as cv/master.yaml evolves.

Run it after any change to the scripts:
    ./.venv/bin/python scripts/selftest.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import keywords as kw
from model import CVError, Master, Tailored, ROOT

FIXTURE_MASTER = ROOT / "scripts" / "testdata" / "master.yaml"
FIXTURE_JOB = ROOT / "scripts" / "testdata" / "sample-data-engineer"

PASS = "  ok   "
FAIL = "  FAIL "
failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"{PASS}{name}")
    else:
        failures.append(name)
        print(f"{FAIL}{name}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    sample_dir = FIXTURE_JOB
    print("Pipeline self-test\n")

    # ---- master -----------------------------------------------------------
    print("master")
    master = Master.load(FIXTURE_MASTER)
    check("fixture master loads", bool(master.contact))
    check("roles indexed", len(master.roles_by_id) >= 3,
          f"got {len(master.roles_by_id)}")
    check("achievements indexed", len(master.achievements_by_id) >= 6,
          f"got {len(master.achievements_by_id)}")
    check("fact text is non-trivial", len(master.all_fact_text()) > 500)

    # ---- reference validation --------------------------------------------
    print("\nreference validation")
    bad = {"summary": "x", "experience": [{"ref": "no-such-role",
                                           "achievements": [{"ref": "nope"}]}]}
    try:
        Tailored(bad, master, Path("bad.yaml"))
        check("dangling role ref rejected", False, "no CVError raised")
    except CVError as exc:
        check("dangling role ref rejected", "no-such-role" in str(exc), str(exc))

    bad2 = {"summary": "x", "experience": [{"ref": "example-current",
                                            "achievements": [{"ref": "not-real"}]}]}
    try:
        Tailored(bad2, master, Path("bad2.yaml"))
        check("dangling achievement ref rejected", False, "no CVError raised")
    except CVError as exc:
        check("dangling achievement ref rejected", "not-real" in str(exc), str(exc))

    try:
        Tailored({"experience": []}, master, Path("bad3.yaml"))
        check("missing summary rejected", False, "no CVError raised")
    except CVError:
        check("missing summary rejected", True)

    # ---- keyword extraction quality --------------------------------------
    print("\nkeyword extraction")
    post = (sample_dir / "post.md").read_text(encoding="utf-8")
    terms, _ = kw.analyse_post(post, frozenset({"northwind", "retail", "group"}))

    filler = {"advanced", "comparable", "similar", "solid", "hands-on",
              "track", "record", "written", "code", "window", "functions",
              "backgrounds", "employer", "group", "northwind"}
    leaked = sorted(t for t in terms if t in filler)
    check("job-post filler suppressed", not leaked, f"leaked: {leaked}")

    check("real skills extracted",
          {"python", "sql", "snowflake", "dbt", "apache airflow",
           "terraform", "amazon web services"}.issubset(set(terms)),
          f"missing: {{'python','sql','snowflake','dbt','apache airflow',"
          f"'terraform','amazon web services'}} - {sorted(set(terms))}")

    check("phrases survive filler filtering",
          {"code review", "window functions", "data quality"} & set(terms) != set(),
          f"got {sorted(t for t in terms if ' ' in t)}")

    check("section weighting finds requirements",
          terms["snowflake"].in_requirements > 0)

    # ---- inflection handling ---------------------------------------------
    print("\ninflection handling")
    haystack = "Built and operated production pipelines across the warehouse."
    for term in ("pipelines", "pipeline"):
        t = {term: kw.Term(term=term, count=1, surfaces={term: 1})}
        present, missing = kw.coverage(t, haystack)
        check(f"'{term}' matches its inflection", len(present) == 1,
              f"missing={[m.term for m in missing]}")

    t = {"warehousing": kw.Term(term="warehousing", count=1, surfaces={"warehousing": 1})}
    present, _ = kw.coverage(t, "Responsible for the data warehouse.")
    check("'warehousing' matches 'warehouse'", len(present) == 1)

    t = {"process": kw.Term(term="process", count=1, surfaces={"process": 1})}
    present, _ = kw.coverage(t, "Documented the onboarding processes end to end.")
    check("'process' matches 'processes'", len(present) == 1)

    t = {"kafka": kw.Term(term="apache kafka", count=1, surfaces={"kafka": 1})}
    present, missing = kw.coverage(t, "Built pipelines in Python and SQL.")
    check("a genuine gap stays a gap", len(missing) == 1)

    # ---- fabrication guard -----------------------------------------------
    print("\nfabrication guard")
    tailored = Tailored.load(sample_dir / "tailored.yaml", master)
    check("sample tailored CV passes the guard",
          not tailored.unsupported_claims(),
          str(tailored.unsupported_claims()))

    data = dict(tailored.data)
    data["skills"] = [{"group": "Invented", "items": ["Quantum Fortran"]}]
    fabricated = Tailored(data, master, Path("fab.yaml"))
    problems = fabricated.unsupported_claims()
    check("invented skill is caught", len(problems) == 1 and "Quantum Fortran" in problems[0],
          str(problems))

    # ---- headline override ------------------------------------------------
    # The master carries exactly one headline, which is right for the master and
    # wrong for a posting in a different discipline. Tailoring may re-lead with a
    # different SUBSET of Ed's real attributes; it may not assert a new one, so
    # the override goes through the same prose check as the summary.
    print("\nheadline override")
    base_data = dict(tailored.data)
    base_t = Tailored(base_data, master, Path("base.yaml"))
    check("no override falls back to the master headline",
          base_t.display_headline() == master.contact["headline"],
          base_t.display_headline())

    hd_data = dict(base_data)
    hd_data["headline"] = "Senior Widget Engineer | Streaming Platforms"
    hd_t = Tailored(hd_data, master, Path("hd.yaml"))
    check("override wins over the master headline",
          hd_t.display_headline() == "Senior Widget Engineer | Streaming Platforms",
          hd_t.display_headline())

    bad_hd = dict(base_data)
    bad_hd["headline"] = "Senior Widget Engineer | Playwright"
    flagged = Tailored(bad_hd, master, Path("hd2.yaml")).unsupported_summary_terms()
    check("headline claiming an unevidenced tool is flagged",
          "playwright" in flagged, str(flagged))

    # A supported headline must stay silent — otherwise the check is noise and
    # gets ignored, which is how real drift slips through.
    ok_hd = dict(base_data)
    ok_hd["headline"] = "Senior Widget Engineer | Apache Airflow | SQL"
    quiet = Tailored(ok_hd, master, Path("hd3.yaml")).unsupported_summary_terms()
    check("headline matching the master is not flagged", not quiet, str(quiet))

    # ---- compound matching ------------------------------------------------
    # Hyphen decomposition used to match on ANY part, which produced false
    # POSITIVES — the report saying Ed was covered for something he is not:
    #   * "front-to-back" was EVIDENCED because the CV contains the word "to".
    #   * "non-functional" was EVIDENCED on "functional", crediting him with a
    #     discipline he has never worked in. A negated compound matching its own
    #     root is the worst case of all.
    # Driven through _term_in with a synthetic index so the assertions do not
    # depend on what today's fixture master happens to contain.
    print("\ncompound matching")
    tok = {"data", "driven", "functional", "sql", "aws", "python", "cloud"}
    hay = " data driven functional sql aws python cloud "
    check("compound matches when it appears as a unit",
          kw._term_in("data-driven", set(), tok, hay))
    check("compound does NOT match on one part only",
          not kw._term_in("non-functional", set(), tok, hay))
    check("compound does NOT match on a preposition part",
          not kw._term_in("front-to-back", set(), tok, hay))
    check("compound of only short words does NOT match",
          not kw._term_in("end-to-end", set(), tok, hay))
    # The first attempt at this fix applied the four-character floor to PLAIN
    # tokens and broke SQL, AWS, API and Git at once — the headline self-test
    # caught it. Short single tokens must take the untouched path.
    for short in ("sql", "aws"):
        check(f"short single token '{short}' still matches",
              kw._term_in(short, set(), tok, hay))

    # Requiring every part to appear SOMEWHERE was still not enough, because
    # unrelated words supply the parts by coincidence. "risk-based" was
    # EVIDENCED against a master containing "Risk Profiling" (risk) and
    # "Kubernetes-based tooling" (based) with the compound itself absent — which
    # credited Ed with risk-based test design, the exact gap the Acme Digital Play run had
    # already declined to claim. The parts must be ADJACENT.
    # Assertions run through _cv_index on realistic prose so the stemmed
    # haystack is built exactly as the pipeline builds it.
    def indexed(cand: str, text: str) -> bool:
        return kw._term_in(cand, set(), *kw._cv_index(text))

    check("compound does NOT match when its parts are scattered",
          not indexed("risk-based", "Risk Profiling and Kubernetes-based tooling"))
    check("...and the same compound DOES match when adjacent",
          indexed("risk-based", "Risk-based test design across releases"))
    # A descriptive adjective was the giveaway shape: the bug also reported
    # "london-based" and "cloud-based" as evidenced on a CV that merely contains
    # London, Confluent Cloud and AWS.
    check("location adjective is not evidenced by the bare location",
          not indexed("london-based", "London, United Kingdom"))
    check("cloud adjective is not evidenced by a product called Cloud",
          not indexed("cloud-based", "Confluent Cloud and AWS"))

    # Adjacency must be judged the same way on BOTH sides. A CV may write the
    # compound as two plain words (the master's own "stakeholder management"
    # tag), as one hyphenated token, or CamelCase. The first version of this fix
    # checked token-SET membership only and silently turned the master's
    # "stakeholder management" into a gap — recovered by the adjacency check.
    check("compound matches a CV that writes it as two words",
          indexed("stakeholder-management", "stakeholder management and communication"))
    check("compound matches a hyphenated CV token",
          indexed("microservice-based", "microservice-based event-driven platforms"))
    check("compound matches a collapsed CamelCase CV token",
          indexed("rest-assured", "RestAssured and WireMock"))
    check("compound with an internal stopword matches when written in full",
          indexed("state-of-the-art", "state-of-the-art tooling"))

    # ---- lexicon round-trip -----------------------------------------------
    # A tool name written with a SPACE instead of CamelCase was silently lost:
    # "Rest Assured" tokenised into 'rest' + 'assured', neither a lexicon key, so
    # the compound term never formed. That is Ed's own tool, named FIRST in the
    # API requirement of three real postings, scored as a miss and reported as a
    # gap called "assured" — which reads like a genuine gap and is actually the
    # engine failing to see a tool he holds. Spaced, hyphenated and CamelCase
    # forms must all resolve to ONE skill term.
    # ---- phrase ordering determinism ---------------------------------------
    # The bug this guards against, found 2026-09-22: the phrase list was built as
    # `sorted({...}, key=len, reverse=True)`. The input is a SET, whose iteration
    # order for strings depends on PYTHONHASHSEED (randomised per process), and
    # sorting by length alone leaves EQUAL-LENGTH phrases in that random order.
    # Because each match is masked out before the next is tried, order changes
    # which phrase wins — so four identical gap.py invocations produced two
    # different reports, and the repo's reproducibility check reported false
    # staleness.
    #
    # A test process has ONE fixed hash seed, so it cannot reproduce the
    # cross-process variation directly. Instead this asserts the property that
    # makes the order hash-independent: a total order, with a deterministic
    # tie-break. Tested on a synthetic equal-length input for exactly that reason.
    print("\nphrase ordering determinism")
    equal = {"bravo thing", "alpha thing", "gamma thing"}   # all 11 chars
    check("equal-length phrases resolve alphabetically",
          kw.order_phrases(equal) == ["alpha thing", "bravo thing", "gamma thing"],
          f"got {kw.order_phrases(equal)}")
    check("longer phrases still win over shorter ones",
          kw.order_phrases({"aa bb", "aa bb cc"}) == ["aa bb cc", "aa bb"])
    # The real regression guard: a different iteration order for the SAME set must
    # not change the output. Reversing the source order is the closest a single
    # process can get to the hash-seed variation that caused the bug.
    check("order is independent of the input's iteration order",
          kw.order_phrases(equal) == kw.order_phrases(set(list(equal)[::-1])))
    # And the consequence at the surface: the shipped phrase list must itself be
    # canonical, so no two equal-length entries are out of alphabetical order.
    live = kw.order_phrases({q for q in kw.SYNONYMS if " " in q})
    out_of_order = [
        (a, b) for a, b in zip(live, live[1:])
        if len(a) == len(b) and a > b
    ]
    check("the live lexicon phrase list is canonically ordered",
          not out_of_order, f"out of order: {out_of_order[:3]}")

    print("\nlexicon round-trip")
    round_trip = (
        ("Rest Assured", "restassured"),
        ("REST Assured", "restassured"),
        ("rest-assured", "restassured"),
        ("RestAssured", "restassured"),
        ("N8N", "n8n"),
        ("n8n", "n8n"),
        ("SoapUI", "soap ui"),
        ("SOAP UI", "soap ui"),
        ("Postman", "postman"),
        ("Playwright", "playwright"),
        ("Kotlin", "kotlin"),
        # Local LLM stack (2026-09-22): a dot and a hyphen both have to survive.
        ("OOP", "object-oriented programming"),
        ("oop", "object-oriented programming"),
        ("Object-Oriented Programming", "object-oriented programming"),
        ("Object Oriented", "object-oriented programming"),
        ("data validation", "data validation"),
        ("Confluence", "confluence"),
        ("Ollama", "ollama"),
        ("llama.cpp", "llama.cpp"),
        ("LLAMA.CPP", "llama.cpp"),
        ("llama cpp", "llama.cpp"),
        ("Qwen3-Coder", "qwen3-coder"),
        ("Qwen3 Coder", "qwen3-coder"),
        ("JSON", "json"),
        ("XML", "xml"),
        # DevOps was a stated must-have before it was a claimable skill; adding
        # it to the master exposed that it had no self-key, so lowercase
        # "devops" in a posting could sink to the background list.
        ("DevOps", "devops"),
        ("devops", "devops"),
        ("Dev Ops", "devops"),
        # The Harnham advert cluster named six tools that had no self-key and so
        # were invisible: selenide was misfiled as generic phrasing, and the rest
        # extracted as anonymous capitalised tokens that sank into the truncated
        # background list.
        ("Selenide", "selenide"),
        ("GitHub Actions", "github actions"),
        ("Elasticsearch", "elasticsearch"),
        ("Sentry", "sentry"),
        ("JMeter", "jmeter"),
        ("Gatling", "gatling"),
        ("Swagger", "swagger"),
        ("OpenAPI", "openapi"),
    )
    for surface, canonical in round_trip:
        terms, _ = kw.analyse_post(surface)
        got = {t for t, term in terms.items() if term.is_skill}
        check(f"'{surface}' resolves to skill '{canonical}'",
              canonical in got, f"got {sorted(got)}")

    # A tool the master DOES hold must never be reported as a gap. This is the
    # assertion that would have caught the Rest Assured bug directly. Scoped to
    # the term of interest: the fixture master deliberately lacks 'api testing',
    # and an unscoped assertion would fail on that unrelated term instead.
    fixture_facts = master.all_fact_text()
    held_terms, _ = kw.analyse_post("Experience with API testing using Rest Assured")
    _, held_missing = kw.coverage(held_terms, fixture_facts)
    missing_terms = {t.term for t in held_missing}
    # all_fact_text() is raw, so the containment check must be lowercased while
    # coverage() normalises internally — hence the two different comparisons.
    check("a spaced tool name the fixture master holds is not a gap",
          "restassured" in fixture_facts.lower() and "restassured" not in missing_terms,
          f"missing={sorted(missing_terms)}")

    # ---- heading classification -------------------------------------------
    # "Preferred Qualifications" is extremely common and is a trap: it contains
    # the requirement keyword "qualification" mid-phrase. It must land in
    # nice-to-have, not requirements.
    print("\nheading classification")
    cases = {
        "Requirements": "requirements",
        "Required Qualifications": "requirements",
        "Preferred Qualifications": "nice",
        "Desirable": "nice",
        "Key Responsibilities": "duties",
        "What you'll bring": "requirements",
        "Benefits": "other",
        "It would also be great if you have experience in some of the following": "nice",
        # LinkedIn adverts head their sections with bare ALL-CAPS phrases that
        # carry no keyword from any table. Unrecognised headings INHERIT the
        # preceding bucket, so "WHY US" after "YOU WILL" had its culture prose
        # weighted as duties, and "YOU HAVE"/"YOU WILL" left an entire posting
        # with no section weighting at all (the Adaptive run: zero priority
        # gaps on a posting that names Ed's whole vocabulary).
        "YOU ARE:": "requirements",
        "YOU HAVE:": "requirements",
        "YOU WILL:": "duties",
        "WHY US:": "other",
        "THE PROCESS": "other",
        "DIVERSITY AND INCLUSION:": "other",
        # "What you need to have" is the standard partner to "Nice to have".
        # Missing it meant an entire requirements list inherited the DUTY bucket
        # from the preceding "What you'll do" and was weighted at 1.5 not 3.0.
        "What you need to have": "requirements",
        "What you'll need to have": "requirements",
        "Must have": "requirements",
        # HYPHENATED headings. The probe stripped hyphens, so "Nice-to-have"
        # became the token "nicetohave", matched nothing, and returned None —
        # which means the heading INHERITED the previous bucket. On the Xe advert
        # every nice-to-have item was weighted as a requirement (3.0) instead of
        # a nice-to-have (0.5). "Must-have" failed identically and was rescued
        # only by inheriting `requirements` from the preceding heading.
        "Nice-to-have": "nice",
        "Must-have": "requirements",
        "Nice-to-haves": "nice",
        "Day-to-day": "duties",
        "What you'll do": "duties",
        "Nice to have": "nice",
        "What we offer": "other",
        # The Workday / LinkedIn ways-of-working headings. Both appeared on the
        # Elsevier advert (2026-09-22) and both failed, differently: "Working
        # Pattern" was recognised as a heading but had no bucket so it inherited
        # `requirements`; "Work in a Way That Works for You" is 7 words so it
        # failed the short-noun-phrase path entirely, was never recognised as a
        # heading, and stayed as CONTENT in `requirements` — where its title-cased
        # words became priority terms and manufactured a requirement gap called
        # "way" plus a bogus evidenced term "works".
        "Work in a Way That Works for You": "other",
        "Working Pattern": "other",
        "Working Patterns": "other",
        # "Why <employer>?" — matched as a PATTERN now, because the keyword list
        # can never hold every employer's name. Found 2026-09-23 on the Version 1
        # advert: "Why Version 1?" was unrecognised, so it inherited
        # `requirements` and 2,251 characters of pension, life-assurance and
        # profit-share prose were weighted as requirements — reporting
        # "wellbeing", "life", "balance" and "scheme" as requirement-level gaps
        # and tripling the denominator. This is the exact hazard the "why us"
        # comment above describes; the list simply could not anticipate the name.
        "Why Version 1?": "other",
        "Why join us": "other",
        "Why work for us": "other",
        "WHY US": "other",
    }
    for heading, expected in cases.items():
        probe = kw._heading_probe(heading)
        got = kw.heading_bucket(probe, at_start=True) or kw.heading_bucket(probe)
        check(f"'{heading[:38]}' -> {expected}", got == expected, f"got {got}")

    # The bare phrases above are matched by EXACT equality for a reason: as
    # prefix keys they also open ordinary requirement bullets, and a bullet eaten
    # as a heading silently re-buckets everything after it.
    print("\nbullet-vs-heading")
    for bullet in (
        "You have five years of experience in test automation",
        "You will work closely with the engineering team to deliver",
        "You are a self-directed engineer who can manage priorities",
        "Experience using Docker to aid testing",
    ):
        check(f"not a heading: '{bullet[:36]}...'", not kw.looks_like_heading(bullet))

    # Short capitalised BULLETS were being discarded as headings, which dropped
    # whole requirements out of the report: "Azure DevOps / TFS" vanished from the
    # Lead Test Engineer advert (a genuine gap), and "SQL", "RDBMS" and
    # "Cloud-native and microservices architectures" vanished from others (real
    # STRENGTHS — so the bug hid wins as well as gaps). The discriminator is
    # typographic: a heading is isolated by blank lines, a list bullet is not.
    print("\nisolation guard")
    for bullet in ("Azure DevOps / TFS", "SQL", "RDBMS",
                   "Cloud-native and microservices architectures"):
        check(f"unescorted bullet is not a heading: '{bullet[:34]}'",
              not kw.looks_like_heading(bullet, isolated=False))
    for heading in ("Requirements", "The Company", "Stakeholder Leadership",
                    "Tools & Methods", "YOU HAVE"):
        check(f"isolated heading is still a heading: '{heading[:34]}'",
              kw.looks_like_heading(heading, isolated=True))
    # The end-to-end guard: a bullet sandwiched between content lines must reach
    # the report rather than being swallowed by split_sections.
    multi = (
        "Requirements:\n"
        "CI/CD pipelines and automated quality gates\n"
        "Azure DevOps / TFS\n"
        "SQL\n"
        "\n"
        "The Role\n"
        "You will own the strategy.\n"
    )
    sect = kw.split_sections(multi)
    for needed in ("Azure DevOps / TFS", "SQL"):
        check(f"'{needed}' survives split_sections",
              needed in sect["requirements"], f"got {sect['requirements']!r}")

    # ---- head-noun headings ------------------------------------------------
    # A noun-phrase heading puts its HEAD NOUN last ("Key Responsibilities",
    # "Technical Skills"), so the at_start prefix test misses it — and when the
    # paste runs the first bullet straight on from the heading, there is no blank
    # line either, so the isolation path above is unavailable too. The heading was
    # then never recognised, was not dropped, and stayed a CONTENT line in the
    # previous bucket, silently mis-weighting its whole section. On the Anson
    # McCade advert that emptied `duties` entirely and demoted every term named
    # only in the responsibilities from priority to background.
    print("\nhead-noun headings")
    head_noun = {
        "Key Responsibilities": "duties",
        "Main Responsibilities": "duties",
        "Your Responsibilities": "duties",
        "Technical Skills": "requirements",
        "Key Requirements": "requirements",
    }
    for heading, expected in head_noun.items():
        probe = kw._heading_probe(heading)
        # Resolved the way split_sections resolves it — at_start first, then the
        # contains-anywhere fallback. Asserting `at_start` alone would be testing
        # a lookup that split_sections never relies on by itself.
        got = kw.heading_bucket(probe, at_start=True) or kw.heading_bucket(probe)
        check(f"head-noun heading '{heading}' -> {expected}", got == expected,
              f"got {got}")
        # Recognised with NO isolation signal at all — this is the whole point,
        # since these arrive glued to the first bullet.
        check(f"head-noun heading '{heading}' recognised unisolated",
              kw.looks_like_heading(heading, isolated=False))

    # The gates that keep the new path from eating requirement bullets. These run
    # with isolated=False deliberately: the new path ignores isolation, so the
    # capitalisation and keyword gates are the only things protecting these.
    # NOT in this list: "Experience of gathering requirements". It IS reported as
    # a heading, but it was reported that way before this change too — the older
    # at_start path matches REQUIREMENT_HEADINGS' 'experience of' prefix key. It
    # is a pre-existing quirk, it loses no content in any posting in the repo
    # (sentence-shaped bullets are rejected by the punctuation guard above), and
    # it is recorded in context/06-engine-bugs.md rather than fixed here.
    for bullet in ("Ownership of requirements",
                   "Azure DevOps / TFS",
                   "SQL",
                   "Cloud-native and microservices architectures",
                   "Key responsibilities are listed below"):
        check(f"still not a heading (unisolated): '{bullet[:34]}'",
              not kw.looks_like_heading(bullet, isolated=False))
    # The lower-case head noun is the documented limitation, asserted so the
    # behaviour is a decision on record rather than a surprise.
    check("lower-case head noun is not caught unisolated (known limitation)",
          not kw.looks_like_heading("Key responsibilities", isolated=False))

    # ---- long benefits headings must not leak into requirements -------------
    # The other half of the same Elsevier bug: a TITLE-CASED heading that is not
    # recognised becomes requirement CONTENT, and every capitalised word in it
    # then qualifies as a priority term. That is a priority-term generator, so
    # assert the end-to-end consequence rather than only the heading predicate:
    # no words from the heading may survive into the requirements bucket.
    print("\nbenefits headings do not leak")
    leaky = (
        "Requirements\n"
        "Strong knowledge of test strategy.\n"
        "\n"
        "Work in a Way That Works for You\n"
        "\n"
        "We promote a healthy work/life balance.\n"
        "\n"
        "Working Pattern\n"
        "\n"
        "Working flexible hours.\n"
    )
    leak_sect = kw.split_sections(leaky)
    for word in ("Way", "Works", "Working", "balance"):
        check(f"'{word}' from a benefits heading stays out of requirements",
              word not in leak_sect["requirements"],
              f"got {leak_sect['requirements']!r}")
    check("the real requirement survives",
          "test strategy" in leak_sect["requirements"])
    check("benefits content lands in other, not requirements",
          "healthy work/life balance" in leak_sect["other"])

    # End-to-end, and the consequence that actually matters: responsibilities
    # must land in `duties`, where is_priority can see them.
    glued = (
        "Requirements\n"
        "Strong commercial experience as a tester.\n"
        "\n"
        "Key Responsibilities\n"
        "Develop, maintain and execute automated test suites.\n"
        "Perform API and service-level testing.\n"
    )
    glued_sect = kw.split_sections(glued)
    check("glued responsibilities reach the duties bucket",
          "service-level testing" in glued_sect["duties"],
          f"got duties={glued_sect['duties']!r}")
    check("the heading is consumed, not left as content",
          "Key Responsibilities" not in glued_sect["duties"])

    # ---- omitted-vs-empty achievements ------------------------------------
    # Absent means "all achievements for the role"; an explicit empty list means
    # none. Conflating them silently blanked the full-inventory baseline.
    print("\nachievements default")
    t_all = Tailored({"summary": "x", "experience": [{"ref": "example-current"}]},
                     master, Path("all.yaml"))
    t_none = Tailored({"summary": "x", "experience": [{"ref": "example-current",
                                                      "achievements": []}]},
                      master, Path("none.yaml"))
    check("omitted achievements means ALL",
          len(t_all.resolved_experience()[0]["bullets"]) == 4,
          f"got {len(t_all.resolved_experience()[0]['bullets'])}")
    check("explicit empty list means NONE",
          len(t_none.resolved_experience()[0]["bullets"]) == 0)
    check("bullet-less roles are reported",
          t_none.roles_without_bullets() == ["example-current"],
          str(t_none.roles_without_bullets()))
    check("roles with bullets are not reported",
          t_all.roles_without_bullets() == [],
          str(t_all.roles_without_bullets()))

    # ---- rendering --------------------------------------------------------
    print("\nrendering")
    import render

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        docx_path = tmp_path / "cv.docx"
        pdf_path = tmp_path / "cv.pdf"

        render.render_docx(master, tailored, docx_path)
        from docx import Document

        doc = Document(str(docx_path))
        check("DOCX has no tables", len(doc.tables) == 0)
        check("DOCX has no images", len(doc.inline_shapes) == 0)
        sec = doc.sections[0]
        check("DOCX header empty", all(not p.text.strip() for p in sec.header.paragraphs))
        check("DOCX footer empty", all(not p.text.strip() for p in sec.footer.paragraphs))
        text = "\n".join(p.text for p in doc.paragraphs)
        check("DOCX contains the name", master.contact["name"] in text)
        check("DOCX contains section headings", "PROFESSIONAL EXPERIENCE" in text)
        # The DOCX exists to be EDITED by hand, so it needs a real outline:
        # Word's navigation pane reads Heading styles, and a flat document of
        # Normal paragraphs gives nothing to navigate or restyle against.
        docx_styles = {p.style.name for p in doc.paragraphs if p.text.strip()}
        check("DOCX sections use Heading 2", "Heading 2" in docx_styles,
              str(sorted(docx_styles)))
        check("DOCX roles use Heading 3", "Heading 3" in docx_styles,
              str(sorted(docx_styles)))
        check("DOCX bullets use a list style", "List Bullet" in docx_styles,
              str(sorted(docx_styles)))
        # Restyling for navigation must not cost the ATS-safety properties.
        check("DOCX still table-free after restyling", len(doc.tables) == 0)

        render.render_pdf(master, tailored, pdf_path)
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        check("PDF is one page", len(reader.pages) == 1, f"{len(reader.pages)} pages")
        pdf_text = reader.pages[0].extract_text()
        check("PDF name extracts", master.contact["name"] in pdf_text)
        check("PDF body text extracts", "PROFESSIONAL EXPERIENCE" in pdf_text)
        check("PDF bullet content extracts", "incremental caching" in pdf_text)

        md = render.render_markdown(master, tailored)
        check("Markdown renders headings", "## Professional Summary" in md)

        # The override has to survive all the way to the page, not just the
        # model — this is the line a screener reads first.
        hd_md = dict(tailored.data)
        hd_md["headline"] = "Senior Widget Engineer | Stream Processing"
        hd_md_t = Tailored(hd_md, master, Path("hd4.yaml"))
        check("overridden headline reaches the rendered output",
              "Senior Widget Engineer | Stream Processing"
              in render.render_markdown(master, hd_md_t))

    # ---- glyph guard ------------------------------------------------------
    # Characters the PDF font cannot draw are silently SUBSTITUTED, not refused:
    # UNICODE_FIXES turns an em-dash into "-", and anything outside latin-1
    # becomes "?" via the encode fallback. Fourteen of sixteen CVs shipped " - "
    # on the page where the file said " — ", and nobody knew until a human read
    # the PDF. The guard makes the substitution visible.
    print("\nglyph guard")
    hd = dict(tailored.data)
    hd["summary"] = (hd["summary"] or "x") + " an em\u2014dash and a curly \u2019quote"
    planted = Tailored(hd, master, Path("glyph.yaml"))
    flagged = dict(render.unrenderable_chars(master, planted))
    check("em-dash in prose is flagged", "\u2014" in flagged, str(flagged))
    check("curly apostrophe in prose is flagged", "\u2019" in flagged, str(flagged))
    check("a clean CV flags nothing",
          not render.unrenderable_chars(master, tailored),
          str(render.unrenderable_chars(master, tailored)))
    # A character with no substitution rule is the worst case: it becomes "?".
    hd2 = dict(tailored.data)
    hd2["summary"] = (hd2["summary"] or "x") + " a snowman \u2603"
    check("a character with no fix rule is flagged",
          "\u2603" in dict(render.unrenderable_chars(
              master, Tailored(hd2, master, Path("glyph2.yaml")))),
          str(render.unrenderable_chars(
              master, Tailored(hd2, master, Path("glyph2.yaml")))))

    # ---- cover letter parsing ---------------------------------------------
    # The separator is a horizontal rule on its OWN LINE. Splitting on the first
    # `---` substring was a real defect that reached a sent document: the
    # editor's note above the separator explains the convention, so it contains
    # "---" in prose ("Everything below the `---` is the letter body"), and the
    # split happened inside the note. The tail of the note plus a literal "---"
    # were printed into the Acme Digital Play cover letter.
    print("\ncover letter parsing")
    import coverletter as clmod

    letter_fixture = sample_dir / "cover-letter.md"
    meta, paras = clmod.parse(letter_fixture)
    check("a note above the separator is not printed",
          not any("Editor's note" in q or "never drift" in q for q in paras),
          str(paras[:2]))
    check("...not even the part of it that mentions the separator",
          not any("letter body" in q for q in paras),
          str(paras[:2]))
    check("the literal separator is not printed as a paragraph",
          not any(q.strip() == "---" for q in paras),
          str(paras))
    check("the body still starts where it should",
          paras[0].startswith("Dear Hiring Team"))
    # Assert the note is excluded from metadata by NAME, not by counting
    # asterisks -- an earlier version of this check counted `**` occurrences and
    # broke the moment the fixture gained or lost a key.
    meta_lines = meta.splitlines()
    check("only **Key:** lines become metadata",
          len(meta_lines) == 2
          and all(ln.startswith("**") and ln.endswith(("Co", ")")) for ln in meta_lines),
          meta)
    check("...and the note is not metadata",
          "Editor" not in meta and "drift" not in meta,
          meta)

    # A leaked note is the failure this parser exists to prevent, so it must
    # refuse to render rather than quietly send one. This is the check that
    # would have caught the Acme Digital Play defect even if the split logic regressed.
    with tempfile.TemporaryDirectory() as tmp:
        leaking = Path(tmp) / "leaking.md"
        leaking.write_text(
            "**To:** Someone\n\n---\n\nDear Hiring Team,\n\n"
            "> this is a note that leaked below the separator\n",
            encoding="utf-8",
        )
        try:
            clmod.parse(leaking)
            check("a leaked note in the body is refused", False,
                  "parse() accepted a body containing a blockquote")
        except SystemExit as exc:
            check("a leaked note in the body is refused",
                  "leaked" in str(exc), str(exc))
        # And a file with no separator line at all must be refused too.
        nosep = Path(tmp) / "nosep.md"
        nosep.write_text("Dear Hiring Team,\n\nNo separator here.\n", encoding="utf-8")
        try:
            clmod.parse(nosep)
            check("a letter with no separator line is refused", False,
                  "parse() accepted a letter with no separator")
        except SystemExit as exc:
            check("a letter with no separator line is refused",
                  "separator" in str(exc), str(exc))

    # ---- output layout ----------------------------------------------------
    # Applications are filed under the date they were PREPARED:
    #   output/21-09-26/<slug>/Ed-Minshull-CV.pdf
    # The date comes from the job's own job.yaml `date:` field, NOT from the
    # clock, so re-rendering an old CV cannot move it into today's folder and
    # scatter one application across two dates. That distinction is the whole
    # point of these checks: the obvious implementation (use date.today()) looks
    # right on the day it is written and is wrong from the next day onwards.
    print("\noutput layout")
    import render as rendermod

    job_path = Path("jobs") / "example-role" / "tailored.yaml"
    check("an ISO date becomes a day-first date directory",
          rendermod.output_dir_for(job_path, {"date": "2026-09-21"}).parent.name
          == "21-09-26",
          rendermod.output_dir_for(job_path, {"date": "2026-09-21"}).parent.name)
    check("...and the slug directory is kept inside it",
          rendermod.output_dir_for(job_path, {"date": "2026-09-21"}).name
          == "example-role")
    # A date that is already day-first must not be silently dumped into archive/.
    check("a day-first date is accepted too",
          rendermod.output_dir_for(job_path, {"date": "03-10-2026"}).parent.name
          == "03-10-26",
          rendermod.output_dir_for(job_path, {"date": "03-10-2026"}).parent.name)
    # Re-rendering the same job twice must resolve to the same directory,
    # whatever today happens to be.
    check("the same job always resolves to the same directory",
          rendermod.output_dir_for(job_path, {"date": "2026-09-17"})
          == rendermod.output_dir_for(job_path, {"date": "2026-09-17"})
          == rendermod.output_dir_for(job_path, {"date": "2026-09-17"}))
    # Cannot-date-it cases go to archive/ rather than being labelled.
    check("a job with no date cannot be dated",
          rendermod.output_dir_for(job_path, {}).parent.name == "archive",
          rendermod.output_dir_for(job_path, {}).parent.name)
    check("a job with a nonsense date cannot be dated",
          rendermod.output_dir_for(job_path, {"date": "sometime"}).parent.name
          == "archive",
          rendermod.output_dir_for(job_path, {"date": "sometime"}).parent.name)
    # A cover letter must be filed WITH the CV it belongs to. This is the kind
    # of thing that silently drifts when two scripts each build the path
    # themselves, so both go through output_dir_for and this asserts they agree.
    letter = Path("jobs") / "example-role" / "cover-letter.md"
    check("a cover letter lands in the same directory as its CV",
          rendermod.output_dir_for(letter, {"date": "2026-09-21"})
          == rendermod.output_dir_for(job_path, {"date": "2026-09-21"}),
          str(rendermod.output_dir_for(letter, {"date": "2026-09-21"})))
    # The un-tailored baseline is a reference document, not an application:
    # filing it under a date would imply it was sent to someone.
    baseline = Path("cv") / "full-inventory.tailored.yaml"
    check("the baseline is NOT dated and not archived",
          rendermod.output_dir_for(baseline, {}) == rendermod.OUTPUT_DIR / "full-inventory",
          str(rendermod.output_dir_for(baseline, {})))

    # ---- tailored-CV text includes the headline ---------------------------
    # The headline is the most-read line of the CV, but tailored_as_text() began
    # with the summary and skipped it -- so a term living only in an overridden
    # headline counted as ABSENT and was reported as "dropped in tailoring". The
    # Appvia run caught it: "Quality Engineering" is in that CV's headline and
    # the report still listed it as a drop.
    import re as re_early
    import gap as gapmod_early

    print("\ntailored text completeness")
    headline_probe = "DistinctiveHeadlineToken"
    hd_data = dict(data)
    hd_data["headline"] = headline_probe
    hd_tailored = Tailored(hd_data, master, Path("hd-headline.yaml"))
    hd_text = gapmod_early.tailored_as_text(master, hd_tailored)
    check("an overridden headline reaches the coverage text",
          headline_probe in hd_text, hd_text[:120])
    check("...and the summary is still included",
          (hd_tailored.summary[:30] in hd_text), hd_text[:120])

    # ---- the dropped table accounts for its own count ---------------------
    # The table used to show only PRIORITY drops while the headline count
    # reported every drop, so eleven reports said "dropped in tailoring: 15"
    # above a table with one row. They must agree, because the background drops
    # are the collateral damage this section exists to reveal.
    print("\ndropped-in-tailoring consistency")
    report = gapmod_early.build_report(
        (sample_dir / "post.md").read_text(encoding="utf-8"),
        master, hd_tailored, title="Sample", company="")
    # Anchored at line start and to the exact label, because gap.py now also
    # reports "Priority terms dropped in tailoring" — a different, narrower
    # count that appears EARLIER in the headline and would otherwise be the
    # first match, breaking a consistency check that is still perfectly valid.
    match = re_early.search(r"^- \*\*In your master but dropped in tailoring: (\d+)", report,
                            re_early.MULTILINE)
    if match:
        count = int(match.group(1))
        section = report.split("## Dropped in tailoring")[1].split("## ")[0]
        rows = [ln for ln in section.splitlines()
                if ln.startswith("| ") and not ln.startswith("| Term")
                and not ln.startswith("| ---") and "_...and" not in ln]
        check("the dropped-table row count matches the reported count",
              count == len(rows) or len(rows) >= 25,
              f"count={count} rows={len(rows)}")
        # Ordering asserted on synthetic terms, so this cannot pass vacuously.
        # A check that cannot fail is a check nobody reads.
        synthetic = [
            kw.Term(term="background-high", count=9),
            kw.Term(term="priority-one", is_skill=True, in_requirements=1),
            kw.Term(term="background-low", count=1),
        ]
        ordered = sorted(synthetic,
                         key=lambda x: (not x.is_priority, -x.weight, x.term))
        check("priority drops are listed before background ones",
              ordered[0].term == "priority-one",
              str([x.term for x in ordered]))
    else:
        check("the fixture drops nothing, so there is nothing to reconcile", True)

    # ---- coverage metric robustness ---------------------------------------
    # The same UBDS advert scored 43% all-term coverage anonymously and 26% with
    # the employer's benefits section attached, with an IDENTICAL priority-gap
    # list. The raw figure moves with advert length, not with fit. Priority-term
    # coverage exists to be comparable between postings, so this proves it does
    # not move when only marketing prose is added.
    print("\njob metadata noise")
    import gap as gapmod

    # The employer name is suppressed so an advert's own branding does not read as
    # a required technology.
    check("employer words are suppressed",
          {"northwind"} <= gapmod.noise_terms({"company": "Northwind"}))
    check("employer initials are suppressed",
          "nfp" in gapmod.noise_terms({"company": "Nucleus Financial Platforms"}))
    # The recruiter is suppressed too. Under the documented convention for an
    # advert with an unnamed employer -- `company: ""` plus `recruiter:` -- the
    # agency name is the only proper noun in the header, and with nothing
    # suppressing it "Anson McCade" was extracted as a posting term.
    check("recruiter words are suppressed when the employer is unnamed",
          {"anson", "mccade"} <= gapmod.noise_terms(
              {"company": "", "recruiter": "Anson McCade"}))
    # Initials come from the employer ALONE. Concatenating employer and agency
    # initials would invent an acronym that appears nowhere in the posting.
    rec_only = gapmod.noise_terms({"company": "", "recruiter": "Anson McCade"})
    check("agency-only metadata does not invent employer initials",
          "am" not in rec_only, f"got {sorted(rec_only)}")
    check("no metadata at all suppresses nothing",
          gapmod.noise_terms({}) == frozenset())

    print("\ncoverage metric")
    post = (sample_dir / "post.md").read_text(encoding="utf-8")
    padded = post + "\n\nBenefits\n\n" + (
        "Professionals choose to grow their careers with us for our reputation as "
        "a dynamic and forward-thinking organisation deeply committed to both "
        "innovation and employee development, offering ample training programmes, "
        "mentorship, and the chance to gain certifications. " * 8
    )
    r_plain = gapmod.build_report(post, master, tailored, title="Sample")
    r_pad = gapmod.build_report(padded, master, tailored, title="Sample")

    def priority_line(report: str) -> str:
        for line in report.splitlines():
            if line.startswith("- **Priority-term coverage:"):
                return line
        return ""

    check("report states priority-term coverage", bool(priority_line(r_plain)),
          priority_line(r_plain))
    check("prose padding does not move priority-term coverage",
          priority_line(r_plain) == priority_line(r_pad),
          f"{priority_line(r_plain)!r} vs {priority_line(r_pad)!r}")
    check("prose padding DOES move all-term coverage",
          "- **All-term coverage:" in r_plain and r_plain != r_pad,
          "the padded report should differ, or the test proves nothing")

    # ---- real master (structure only) -------------------------------------
    # Asserts nothing about content — the real CV changes independently of the
    # code. It only proves the file parses and its references are intact, so a
    # bad edit surfaces here rather than mid-application.
    print("\nreal master")
    real_path = ROOT / "cv" / "master.yaml"
    if real_path.exists():
        try:
            real = Master.load(real_path)
            check("cv/master.yaml parses", True)
            check("real master has experience",
                  len(real.experience) > 0,
                  f"{len(real.experience)} roles")
            check("real master is not placeholder-only",
                  not real.is_placeholder,
                  "meta.placeholder is still true")
            print(f"         ({len(real.roles_by_id)} roles, "
                  f"{len(real.achievements_by_id)} achievements, "
                  f"{len(real.skill_groups)} skill groups)")
        except CVError as exc:
            check("cv/master.yaml parses", False, str(exc))
    else:
        print("  skip  cv/master.yaml not present")

    # ---- summary ----------------------------------------------------------
    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
