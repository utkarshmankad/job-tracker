import { useState, useCallback } from "react";
import {
  X, CheckCircle, SkipForward, RefreshCw, AlertCircle, ArrowUpCircle, Cpu,
} from "lucide-react";
import { api } from "../api/client";

const MODES = [
  {
    id: "applied",
    label: "Applied",
    description: "LinkedIn → My Jobs → Applied. Creates new records (skips duplicates).",
    placeholder:
      "Google\n\nSoftware Engineer\n\nBengaluru, Karnataka\n\nApplied 2d ago\n\nMeta\n\nProduct Manager\n\nMumbai\n\nApplied 1w ago",
  },
  {
    id: "archived",
    label: "Archived",
    description: "LinkedIn → My Jobs → Archived. Creates records for archived applications (status: Applied).",
    placeholder:
      "Google\n\nSoftware Engineer\n\nBengaluru\n\nArchived\n\nMeta\n\nProduct Manager\n\nMumbai\n\nArchived",
  },
  {
    id: "interview",
    label: "Interview",
    description: "LinkedIn → My Jobs → In Progress. Creates new or advances existing to Interview In Progress.",
    placeholder:
      "Google\n\nSoftware Engineer\n\nBengaluru\n\nInterviewing\n\nMeta\n\nProduct Manager\n\nMumbai\n\nIn progress",
  },
];

const ACTION_META = {
  create:  { label: "Will create",     color: "bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400" },
  update:  { label: "Will advance",    color: "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400" },
  skip:    { label: "Already tracked", color: "bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400" },
  no_data: { label: "No data",         color: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/30 dark:text-yellow-400" },
};

const RESULT_ICON = {
  created: <CheckCircle size={13} className="text-green-500 shrink-0" />,
  updated: <ArrowUpCircle size={13} className="text-blue-500 shrink-0" />,
  skipped: <SkipForward size={13} className="text-gray-400 shrink-0" />,
  failed:  <AlertCircle size={13} className="text-red-500 shrink-0" />,
};

function formatDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

/* ── Annotated text view ── */
function AnnotatedText({ text, garbageLines }) {
  const lineSet = new Set(garbageLines);
  const lines = text.split("\n");
  return (
    <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-auto max-h-44 bg-gray-50 dark:bg-gray-900/50 p-3">
      <pre className="text-xs font-mono leading-5 whitespace-pre-wrap break-all">
        {lines.map((line, i) => {
          const lineNum = i + 1;
          const isGarbage = lineSet.has(lineNum);
          if (!line.trim()) {
            return <span key={i}>{"\n"}</span>;
          }
          return (
            <span
              key={i}
              style={
                isGarbage
                  ? {
                      textDecoration: "underline",
                      textDecorationStyle: "wavy",
                      textDecorationColor: "rgb(239,68,68)",
                      color: "rgb(185,28,28)",
                    }
                  : undefined
              }
              className={isGarbage ? "dark:!text-red-400" : "text-gray-700 dark:text-gray-300"}
              title={isGarbage ? "Identified as noise / garbage" : undefined}
            >
              {line}
              {"\n"}
            </span>
          );
        })}
      </pre>
    </div>
  );
}

export default function LinkedInImportModal({ onClose, onSuccess }) {
  const [mode, setMode] = useState("applied");
  const [text, setText] = useState("");
  const [step, setStep] = useState("paste"); // "paste" | "preview" | "result"
  const [previewRows, setPreviewRows] = useState([]);
  const [garbageLines, setGarbageLines] = useState([]);
  const [llmUsed, setLlmUsed] = useState(false);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const current = MODES.find((m) => m.id === mode);

  /* ── Step 1: parse + preview ── */
  const handlePreview = useCallback(async () => {
    if (!text.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await api.linkedinPreview(text, mode);
      setGarbageLines(res.garbage_lines ?? []);
      setLlmUsed(res.llm_used ?? false);
      const rows = res.entries.map((e) => ({ ...e, include: e.predicted_action !== "skip" }));
      setPreviewRows(rows);
      setStep("preview");
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [text, mode]);

  /* ── Step 2: confirm import ── */
  const handleConfirm = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const entries = previewRows.map((r) => ({
        company: r.company,
        role: r.role,
        applied_date: r.applied_date,
        include: r.include,
      }));
      const res = await api.linkedinImportConfirmed(mode, entries);
      setResult(res);
      setStep("result");
      onSuccess?.();
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [previewRows, mode, onSuccess]);

  const updateRow = (i, field, value) =>
    setPreviewRows((rows) => rows.map((r, idx) => (idx === i ? { ...r, [field]: value } : r)));

  const toggleAll = (v) =>
    setPreviewRows((rows) => rows.map((r) => ({ ...r, include: v })));

  const includedCount = previewRows.filter((r) => r.include).length;

  const handleReset = () => {
    setText("");
    setPreviewRows([]);
    setGarbageLines([]);
    setLlmUsed(false);
    setResult(null);
    setError(null);
    setStep("paste");
  };

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/40 pt-8">
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl w-full max-w-3xl max-h-[90vh] flex flex-col">

        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-gray-700 shrink-0">
          <div className="flex items-center gap-3">
            <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">
              LinkedIn — Import Jobs
            </h2>
            {step === "preview" && (
              <div className="flex items-center gap-1.5 text-xs text-gray-400 dark:text-gray-500">
                {llmUsed ? (
                  <>
                    <Cpu size={11} />
                    <span>AI parsed</span>
                  </>
                ) : (
                  <span>regex fallback</span>
                )}
              </div>
            )}
            {step === "result" && (
              <span className="text-xs text-gray-400 dark:text-gray-500">Done</span>
            )}
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded-md text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4 min-h-0">

          {/* ── STEP: paste ── */}
          {step === "paste" && (
            <>
              <div className="flex gap-1 bg-gray-100 dark:bg-gray-800 rounded-lg p-1 w-fit">
                {MODES.map((m) => (
                  <button
                    key={m.id}
                    onClick={() => setMode(m.id)}
                    className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                      mode === m.id
                        ? "bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 shadow-sm"
                        : "text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
                    }`}
                  >
                    {m.label}
                  </button>
                ))}
              </div>

              <p className="text-sm text-gray-600 dark:text-gray-400">{current.description}</p>
              <p className="text-xs text-gray-400 dark:text-gray-500">
                Select all on the LinkedIn page and paste. AI will parse and flag noise — you review before saving.
              </p>

              <textarea
                className="w-full h-60 border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none font-mono"
                placeholder={current.placeholder}
                value={text}
                onChange={(e) => setText(e.target.value)}
                autoFocus
              />

              {error && <p className="text-sm text-red-600 dark:text-red-400">Error: {error}</p>}

              <div className="flex justify-end gap-2 pt-1">
                <button onClick={onClose} className="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors">
                  Cancel
                </button>
                <button
                  onClick={handlePreview}
                  disabled={loading || !text.trim()}
                  className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
                >
                  {loading ? "Parsing with AI…" : "Parse →"}
                </button>
              </div>
            </>
          )}

          {/* ── STEP: preview / edit ── */}
          {step === "preview" && (
            <>
              {/* Annotated text — shows garbage lines with red wavy underline */}
              {garbageLines.length > 0 && (
                <div>
                  <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1.5">
                    Input — <span className="text-red-500">{garbageLines.length} noise line{garbageLines.length !== 1 ? "s" : ""} flagged</span>
                  </p>
                  <AnnotatedText text={text} garbageLines={garbageLines} />
                </div>
              )}

              {/* Preview table */}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                    {previewRows.length} entr{previewRows.length === 1 ? "y" : "ies"} parsed
                  </span>
                  <span className="text-xs text-gray-400">{includedCount} selected</span>
                </div>
                <div className="flex items-center gap-2 text-xs">
                  <button onClick={() => toggleAll(true)} className="text-blue-600 dark:text-blue-400 hover:underline">Select all</button>
                  <span className="text-gray-300 dark:text-gray-600">·</span>
                  <button onClick={() => toggleAll(false)} className="text-gray-500 dark:text-gray-400 hover:underline">None</button>
                </div>
              </div>

              <p className="text-xs text-gray-400 dark:text-gray-500 -mt-2">
                Edit Role or Company if parsing got something wrong. Uncheck to skip.
              </p>

              <div className="border border-gray-200 dark:border-gray-700 rounded-lg overflow-hidden">
                <table className="w-full text-xs">
                  <thead className="bg-gray-50 dark:bg-gray-800">
                    <tr>
                      <th className="w-8 px-2 py-2 text-center">
                        <input
                          type="checkbox"
                          checked={includedCount === previewRows.length && previewRows.length > 0}
                          onChange={(e) => toggleAll(e.target.checked)}
                          className="accent-blue-600"
                        />
                      </th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600 dark:text-gray-400">Company</th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600 dark:text-gray-400">Role</th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600 dark:text-gray-400 w-24">Date</th>
                      <th className="px-3 py-2 text-left font-medium text-gray-600 dark:text-gray-400 w-28">Action</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-800">
                    {previewRows.length === 0 ? (
                      <tr>
                        <td colSpan={5} className="px-4 py-6 text-center text-gray-400 dark:text-gray-500">
                          No applications detected. Try editing the text or switch to regex fallback.
                        </td>
                      </tr>
                    ) : (
                      previewRows.map((row, i) => (
                        <tr
                          key={i}
                          className={`transition-colors ${
                            row.include
                              ? "bg-white dark:bg-gray-900"
                              : "bg-gray-50 dark:bg-gray-900/50 opacity-50"
                          }`}
                        >
                          <td className="px-2 py-1.5 text-center">
                            <input
                              type="checkbox"
                              checked={row.include}
                              onChange={(e) => updateRow(i, "include", e.target.checked)}
                              className="accent-blue-600"
                            />
                          </td>
                          <td className="px-3 py-1.5">
                            <input
                              type="text"
                              value={row.company ?? ""}
                              onChange={(e) => updateRow(i, "company", e.target.value || null)}
                              className="w-full bg-transparent border-0 border-b border-transparent hover:border-gray-300 dark:hover:border-gray-600 focus:border-blue-500 focus:outline-none py-0.5 text-gray-800 dark:text-gray-200 font-medium"
                              placeholder="(unknown)"
                            />
                          </td>
                          <td className="px-3 py-1.5">
                            <input
                              type="text"
                              value={row.role ?? ""}
                              onChange={(e) => updateRow(i, "role", e.target.value || null)}
                              className="w-full bg-transparent border-0 border-b border-transparent hover:border-gray-300 dark:hover:border-gray-600 focus:border-blue-500 focus:outline-none py-0.5 text-gray-700 dark:text-gray-300"
                              placeholder="(unknown)"
                            />
                          </td>
                          <td className="px-3 py-1.5 text-gray-500 dark:text-gray-400 whitespace-nowrap">
                            {formatDate(row.applied_date)}
                          </td>
                          <td className="px-3 py-1.5">
                            {ACTION_META[row.predicted_action] && (
                              <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-medium ${ACTION_META[row.predicted_action].color}`}>
                                {ACTION_META[row.predicted_action].label}
                              </span>
                            )}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>

              {error && <p className="text-sm text-red-600 dark:text-red-400">Error: {error}</p>}

              <div className="flex justify-between gap-2 pt-1">
                <button
                  onClick={() => { setStep("paste"); setError(null); }}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors"
                >
                  <RefreshCw size={13} />
                  Back
                </button>
                <button
                  onClick={handleConfirm}
                  disabled={loading || includedCount === 0}
                  className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white rounded-lg text-sm font-medium transition-colors"
                >
                  {loading ? "Importing…" : `Confirm Import (${includedCount})`}
                </button>
              </div>
            </>
          )}

          {/* ── STEP: result ── */}
          {step === "result" && result && (
            <>
              <div className="flex items-center gap-8 py-3 mb-2">
                {[
                  { label: "Created",  value: result.created, color: "text-green-600 dark:text-green-400" },
                  { label: "Advanced", value: result.updated, color: "text-blue-600 dark:text-blue-400" },
                  { label: "Skipped",  value: result.skipped, color: "text-gray-500 dark:text-gray-400" },
                  ...(result.failed > 0 ? [{ label: "Failed", value: result.failed, color: "text-red-600 dark:text-red-400" }] : []),
                ].map(({ label, value, color }) => (
                  <div key={label} className="text-center">
                    <div className={`text-2xl font-bold ${color}`}>{value}</div>
                    <div className="text-xs text-gray-500 dark:text-gray-400">{label}</div>
                  </div>
                ))}
              </div>

              <div className="space-y-0.5 max-h-64 overflow-y-auto border border-gray-100 dark:border-gray-800 rounded-lg p-2">
                {result.entries.map((e, i) => (
                  <div key={i} className="flex items-center gap-2 text-xs py-1 px-1 rounded hover:bg-gray-50 dark:hover:bg-gray-800">
                    {RESULT_ICON[e.action]}
                    <span className="font-medium text-gray-800 dark:text-gray-200 truncate">{e.company || "—"}</span>
                    <span className="text-gray-400 shrink-0">·</span>
                    <span className="text-gray-600 dark:text-gray-400 truncate min-w-0">{e.role || "—"}</span>
                    <span className="ml-auto text-gray-400 shrink-0 capitalize">{e.action}</span>
                  </div>
                ))}
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <button onClick={handleReset} className="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 border border-gray-300 dark:border-gray-600 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors">
                  Import more
                </button>
                <button onClick={onClose} className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors">
                  Done
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
