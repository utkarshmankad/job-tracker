import { useState } from "react";
import { api } from "../api/client";
import { undoConflictMessages } from "../utils/mergeErrors";

/** Success message after a merge, with an Undo action and safe-undo conflict display. */
export default function MergeNotice({ operation, onUndone, onDismiss }) {
  const [busy, setBusy] = useState(false);
  const [undone, setUndone] = useState(false);
  const [failure, setFailure] = useState(null);
  const sources = operation.source_application_ids ?? [];

  const undo = async () => {
    setBusy(true);
    setFailure(null);
    try {
      await api.undoMerge(operation.id);
      setUndone(true);
      onUndone?.();
    } catch (err) {
      setFailure(undoConflictMessages(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-2 rounded-lg border border-green-200 bg-green-50 px-4 py-2.5 text-sm text-green-900 dark:border-green-800 dark:bg-green-900/30 dark:text-green-100"
    >
      <span className="flex-1">
        {undone
          ? `Merge undone — ${sources.length + 1} applications are active again.`
          : `Merged ${sources.length + 1} applications into #${operation.survivor_application_id}. Nothing was deleted.`}
      </span>
      {!undone && (
        <button
          type="button"
          onClick={undo}
          disabled={busy}
          className="rounded-md border border-green-300 px-2.5 py-1 text-xs font-medium hover:bg-green-100 disabled:opacity-50 dark:border-green-700 dark:hover:bg-green-900/50"
        >
          {busy ? "Undoing…" : "Undo"}
        </button>
      )}
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss merge message"
        className="rounded-md px-2 py-1 text-xs hover:bg-green-100 dark:hover:bg-green-900/50"
      >
        Dismiss
      </button>
      {failure && (
        <div role="alert" className="w-full text-xs text-red-700 dark:text-red-300">
          <p>{failure.message}</p>
          {failure.conflicts.length > 0 && (
            <ul className="list-disc pl-5">
              {failure.conflicts.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
