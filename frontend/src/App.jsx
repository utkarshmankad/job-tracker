import { useState } from "react";
import {
  LayoutList, BarChart2, Activity,
  Download, PlusCircle, Sun, Moon,
} from "lucide-react";

function LinkedInIcon({ size = 16 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
    </svg>
  );
}
import { api } from "./api/client";
import { useTheme } from "./contexts/ThemeContext";
import PollerStatusBar from "./components/PollerStatusBar";
import Filters from "./components/Filters";
import ApplicationsTable from "./components/ApplicationsTable";
import ApplicationDetail from "./components/ApplicationDetail";
import AddApplicationForm from "./components/AddApplicationForm";
import AnalyticsPanel from "./components/AnalyticsPanel";
import StatusPage from "./components/StatusPage";
import LinkedInWithdrawModal from "./components/LinkedInWithdrawModal";

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
  const [showLinkedInModal, setShowLinkedInModal] = useState(false);
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
          <div className="flex items-center gap-2.5">
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
          </div>
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
                onClick={() => setShowLinkedInModal(true)}
                title="Mark closed LinkedIn positions as Withdrawn"
                aria-label="LinkedIn withdraw"
                className="p-2 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 transition-colors"
              >
                <LinkedInIcon size={16} />
              </button>
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
            onDelete={() => {
              setSelectedId(null);
              setFilters((f) => ({ ...f }));
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
        {showLinkedInModal && (
          <LinkedInWithdrawModal
            onClose={() => setShowLinkedInModal(false)}
            onSuccess={() => setFilters((f) => ({ ...f }))}
          />
        )}
      </div>
    </div>
  );
}
