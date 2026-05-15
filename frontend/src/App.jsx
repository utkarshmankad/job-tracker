import { useState } from "react";
import { api } from "./api/client";
import { useTheme } from "./contexts/ThemeContext";
import PollerStatusBar from "./components/PollerStatusBar";
import Filters from "./components/Filters";
import ApplicationsTable from "./components/ApplicationsTable";
import ApplicationDetail from "./components/ApplicationDetail";
import AddApplicationForm from "./components/AddApplicationForm";
import AnalyticsPanel from "./components/AnalyticsPanel";
import StatusPage from "./components/StatusPage";

export default function App() {
  const [activeTab, setActiveTab] = useState("applications");
  const [filters, setFilters] = useState({});
  const [selectedId, setSelectedId] = useState(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const { dark, toggle } = useTheme();

  const handleExport = async () => {
    const res = await api.exportApplications("csv");
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "applications.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-950 transition-colors">
      <PollerStatusBar />
      <div className="max-w-7xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Job Application Tracker
          </h1>
          <div className="flex gap-2 flex-wrap">
            <button
              onClick={() => setActiveTab("applications")}
              className={`px-4 py-2 rounded text-sm font-medium ${
                activeTab === "applications"
                  ? "bg-blue-600 text-white"
                  : "bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300"
              }`}
            >
              Applications
            </button>
            <button
              onClick={() => setActiveTab("analytics")}
              className={`px-4 py-2 rounded text-sm font-medium ${
                activeTab === "analytics"
                  ? "bg-blue-600 text-white"
                  : "bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300"
              }`}
            >
              Analytics
            </button>
            <button
              onClick={() => setActiveTab("status")}
              className={`px-4 py-2 rounded text-sm font-medium ${
                activeTab === "status"
                  ? "bg-blue-600 text-white"
                  : "bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300"
              }`}
            >
              Status
            </button>
            <button
              onClick={handleExport}
              className="px-4 py-2 bg-gray-600 hover:bg-gray-700 text-white rounded text-sm font-medium"
            >
              Export
            </button>
            <button
              onClick={() => setShowAddForm(true)}
              className="px-4 py-2 bg-green-600 hover:bg-green-700 text-white rounded text-sm font-medium"
            >
              + Add Application
            </button>
            <button
              onClick={toggle}
              title={dark ? "Switch to light mode" : "Switch to dark mode"}
              className="px-3 py-2 rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 text-sm"
            >
              {dark ? "☀︎" : "☾"}
            </button>
          </div>
        </div>

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
