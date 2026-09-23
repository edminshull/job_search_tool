"use client";

import {useEffect, useMemo, useState} from "react";
import {DatedJob, Job, SaveState, SortKey, StoredTailoring} from "@/app/types";
import Toolbar from "@/app/components/Toolbar";
import JobsTable from "@/app/components/JobsTable";
import JobDetailPanel from "@/app/components/JobDetailPanel";
import TailoringPreview from "@/app/components/TailoringPreview";
import {DATE_WINDOWS, DEFAULT_DATE_WINDOW, postedAge, windowDays, withinWindow} from "@/lib/dates";

function sortValue(j: DatedJob, key: SortKey): number | string | null {
  switch (key) {
    // Sorted on the PARSED timestamp, not the raw string: posted_at can be
    // an ISO date, epoch millis, or the words "2 weeks ago" depending on
    // which source produced it, and comparing those as text is meaningless.
    case "posted_at": return j.age.date ? j.age.date.getTime() : null;
    case "match_score": return j.match_score;
    case "day_rate_max": return j.day_rate_max;
    case "language_rank": return j.language_rank;
    default: return (j as unknown as Record<string, string | null>)[key] ?? null;
  }
}

function compare(a: DatedJob, b: DatedJob, key: SortKey, dir: "asc" | "desc"): number {
  const av = sortValue(a, key);
  const bv = sortValue(b, key);
  // Unknowns always sink to the bottom, whichever direction is active —
  // "no rate stated" is not a low rate, and an undated posting is not an
  // old one.
  if (av === null && bv === null) return 0;
  if (av === null) return 1;
  if (bv === null) return -1;
  if (typeof av === "number" && typeof bv === "number") {
    return dir === "asc" ? av - bv : bv - av;
  }
  const as = String(av).toLowerCase();
  const bs = String(bv).toLowerCase();
  if (as === bs) return 0;
  const less = as < bs;
  return (dir === "asc") === less ? -1 : 1;
}

export default function Page() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [aiStatusFilter, setAiStatusFilter] = useState("all");
  const [myStatusFilter, setMyStatusFilter] = useState("all");
  const [typeFilter, setTypeFilter] = useState("all");
  const [languageFilter, setLanguageFilter] = useState("all");
  const [dateWindow, setDateWindow] = useState(DEFAULT_DATE_WINDOW);
  const [search, setSearch] = useState("");

  const [sortKey, setSortKey] = useState<SortKey>("match_score");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const [selectedUrl, setSelectedUrl] = useState<string | null>(null);
  const [draftStatus, setDraftStatus] = useState("");
  const [draftNotes, setDraftNotes] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("idle");

  // The tailoring overlay. Kept at page level, keyed by the job's URL rather
  // than by the panel, because a drafted CV belongs to the posting: closing the
  // detail panel must not throw away a 90-second draft, and reopening the job
  // should show the same draft back.
  const [tailorUrl, setTailorUrl] = useState<string | null>(null);
  const [storedTailoring, setStoredTailoring] = useState<StoredTailoring | null>(null);
  const [tailoringLoaded, setTailoringLoaded] = useState(false);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/jobs");
      if (!res.ok) throw new Error((await res.json()).error || res.statusText);
      setJobs(await res.json());
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  // Fetch the stored tailoring record when the overlay opens. The board's own
  // payload carries only the summary columns (status, paths, coverage) because
  // the YAML and the verifier's findings are several KB per job — reading them
  // for 300+ rows to serve one open panel would be waste.
  useEffect(() => {
    if (!tailorUrl) return;
    let cancelled = false;
    setTailoringLoaded(false);
    (async () => {
      try {
        const res = await fetch(`/api/jobs/tailoring?url=${encodeURIComponent(tailorUrl)}`);
        const data = await res.json();
        if (!cancelled) setStoredTailoring(res.ok ? data.tailoring : null);
      } catch {
        if (!cancelled) setStoredTailoring(null);
      } finally {
        if (!cancelled) setTailoringLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, [tailorUrl]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      // Match the default direction to what the column is for: score and
      // posting date are both "highest/newest first" columns, and starting
      // either of them ascending would show the worst match or the oldest
      // advert at the top, which is never what was meant.
      setSortDir(key === "match_score" || key === "posted_at" || key === "day_rate_max"
        || key === "language_rank" ? "desc" : "asc");
    }
  }

  // Parse the date once per job per refresh rather than on every render —
  // toLocaleDateString is not free and the table re-renders on every
  // keystroke in the search box.
  const dated: DatedJob[] = useMemo(
    () => jobs.map((j) => ({...j, age: postedAge(j.posted_at)})),
    [jobs]
  );

  const {rows, hiddenByDate} = useMemo(() => {
    const passesOthers = (j: DatedJob) => {
      if (aiStatusFilter !== "all" && j.status !== aiStatusFilter) return false;
      if (myStatusFilter !== "all") {
        if (myStatusFilter === "none" ? j.my_status : j.my_status !== myStatusFilter) return false;
      }
      if (typeFilter !== "all") {
        // ATS boards rarely declare an employment type, so filtering to
        // "Permanent" would hide most of the board. Filtering to CONTRACT
        // is the useful direction and is exact.
        if (typeFilter === "contract" ? j.employment_type !== "contract" : j.employment_type === "contract") return false;
      }
      if (languageFilter !== "all") {
        // `none` (no language named) is only included by the "known" option —
        // it is not evidence of a match, so it must not be swept into a
        // Java/Ruby filter.
        if (languageFilter === "priority" && j.language_tier !== "priority") return false;
        if (languageFilter === "priority+"
            && j.language_tier !== "priority" && j.language_tier !== "secondary") return false;
        if (languageFilter === "known"
            && (j.language_tier === "none" || j.language_tier === null)) return false;
      }
      if (search) {
        const hay = `${j.company} ${j.title}`.toLowerCase();
        if (!hay.includes(search.toLowerCase())) return false;
      }
      return true;
    };

    const others = dated.filter(passesOthers);
    // A posting the date window would hide is kept anyway if it is STILL LISTED
    // — seen in a board fetch inside the same window. posted_at is the
    // employer's claim about when the advert went up, and it can be years stale
    // on a role that is genuinely open: Trading 212's London QA role is
    // advertised today with a 2023-06-28 date, and a 30-day default was hiding
    // it, so its tailored CV pointed at a job the board would not show.
    //
    // Board-sourced rows carry last_listed_at; aggregator rows do not, and for
    // those the posted_at window is all there is — which is correct, since
    // Adzuna's date is the only evidence available for them.
    const listedWithinWindow = (j: DatedJob): boolean => {
      const seen = postedAge(j.last_listed_at);
      if (seen.days === null) return false;
      const days = windowDays(dateWindow);
      return days === null || seen.days <= days;
    };
    const visible = others.filter((j) => withinWindow(j.age, windowDays(dateWindow)) || listedWithinWindow(j));
    visible.sort((a, b) => compare(a, b, sortKey, sortDir));
    return {rows: visible, hiddenByDate: others.length - visible.length};
  }, [dated, aiStatusFilter, myStatusFilter, typeFilter, languageFilter, search, dateWindow, sortKey, sortDir]);

  // Derived from `jobs` by url (rather than a frozen snapshot object) so
  // the open panel reflects the latest score/description/status after a
  // background refresh instead of showing stale AI analysis.
  const selected = useMemo(
    () => dated.find((j) => j.url === selectedUrl) ?? null,
    [dated, selectedUrl]
  );

  function openPanel(j: DatedJob) {
    setSelectedUrl(j.url);
    setDraftStatus(j.my_status ?? "");
    setDraftNotes(j.notes ?? "");
    setSaveState("idle");
  }

  async function saveStatus(url: string, myStatus: string, notes: string, isPanelSave: boolean) {
    if (isPanelSave) setSaveState("saving");
    try {
      const res = await fetch("/api/status", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({url, my_status: myStatus || null, notes: notes || null}),
      });
      if (!res.ok) throw new Error((await res.json()).error || res.statusText);
      setJobs((prev) =>
        prev.map((j) => (j.url === url ? {...j, my_status: myStatus || null, notes: notes || null} : j))
      );
      if (isPanelSave) setSaveState("saved");
    } catch (e) {
      if (isPanelSave) setSaveState("error");
      else alert("Failed to save status: " + (e as Error).message);
    }
  }

  const activeWindow = DATE_WINDOWS.find((w) => w.value === dateWindow);

  return (
    <>
      <header>
        <h1>Job Search Board</h1>
        <div className="subtitle">
          Reads and writes data/seen_jobs.sqlite3 directly — no export/import step.
          {activeWindow?.days != null && (
            <> Showing the <strong>{activeWindow.label.toLowerCase()}</strong> by default; widen it in the toolbar.</>
          )}
        </div>
      </header>

      <Toolbar
        aiStatusFilter={aiStatusFilter}
        onAiStatusFilterChange={setAiStatusFilter}
        myStatusFilter={myStatusFilter}
        onMyStatusFilterChange={setMyStatusFilter}
        typeFilter={typeFilter}
        onTypeFilterChange={setTypeFilter}
        languageFilter={languageFilter}
        onLanguageFilterChange={setLanguageFilter}
        dateWindow={dateWindow}
        onDateWindowChange={setDateWindow}
        search={search}
        onSearchChange={setSearch}
        count={rows.length}
        total={jobs.length}
        hiddenByDate={hiddenByDate}
        onRefresh={load}
      />

      <main>
        {loading && <div className="loading-state">Loading...</div>}
        {error && (
          <div className="error-state">
            Can&apos;t read db: {error}
            <br />
            Check that `make run` was run at least once.
          </div>
        )}
        {!loading && !error && (
          rows.length === 0 ? (
            <div className="empty-state">
              Nothing matches the current filter/search.
              {hiddenByDate > 0 && (
                <div style={{marginTop: 8}}>
                  {hiddenByDate} posting{hiddenByDate === 1 ? "" : "s"} hidden by the date window —{" "}
                  <button className="link-btn" onClick={() => setDateWindow("all")}>show all dates</button>
                </div>
              )}
            </div>
          ) : (
            <JobsTable
              rows={rows}
              sortKey={sortKey}
              sortDir={sortDir}
              onToggleSort={toggleSort}
              onSelectJob={openPanel}
              onQuickStatusChange={(job, status) => saveStatus(job.url, status, job.notes ?? "", false)}
            />
          )
        )}
      </main>

      {selected && (
        <JobDetailPanel
          job={selected}
          draftStatus={draftStatus}
          onDraftStatusChange={setDraftStatus}
          draftNotes={draftNotes}
          onDraftNotesChange={setDraftNotes}
          saveState={saveState}
          onSave={() => saveStatus(selected.url, draftStatus, draftNotes, true)}
          onClose={() => setSelectedUrl(null)}
          onTailorCv={() => setTailorUrl(selected.url)}
        />
      )}

      {tailorUrl && (() => {
        // Read the job from `dated` rather than from a snapshot taken when the
        // overlay opened, so a board refresh mid-draft still shows current data.
        const job = dated.find((j) => j.url === tailorUrl);
        if (!job) return null;
        // Wait for the stored record: mounting the overlay before it arrives
        // would show "never tailored" for a job that already has a CV, and the
        // first thing the user would see is the wrong state.
        if (!tailoringLoaded) {
          return (
            <div id="overlay" className="tailor-overlay"
                 onClick={(e) => { if (e.target === e.currentTarget) setTailorUrl(null); }}>
              <div id="panel" className="tailor-panel">
                <div id="panel-body">
                  <div className="loading-state">Loading the stored CV…</div>
                </div>
              </div>
            </div>
          );
        }
        return (
          <TailoringPreview
            job={job}
            stored={storedTailoring}
            onClose={() => setTailorUrl(null)}
            onRendered={load}
          />
        );
      })()}
    </>
  );
}
