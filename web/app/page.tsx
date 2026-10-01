"use client";

import {useEffect, useMemo, useState} from "react";
import {DatedJob, Job, SaveState, ScanPayload, SortKey} from "@/app/types";
import Toolbar from "@/app/components/Toolbar";
import JobsTable from "@/app/components/JobsTable";
import JobDetailPanel from "@/app/components/JobDetailPanel";
import CvScanPanel from "@/app/components/CvScanPanel";
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

  // The CV-scan overlay. Kept at page level, keyed by the job's URL rather than
  // by the panel, for the same reason the tailoring overlay was: a scan belongs
  // to the posting, so closing the detail panel must not throw it away and
  // reopening the job should show the same result back.
  const [scanUrl, setScanUrl] = useState<string | null>(null);
  const [storedScan, setStoredScan] = useState<ScanPayload | null>(null);
  const [scanLoaded, setScanLoaded] = useState(false);
  /** The url a "scan the fuller copy" click asked for that is no longer on the
   *  board. Kept so the panel can say why nothing happened, instead of a click
   *  that silently does nothing. */
  const [switchError, setSwitchError] = useState<string | null>(null);

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

  // Fetch the stored scan when the overlay opens. The board's payload carries
  // only the summary columns (verdict, coverage, counts) because the full result
  // — gap lists, suggestions, the evidence behind the verdict — is several KB
  // per job, and reading that for 300+ rows to serve one open panel would be
  // waste. Opening the overlay must not RUN anything either: a scan is only ever
  // run when the user asks for one, so reopening a job is instant and a page
  // load cannot fire a Google Docs fetch per row.
  useEffect(() => {
    if (!scanUrl) return;
    let cancelled = false;
    setSwitchError(null);
    setScanLoaded(false);
    (async () => {
      try {
        const res = await fetch(`/api/jobs/scan?url=${encodeURIComponent(scanUrl)}`);
        const data = await res.json();
        if (!cancelled) setStoredScan(res.ok ? data.scan : null);
      } catch {
        if (!cancelled) setStoredScan(null);
      } finally {
        if (!cancelled) setScanLoaded(true);
      }
    })();
    return () => { cancelled = true; };
  }, [scanUrl]);

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
          {activeWindow?.days == null ? (
            <> Showing <strong>every posting</strong>; narrow it with &quot;Posted within&quot; in the toolbar.</>
          ) : (
            <> Showing the <strong>{activeWindow.label.toLowerCase()}</strong>; widen it in the toolbar.</>
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
          onScanCv={() => setScanUrl(selected.url)}
          // Resolved against the loaded board rows, so a notice never offers a
          // button to a row that is not here: the original advert can have been
          // filtered out, and the status can have been inherited from a third
          // advert of the same job (see app/duplicates.py).
          duplicateOriginal={dated.find((j) => j.url === selected.duplicate_of) ?? null}
          inheritedSource={dated.find((j) => j.url === selected.status_inherited_from) ?? null}
          onOpenJob={(job) => setSelectedUrl(job.url)}
        />
      )}

      {scanUrl && (() => {
        // Read the job from `dated` rather than from a snapshot taken when the
        // overlay opened, so a board refresh mid-scan still shows current data.
        const job = dated.find((j) => j.url === scanUrl);
        if (!job) return null;
        // Wait for the stored record: mounting before it arrives would show
        // "never scanned" for a job that has a scan, and the first thing the
        // user would see is the wrong state.
        if (!scanLoaded) {
          return (
            <div id="overlay" className="tailor-overlay"
                 onClick={(e) => { if (e.target === e.currentTarget) setScanUrl(null); }}>
              <div id="panel" className="tailor-panel">
                <div id="panel-body">
                  <div className="loading-state">Loading the stored scan…</div>
                </div>
              </div>
            </div>
          );
        }
        return (
          <CvScanPanel
            job={job}
            stored={storedScan}
            switchError={switchError}
            onClose={() => { setScanUrl(null); setSwitchError(null); }}
            onScanned={() => {
              load();
              // Refresh the stored row in place, so the footer's "last updated"
              // agrees with what was just written. Deliberately WITHOUT
              // dropping `scanLoaded` first: that would unmount this panel for
              // the length of the request and flash the whole overlay back to
              // "Loading the stored scan…" immediately after a scan the user
              // just watched finish. `stored` feeds the footer only — the
              // result on screen comes from the panel's own state.
              fetch(`/api/jobs/scan?url=${encodeURIComponent(job.url)}`)
                .then((r) => (r.ok ? r.json() : null))
                .then((d) => setStoredScan(d?.scan ?? null))
                .catch(() => {});
            }}
            onSwitchJob={(url) => {
              // The target comes from a `better_source` link, which the pipeline
              // only ever records for a row that is on the board — but a STORED
              // scan keeps naming its sibling long after that sibling has been
              // filtered out or removed, and the url is then one `dated` cannot
              // resolve. Setting `scanUrl` to it would render `null` above,
              // leaving the overlay invisible while the url stayed set — so the
              // button that opened it would look dead on the next click.
              if (dated.some((j) => j.url === url)) setScanUrl(url);
              else setSwitchError(url);
            }}
          />
        );
      })()}
    </>
  );
}
