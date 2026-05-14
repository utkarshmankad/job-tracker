import { useState } from "react";
import PollerStatusBar from "./components/PollerStatusBar";
import Filters from "./components/Filters";
import ApplicationsTable from "./components/ApplicationsTable";
import ApplicationDetail from "./components/ApplicationDetail";
import AddApplicationForm from "./components/AddApplicationForm";
import AnalyticsPanel from "./components/AnalyticsPanel";

export default function App() {
  const [activeTab, setActiveTab] = useState("applications");
  const [filters, setFilters] = useState({});
  const [selectedId, setSelectedId] = useState(null);
  const [showAddForm, setShowAddForm] = useState(false);

  return (
    <div className="min-h-screen bg-gray-50">
      <PollerStatusBar />
      <div className="max-w-7xl mx-auto px-4 py-6">
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold text-gray-900">Job Application Tracker</h1>
          <div className="flex gap-2">
            <button
              onClick={() => setActiveTab("applications")}
              className={`px-4 py-2 rounded ${
                activeTab === "applications" ? "bg-blue-600 text-white" : "bg-white border"
              }`}
            >
              Applications
            </button>
            <button
              onClick={() => setActiveTab("analytics")}
              className={`px-4 py-2 rounded ${
                activeTab === "analytics" ? "bg-blue-600 text-white" : "bg-white border"
              }`}
            >
              Analytics
            </button>
            <button
              onClick={() => setShowAddForm(true)}
              className="px-4 py-2 bg-green-600 text-white rounded"
            >
              + Add Application
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
