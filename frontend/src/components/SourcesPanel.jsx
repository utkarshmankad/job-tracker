import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, CheckCircle2, CircleSlash, Clipboard, ClipboardCheck, Clock, KeyRound,
  Loader2, LogIn, RefreshCw, ShieldAlert, XCircle,
} from "lucide-react";
import { api } from "../api/client";
import { formatDate } from "../utils/formatters";

// Display names for sources the server may mention before its catalog has loaded. Which
// sources can be collected comes only from the server (/collection/source-catalog).
const SOURCE_LABEL = {
  linkedin: "LinkedIn",
  naukri: "Naukri",
  indeed: "Indeed",
  instahyre: "Instahyre",
  careernet: "CareerNet",
};

// Every state has an icon AND words — never colour alone.
const RUN_STATE = {
  succeeded: { label: "Collected", Icon: CheckCircle2, tone: "green" },
  partial: { label: "Partly collected", Icon: AlertTriangle, tone: "amber" },
  failed: { label: "Failed", Icon: XCircle, tone: "red" },
  signed_out: { label: "Signed out", Icon: LogIn, tone: "amber" },
  challenged: { label: "Challenged", Icon: ShieldAlert, tone: "amber" },
  unsupported: { label: "Unsupported", Icon: CircleSlash, tone: "gray" },
  running: { label: "Running", Icon: Clock, tone: "blue" },
};
const DECISION = {
  created: "Imported",
  linked: "Linked",
  review: "Needs review",
  unchanged: "Already known",
  ignored: "Ignored",
  error: "Failed",
  pending: "Pending",
};
const COLLECTOR_STATE = {
  pending_enrollment: "Waiting for setup",
  active: "Connected",
  revoked: "Revoked",
};
const TONES = {
  green: "bg-green-50 text-green-800 dark:bg-green-900/30 dark:text-green-200",
  amber: "bg-amber-50 text-amber-900 dark:bg-amber-900/30 dark:text-amber-200",
  red: "bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-200",
  blue: "bg-blue-50 text-blue-800 dark:bg-blue-900/30 dark:text-blue-200",
  gray: "bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300",
};
const card = "rounded-xl border border-gray-200 bg-white p-4 sm:p-5 dark:border-gray-700 dark:bg-gray-900";
const button =
  "inline-flex items-center gap-1.5 rounded-md border border-gray-300 px-2.5 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-800";

function humanize(value) {
  return value ? value.replaceAll("_", " ") : "";
}

function StateBadge({ status }) {
  if (!status) return <span className="text-xs text-gray-500">Never run</span>;
  const state = RUN_STATE[status] ?? { label: humanize(status), Icon: Clock, tone: "gray" };
  const { Icon } = state;
  return (
    <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-medium ${TONES[state.tone]}`}>
      <Icon size={12} aria-hidden="true" />
      {state.label}
    </span>
  );
}

function Metric({ label, value }) {
  return (
    <div className="rounded-lg bg-gray-50 p-3 dark:bg-gray-800">
      <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
      <dd className="text-lg font-semibold text-gray-900 dark:text-gray-100">{value ?? 0}</dd>
    </div>
  );
}

function RunDetail({ runId }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let cancelled = false;
    api
      .getCollectionRun(runId)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => !cancelled && setError("Could not load this run."));
    return () => {
      cancelled = true;
    };
  }, [runId]);
  if (error) return <p role="alert" className="text-sm text-red-600">{error}</p>;
  if (!detail) return <p className="text-sm text-gray-500">Loading run…</p>;
  if (detail.observations.length === 0)
    return <p className="text-sm text-gray-500">No observations were sent in this run.</p>;
  return (
    <ul className="divide-y divide-gray-100 text-sm dark:divide-gray-800">
      {detail.observations.map((o) => (
        <li key={o.id} className="flex flex-wrap items-baseline justify-between gap-2 py-2">
          <span className="min-w-0">
            <span className="font-medium text-gray-900 dark:text-gray-100">{o.company}</span>
            {o.role && <span className="text-gray-600 dark:text-gray-300"> — {o.role}</span>}
            <span className="block text-xs text-gray-500">
              site status: {humanize(o.status)} · {o.extraction === "verified" ? "verified selectors" : `${o.extraction} selectors`}
            </span>
          </span>
          <span className="text-xs text-gray-700 dark:text-gray-300">
            {DECISION[o.decision] ?? humanize(o.decision)}
            {o.decision_reason && <span className="text-gray-500"> ({humanize(o.decision_reason)})</span>}
            {o.application_id && <span> · #{o.application_id}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

function ReviewItem({ item, onDone }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const candidates = item.evidence.resolution?.candidates?.filter((c) => c.application) ?? [];
  const act = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onDone();
    } catch {
      setError("That did not work. Refresh and try again.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <li className="py-3">
      <p className="text-sm">
        <span className="font-medium text-gray-900 dark:text-gray-100">{item.company}</span>
        {item.role && <span className="text-gray-600 dark:text-gray-300"> — {item.role}</span>}
      </p>
      <p className="text-xs text-gray-500">
        {SOURCE_LABEL[item.source_key] ?? item.source_key} · site status {item.raw_status || humanize(item.status)}
        {item.applied_on && ` · applied ${formatDate(item.applied_on)}`} · why: {humanize(item.evidence.review_reason)}
      </p>
      <div className="mt-2 flex flex-wrap gap-2">
        {candidates.map((c) => (
          <button
            key={c.application_id}
            type="button"
            disabled={busy}
            className={button}
            onClick={() => act(() => api.acceptEvidence(item.evidence.id, c.application_id))}
          >
            Same as #{c.application_id} {c.application.company} — {c.application.role ?? "no role"}
          </button>
        ))}
        <button
          type="button"
          disabled={busy}
          className={button}
          onClick={() => act(() => api.createApplicationFromEvidence(item.evidence.id))}
        >
          Create application
        </button>
        <button
          type="button"
          disabled={busy}
          className={button}
          onClick={() => act(() => api.dismissEvidence(item.evidence.id))}
        >
          Not an application
        </button>
      </div>
      {error && <p role="alert" className="mt-1 text-xs text-red-600">{error}</p>}
    </li>
  );
}

function CollectorRow({ collector, onChanged, onSetup }) {
  const unsupported = collector.unsupported_scopes ?? [];
  const [confirm, setConfirm] = useState(null); // "rotate" | "revoke" | null
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async (action) => {
    setBusy(true);
    setError(null);
    try {
      if (action === "rotate") onSetup(await api.rotateCollector(collector.id));
      else await api.revokeCollector(collector.id);
      setConfirm(null);
      onChanged();
    } catch {
      setError("That did not work. Refresh and try again.");
    } finally {
      setBusy(false);
    }
  };
  const revoked = collector.state === "revoked";
  const canRotate = unsupported.length === 0;
  return (
    <li className="flex flex-wrap items-start justify-between gap-2 py-3">
      <div className="min-w-0 text-sm">
        <p className="font-medium text-gray-900 dark:text-gray-100">
          {collector.name} <span className="font-mono text-xs text-gray-500">{collector.token_hint}</span>
        </p>
        <p className="text-xs text-gray-500">
          {COLLECTOR_STATE[collector.state] ?? collector.state} · sources:{" "}
          {collector.scopes
            .map((s) => `${SOURCE_LABEL[s] ?? s}${unsupported.includes(s) ? " (unsupported)" : ""}`)
            .join(", ")}
          {collector.last_used_at && ` · last used ${formatDate(collector.last_used_at)}`}
        </p>
        {!revoked && unsupported.length > 0 && (
          <p className="text-xs text-amber-700 dark:text-amber-300">
            Includes sources that are no longer supported. It cannot be rotated or collect them; revoke it and
            set up a new collector.
          </p>
        )}
      </div>
      {!revoked && (
        <div className="flex flex-wrap items-center gap-2">
          {confirm ? (
            <>
              <span className="text-xs text-gray-600 dark:text-gray-300">
                {confirm === "revoke"
                  ? "Revoke permanently? The collector stops working at once."
                  : "Rotate? The current credential stops working at once."}
              </span>
              <button type="button" className={button} disabled={busy} onClick={() => run(confirm)}>
                {busy ? "Working…" : `Yes, ${confirm}`}
              </button>
              <button type="button" className={button} disabled={busy} onClick={() => setConfirm(null)}>
                Cancel
              </button>
            </>
          ) : (
            <>
              {canRotate && (
                <button type="button" className={button} onClick={() => setConfirm("rotate")}>
                  <KeyRound size={14} aria-hidden="true" /> Rotate
                </button>
              )}
              <button type="button" className={button} onClick={() => setConfirm("revoke")}>
                Revoke
              </button>
            </>
          )}
        </div>
      )}
      {error && <p role="alert" className="w-full text-xs text-red-600">{error}</p>}
    </li>
  );
}

function SetupPanel({ setup, onClose }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(setup.command);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div role="status" className="mt-3 rounded-lg border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900 dark:border-blue-800 dark:bg-blue-900/30 dark:text-blue-100">
      <p className="font-medium">Run this on the computer with your signed-in browser.</p>
      <p className="text-xs">
        The setup code works once and expires at {new Date(setup.expires_at).toLocaleTimeString()}.
        The credential it creates goes straight into that computer's keychain and is never shown here.
      </p>
      <code className="mt-2 block overflow-x-auto whitespace-pre rounded bg-white p-2 font-mono text-xs dark:bg-gray-900">
        {setup.command}
      </code>
      <div className="mt-2 flex gap-2">
        <button type="button" className={button} onClick={copy}>
          {copied ? <ClipboardCheck size={14} aria-hidden="true" /> : <Clipboard size={14} aria-hidden="true" />}
          {copied ? "Copied" : "Copy command"}
        </button>
        <button type="button" className={button} onClick={onClose}>
          Done
        </button>
      </div>
    </div>
  );
}

function SourceChoice({ source, checked, onToggle }) {
  const reasonId = `source-reason-${source.key}`;
  return (
    <div className="min-w-0">
      <label
        className={`inline-flex items-center gap-1.5 text-sm ${source.supported ? "" : "text-gray-400 dark:text-gray-500"}`}
      >
        <input
          type="checkbox"
          checked={checked}
          disabled={!source.supported}
          aria-describedby={source.supported ? undefined : reasonId}
          onChange={() => onToggle(source.key)}
        />
        {source.label}
        <span
          className={`rounded px-1.5 py-0.5 text-xs font-medium ${TONES[source.supported ? "green" : "gray"]}`}
        >
          {source.supported ? "Live verified" : "Unsupported"}
        </span>
      </label>
      {!source.supported && source.reason && (
        <p id={reasonId} className="ml-5 text-xs text-gray-500 dark:text-gray-400">
          {source.reason}
        </p>
      )}
    </div>
  );
}

function NewCollectorForm({ catalog, onCreated }) {
  const supported = catalog.filter((s) => s.supported).map((s) => s.key);
  const [name, setName] = useState("My laptop");
  const [scopes, setScopes] = useState(supported.includes("indeed") ? ["indeed"] : []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const toggle = (key) => {
    if (!supported.includes(key)) return;
    setScopes((current) => (current.includes(key) ? current.filter((s) => s !== key) : [...current, key]));
  };
  const submit = async (event) => {
    event.preventDefault();
    if (scopes.length === 0) {
      setError("Choose at least one source.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onCreated(await api.createCollector(name.trim(), scopes));
    } catch {
      setError("The collector could not be created. Check the name (letters, digits, spaces, . _ -).");
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="mt-3 space-y-3" aria-label="Set up a collector">
      <div>
        <label htmlFor="collector-name" className="block text-xs text-gray-600 dark:text-gray-400">
          Collector name
        </label>
        <input
          id="collector-name"
          value={name}
          maxLength={60}
          onChange={(e) => setName(e.target.value)}
          className="mt-1 w-full max-w-xs rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800"
        />
      </div>
      <fieldset>
        <legend className="text-xs text-gray-600 dark:text-gray-400">Sources it may collect</legend>
        <div className="mt-1 flex flex-col gap-2">
          {catalog.map((s) => (
            <SourceChoice key={s.key} source={s} checked={scopes.includes(s.key)} onToggle={toggle} />
          ))}
        </div>
      </fieldset>
      {error && <p role="alert" className="text-xs text-red-600">{error}</p>}
      <button type="submit" disabled={busy} className={button}>
        {busy ? "Creating…" : "Create setup command"}
      </button>
    </form>
  );
}

/**
 * Read-only collection from job sites' application histories. Nothing on this page acts on
 * a job site: collection runs on the user's own computer with a scoped credential.
 */
export default function SourcesPanel() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [openRun, setOpenRun] = useState(null);
  const [setup, setSetup] = useState(null);
  const [showForm, setShowForm] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    Promise.all([
      api.listCollectionSources(),
      api.listCollectionRuns({ limit: 20 }),
      api.getCollectionMetrics(),
      api.listCollectors(),
      api.listCollectionReview(),
      api.getCollectionSourceCatalog(),
    ])
      .then(([sources, runs, metrics, collectors, review, catalog]) =>
        setData({ sources, runs, metrics, collectors, review, catalog }),
      )
      .catch(() => setError("Collection status could not be loaded."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    // Initial remote load of collection state.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  const totals = data?.metrics?.observation_totals ?? {};
  const attention = data?.sources.filter((s) => s.needs_attention) ?? [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Sources</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Applications collected from job sites' own history pages by a read-only collector on your computer.
            It never applies, withdraws, messages or changes anything on those sites, and it stops at any
            sign-in, CAPTCHA or verification page.
          </p>
        </div>
        <button type="button" onClick={load} className={button} disabled={loading}>
          {loading ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <RefreshCw size={14} aria-hidden="true" />}
          Refresh
        </button>
      </div>

      {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}
      {loading && !data && <p role="status" className="text-sm text-gray-500">Loading collection status…</p>}

      {data && (
        <>
          {attention.length > 0 && (
            <div role="alert" className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
              <p className="font-medium">Needs your attention</p>
              <ul className="list-disc pl-5">
                {attention.map((s) => (
                  <li key={s.id}>
                    {SOURCE_LABEL[s.source_key] ?? s.source_key} ({s.account_label}): {s.attention_message}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <section aria-labelledby="collection-totals" className={card}>
            <h3 id="collection-totals" className="mb-3 font-semibold text-gray-900 dark:text-gray-100">Totals</h3>
            <dl className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              <Metric label="Observed" value={totals.observations} />
              <Metric label="Imported (new)" value={totals.created} />
              <Metric label="Linked" value={totals.linked} />
              <Metric label="Needs review" value={data.review.length} />
              <Metric label="Already known" value={totals.unchanged} />
              <Metric label="Failed items" value={totals.errors} />
            </dl>
          </section>

          <section aria-labelledby="collection-sources" className={card}>
            <h3 id="collection-sources" className="mb-2 font-semibold text-gray-900 dark:text-gray-100">Configured sources</h3>
            {data.sources.length === 0 ? (
              <p className="text-sm text-gray-500">No collection has run yet. Set up a collector below.</p>
            ) : (
              <ul className="divide-y divide-gray-100 dark:divide-gray-800">
                {data.sources.map((s) => (
                  <li key={s.id} className="flex flex-wrap items-center justify-between gap-2 py-3 text-sm">
                    <span className="min-w-0">
                      <span className="font-medium text-gray-900 dark:text-gray-100">{SOURCE_LABEL[s.source_key] ?? s.source_key}</span>
                      <span className="text-gray-500"> · {s.account_label}</span>
                      <span className="block text-xs text-gray-500">
                        Last success: {s.last_success_at ? formatDate(s.last_success_at) : "never"} · last attempt:{" "}
                        {s.last_attempt_at ? formatDate(s.last_attempt_at) : "never"} · {s.items} applications observed
                      </span>
                    </span>
                    <StateBadge status={s.last_status} />
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section aria-labelledby="collection-review" className={card}>
            <h3 id="collection-review" className="font-semibold text-gray-900 dark:text-gray-100">
              Needs review <span className="text-gray-400">({data.review.length})</span>
            </h3>
            <p className="mb-2 text-xs text-gray-500">
              Collected applications the tracker would not decide on its own. Nothing is merged automatically.
            </p>
            {data.review.length === 0 ? (
              <p className="text-sm text-gray-500">Nothing waiting.</p>
            ) : (
              <ul className="divide-y divide-gray-100 dark:divide-gray-800">
                {data.review.map((item) => (
                  <ReviewItem key={item.evidence.id} item={item} onDone={load} />
                ))}
              </ul>
            )}
          </section>

          <section aria-labelledby="collection-runs" className={card}>
            <h3 id="collection-runs" className="mb-2 font-semibold text-gray-900 dark:text-gray-100">Recent runs</h3>
            {data.runs.length === 0 ? (
              <p className="text-sm text-gray-500">No runs yet.</p>
            ) : (
              <ul className="divide-y divide-gray-100 dark:divide-gray-800">
                {data.runs.map((run) => {
                  const open = openRun === run.id;
                  return (
                    <li key={run.id} className="py-3 text-sm">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <span className="min-w-0">
                          <span className="font-medium text-gray-900 dark:text-gray-100">{SOURCE_LABEL[run.source_key] ?? run.source_key}</span>{" "}
                          <StateBadge status={run.status} />
                          <span className="block text-xs text-gray-500">
                            {formatDate(run.started_at)} · seen {run.items_seen} · imported {run.created_count} · linked{" "}
                            {run.linked_count} · review {run.review_count} · already known {run.unchanged_count} · failed{" "}
                            {run.error_count}
                          </span>
                          {run.error_message && <span className="block text-xs text-amber-700 dark:text-amber-300">{run.error_message}</span>}
                        </span>
                        <button
                          type="button"
                          className={button}
                          aria-expanded={open}
                          aria-controls={`run-${run.id}`}
                          onClick={() => setOpenRun(open ? null : run.id)}
                        >
                          {open ? "Hide" : "Inspect"}
                        </button>
                      </div>
                      {open && (
                        <div id={`run-${run.id}`} className="mt-2">
                          <RunDetail runId={run.id} />
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          <section aria-labelledby="collection-collectors" className={card}>
            <h3 id="collection-collectors" className="font-semibold text-gray-900 dark:text-gray-100">Collectors</h3>
            <p className="text-xs text-gray-500">
              Each collector has its own revocable credential that can only send observations for its sources.
            </p>
            {data.collectors.length > 0 && (
              <ul className="divide-y divide-gray-100 dark:divide-gray-800">
                {data.collectors.map((c) => (
                  <CollectorRow key={c.id} collector={c} onChanged={load} onSetup={setSetup} />
                ))}
              </ul>
            )}
            {setup && <SetupPanel setup={setup} onClose={() => setSetup(null)} />}
            {!setup &&
              (showForm ? (
                <NewCollectorForm
                  catalog={data.catalog}
                  onCreated={(created) => {
                    setSetup(created);
                    setShowForm(false);
                    load();
                  }}
                />
              ) : (
                <button type="button" className={`${button} mt-3`} onClick={() => setShowForm(true)}>
                  Set up a collector
                </button>
              ))}
          </section>
        </>
      )}
    </div>
  );
}
