import { useCallback, useEffect, useState } from "react";
import { GitMerge, RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { formatDate } from "../utils/formatters";
import MergeDialog from "./MergeDialog";
import MergeNotice from "./MergeNotice";
import { undoConflictMessages } from "../utils/mergeErrors";
import { APPLICATION_METHODS, SOURCE_PORTALS } from "../utils/constants";

const selectClass = "rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-100";

function RecordSummary({ app }) {
  return (
    <div className="min-w-0">
      <p className="truncate font-medium text-gray-900 dark:text-gray-100">{app.company || "Unknown company"}</p>
      <p className="truncate text-sm text-gray-500 dark:text-gray-400">{app.role || "Unknown role"}</p>
      <p className="text-xs text-gray-400">{app.source_portal} · {app.current_status} · #{app.id}</p>
    </div>
  );
}

function MergeHistoryRow({ operation, onUndone }) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);
  const undone = operation.status === "undone";

  const undo = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await api.undoMerge(operation.id);
      onUndone();
    } catch (err) {
      setFailure(undoConflictMessages(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className="py-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 text-sm">
          <p className="font-medium text-gray-900 dark:text-gray-100">
            Merge #{operation.id} · kept #{operation.survivor_application_id}
            {undone && <span className="ml-2 rounded bg-gray-100 px-1.5 py-0.5 text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">Undone</span>}
          </p>
          <ul className="text-xs text-gray-500 dark:text-gray-400">
            {operation.applications.map((app) => (
              <li key={app.id}>
                #{app.id} {app.company || "Unknown company"} — {app.role || "Unknown role"}
                {app.survivor ? " (kept)" : " (merged)"}
              </li>
            ))}
          </ul>
          <p className="text-xs text-gray-400">
            {formatDate(operation.created_at)}
            {operation.reason ? ` · ${operation.reason}` : ""}
          </p>
        </div>
        {!undone && (
          <button
            type="button"
            onClick={undo}
            disabled={busy}
            aria-label={`Undo merge #${operation.id}`}
            className="rounded-md border border-gray-300 px-2.5 py-1 text-xs font-medium hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:hover:bg-gray-800"
          >
            {busy ? "Undoing…" : "Undo"}
          </button>
        )}
      </div>
      {failure && (
        <div role="alert" className="mt-2 rounded-md bg-red-50 p-2 text-xs text-red-700 dark:bg-red-900/30 dark:text-red-300">
          <p>{failure.message}</p>
          {failure.conflicts.length > 0 && (
            <ul className="list-disc pl-5">
              {failure.conflicts.map((c) => <li key={c}>{c}</li>)}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

export default function DataQualityPanel() {
  const [duplicates, setDuplicates] = useState([]);
  const [unknown, setUnknown] = useState([]);
  const [merges, setMerges] = useState([]);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);
  const [mergeIds, setMergeIds] = useState(null);
  const [mergeOperation, setMergeOperation] = useState(null);

  const load = useCallback(() => {
    setError(null);
    Promise.all([
      api.listDuplicateCandidates(),
      api.listApplications({ source_portal: "Direct/Unknown", page_size: 500 }),
      api.listMerges({ page_size: 20 }),
    ])
      .then(([pairs, apps, history]) => {
        setDuplicates(pairs);
        setUnknown(apps.items ?? []);
        setMerges(history.items ?? []);
      })
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    // Initial remote load populates both quality queues.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  const dismiss = async (pair) => {
    setBusy(`dismiss-${pair.pair_key}`);
    try {
      await api.dismissDuplicate([pair.primary.id, pair.duplicate.id]);
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  const classify = async (app, field, value) => {
    setBusy(`classify-${app.id}`);
    try {
      await api.updateApplication(app.id, { [field]: value });
      load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Data Quality</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">Review likely duplicates and clean up unattributed applications.</p>
        </div>
        <button onClick={load} className="inline-flex items-center gap-2 rounded-lg border border-gray-300 px-3 py-2 text-sm dark:border-gray-600">
          <RefreshCw size={14} /> Refresh
        </button>
      </div>
      {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      {mergeOperation && (
        <MergeNotice key={mergeOperation.id} operation={mergeOperation} onUndone={load} onDismiss={() => setMergeOperation(null)} />
      )}
      {mergeIds && (
        <MergeDialog
          applicationIds={mergeIds}
          onClose={() => setMergeIds(null)}
          onMerged={(operation) => {
            setMergeIds(null);
            setMergeOperation(operation);
            load();
          }}
        />
      )}

      <section className="rounded-xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900">
        <h3 className="font-semibold text-gray-900 dark:text-gray-100">Possible duplicates <span className="text-gray-400">({duplicates.length})</span></h3>
        <p className="mb-4 text-xs text-gray-500">Suggestions only — nothing merges until you review and confirm. Merging keeps every email, thread and status change, deletes nothing, and can be undone.</p>
        {duplicates.length === 0 ? <p className="text-sm text-gray-500">No high-confidence duplicate pairs found.</p> : (
          <div className="divide-y divide-gray-100 dark:divide-gray-800">
            {duplicates.map((pair) => (
              <div key={`${pair.primary.id}-${pair.duplicate.id}`} className="grid gap-3 py-4 md:grid-cols-[1fr_auto_1fr_auto] md:items-center">
                <RecordSummary app={pair.primary} />
                <GitMerge size={18} className="text-gray-400" aria-hidden="true" />
                <RecordSummary app={pair.duplicate} />
                <div className="flex flex-wrap gap-2">
                  <button type="button" disabled={busy != null} onClick={() => setMergeIds([pair.primary.id, pair.duplicate.id])} className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
                    Review merge · {pair.score}%
                  </button>
                  <button type="button" disabled={busy != null} onClick={() => dismiss(pair)} className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200">
                    {busy === `dismiss-${pair.pair_key}` ? "Saving…" : "Not a duplicate"}
                  </button>
                </div>
                <p className="text-xs text-gray-400 md:col-span-4">{pair.reasons.join(" · ")}</p>
              </div>
            ))}
          </div>
        )}
      </section>

      <section aria-labelledby="merge-history-heading" className="rounded-xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900">
        <h3 id="merge-history-heading" className="font-semibold text-gray-900 dark:text-gray-100">Merge history <span className="text-gray-400">({merges.length})</span></h3>
        <p className="mb-2 text-xs text-gray-500">Undo restores the original records exactly. If something changed after the merge, undo stops and explains instead of overwriting it.</p>
        {merges.length === 0 ? <p className="text-sm text-gray-500">No merges yet.</p> : (
          <ul className="divide-y divide-gray-100 dark:divide-gray-800">
            {merges.map((op) => <MergeHistoryRow key={op.id} operation={op} onUndone={load} />)}
          </ul>
        )}
      </section>

      <section className="rounded-xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-900">
        <h3 className="font-semibold text-gray-900 dark:text-gray-100">Direct/Unknown cleanup <span className="text-gray-400">({unknown.length})</span></h3>
        <p className="mb-4 text-xs text-gray-500">Assign the real source and method so channel comparisons become meaningful.</p>
        {unknown.length === 0 ? <p className="text-sm text-gray-500">All applications have an attributed source.</p> : (
          <div className="space-y-3">
            {unknown.map((app) => (
              <div key={app.id} className="grid gap-2 rounded-lg bg-gray-50 p-3 dark:bg-gray-800 md:grid-cols-[1fr_180px_180px] md:items-center">
                <RecordSummary app={app} />
                <select aria-label={`Source for ${app.company || app.id}`} className={selectClass} defaultValue="" onChange={(event) => event.target.value && classify(app, "source_portal", event.target.value)} disabled={busy != null}>
                  <option value="">Choose source…</option>
                  {SOURCE_PORTALS.filter((source) => source !== "Direct/Unknown").map((source) => <option key={source} value={source}>{source}</option>)}
                </select>
                <select aria-label={`Method for ${app.company || app.id}`} className={selectClass} value={app.application_method || "Unknown"} onChange={(event) => classify(app, "application_method", event.target.value)} disabled={busy != null}>
                  {APPLICATION_METHODS.map((method) => <option key={method} value={method}>{method}</option>)}
                </select>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
