import { STATUS_OPTIONS, SOURCE_PORTALS } from "../utils/constants";

const EMPTY = {
  search: "",
  status: "",
  source_portal: "",
  date_from: "",
  date_to: "",
};

const inputCls =
  "border border-gray-300 dark:border-gray-600 rounded px-2 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100";

export default function Filters({ filters, onChange }) {
  const update = (key, value) => onChange({ ...filters, [key]: value });
  const clear = () => onChange({ ...EMPTY });

  return (
    <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-700 p-4 mb-4 flex flex-wrap gap-3 items-end">
      <div className="flex flex-col gap-1">
        <label className="text-xs text-gray-500 dark:text-gray-400">Search</label>
        <input
          type="text"
          placeholder="Company or role…"
          value={filters.search || ""}
          onChange={(e) => update("search", e.target.value)}
          className={`${inputCls} w-44`}
        />
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="status-filter" className="text-xs text-gray-500 dark:text-gray-400">Status</label>
        <select
          id="status-filter"
          value={filters.status || ""}
          onChange={(e) => update("status", e.target.value)}
          className={`${inputCls} w-44`}
        >
          <option value="">All</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-xs text-gray-500 dark:text-gray-400">Source</label>
        <select
          value={filters.source_portal || ""}
          onChange={(e) => update("source_portal", e.target.value)}
          className={`${inputCls} w-40`}
        >
          <option value="">All</option>
          {SOURCE_PORTALS.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-xs text-gray-500 dark:text-gray-400">From</label>
        <input
          type="date"
          value={filters.date_from || ""}
          onChange={(e) => update("date_from", e.target.value)}
          className={inputCls}
        />
      </div>

      <div className="flex flex-col gap-1">
        <label className="text-xs text-gray-500 dark:text-gray-400">To</label>
        <input
          type="date"
          value={filters.date_to || ""}
          onChange={(e) => update("date_to", e.target.value)}
          className={inputCls}
        />
      </div>

      <button
        onClick={clear}
        className="px-3 py-1.5 text-sm border border-gray-300 dark:border-gray-600 rounded text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
      >
        Clear Filters
      </button>
    </div>
  );
}
