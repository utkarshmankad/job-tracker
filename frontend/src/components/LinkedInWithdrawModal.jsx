import { useState } from "react";
import { X, AlertTriangle } from "lucide-react";
import { api } from "../api/client";

function parseCompaniesFromText(text) {
  // Extract non-empty lines that could be company names.
  // LinkedIn job card paste typically has: job title, company, location, etc.
  // We return all non-empty lines for the backend to match against.
  return [
    ...new Set(
      text
        .split(/\n/)
        .map((l) => l.trim())
        .filter((l) => l.length > 1 && l.length < 120)
        // Drop lines that look like locations, dates, or numbers
        .filter((l) => !/^\d+/.test(l))
        .filter((l) => !/\bago\b|\bviews?\b|\bapplicant/i.test(l))
    ),
  ];
}

export default function LinkedInWithdrawModal({ onClose, onSuccess }) {
  const [text, setText] = useState("");
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const companies = parseCompaniesFromText(text);

  const handleSubmit = async () => {
    if (!companies.length) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.bulkWithdraw(companies);
      setResult(res);
      onSuccess?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-16">
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl w-full max-w-lg max-h-[80vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
            LinkedIn — Mark Closed Positions
          </h2>
          <button
            onClick={onClose}
            className="p-1 rounded-md text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        <div className="px-6 py-4 space-y-4">
          {result ? (
            <div className="text-center py-6">
              <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">{result.updated}</p>
              <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
                application{result.updated !== 1 ? "s" : ""} marked Withdrawn (Company Closed)
              </p>
              <button
                onClick={onClose}
                className="mt-4 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors"
              >
                Done
              </button>
            </div>
          ) : (
            <>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Paste company names or LinkedIn job card text below. Applications at matching
                companies will be marked <strong>Withdrawn — Company Closed</strong>.
              </p>

              <textarea
                className="w-full h-48 border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none font-mono"
                placeholder={"Google\nMeta\nStripe\n\n— or paste LinkedIn job card text —"}
                value={text}
                onChange={(e) => setText(e.target.value)}
              />

              {companies.length > 0 && (
                <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-700 rounded-lg px-3 py-2">
                  <div className="flex items-center gap-1.5 text-xs font-medium text-amber-700 dark:text-amber-400 mb-1">
                    <AlertTriangle size={12} />
                    {companies.length} candidate line{companies.length !== 1 ? "s" : ""} detected
                  </div>
                  <ul className="text-xs text-amber-800 dark:text-amber-300 space-y-0.5 max-h-28 overflow-y-auto">
                    {companies.slice(0, 30).map((c, i) => (
                      <li key={i} className="truncate">• {c}</li>
                    ))}
                    {companies.length > 30 && (
                      <li className="text-amber-500">…and {companies.length - 30} more</li>
                    )}
                  </ul>
                </div>
              )}

              {error && (
                <p className="text-sm text-red-600 dark:text-red-400">Error: {error}</p>
              )}

              <div className="flex items-center justify-end gap-2 pt-2">
                <button
                  onClick={onClose}
                  className="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleSubmit}
                  disabled={loading || companies.length === 0}
                  className="px-4 py-1.5 bg-red-600 hover:bg-red-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
                >
                  {loading ? "Withdrawing…" : `Withdraw Matching (${companies.length})`}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
