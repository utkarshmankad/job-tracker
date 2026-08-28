import { useState, useEffect, useCallback } from "react";
import {
  LayoutList, Activity, Home, AlertTriangle,
  Download, PlusCircle, Sun, Moon,
} from "lucide-react";
import { api } from "./api/client";
import { useTheme } from "./contexts/ThemeContext";
import PollerStatusBar from "./components/PollerStatusBar";
import Filters from "./components/Filters";
import ApplicationsTable from "./components/ApplicationsTable";
import ApplicationDetail from "./components/ApplicationDetail";
import AddApplicationForm from "./components/AddApplicationForm";
import AnalyticsPanel from "./components/AnalyticsPanel";
import StatusPage from "./components/StatusPage";
// LinkedIn import/withdraw tools removed — see CLAUDE.md task; endpoints in
// backend/api/routes.py (linkedin_import_preview/confirmed) are now unused
// by the UI but left intact server-side.

const NAV_TABS = [
  { id: "home", label: "Home", Icon: Home },
  { id: "applications", label: "Applications", Icon: LayoutList },
  { id: "stale", label: "Stale", Icon: AlertTriangle },
  { id: "status", label: "Status", Icon: Activity },
];

export default function App() {
  const [activeTab, setActiveTab] = useState("home");
  // Tabs mount lazily on first visit, then stay mounted (kept alive, hidden via CSS)
  // so revisiting one is instant instead of re-fetching from scratch every time.
  const [visitedTabs, setVisitedTabs] = useState(() => new Set(["home"]));
  const [filters, setFilters] = useState({});
  const [selectedId, setSelectedId] = useState(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const { dark, toggle } = useTheme();
  const [exportError, setExportError] = useState(null);
  const [staleCount, setStaleCount] = useState(null);

  const refreshStaleCount = useCallback(() => {
    api
      .listApplications({ is_stale: true, page_size: 1 })
      .then((res) => setStaleCount(res.total ?? 0))
      .catch(() => setStaleCount(null)); // badge just stays hidden on failure
  }, []);

  // Poll cadence matches the Gmail poller (every 5 min) — stale status can only
  // change that often anyway, so there's no value checking more frequently.
  useEffect(() => {
    refreshStaleCount();
    const interval = setInterval(refreshStaleCount, 5 * 60 * 1000);
    return () => clearInterval(interval);
  }, [refreshStaleCount]);

  const selectTab = (id) => {
    setActiveTab(id);
    setVisitedTabs((prev) => (prev.has(id) ? prev : new Set(prev).add(id)));
    if (id === "stale") refreshStaleCount();
  };

  const handleExport = async () => {
    setExportError(null);
    try {
      const res = await api.exportApplications("csv");
      if (!res.ok) throw new Error(`Export failed: ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "applications.csv";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setExportError(e.message);
    }
  };

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-950 transition-colors">
      <PollerStatusBar />
      <div className="max-w-7xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6 gap-4 flex-wrap">
          <button
            type="button"
            onClick={() => selectTab("home")}
            className="flex items-center gap-2.5"
            aria-label="Go to home"
          >
            <svg width="32" height="32" viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
              <defs>
                <linearGradient id="logo-grad" x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
                  <stop offset="0%" stopColor="#38bdf8"/>
                  <stop offset="100%" stopColor="#0369a1"/>
                </linearGradient>
              </defs>
              <rect width="32" height="32" rx="7" fill="url(#logo-grad)"/>
              <path d="M12 15V12a4 4 0 0 1 8 0v3" stroke="white" strokeWidth="2.5" strokeLinecap="round"/>
              <rect x="4" y="15" width="24" height="13" rx="3" fill="white"/>
              <path d="M10 22.5l3.5 3.5 8.5-8.5" stroke="#0ea5e9" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            <span className="text-2xl font-bold text-gray-900 dark:text-gray-100">
              Job Tracker
            </span>
          </button>
          <div className="flex items-center gap-3 flex-wrap">
            <nav className="flex items-center gap-1 bg-gray-100 dark:bg-gray-800 rounded-lg p-1">
              {NAV_TABS.map(({ id, label, Icon }) => (
                <button
                  key={id}
                  onClick={() => selectTab(id)}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                    activeTab === id
                      ? "bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 shadow-sm"
                      : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                  }`}
                >
                  <Icon size={14} aria-hidden="true" />
                  {label}
                  {id === "stale" && staleCount > 0 && (
                    <span
                      aria-label={`${staleCount} stale application${staleCount !== 1 ? "s" : ""}`}
                      className="ml-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-semibold leading-none bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300"
                    >
                      {staleCount}
                    </span>
                  )}
                </button>
              ))}
            </nav>

            <div className="flex items-center gap-2">
              <button
                onClick={handleExport}
                title="Export as CSV"
                aria-label="Export"
                className="p-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
              >
                <Download size={16} aria-hidden="true" />
              </button>
              <button
                onClick={() => setShowAddForm(true)}
                className="flex items-center gap-1.5 px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors"
              >
                <PlusCircle size={15} aria-hidden="true" />
                Add Application
              </button>
              <button
                onClick={toggle}
                title={dark ? "Switch to light mode" : "Switch to dark mode"}
                aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
                className="p-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
              >
                {dark ? <Sun size={16} aria-hidden="true" /> : <Moon size={16} aria-hidden="true" />}
              </button>
            </div>
          </div>
        </div>

        {exportError && (
          <div className="mb-4 text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
            Export failed: {exportError}
          </div>
        )}
        {/*
          Each tab mounts lazily on its first visit (visitedTabs), then stays mounted
          and is hidden via CSS instead of being conditionally rendered/unmounted.
          Conditional rendering (`activeTab === X && <Y />`) unmounts the component on
          every tab switch, forcing ApplicationsTable/AnalyticsPanel to refetch from
          scratch each time — the main source of the "switching tabs feels slow" lag.
          Keeping visited tabs mounted means revisiting one is instant; only the first
          visit pays for a fetch (and even that is now backed by the server-side Redis
          cache, so it's fast too).
        */}
        {visitedTabs.has("home") && (
          <div className={activeTab === "home" ? "" : "hidden"}>
            <AnalyticsPanel />
          </div>
        )}
        {visitedTabs.has("applications") && (
          <div className={activeTab === "applications" ? "" : "hidden"}>
            <Filters filters={filters} onChange={setFilters} />
            <ApplicationsTable
              filters={{ ...filters, is_stale: false }}
              onSelectId={setSelectedId}
            />
          </div>
        )}
        {visitedTabs.has("stale") && (
          <div className={activeTab === "stale" ? "" : "hidden"}>
            <ApplicationsTable filters={{ is_stale: true }} onSelectId={setSelectedId} />
          </div>
        )}
        {visitedTabs.has("status") && (
          <div className={activeTab === "status" ? "" : "hidden"}>
            <StatusPage />
          </div>
        )}
        {selectedId && (
          <ApplicationDetail
            applicationId={selectedId}
            onClose={() => setSelectedId(null)}
            onDelete={() => {
              setSelectedId(null);
              setFilters((f) => ({ ...f }));
              refreshStaleCount();
            }}
          />
        )}
        {showAddForm && (
          <AddApplicationForm
            onSuccess={() => {
              setShowAddForm(false);
              setFilters({ ...filters });
            }}
            onClose={() => setShowAddForm(false)}
          />
        )}
      </div>
    </div>
  );
}
