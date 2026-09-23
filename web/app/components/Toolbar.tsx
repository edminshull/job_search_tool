import {MY_STATUS_LABEL, MY_STATUS_VALUES} from "@/app/constants";
import {DATE_WINDOWS} from "@/lib/dates";

type ToolbarProps = {
  aiStatusFilter: string;
  onAiStatusFilterChange: (value: string) => void;
  myStatusFilter: string;
  onMyStatusFilterChange: (value: string) => void;
  typeFilter: string;
  onTypeFilterChange: (value: string) => void;
  languageFilter: string;
  onLanguageFilterChange: (value: string) => void;
  dateWindow: string;
  onDateWindowChange: (value: string) => void;
  search: string;
  onSearchChange: (value: string) => void;
  count: number;
  total: number;
  hiddenByDate: number;
  onRefresh: () => void;
};

const Toolbar = ({
  aiStatusFilter, onAiStatusFilterChange,
  myStatusFilter, onMyStatusFilterChange,
  typeFilter, onTypeFilterChange,
  languageFilter, onLanguageFilterChange,
  dateWindow, onDateWindowChange,
  search, onSearchChange,
  count, total, hiddenByDate, onRefresh,
}: ToolbarProps) => {
  const windowLabel = DATE_WINDOWS.find((w) => w.value === dateWindow)?.label ?? dateWindow;
  return (
    <>
      <div className="toolbar">
        <label>
          Posted within
          <select value={dateWindow} onChange={(e) => onDateWindowChange(e.target.value)}>
            {DATE_WINDOWS.map((w) => (
              <option key={w.value} value={w.value}>{w.label}</option>
            ))}
          </select>
        </label>
        <label>
          Type
          <select value={typeFilter} onChange={(e) => onTypeFilterChange(e.target.value)}>
            <option value="all">All</option>
            <option value="permanent">Permanent</option>
            <option value="contract">Contract</option>
          </select>
        </label>
        <label>
          Language
          <select value={languageFilter} onChange={(e) => onLanguageFilterChange(e.target.value)}>
            <option value="all">All</option>
            <option value="priority">Java/Ruby only</option>
            <option value="priority+">Java/Ruby &amp; JS/Node</option>
            <option value="known">Any language named</option>
          </select>
        </label>
        <label>
          AI status
          <select value={aiStatusFilter} onChange={(e) => onAiStatusFilterChange(e.target.value)}>
            <option value="all">All</option>
            <option value="apply">Apply</option>
            <option value="consider">Consider</option>
            <option value="skip">Skip</option>
            <option value="not_evaluated">Not evaluated</option>
          </select>
        </label>
        <label>
          My status
          <select value={myStatusFilter} onChange={(e) => onMyStatusFilterChange(e.target.value)}>
            <option value="all">All</option>
            <option value="none">No status</option>
            {MY_STATUS_VALUES.map((v) => (
              <option key={v} value={v}>{MY_STATUS_LABEL[v]}</option>
            ))}
          </select>
        </label>
        <label>
          Search
          <input
            type="search"
            value={search}
            onChange={(e) => onSearchChange(e.target.value)}
            placeholder="company or title..."
          />
        </label>
        <div className="spacer" />
        <span className="count-badge">{count} / {total}</span>
        <button className="btn secondary" onClick={onRefresh}>↻ Refresh</button>
      </div>
      {/* The window is a default, not a limit — so when it IS hiding rows,
          say so explicitly and offer the one-click way out. Silently
          dropping postings would be indistinguishable from having no
          postings. */}
      {hiddenByDate > 0 && (
        <div className="window-note">
          Hiding <strong>{hiddenByDate}</strong> posting{hiddenByDate === 1 ? "" : "s"} older than{" "}
          {windowLabel.toLowerCase()}.
          <button className="link-btn" onClick={() => onDateWindowChange("all")}>Show all dates</button>
        </div>
      )}
    </>
  );
};

export default Toolbar;
