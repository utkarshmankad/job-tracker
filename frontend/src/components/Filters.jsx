import { useState, useEffect, useRef, useId } from "react";
import { Search, X } from "lucide-react";
import { STATUS_OPTIONS, SOURCE_PORTALS } from "../utils/constants";

const EMPTY = {
  search: "",
  status: "",
  source_portal: "",
  date_from: "",
  date_to: "",
  is_stale: null,
};

const inputCls =
  "border border-gray-300 dark:border-gray-600 rounded-lg px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100";

export default function Filters({ filters, onChange }) {
  const [searchDraft, setSearchDraft] = useState(filters.search || "");
  const timerRef = useRef(null);
  const onChangeRef = useRef(onChange);
  const filtersRef = useRef(filters);
  useEffect(() => { onChangeRef.current = onChange; });
  useEffect(() => { filtersRef.current = filters; });

  // Filters renders more than once on the page (Applications tab + Stale tab), so
  // ids must be unique per instance — useId prevents duplicate-id label collisions.
  const uid = useId();
  const searchId = `${uid}-search`;
  const statusId = `${uid}-status`;
  const sourceId = `${uid}-source`;
  const dateFromId = `${uid}-date-from`;
  const dateToId = `${uid}-date-to`;

  const isActive =
    !!filters.search || !!filters.status || !!filters.source_portal ||
    !!filters.date_from || !!filters.date_to;

  // Sync draft when parent clears all filters
  useEffect(() => {
    setSearchDraft(filters.search || "");
  }, [filters.search]);

  const update = (key, value) => onChange({ ...filters, [key]: value });
  const clear = () => { setSearchDraft(""); onChange({ ...EMPTY }); };

  const handleSearch = (val) => {
    setSearchDraft(val);
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      onChangeRef.current({ ...filtersRef.current, search: val });
    }, 300);
  };

  return (
    <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-4 mb-4 flex flex-wrap gap-3 items-end">
      <div className="flex flex-col gap-1">
        <label htmlFor={searchId} className="text-xs text-gray-500 dark:text-gray-400">
          Search
        </label>
        <div className="relative">
          <Search
            size={13}
            className="absolute left-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none"
            aria-hidden="true"
          />
          <input
            id={searchId}
            type="text"
            placeholder="Company or role…"
            value={searchDraft}
            onChange={(e) => handleSearch(e.target.value)}
            className={`${inputCls} w-44 pl-8`}
          />
        </div>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor={statusId} className="text-xs text-gray-500 dark:text-gray-400">
          Status
        </label>
        <select
          id={statusId}
          value={filters.status || ""}
          onChange={(e) => update("status", e.target.value)}
          className={`${inputCls} w-44`}
        >
          <option value="">All</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor={sourceId} className="text-xs text-gray-500 dark:text-gray-400">
          Source
        </label>
        <select
          id={sourceId}
          value={filters.source_portal || ""}
          onChange={(e) => update("source_portal", e.target.value)}
          className={`${inputCls} w-40`}
        >
          <option value="">All</option>
          {SOURCE_PORTALS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      <fieldset className="flex gap-3 border-0 p-0 m-0">
        <legend className="sr-only">Applied date range</legend>
        <div className="flex flex-col gap-1">
          <label htmlFor={dateFromId} className="text-xs text-gray-500 dark:text-gray-400">
            From
          </label>
          <input
            id={dateFromId}
            type="date"
            aria-label="Applied from date"
            value={filters.date_from || ""}
            onChange={(e) => update("date_from", e.target.value)}
            className={inputCls}
          />
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor={dateToId} className="text-xs text-gray-500 dark:text-gray-400">
            To
          </label>
          <input
            id={dateToId}
            type="date"
            aria-label="Applied to date"
            value={filters.date_to || ""}
            onChange={(e) => update("date_to", e.target.value)}
            className={inputCls}
          />
        </div>
      </fieldset>

      {isActive && (
        <button
          onClick={clear}
          title="Clear all filters"
          aria-label="Clear filters"
          className="flex items-center gap-1.5 px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded-lg text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
        >
          <X size={14} aria-hidden="true" />
          Clear
        </button>
      )}
    </div>
  );
}
