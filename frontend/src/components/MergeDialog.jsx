import { useCallback, useEffect, useMemo, useState } from "react";
import { GitMerge, X, AlertTriangle, ShieldCheck, Loader2 } from "lucide-react";
import { api } from "../api/client";
import { useModalA11y } from "../hooks/useModalA11y";
import { formatDate } from "../utils/formatters";

const FIELD_LABELS = {
  company: "Company",
  role: "Role",
  source_portal: "Source",
  application_method: "Application method",
  job_url: "Job link",
  external_job_id: "Job ID",
  applied_date: "Applied date",
  current_status: "Status",
  withdraw_reason: "Withdraw reason",
  is_false_positive: "Marked not a job",
};

const RULE_TEXT = {
  earliest: "earliest date kept",
  only_value: "only one value",
  empty: "empty on all records",
  survivor_preferred: "from the record you keep",
  most_advanced: "most advanced status",
};

function newIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID().replaceAll("-", "");
  return `${Date.now()}${Math.random().toString(16).slice(2)}`;
}

function display(name, value) {
  if (value === null || value === undefined || value === "") return "—";
  if (name === "applied_date") return formatDate(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

/**
 * Human-controlled merge: preview two or more applications, pick the record to keep,
 * resolve every conflicting field, and confirm. Nothing is deleted — merged records are
 * hidden and the merge can be undone.
 */
export default function MergeDialog({ applicationIds, onClose, onMerged }) {
  const dialogRef = useModalA11y(onClose);
  const [survivorId, setSurvivorId] = useState(null);
  const [preview, setPreview] = useState(null);
  const [choices, setChoices] = useState({});
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [stale, setStale] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);
  const [idempotencyKey] = useState(newIdempotencyKey);

  useEffect(() => {
    let cancelled = false;
    api
      .previewMerge(applicationIds, survivorId)
      .then((result) => {
        if (cancelled) return;
        setPreview(result);
        // Conflicts start unresolved: the user must pick every value explicitly.
        setChoices({});
        setStale(false);
        setError(null);
      })
      .catch((err) => {
        if (!cancelled) setError(err.detail ?? "Could not load the merge preview.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [applicationIds, survivorId, reloadToken]);

  const chooseSurvivor = (id) => {
    setLoading(true);
    setSurvivorId(id);
  };

  const refresh = useCallback(() => {
    setLoading(true);
    setReloadToken((n) => n + 1);
  }, []);

  const appsById = useMemo(
    () => Object.fromEntries((preview?.applications ?? []).map((a) => [a.id, a])),
    [preview],
  );
  const unresolved = (preview?.conflicts ?? []).filter((name) => choices[name] == null);
  const canConfirm =
    preview && preview.safe && !stale && !loading && !submitting && unresolved.length === 0;

  const confirm = async () => {
    if (!canConfirm) return;
    setSubmitting(true);
    setError(null);
    try {
      const operation = await api.executeMerge({
        application_ids: preview.application_ids,
        survivor_id: preview.survivor_id,
        field_choices: choices,
        preview_token: preview.preview_token,
        idempotency_key: idempotencyKey,
      });
      onMerged(operation);
    } catch (err) {
      if (err.status === 409) {
        setStale(true);
        setError(err.detail ?? "These applications changed since the preview.");
      } else if (err.status === 422) {
        setError(err.detail ?? "Some choices are missing or invalid.");
      } else {
        setError("The merge failed. Nothing was changed. Try again.");
      }
    } finally {
      setSubmitting(false);
    }
  };

  const conflictFields = preview?.fields.filter((f) => f.conflict) ?? [];
  const otherFields = preview?.fields.filter((f) => !f.conflict) ?? [];

  return (
    <div className="fixed inset-0 z-50 flex items-start sm:items-center justify-center bg-black/40 p-2 sm:p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="merge-dialog-title"
        aria-describedby="merge-dialog-note"
        tabIndex={-1}
        className="w-full max-w-3xl max-h-[95vh] overflow-y-auto rounded-xl bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 shadow-xl"
      >
        <div className="sticky top-0 flex items-center justify-between gap-3 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-4 sm:px-6 py-4">
          <h2 id="merge-dialog-title" className="flex items-center gap-2 text-lg font-semibold text-gray-900 dark:text-gray-100">
            <GitMerge size={18} aria-hidden="true" />
            Merge {applicationIds.length} applications
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close merge dialog"
            className="rounded-md p-1.5 text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-800"
          >
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        <div className="space-y-5 px-4 sm:px-6 py-5">
          <p id="merge-dialog-note" className="flex items-start gap-2 rounded-lg bg-blue-50 dark:bg-blue-900/30 px-3 py-2 text-sm text-blue-900 dark:text-blue-100">
            <ShieldCheck size={16} className="mt-0.5 shrink-0" aria-hidden="true" />
            <span>
              Nothing is permanently deleted. The records you don&apos;t keep are hidden, every
              email, status change and milestone moves to the record you keep, and you can undo
              the merge.
            </span>
          </p>

          {loading && (
            <p role="status" className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
              <Loader2 size={16} className="animate-spin" aria-hidden="true" />
              Loading preview…
            </p>
          )}

          {error && (
            <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/30 dark:text-red-300">
              <p>{error}</p>
              {stale && (
                <button
                  type="button"
                  onClick={refresh}
                  className="mt-2 rounded-md border border-red-300 px-2.5 py-1 text-xs font-medium hover:bg-red-100 dark:hover:bg-red-900/50"
                >
                  Refresh preview
                </button>
              )}
            </div>
          )}

          {preview && !preview.safe && (
            <div role="alert" className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/30 dark:text-red-300">
              <p className="font-medium">This merge can&apos;t be done right now:</p>
              <ul className="list-disc pl-5">
                {preview.blocking.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>
            </div>
          )}

          {preview && (
            <>
              <fieldset>
                <legend className="mb-2 text-sm font-semibold text-gray-900 dark:text-gray-100">
                  Record to keep
                </legend>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  {preview.applications.map((app) => {
                    const keep = app.id === preview.survivor_id;
                    return (
                      <label
                        key={app.id}
                        className={`flex cursor-pointer gap-3 rounded-lg border p-3 text-sm ${
                          keep
                            ? "border-blue-500 bg-blue-50 dark:bg-blue-900/20"
                            : "border-gray-200 dark:border-gray-700"
                        }`}
                      >
                        <input
                          type="radio"
                          name="survivor"
                          value={app.id}
                          checked={keep}
                          disabled={loading || submitting}
                          onChange={() => chooseSurvivor(app.id)}
                          aria-label={`Keep #${app.id} ${app.company ?? ""} ${app.role ?? ""}`}
                          className="mt-1"
                        />
                        <span className="min-w-0">
                          <span className="block font-medium text-gray-900 dark:text-gray-100">
                            #{app.id} {app.company || "Unknown company"}
                          </span>
                          <span className="block text-gray-600 dark:text-gray-300">{app.role || "Unknown role"}</span>
                          <span className="block text-xs text-gray-500 dark:text-gray-400">
                            {app.source_portal} · {app.current_status} · {formatDate(app.applied_date)}
                          </span>
                          <span className="block text-xs text-gray-500 dark:text-gray-400">
                            {app.evidence_count} emails · {app.status_history_count} status changes ·{" "}
                            {app.event_count} milestones
                          </span>
                          <span className={`mt-1 inline-block rounded px-1.5 py-0.5 text-xs font-medium ${
                            keep
                              ? "bg-blue-600 text-white"
                              : "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300"
                          }`}>
                            {keep ? "Kept" : "Will be hidden (merged)"}
                          </span>
                          {app.id === preview.default_survivor_id && !keep && (
                            <span className="ml-1 text-xs text-gray-500">recommended</span>
                          )}
                        </span>
                      </label>
                    );
                  })}
                </div>
              </fieldset>

              {preview.warnings.length > 0 && (
                <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
                  <p className="flex items-center gap-1.5 font-medium">
                    <AlertTriangle size={14} aria-hidden="true" /> Check before merging
                  </p>
                  <ul className="list-disc pl-5">
                    {preview.warnings.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                </div>
              )}

              {conflictFields.length > 0 && (
                <section aria-labelledby="merge-conflicts-heading">
                  <h3 id="merge-conflicts-heading" className="mb-2 text-sm font-semibold text-gray-900 dark:text-gray-100">
                    Conflicting fields — choose the value to keep ({conflictFields.length})
                  </h3>
                  <div className="space-y-3">
                    {conflictFields.map((field) => (
                      <fieldset key={field.name} className="rounded-lg border border-gray-200 dark:border-gray-700 p-3">
                        <legend className="px-1 text-sm font-medium text-gray-800 dark:text-gray-200">
                          {FIELD_LABELS[field.name] ?? field.name}
                        </legend>
                        <div className="flex flex-col gap-1.5">
                          {Object.entries(field.values).map(([appId, value]) => (
                            <label key={appId} className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                              <input
                                type="radio"
                                name={`field-${field.name}`}
                                value={appId}
                                checked={choices[field.name] === Number(appId)}
                                disabled={submitting}
                                onChange={() => setChoices((c) => ({ ...c, [field.name]: Number(appId) }))}
                              />
                              <span>
                                {display(field.name, value)}{" "}
                                <span className="text-xs text-gray-500">
                                  from #{appId}
                                  {appsById[appId] ? ` ${appsById[appId].company ?? ""}` : ""}
                                  {field.proposed_from === Number(appId) ? " (suggested)" : ""}
                                </span>
                              </span>
                            </label>
                          ))}
                        </div>
                      </fieldset>
                    ))}
                  </div>
                </section>
              )}

              <section aria-labelledby="merge-result-heading">
                <h3 id="merge-result-heading" className="mb-2 text-sm font-semibold text-gray-900 dark:text-gray-100">
                  Kept automatically
                </h3>
                <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-2">
                  {otherFields.map((field) => (
                    <div key={field.name} className="flex justify-between gap-2 border-b border-gray-100 dark:border-gray-800 py-1">
                      <dt className="text-gray-500 dark:text-gray-400">{FIELD_LABELS[field.name] ?? field.name}</dt>
                      <dd className="text-right text-gray-900 dark:text-gray-100">
                        {display(field.name, field.proposed)}
                        <span className="block text-xs text-gray-500">{RULE_TEXT[field.rule] ?? field.rule}</span>
                      </dd>
                    </div>
                  ))}
                </dl>
              </section>

              <p className="text-sm text-gray-600 dark:text-gray-300" data-testid="merge-counts">
                {preview.relink.evidence.length} emails, {preview.relink.status_history.length} status
                changes and {preview.relink.events.length} milestones move to #{preview.survivor_id}.
                {" "}
                {preview.counts.status_history_superseded + preview.counts.events_superseded > 0 &&
                  `${preview.counts.status_history_superseded + preview.counts.events_superseded} duplicate entries will be hidden so nothing is counted twice.`}
              </p>
            </>
          )}
        </div>

        <div className="sticky bottom-0 flex flex-col-reverse gap-2 border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-4 sm:px-6 py-3 sm:flex-row sm:justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-2 text-sm text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={confirm}
            disabled={!canConfirm}
            aria-describedby={unresolved.length ? "merge-unresolved" : undefined}
            className="rounded-lg bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {submitting ? "Merging…" : "Confirm merge"}
          </button>
          {unresolved.length > 0 && (
            <span id="merge-unresolved" className="text-xs text-red-600 sm:self-center">
              Choose a value for: {unresolved.map((n) => FIELD_LABELS[n] ?? n).join(", ")}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
