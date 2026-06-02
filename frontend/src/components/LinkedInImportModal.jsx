import { useState, useRef } from "react";
import { X, Upload, CheckCircle, AlertCircle, SkipForward, RefreshCw } from "lucide-react";
import { api } from "../api/client";

const ACTION_STYLE = {
  created:  { icon: CheckCircle,  cls: "text-green-600 dark:text-green-400" },
  updated:  { icon: RefreshCw,    cls: "text-blue-600 dark:text-blue-400"  },
  skipped:  { icon: SkipForward,  cls: "text-gray-400 dark:text-gray-500"  },
  error:    { icon: AlertCircle,  cls: "text-red-600 dark:text-red-400"    },
};

function Step({ n, title, children }) {
  return (
    <div className="flex gap-3">
      <div className="flex-shrink-0 w-6 h-6 rounded-full bg-blue-600 text-white text-xs font-bold flex items-center justify-center mt-0.5">
        {n}
      </div>
      <div>
        <p className="text-sm font-medium text-gray-800 dark:text-gray-200">{title}</p>
        {children && <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{children}</p>}
      </div>
    </div>
  );
}

export default function LinkedInImportModal({ onClose, onImported }) {
  const [file, setFile] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  const handleFile = (f) => {
    if (!f) return;
    if (!f.name.toLowerCase().endsWith(".csv")) {
      setError("Please select a .csv file.");
      return;
    }
    setFile(f);
    setError(null);
    setResult(null);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    handleFile(e.dataTransfer.files[0]);
  };

  const handleImport = async () => {
    if (!file) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.importLinkedIn(file);
      setResult(res);
      onImported?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const resultSummary = result
    ? [
        result.created > 0 && `${result.created} created`,
        result.updated > 0 && `${result.updated} updated`,
        result.skipped > 0 && `${result.skipped} skipped`,
        result.errors > 0  && `${result.errors} errors`,
      ].filter(Boolean).join(" · ")
    : null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-12 px-4">
      <div className="bg-white dark:bg-gray-900 rounded-xl shadow-xl w-full max-w-lg max-h-[88vh] flex flex-col">

        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2.5">
            <svg viewBox="0 0 24 24" className="w-5 h-5 fill-[#0A66C2]" aria-hidden="true">
              <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
            </svg>
            <span className="text-base font-semibold text-gray-900 dark:text-gray-100">
              Import from LinkedIn
            </span>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="p-1 rounded-md text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          >
            <X size={18} />
          </button>
        </div>

        <div className="overflow-y-auto flex-1 px-6 py-5 space-y-5">

          {/* Instructions */}
          {!result && (
            <div className="space-y-3">
              <p className="text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                How to get your LinkedIn CSV
              </p>
              <div className="space-y-3">
                <Step n="A" title="Fast export (recommended)">
                  Open{" "}
                  <span className="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 rounded">
                    linkedin.com/jobs-tracker/
                  </span>
                  , click the <strong>↗ export icon</strong> in the top-right of the tracker.
                  CSV downloads instantly.
                </Step>
                <Step n="B" title="Full data export (slower)">
                  Settings → Data Privacy → <em>Get a copy of your data</em> → check{" "}
                  <strong>Jobs</strong> → Request archive. LinkedIn emails you a download link
                  (up to 24 h). Extract the ZIP, find{" "}
                  <span className="font-mono text-xs bg-gray-100 dark:bg-gray-800 px-1 rounded">
                    Job Applications.csv
                  </span>.
                </Step>
              </div>

              <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
                <strong>Deduplication:</strong> rows already in the tracker (matched by job URL
                or company + role + date) are skipped unless LinkedIn shows a more advanced
                status, in which case the existing record is updated.
              </div>
            </div>
          )}

          {/* Drop zone */}
          {!result && (
            <div
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={handleDrop}
              onClick={() => inputRef.current?.click()}
              className={`border-2 border-dashed rounded-xl p-8 flex flex-col items-center gap-2 cursor-pointer transition-colors ${
                dragging
                  ? "border-blue-500 bg-blue-50 dark:bg-blue-900/20"
                  : "border-gray-300 dark:border-gray-600 hover:border-blue-400 hover:bg-gray-50 dark:hover:bg-gray-800/50"
              }`}
            >
              <Upload size={28} className="text-gray-400" />
              <p className="text-sm text-gray-600 dark:text-gray-300 font-medium">
                {file ? file.name : "Drop your CSV here or click to browse"}
              </p>
              {file && (
                <p className="text-xs text-gray-400">
                  {(file.size / 1024).toFixed(1)} KB
                </p>
              )}
              <input
                ref={inputRef}
                type="file"
                accept=".csv"
                className="hidden"
                onChange={(e) => handleFile(e.target.files?.[0])}
              />
            </div>
          )}

          {/* Error banner */}
          {error && (
            <div className="flex items-start gap-2 text-sm text-red-700 dark:text-red-400 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg px-3 py-2">
              <AlertCircle size={15} className="flex-shrink-0 mt-0.5" />
              {error}
            </div>
          )}

          {/* Warnings */}
          {result?.warnings?.length > 0 && (
            <div className="bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg px-3 py-2 space-y-1">
              {result.warnings.map((w, i) => (
                <p key={i} className="text-xs text-amber-800 dark:text-amber-300">{w}</p>
              ))}
            </div>
          )}

          {/* Result summary */}
          {result && (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <CheckCircle size={18} className="text-green-600 dark:text-green-400 flex-shrink-0" />
                <span className="text-sm font-medium text-gray-800 dark:text-gray-200">
                  Import complete — {resultSummary}
                </span>
              </div>

              {/* Per-row results */}
              {result.rows.length > 0 && (
                <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                  <div className="max-h-64 overflow-y-auto divide-y divide-gray-100 dark:divide-gray-700">
                    {result.rows.map((row, i) => {
                      const { icon: Icon, cls } = ACTION_STYLE[row.action] ?? ACTION_STYLE.skipped;
                      return (
                        <div key={i} className="flex items-start gap-2 px-3 py-2 text-xs">
                          <Icon size={13} className={`${cls} flex-shrink-0 mt-0.5`} />
                          <div className="flex-1 min-w-0">
                            <span className="font-medium text-gray-800 dark:text-gray-200">
                              {row.company || "—"}
                            </span>
                            {row.role && (
                              <span className="text-gray-500 dark:text-gray-400"> · {row.role}</span>
                            )}
                          </div>
                          <span className="text-gray-400 dark:text-gray-500 whitespace-nowrap ml-2">
                            {row.detail}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-gray-200 dark:border-gray-700">
          {result ? (
            <button
              onClick={onClose}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors"
            >
              Done
            </button>
          ) : (
            <>
              <button
                onClick={onClose}
                className="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleImport}
                disabled={!file || loading}
                className="flex items-center gap-1.5 px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
              >
                {loading ? (
                  <>
                    <span className="animate-spin inline-block w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full" />
                    Importing…
                  </>
                ) : (
                  <>
                    <Upload size={14} />
                    Import
                  </>
                )}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
