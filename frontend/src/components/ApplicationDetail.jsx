import { useState, useEffect } from "react";
import { api } from "../api/client";
import { STATUS_OPTIONS, STATUS_COLORS } from "../utils/constants";
import { formatDate } from "../utils/formatters";

export default function ApplicationDetail({ applicationId, onClose }) {
  const [app, setApp] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [statusEdit, setStatusEdit] = useState("");
  const [saving, setSaving] = useState(false);

  const fetchApp = (signal) => {
    setLoading(true);
    api
      .getApplication(applicationId, signal)
      .then((data) => {
        setApp(data);
        setStatusEdit(data.current_status);
        setError(null);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    const controller = new AbortController();
    fetchApp(controller.signal);
    return () => controller.abort();
  }, [applicationId]);

  const handleStatusChange = async (newStatus) => {
    setStatusEdit(newStatus);
    setSaving(true);
    try {
      await api.updateApplication(applicationId, { current_status: newStatus });
      fetchApp();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-16">
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl w-full max-w-2xl max-h-[80vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Application Detail</h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 text-xl leading-none"
          >
            ✕
          </button>
        </div>

        <div className="px-6 py-4">
          {loading && <p className="text-gray-500 dark:text-gray-400 text-sm">Loading…</p>}
          {error && <p className="text-red-600 text-sm">Error: {error}</p>}

          {app && (
            <>
              <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm mb-6">
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">Company</dt>
                  <dd className="font-medium text-gray-900 dark:text-gray-100">{app.company || "—"}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">Role</dt>
                  <dd className="font-medium text-gray-900 dark:text-gray-100">{app.role || "—"}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">Source</dt>
                  <dd className="font-medium text-gray-900 dark:text-gray-100">{app.source_portal}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">Applied Date</dt>
                  <dd className="font-medium text-gray-900 dark:text-gray-100">{formatDate(app.applied_date)}</dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">Job URL</dt>
                  <dd>
                    {app.job_url ? (
                      <a
                        href={app.job_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-blue-600 dark:text-blue-400 hover:underline break-all"
                      >
                        {app.job_url}
                      </a>
                    ) : (
                      "—"
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">Status</dt>
                  <dd className="flex items-center gap-2">
                    <select
                      value={statusEdit}
                      onChange={(e) => handleStatusChange(e.target.value)}
                      disabled={saving}
                      className="border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    >
                      {STATUS_OPTIONS.map((s) => (
                        <option key={s} value={s}>{s}</option>
                      ))}
                    </select>
                    {saving && <span className="text-xs text-gray-400">Saving…</span>}
                  </dd>
                </div>
              </dl>

              <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">Status History</h3>
              {app.status_history && app.status_history.length > 0 ? (
                <ol className="relative border-l border-gray-200 dark:border-gray-700 ml-2 space-y-4">
                  {[...app.status_history]
                    .sort((a, b) => new Date(a.changed_at) - new Date(b.changed_at))
                    .map((entry) => {
                      const colorClass = STATUS_COLORS[entry.to_status] || "bg-gray-100 text-gray-700";
                      return (
                        <li key={entry.id} className="ml-4">
                          <div className="absolute -left-1.5 mt-1 w-3 h-3 rounded-full bg-gray-300 dark:bg-gray-600 border-2 border-white dark:border-gray-900" />
                          <p className="text-xs text-gray-400">{formatDate(entry.changed_at)}</p>
                          <p className="text-sm text-gray-700 dark:text-gray-300">
                            {entry.from_status && (
                              <>
                                <span className={`px-1.5 py-0.5 rounded text-xs ${STATUS_COLORS[entry.from_status] || "bg-gray-100 text-gray-600"}`}>
                                  {entry.from_status}
                                </span>
                                {" → "}
                              </>
                            )}
                            <span className={`px-1.5 py-0.5 rounded text-xs ${colorClass}`}>
                              {entry.to_status}
                            </span>
                            <span className="ml-2 text-xs text-gray-400">via {entry.trigger}</span>
                          </p>
                        </li>
                      );
                    })}
                </ol>
              ) : (
                <p className="text-sm text-gray-400">No history yet.</p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
