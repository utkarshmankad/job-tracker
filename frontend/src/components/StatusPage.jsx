import { useState, useEffect } from "react";
import { api } from "../api/client";

const REFRESH_INTERVAL = 30_000;

function formatUptime(seconds) {
  if (seconds == null) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatRelativeTime(isoString) {
  if (!isoString) return "never";
  const diff = Math.round((Date.now() - new Date(isoString).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  return `${Math.floor(diff / 3600)}h ago`;
}

const STATUS_CONFIG = {
  operational: {
    dot: "bg-green-500",
    badge: "bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200",
    banner: "bg-green-500",
    label: "Operational",
  },
  degraded: {
    dot: "bg-yellow-400",
    badge: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200",
    banner: "bg-yellow-400",
    label: "Degraded Performance",
  },
  outage: {
    dot: "bg-red-500",
    badge: "bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200",
    banner: "bg-red-500",
    label: "Partial Outage",
  },
};

function ComponentRow({ component }) {
  const cfg = STATUS_CONFIG[component.status] ?? STATUS_CONFIG.degraded;
  return (
    <div className="flex items-center justify-between py-4 border-b border-gray-100 dark:border-gray-800 last:border-0">
      <div className="flex items-center gap-3">
        <span className={`w-3 h-3 rounded-full flex-shrink-0 ${cfg.dot}`} />
        <div>
          <p className="font-medium text-gray-900 dark:text-gray-100 text-sm">{component.name}</p>
          <p className="text-gray-500 dark:text-gray-400 text-xs mt-0.5">{component.description}</p>
        </div>
      </div>
      <span className={`text-xs font-medium px-2.5 py-1 rounded-full flex-shrink-0 ml-4 ${cfg.badge}`}>
        {cfg.label}
      </span>
    </div>
  );
}

function StatCard({ label, value }) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-4 text-center">
      <p className="text-2xl font-semibold text-gray-900 dark:text-gray-100">{value}</p>
      <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{label}</p>
    </div>
  );
}

export default function StatusPage() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(false);
  const [lastFetch, setLastFetch] = useState(null);

  const fetch = async () => {
    try {
      const result = await api.getSystemStatus();
      setData(result);
      setError(false);
      setLastFetch(new Date());
    } catch {
      setError(true);
    }
  };

  useEffect(() => {
    fetch();
    const interval = setInterval(fetch, REFRESH_INTERVAL);
    return () => clearInterval(interval);
  }, []);

  if (error && !data) {
    return (
      <div className="flex items-center justify-center py-32">
        <div className="text-center">
          <div className="w-4 h-4 bg-red-500 rounded-full mx-auto mb-3" />
          <p className="text-gray-600 dark:text-gray-400 text-sm">Unable to reach API server</p>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex items-center justify-center py-32">
        <p className="text-gray-400 text-sm">Loading…</p>
      </div>
    );
  }

  const cfg = STATUS_CONFIG[data.overall] ?? STATUS_CONFIG.degraded;
  const allOk = data.overall === "operational";

  return (
    <div className="max-w-2xl mx-auto">
      {/* Overall banner */}
      <div className={`rounded-xl p-6 mb-8 text-white ${cfg.banner}`}>
        <div className="flex items-center gap-3">
          <span className="w-4 h-4 bg-white/30 rounded-full inline-flex items-center justify-center">
            <span className="w-2.5 h-2.5 bg-white rounded-full" />
          </span>
          <h2 className="text-xl font-semibold">
            {allOk ? "All Systems Operational" : cfg.label}
          </h2>
        </div>
        {!allOk && (
          <p className="mt-2 text-white/80 text-sm">
            One or more components need attention.
          </p>
        )}
      </div>

      {/* Components */}
      <section className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-700 px-6 mb-8">
        <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider pt-4 pb-2">
          Components
        </h3>
        {data.components.map((c) => (
          <ComponentRow key={c.name} component={c} />
        ))}
      </section>

      {/* Stats */}
      <section className="mb-8">
        <h3 className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wider mb-3">
          Stats
        </h3>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
          <StatCard
            label="Applications"
            value={data.stats.total_applications ?? "—"}
          />
          <StatCard
            label="DB Size"
            value={data.stats.db_size_kb != null ? `${data.stats.db_size_kb} KB` : "—"}
          />
          <StatCard
            label="Last Poll"
            value={formatRelativeTime(data.stats.last_poll_at)}
          />
          <StatCard
            label="API Uptime"
            value={formatUptime(data.stats.uptime_seconds)}
          />
        </div>
      </section>

      {/* Footer */}
      <p className="text-center text-xs text-gray-400 dark:text-gray-600 pb-6">
        Auto-refreshes every 30s
        {lastFetch && ` · Last checked ${formatRelativeTime(lastFetch.toISOString())}`}
      </p>
    </div>
  );
}
