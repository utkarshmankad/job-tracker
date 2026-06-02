import { useState, useEffect } from "react";
import { X, ExternalLink, Mail, Trash2, Pencil, Check } from "lucide-react";
import { api } from "../api/client";
import { STATUS_OPTIONS, STATUS_COLORS } from "../utils/constants";
import { formatDate } from "../utils/formatters";

const inputCls =
  "border border-gray-300 dark:border-gray-600 rounded px-2 py-1 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 w-full";

export default function ApplicationDetail({ applicationId, onClose, onDelete }) {
  const [app, setApp] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [statusEdit, setStatusEdit] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // Edit mode state
  const [editMode, setEditMode] = useState(false);
  const [editValues, setEditValues] = useState({ company: "", role: "", job_url: "", applied_date: "" });
  const [editSaving, setEditSaving] = useState(false);

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

  const handleDelete = async () => {
    setDeleting(true);
    try {
      await api.deleteApplication(applicationId);
      onDelete?.();
    } catch (e) {
      setError(e.message);
      setDeleting(false);
      setConfirmDelete(false);
    }
  };

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

  const enterEdit = () => {
    setEditValues({
      company: app.company || "",
      role: app.role || "",
      job_url: app.job_url || "",
      applied_date: app.applied_date ? app.applied_date.slice(0, 10) : "",
    });
    setEditMode(true);
  };

  const cancelEdit = () => setEditMode(false);

  const saveEdit = async () => {
    setEditSaving(true);
    try {
      const patch = {};
      if (editValues.company !== (app.company || "")) patch.company = editValues.company || null;
      if (editValues.role !== (app.role || "")) patch.role = editValues.role || null;
      if (editValues.job_url !== (app.job_url || "")) patch.job_url = editValues.job_url || null;
      const originalDate = app.applied_date ? app.applied_date.slice(0, 10) : "";
      if (editValues.applied_date !== originalDate && editValues.applied_date) {
        patch.applied_date = editValues.applied_date + "T00:00:00";
      }
      if (Object.keys(patch).length > 0) {
        await api.updateApplication(applicationId, patch);
      }
      setEditMode(false);
      fetchApp();
    } catch (e) {
      setError(e.message);
    } finally {
      setEditSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-16">
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl w-full max-w-2xl max-h-[80vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            Application Detail
          </h2>
          <div className="flex items-center gap-2">
            {app && (() => {
              const ids = JSON.parse(app.thread_ids || "[]");
              return ids[0] ? (
                <a
                  href={`https://mail.google.com/mail/u/0/#all/${ids[0]}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label="View original email in Gmail"
                  title="View in Gmail"
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium text-blue-600 dark:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/30 border border-blue-200 dark:border-blue-800 transition-colors"
                >
                  <Mail size={13} aria-hidden="true" />
                  View in Gmail
                </a>
              ) : null;
            })()}

            {app && !editMode && (
              <button
                onClick={enterEdit}
                aria-label="Edit application"
                title="Edit"
                className="p-1 rounded-md text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-blue-50 dark:hover:bg-blue-900/20 transition-colors"
              >
                <Pencil size={16} aria-hidden="true" />
              </button>
            )}

            {confirmDelete ? (
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-gray-500 dark:text-gray-400">Delete?</span>
                <button
                  onClick={handleDelete}
                  disabled={deleting}
                  className="px-2.5 py-1 rounded-md text-xs font-medium bg-red-600 text-white hover:bg-red-700 disabled:opacity-50 transition-colors"
                >
                  {deleting ? "Deleting…" : "Confirm"}
                </button>
                <button
                  onClick={() => setConfirmDelete(false)}
                  disabled={deleting}
                  className="px-2.5 py-1 rounded-md text-xs font-medium text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                onClick={() => setConfirmDelete(true)}
                aria-label="Delete application"
                title="Delete application"
                className="p-1 rounded-md text-gray-400 hover:text-red-600 dark:hover:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
              >
                <Trash2 size={16} aria-hidden="true" />
              </button>
            )}

            <button
              onClick={onClose}
              aria-label="Close"
              className="p-1 rounded-md text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
            >
              <X size={18} aria-hidden="true" />
            </button>
          </div>
        </div>

        <div className="px-6 py-4">
          {loading && <p className="text-gray-500 dark:text-gray-400 text-sm">Loading…</p>}
          {error && <p className="text-red-600 text-sm">Error: {error}</p>}

          {app && (
            <>
              <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm mb-6">
                <div>
                  <dt className="text-gray-500 dark:text-gray-400 mb-1">Company</dt>
                  <dd className="font-medium text-gray-900 dark:text-gray-100">
                    {editMode ? (
                      <input
                        className={inputCls}
                        value={editValues.company}
                        onChange={(e) => setEditValues((v) => ({ ...v, company: e.target.value }))}
                        placeholder="Company name"
                      />
                    ) : (
                      app.company || "—"
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400 mb-1">Role</dt>
                  <dd className="font-medium text-gray-900 dark:text-gray-100">
                    {editMode ? (
                      <input
                        className={inputCls}
                        value={editValues.role}
                        onChange={(e) => setEditValues((v) => ({ ...v, role: e.target.value }))}
                        placeholder="Role title"
                      />
                    ) : (
                      app.role || "—"
                    )}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400">Source</dt>
                  <dd className="font-medium text-gray-900 dark:text-gray-100">
                    {app.source_portal}
                  </dd>
                </div>
                <div>
                  <dt className="text-gray-500 dark:text-gray-400 mb-1">Applied Date</dt>
                  <dd className="font-medium text-gray-900 dark:text-gray-100">
                    {editMode ? (
                      <input
                        type="date"
                        className={inputCls}
                        value={editValues.applied_date}
                        onChange={(e) => setEditValues((v) => ({ ...v, applied_date: e.target.value }))}
                      />
                    ) : (
                      formatDate(app.applied_date)
                    )}
                  </dd>
                </div>
                <div className="col-span-2">
                  <dt className="text-gray-500 dark:text-gray-400 mb-1">Job URL</dt>
                  <dd>
                    {editMode ? (
                      <input
                        className={inputCls}
                        value={editValues.job_url}
                        onChange={(e) => setEditValues((v) => ({ ...v, job_url: e.target.value }))}
                        placeholder="https://…"
                      />
                    ) : app.job_url ? (
                      <a
                        href={app.job_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400 hover:underline break-all"
                      >
                        {app.job_url}
                        <ExternalLink size={11} aria-hidden="true" className="flex-shrink-0" />
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
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                    {saving && <span className="text-xs text-gray-400">Saving…</span>}
                  </dd>
                </div>
              </dl>

              {editMode && (
                <div className="flex items-center gap-2 mb-6">
                  <button
                    onClick={saveEdit}
                    disabled={editSaving}
                    className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                  >
                    <Check size={14} aria-hidden="true" />
                    {editSaving ? "Saving…" : "Save"}
                  </button>
                  <button
                    onClick={cancelEdit}
                    disabled={editSaving}
                    className="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              )}

              <h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-3">
                Status History
              </h3>
              {app.status_history && app.status_history.length > 0 ? (
                <ol className="relative border-l border-gray-200 dark:border-gray-700 ml-2 space-y-4">
                  {[...app.status_history]
                    .sort((a, b) => new Date(a.changed_at) - new Date(b.changed_at))
                    .map((entry) => {
                      const colorClass =
                        STATUS_COLORS[entry.to_status] || "bg-gray-100 text-gray-700";
                      return (
                        <li key={entry.id} className="ml-4">
                          <div className="absolute -left-1.5 mt-1 w-3 h-3 rounded-full bg-gray-300 dark:bg-gray-600 border-2 border-white dark:border-gray-900" />
                          <p className="text-xs text-gray-400">{formatDate(entry.changed_at)}</p>
                          <p className="text-sm text-gray-700 dark:text-gray-300">
                            {entry.from_status && (
                              <>
                                <span
                                  className={`px-1.5 py-0.5 rounded text-xs ${
                                    STATUS_COLORS[entry.from_status] || "bg-gray-100 text-gray-600"
                                  }`}
                                >
                                  {entry.from_status}
                                </span>
                                {" → "}
                              </>
                            )}
                            <span className={`px-1.5 py-0.5 rounded text-xs ${colorClass}`}>
                              {entry.to_status}
                            </span>
                            <span className="ml-2 text-xs text-gray-400">
                              via {entry.trigger}
                            </span>
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
