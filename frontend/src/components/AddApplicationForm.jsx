import { useState } from "react";
import { api } from "../api/client";
import { STATUS_OPTIONS, SOURCE_PORTALS } from "../utils/constants";

const EMPTY = {
  company: "",
  role: "",
  source_portal: "",
  job_url: "",
  applied_date: new Date().toISOString().split("T")[0],
  current_status: "Applied",
};

const inputCls =
  "w-full border border-gray-300 dark:border-gray-600 rounded px-3 py-2 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500";

export default function AddApplicationForm({ onSuccess, onClose }) {
  const [form, setForm] = useState({ ...EMPTY });
  const [errors, setErrors] = useState({});
  const [submitting, setSubmitting] = useState(false);
  const [apiError, setApiError] = useState(null);

  const update = (key, value) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    if (errors[key]) setErrors((prev) => ({ ...prev, [key]: null }));
  };

  const validate = () => {
    const errs = {};
    if (!form.source_portal) errs.source_portal = "Source portal is required.";
    if (!form.company) errs.company = "Company is required.";
    if (!form.applied_date) errs.applied_date = "Applied date is required.";
    return errs;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    const errs = validate();
    if (Object.keys(errs).length > 0) {
      setErrors(errs);
      return;
    }
    setSubmitting(true);
    setApiError(null);
    try {
      await api.createApplication({
        ...form,
        applied_date: form.applied_date ? new Date(form.applied_date).toISOString() : undefined,
      });
      onSuccess();
    } catch (e) {
      setApiError(e.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-16">
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl w-full max-w-lg">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Add Application</h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 text-xl leading-none"
          >
            ✕
          </button>
        </div>

        <form onSubmit={handleSubmit} className="px-6 py-4 space-y-4">
          {apiError && (
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded p-2">
              {apiError}
            </p>
          )}

          <div>
            <label htmlFor="company" className="block text-sm text-gray-600 dark:text-gray-400 mb-1">Company *</label>
            <input id="company" type="text" value={form.company} onChange={(e) => update("company", e.target.value)} className={inputCls} />
            {errors.company && <p className="text-xs text-red-600 mt-1">{errors.company}</p>}
          </div>

          <div>
            <label htmlFor="role" className="block text-sm text-gray-600 dark:text-gray-400 mb-1">Role</label>
            <input id="role" type="text" value={form.role} onChange={(e) => update("role", e.target.value)} className={inputCls} />
          </div>

          <div>
            <label htmlFor="source_portal" className="block text-sm text-gray-600 dark:text-gray-400 mb-1">Source Portal *</label>
            <select id="source_portal" value={form.source_portal} onChange={(e) => update("source_portal", e.target.value)} className={inputCls}>
              <option value="">Select…</option>
              {SOURCE_PORTALS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
            {errors.source_portal && <p className="text-xs text-red-600 mt-1">{errors.source_portal}</p>}
          </div>

          <div>
            <label className="block text-sm text-gray-600 dark:text-gray-400 mb-1">Job URL</label>
            <input type="text" value={form.job_url} onChange={(e) => update("job_url", e.target.value)} className={inputCls} />
          </div>

          <div>
            <label htmlFor="applied_date" className="block text-sm text-gray-600 dark:text-gray-400 mb-1">Applied Date *</label>
            <input id="applied_date" type="date" value={form.applied_date} onChange={(e) => update("applied_date", e.target.value)} className={inputCls} />
            {errors.applied_date && <p className="text-xs text-red-600 mt-1">{errors.applied_date}</p>}
          </div>

          <div>
            <label className="block text-sm text-gray-600 dark:text-gray-400 mb-1">Status</label>
            <select value={form.current_status} onChange={(e) => update("current_status", e.target.value)} className={inputCls}>
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm border border-gray-300 dark:border-gray-600 rounded text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-4 py-2 text-sm bg-green-600 text-white rounded hover:bg-green-700 disabled:opacity-60"
            >
              {submitting ? "Saving…" : "Submit"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
