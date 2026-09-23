"""Sanity test for discover_companies.py — runs against a SYNTHETIC registry,
never the real file and never a copy of it.

This used to copy companies.yaml and assert on entries inside it
("workable/treewalk", a comment attached to "coveodeven"). That made the test
break for the wrong reason whenever the registry was curated — which is exactly
what happened on 2026-09-23, when 41 dead and duplicated entries were removed and
the test failed with "('workable', 'treewalk') in known" while the code under
test was working perfectly. A test whose fixtures are the production data is
testing the data, not the mechanism.

So the fixture below is deliberately tiny and self-contained. What is being
verified is the BEHAVIOUR: known companies are never re-suggested, duplicates
within one run collapse, appending preserves the existing file's comments and
formatting, the appended block is valid YAML that round-trips, and re-running is
idempotent. None of that depends on which companies are registered today.

Run with: python -m app.tests.test_discover_companies
"""
import os

import yaml

from app import discover_companies

TEST_YAML = "data/test_companies.yaml"

# A minimal registry that exercises every branch: one known greenhouse entry
# (with a quoted name and a comment, since the real file has both), one known
# entry on another ATS, and legal structure the writer must preserve.
FIXTURE = '''\
# Company registry for the job discovery pipeline.
# ats: one of "greenhouse" | "ashby" | "workable" | "lever" | "smartrecruiters"
#
# A comment attached to an entry, of the kind this file is full of.
companies:
  - name: Affirm
    ats: greenhouse
    slug: affirm   # verified against the live board
  - name: "Some Workable Co"
    ats: workable
    slug: someworkable
  - name: "A Lever Co"
    ats: lever
    slug: aleverco
'''


def main():
    os.makedirs("data", exist_ok=True)
    with open(TEST_YAML, "w") as f:
        f.write(FIXTURE)

    with open(TEST_YAML) as f:
        original_text = f.read()
    original_companies = yaml.safe_load(original_text)["companies"]
    assert len(original_companies) == 3

    # --- every registered (ats, slug) is recognised, so it is never re-suggested
    known = discover_companies.load_known_keys(TEST_YAML)
    assert ("greenhouse", "affirm") in known
    assert ("workable", "someworkable") in known
    assert ("lever", "aleverco") in known
    print(f"load_known_keys OK: {len(known)} pairs loaded")

    discovered = [
        {"name": "Affirm Inc.", "ats": "greenhouse", "slug": "affirm"},  # known -> skip
        {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"},  # new
        {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"},  # dup in-run -> collapse
        {"name": "Some New Co", "ats": "lever", "slug": "somenewco"},  # new, different ATS
    ]
    added = discover_companies.append_new_companies(discovered, path=TEST_YAML)

    assert len(added) == 2, f"expected 2 new companies appended, got {len(added)}: {added}"
    assert {"name": "Warner Music Group", "ats": "greenhouse", "slug": "warnermusicgroup"} in added
    assert {"name": "Some New Co", "ats": "lever", "slug": "somenewco"} in added
    print("append_new_companies OK: correctly deduped known + within-call dupes, added 2")

    # --- existing content (comments included) must be untouched, only appended to
    with open(TEST_YAML) as f:
        new_text = f.read()
    assert new_text.startswith(original_text), "existing file content was modified, not just appended to"
    assert "# Company registry for the job discovery pipeline." in new_text  # header survived
    assert "# verified against the live board" in new_text, "a per-entry comment was lost"
    print("File comments/formatting preserved OK")

    # --- the whole file must still be valid, loadable YAML
    with open(TEST_YAML) as f:
        reloaded = yaml.safe_load(f)
    assert len(reloaded["companies"]) == len(original_companies) + 2
    names = {c["name"] for c in reloaded["companies"]}
    assert "Warner Music Group" in names and "Some New Co" in names
    print(f"Round-trip YAML valid OK: {len(reloaded['companies'])} companies total")

    # --- re-running with the same discoveries adds nothing (idempotent)
    added_again = discover_companies.append_new_companies(discovered, path=TEST_YAML)
    assert added_again == [], f"expected no new companies on second run, got {added_again}"
    print("Idempotency OK: re-running with the same discoveries adds nothing")

    # --- an entry already present under a DIFFERENT ATS is still new, because it
    # is a different board: the same company can run two ATSes and both are worth
    # fetching (Wayve does exactly this).
    cross_ats = [{"name": "Affirm", "ats": "ashby", "slug": "affirm"}]
    added_cross = discover_companies.append_new_companies(cross_ats, path=TEST_YAML)
    assert len(added_cross) == 1, "a second ATS for a known company is a new board, not a duplicate"
    print("Cross-ATS OK: the same company on another ATS is treated as a new board")

    os.remove(TEST_YAML)
    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
