import { useState } from "react";
import {
  LayoutList, BarChart2, Activity,
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

const NAV_TABS = [
  { id: "applications", label: "Applications", Icon: LayoutList },
  { id: "analytics", label: "Analytics", Icon: BarChart2 },
  { id: "status", label: "Status", Icon: Activity },
];

export default function App() {
  const [activeTab, setActiveTab] = useState("applications");
  const [filters, setFilters] = useState({});
  const [selectedId, setSelectedId] = useState(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const { dark, toggle } = useTheme();
  const [exportError, setExportError] = useState(null);

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
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Job Application Tracker
          </h1>
          <div className="flex items-center gap-3 flex-wrap">
            <nav className="flex items-center gap-1 bg-gray-100 dark:bg-gray-800 rounded-lg p-1">
              {NAV_TABS.map(({ id, label, Icon }) => (
                <button
                  key={id}
                  onClick={() => setActiveTab(id)}
                  className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                    activeTab === id
                      ? "bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 shadow-sm"
                      : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                  }`}
                >
                  <Icon size={14} aria-hidden="true" />
                  {label}
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
        {activeTab === "applications" && (
          <>
            <Filters filters={filters} onChange={setFilters} />
            <ApplicationsTable filters={filters} onSelectId={setSelectedId} />
          </>
        )}
        {activeTab === "analytics" && <AnalyticsPanel />}
        {activeTab === "status" && <StatusPage />}
        {selectedId && (
          <ApplicationDetail
            applicationId={selectedId}
            onClose={() => setSelectedId(null)}
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
