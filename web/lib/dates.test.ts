// Tests for the posted_at parser. Run with: npm test
// (node --test uses Node's built-in runner; Node >= 23 strips the types
// natively, so no transpile step or test framework is needed.)
//
// Every case here comes from a real value seen in data/seen_jobs.sqlite3 or
// from app/add_job.py's accepted input — the point of the parser is that
// these four different shapes coexist in ONE column.
import {test} from "node:test";
import assert from "node:assert/strict";

import {DATE_WINDOWS, DEFAULT_DATE_WINDOW, parsePostedAt, postedAge, windowDays, withinWindow} from "./dates.ts";

const NOW = new Date("2026-09-21T12:00:00");

test("parses every posted_at shape the sources actually produce", () => {
  // Adzuna
  assert.equal(parsePostedAt("2026-09-16T04:04:36Z")?.toISOString(), "2026-09-16T04:04:36.000Z");
  // Greenhouse / Lever
  assert.equal(parsePostedAt("2026-08-01T00:00:00.000Z")?.toISOString(), "2026-08-01T00:00:00.000Z");
  // SQLite CURRENT_TIMESTAMP form — no zone, so parsed as local time
  assert.notEqual(parsePostedAt("2026-09-18 16:43:22"), null);
  // SmartRecruiters: epoch milliseconds, as a number AND as the string
  // SQLite may hand back for a column holding one
  assert.equal(parsePostedAt(1758000000000)?.toISOString(), "2025-09-16T05:20:00.000Z");
  assert.equal(parsePostedAt("1758000000000")?.toISOString(), "2025-09-16T05:20:00.000Z");
  // Epoch seconds, in case a source uses 10 digits
  assert.equal(parsePostedAt(1758000000)?.toISOString(), "2025-09-16T05:20:00.000Z");
});

test("returns null rather than an Invalid Date for unparseable input", () => {
  // Invalid Date is the dangerous outcome: every comparison against it is
  // false, so a job with a bad date would silently vanish from a recency
  // filter with no error anywhere.
  for (const bad of ["", "   ", "Not stated", "Competitive", "ASAP", null, undefined, "TBC"]) {
    assert.equal(parsePostedAt(bad), null, `expected null for ${JSON.stringify(bad)}`);
  }
});

test("parses the relative phrases app/add_job.py accepts from a pasted advert", () => {
  assert.equal(postedAge("2 weeks ago", NOW).days, 14);
  assert.equal(postedAge("3 days ago", NOW).days, 3);
  assert.equal(postedAge("yesterday", NOW).days, 1);
  assert.equal(postedAge("today", NOW).days, 0);
  assert.equal(postedAge("a week ago", NOW).days, 7);
  assert.equal(postedAge("1 day ago", NOW).days, 1);

  // MONTHS ARE CALENDAR MONTHS, so the day count varies with the month it lands
  // in — 21 Sep back two months is 21 Jul, which is 62 days, not 60. This
  // assertion used to say 60, from a 30-days-per-month approximation that was
  // also applied as a millisecond subtraction, which additionally lost a day
  // whenever the clock was not exactly midnight. The label is what the user
  // reads and it is right either way; the number is what the date window
  // filters on, so it has to be the real gap.
  assert.equal(postedAge("about 2 months ago", NOW).days, 62);
  assert.equal(postedAge("about 2 months ago", NOW).label, "2 months ago");
  // A month of 31 days cannot round-trip as exactly 31 days, and must not be
  // claimed to: 21 Sep back one month is 21 Aug = 31 days.
  assert.equal(postedAge("1 month ago", NOW).days, 31);
});

test("labels are human, and exact text survives for the tooltip", () => {
  assert.equal(postedAge("2026-09-20T10:00:00", NOW).label, "yesterday");
  assert.equal(postedAge("2026-09-18T10:00:00", NOW).label, "3 days ago");
  assert.equal(postedAge("2026-09-14T10:00:00", NOW).label, "1 week ago");
  assert.equal(postedAge("2026-09-01T10:00:00", NOW).label, "3 weeks ago");
  assert.equal(postedAge("2026-06-01T10:00:00", NOW).label, "4 months ago");
  assert.match(postedAge("2026-06-01T10:00:00", NOW).exact, /1 Jun 2026 \(112 days ago\)/);
  assert.equal(postedAge("Not stated", NOW).label, "no date");
});

test("the 30-day default window keeps a month and drops older", () => {
  const days = DATE_WINDOWS.find((w) => w.value === DEFAULT_DATE_WINDOW)?.days;
  assert.equal(days, 30, "the default window is meant to be one month");

  assert.equal(withinWindow(postedAge("5 days ago", NOW), days), true);
  assert.equal(withinWindow(postedAge("29 days ago", NOW), days), true);
  assert.equal(withinWindow(postedAge("31 days ago", NOW), days), false);
  assert.equal(withinWindow(postedAge("8 months ago", NOW), days), false);
});

test("an undated posting is never hidden by the window", () => {
  // Deliberate: an advert that states no date cannot be proven stale, and
  // silently dropping it is indistinguishable from the job not existing.
  // It is shown with a "no date" marker instead.
  assert.equal(withinWindow(postedAge("Not stated", NOW), 30), true);
  assert.equal(withinWindow(postedAge(null, NOW), 7), true);
  // ...but "all dates" keeps everything too, including the genuinely old
  assert.equal(withinWindow(postedAge("3 years ago", NOW), null), true);
});

test("every offered window is overridable and ordered narrowest first", () => {
  assert.deepEqual(DATE_WINDOWS.map((w) => w.value), ["7", "30", "90", "180", "all"]);
  assert.equal(DATE_WINDOWS[DATE_WINDOWS.length - 1].days, null);
  assert.ok(DATE_WINDOWS.some((w) => w.value === DEFAULT_DATE_WINDOW));
});

test("windowDays maps every option, and 'all' really means all", () => {
  assert.equal(windowDays("7"), 7);
  assert.equal(windowDays("30"), 30);
  assert.equal(windowDays("90"), 90);
  assert.equal(windowDays("180"), 180);
  // The regression this function exists for: "all" has days === null, and
  // the natural inline `?? 30` would quietly turn "show everything" back
  // into a 30-day filter — a bug with no error and no visible symptom,
  // because a filtered board still looks exactly like a board.
  assert.equal(windowDays("all"), null);
  assert.equal(withinWindow(postedAge("3 years ago"), windowDays("all")), true);
  // An unrecognised value falls back to the default rather than showing
  // everything or nothing.
  assert.equal(windowDays("nonsense"), 30);
});
