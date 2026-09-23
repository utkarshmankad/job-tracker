import { useState, useEffect } from "react";
import {
  PieChart, Pie, Cell, Tooltip, ResponsiveContainer,
  AreaChart, Area, XAxis, YAxis, CartesianGrid,
  BarChart, Bar, Legend,
} from "recharts";
import { api } from "../api/client";
import { formatPercent } from "../utils/formatters";

// ── Colour palette ──────────────────────────────────────────────────────────

const REJECTION_COLORS = {
  rejected: "#ef4444",
  withdrawn: "#f97316",
};

// ── KPI card ────────────────────────────────────────────────────────────────

function KpiCard({ label, value, sub, accent }) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-5 flex flex-col gap-1">
      <span className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">{label}</span>
      <span className={`text-3xl font-bold ${accent ?? "text-gray-900 dark:text-gray-100"}`}>{value}</span>
      {sub && <span className="text-xs text-gray-400 dark:text-gray-500">{sub}</span>}
    </div>
  );
}

function PeriodPicker({ value, onChange }) {
  return (
    <div className="inline-flex rounded-lg border border-gray-200 dark:border-gray-700 p-1" aria-label="Analytics period">
      {[7, 28, 90].map((days) => (
        <button
          key={days}
          type="button"
          onClick={() => onChange(days)}
          className={`rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            value === days
              ? "bg-indigo-600 text-white"
              : "text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
          }`}
          aria-pressed={value === days}
        >
          {days} days
        </button>
      ))}
    </div>
  );
}

function StageFunnel({ nodes }) {
  const byId = Object.fromEntries(nodes.map((node) => [node.id, node.count]));
  const stages = [
    { label: "Applied", value: byId.Applied ?? 0, color: "bg-indigo-500" },
    { label: "Shortlisted", value: byId.Shortlisted ?? 0, color: "bg-amber-500" },
    { label: "Interview", value: byId.Interview ?? 0, color: "bg-violet-500" },
    { label: "Offer / Joined", value: byId["Offer / Joined"] ?? 0, color: "bg-green-500" },
  ];
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-4">
      {stages.map((stage, index) => {
        const previous = index > 0 ? stages[index - 1].value : null;
        const conversion = previous ? stage.value / previous : null;
        return (
          <div key={stage.label} className="relative rounded-lg bg-gray-50 p-4 dark:bg-gray-900/40">
            {index > 0 && (
              <span className="absolute -top-2 left-3 rounded-full bg-white px-2 text-xs font-medium text-gray-500 shadow-sm dark:bg-gray-800 dark:text-gray-400">
                {conversion == null ? "—" : formatPercent(conversion)} from prior
              </span>
            )}
            <div className={`mb-3 h-1.5 rounded-full ${stage.color}`} />
            <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{stage.value}</div>
            <div className="text-sm text-gray-500 dark:text-gray-400">{stage.label}</div>
          </div>
        );
      })}
    </div>
  );
}

// ── Outcomes donut ───────────────────────────────────────────────────────────

function OutcomesDonut({ outcomes }) {
  const total = outcomes.reduce((s, o) => s + o.value, 0);
  return (
    <div className="flex flex-col items-center gap-4">
      <ResponsiveContainer width="100%" height={200}>
        <PieChart>
          <Pie
            data={outcomes}
            dataKey="value"
            nameKey="name"
            cx="50%"
            cy="50%"
            innerRadius={60}
            outerRadius={88}
            paddingAngle={2}
          >
            {outcomes.map((o, i) => (
              <Cell key={i} fill={o.color} />
            ))}
          </Pie>
          <Tooltip formatter={(v) => [`${v} (${formatPercent(v / total)})`, ""]} />
        </PieChart>
      </ResponsiveContainer>
      <div className="flex flex-wrap justify-center gap-3">
        {outcomes.map((o) => (
          <div key={o.name} className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
            <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: o.color }} />
            {o.name} ({o.value})
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Weekly activity chart ────────────────────────────────────────────────────

function WeeklyActivity({ data }) {
  const formatted = data.map((d) => ({
    ...d,
    label: new Date(d.week + "T00:00:00").toLocaleDateString("en-IN", { month: "short", day: "numeric" }),
  }));
  return (
    <ResponsiveContainer width="100%" height={160}>
      <AreaChart data={formatted} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
        <defs>
          <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3} />
            <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" className="dark:[stroke:#374151]" />
        <XAxis dataKey="label" tick={{ fontSize: 10 }} interval={1} />
        <YAxis allowDecimals={false} tick={{ fontSize: 10 }} />
        <Tooltip />
        <Legend iconType="circle" iconSize={9} wrapperStyle={{ fontSize: 12 }} />
        <Area
          type="monotone"
          dataKey="applied"
          name="Applied date"
          stroke="#6366f1"
          strokeWidth={2}
          fill="url(#areaGrad)"
          dot={false}
        />
        <Area
          type="monotone"
          dataKey="captured"
          name="Captured by tracker"
          stroke="#14b8a6"
          strokeWidth={2}
          fill="none"
          strokeDasharray="5 4"
          dot={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ── Channel table ────────────────────────────────────────────────────────────

const SIGNAL_CONFIG = {
  green: { icon: "🟢", label: "Healthy" },
  red: { icon: "🔴", label: "Poor" },
  neutral: { icon: "⚪", label: "No data" },
};

function SignalBadge({ signal }) {
  const cfg = SIGNAL_CONFIG[signal] ?? SIGNAL_CONFIG.neutral;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span aria-hidden="true">{cfg.icon}</span>
      <span className="text-xs text-gray-600 dark:text-gray-400">{cfg.label}</span>
    </span>
  );
}

function ChannelTable({ channels }) {
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-gray-100 dark:border-gray-700 text-left text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">
          <th className="pb-2 pr-4">Source</th>
          <th className="pb-2 pr-4">Apps</th>
          <th className="pb-2 pr-4">Interview %</th>
          <th className="pb-2 pr-4">Offer %</th>
          <th className="pb-2">Signal</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-gray-50 dark:divide-gray-700">
        {channels.map((ch) => (
          <tr key={ch.source}>
            <td className="py-2 pr-4 font-medium text-gray-800 dark:text-gray-200">{ch.source}</td>
            <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">{ch.application_count ?? ch.total}</td>
            <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">
              {ch.interview_rate != null ? formatPercent(ch.interview_rate) : "—"}
            </td>
            <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">
              {ch.offer_rate != null ? formatPercent(ch.offer_rate) : "—"}
            </td>
            <td className="py-2">
              <SignalBadge signal={ch.signal ?? ch.flag} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── Rejection: stage breakdown bars ─────────────────────────────────────────

function StageBars({ stage_breakdown }) {
  const { rejection, withdrawal } = stage_breakdown;
  const stages = ["Applied", "Shortlisted", "Interview"];
  const data = stages.map((s) => ({
    stage: s,
    Rejected: rejection[s] ?? 0,
    Withdrawn: withdrawal[s] ?? 0,
  }));
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="stage" tick={{ fontSize: 12 }} />
        <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
        <Tooltip />
        <Legend iconType="circle" iconSize={10} wrapperStyle={{ fontSize: 12 }} />
        <Bar dataKey="Rejected" fill={REJECTION_COLORS.rejected} radius={[3, 3, 0, 0]} />
        <Bar dataKey="Withdrawn" fill={REJECTION_COLORS.withdrawn} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

// ── Rejection: monthly trend bars ────────────────────────────────────────────

function MonthlyTrendBars({ monthly_trend }) {
  return (
    <ResponsiveContainer width="100%" height={180}>
      <BarChart data={monthly_trend} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="label" tick={{ fontSize: 10 }} />
        <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
        <Tooltip />
        <Legend iconType="circle" iconSize={10} wrapperStyle={{ fontSize: 12 }} />
        <Bar dataKey="rejected" name="Rejected" fill={REJECTION_COLORS.rejected} radius={[3, 3, 0, 0]} stackId="a" />
        <Bar dataKey="withdrawn" name="Withdrawn" fill={REJECTION_COLORS.withdrawn} radius={[3, 3, 0, 0]} stackId="a" />
      </BarChart>
    </ResponsiveContainer>
  );
}

// ── Rejection: portal table ──────────────────────────────────────────────────

function PortalTable({ portal_breakdown }) {
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-gray-100 dark:border-gray-700 text-left text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider">
          <th className="pb-2 pr-4">Portal</th>
          <th className="pb-2 pr-4">Total</th>
          <th className="pb-2 pr-4">Rejected</th>
          <th className="pb-2 pr-4">Withdrawn</th>
          <th className="pb-2">Rejection Rate</th>
        </tr>
      </thead>
      <tbody className="divide-y divide-gray-50 dark:divide-gray-700">
        {portal_breakdown.map((row) => (
          <tr key={row.portal}>
            <td className="py-2 pr-4 font-medium text-gray-800 dark:text-gray-200">{row.portal}</td>
            <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">{row.total}</td>
            <td className="py-2 pr-4 text-red-600 dark:text-red-400">{row.rejected}</td>
            <td className="py-2 pr-4 text-orange-500 dark:text-orange-400">{row.withdrawn}</td>
            <td className="py-2">
              {row.rejection_rate != null ? (
                <span className={`font-medium ${row.rejection_rate >= 0.6 ? "text-red-600 dark:text-red-400" : row.rejection_rate >= 0.3 ? "text-yellow-600 dark:text-yellow-400" : "text-gray-700 dark:text-gray-300"}`}>
                  {formatPercent(row.rejection_rate)}
                </span>
              ) : "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ── Main panel ───────────────────────────────────────────────────────────────

const card = "bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6";

export default function AnalyticsPanel() {
  const [flow, setFlow] = useState(null);
  const [insights, setInsights] = useState(null);
  const [rejection, setRejection] = useState(null);
  const [pulse, setPulse] = useState(null);
  const [windowDays, setWindowDays] = useState(28);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    Promise.all([api.getFlowData(), api.getInsights(), api.getRejectionData(), api.getSearchPulse(28)])
      .then(([f, i, r, p]) => { setFlow(f); setInsights(i); setRejection(r); setPulse(p); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const changeWindow = (days) => {
    setWindowDays(days);
    api.getSearchPulse(days)
      .then((data) => { setPulse(data); setError(null); })
      .catch((e) => setError(e.message));
  };

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-12 text-center">Loading analytics…</div>;
  if (error) return <div className="text-sm text-red-600 py-8 text-center">Error: {error}</div>;
  if (!flow || flow.insufficient_data) {
    return (
      <div className={`${card} py-12 text-center text-gray-500 dark:text-gray-400 text-sm`}>
        Add at least 10 applications to see analytics.
      </div>
    );
  }

  const { kpis, nodes, weekly_activity } = flow;
  const outcomes = (flow.outcomes ?? []).filter((o) => o.value > 0);
  const channelData = insights?.channels?.map((c) => ({
    ...c,
    application_count: c.total,
    interview_rate: c.total > 0 ? c.interviewed / c.total : null,
    offer_rate: c.total > 0 ? c.offered / c.total : null,
    signal: insights.insights?.find((ins) => ins.source === c.source)?.flag ?? "neutral",
  })) ?? [];

  const hasRejectionData = rejection && !rejection.insufficient_data;

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Search Pulse</h2>
          <p className="text-sm text-gray-500 dark:text-gray-400">Recent activity and fair conversion rates from applications old enough to have received a response.</p>
        </div>
        <PeriodPicker value={windowDays} onChange={changeWindow} />
      </div>

      {pulse && (
        <>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
            <KpiCard label={`Applications · ${windowDays}d`} value={pulse.recent.applications} sub="by application date" />
            <KpiCard label="Captured" value={pulse.recent.captured} sub={`${pulse.recent.late_imports} imported 7+ days late`} />
            <KpiCard label="Interviews" value={pulse.recent.interviews} sub={`from recent applications`} />
            <KpiCard label="Matured response" value={pulse.matured_cohort.response_rate == null ? "—" : formatPercent(pulse.matured_cohort.response_rate)} sub={`${pulse.matured_cohort.responses}/${pulse.matured_cohort.applications} applications`} />
            <KpiCard label="Matured shortlist" value={pulse.matured_cohort.shortlist_rate == null ? "—" : formatPercent(pulse.matured_cohort.shortlist_rate)} sub={`excludes newest ${pulse.maturity_days} days`} />
          </div>
          {pulse.matured_cohort.applications === 0 && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300">
              No applications are old enough in this comparison period yet. Conversion rates will appear after the {pulse.maturity_days}-day response window.
            </div>
          )}
        </>
      )}

      {/* Lifetime KPI row */}
      <h2 className="text-base font-semibold text-gray-800 dark:text-gray-200">All-time context</h2>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
        <KpiCard label="Total Applications" value={kpis.total} />
        <KpiCard label="Active" value={kpis.active} sub="in progress" />
        <KpiCard label="Stale" value={kpis.stale ?? 0} sub="no update in 14d" accent="text-yellow-500 dark:text-yellow-400" />
        <KpiCard label="Withdrawn" value={kpis.withdrawn ?? 0} sub="self-withdrew" accent="text-orange-500 dark:text-orange-400" />
        <KpiCard label="Interview Rate" value={formatPercent(kpis.interview_rate)} sub="of all applications" />
        <KpiCard label="Offer Rate" value={formatPercent(kpis.offer_rate)} sub="of all applications" />
      </div>

      {/* Funnel + Donut row */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className={`${card} lg:col-span-2`}>
          <h2 className="text-base font-semibold text-gray-800 dark:text-gray-200 mb-4">Stage Conversion</h2>
          <StageFunnel nodes={nodes} />
        </div>
        <div className={card}>
          <h2 className="text-base font-semibold text-gray-800 dark:text-gray-200 mb-4">Outcomes</h2>
          <OutcomesDonut outcomes={outcomes} />
        </div>
      </div>

      {/* Weekly activity */}
      {(pulse?.activity ?? weekly_activity)?.length > 0 && (
        <div className={card}>
          <h2 className="text-base font-semibold text-gray-800 dark:text-gray-200">Application Activity</h2>
          <p className="mb-4 text-xs text-gray-400 dark:text-gray-500">Applied date shows search effort; captured date reveals delayed imports and backfills.</p>
          <WeeklyActivity data={pulse?.activity ?? weekly_activity} />
        </div>
      )}

      {/* Channel performance */}
      {channelData.length > 0 && (
        <div className={card}>
          <h2 className="text-base font-semibold text-gray-800 dark:text-gray-200 mb-4">Channel Performance</h2>
          <ChannelTable channels={channelData} />
        </div>
      )}

      {/* Insights */}
      {insights?.insights?.length > 0 && (
        <div className={card}>
          <h2 className="text-base font-semibold text-gray-800 dark:text-gray-200 mb-4">Insights</h2>
          <ul className="space-y-2">
            {insights.insights.map((ins, i) => {
              const label =
                ins.flag === "green" ? "Positive" : ins.flag === "red" ? "Needs attention" : "Info";
              return (
                <li key={i} className="flex items-start gap-2 text-sm">
                  <span
                    className={`mt-0.5 px-2 py-0.5 rounded-full text-xs font-medium shrink-0 ${
                      ins.flag === "green"
                        ? "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300"
                        : ins.flag === "red"
                        ? "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300"
                        : "bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400"
                    }`}
                  >
                    {label}
                  </span>
                  <span className="text-gray-700 dark:text-gray-300">{ins.message}</span>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* ── Rejection Analysis ─────────────────────────────────────────────── */}
      {hasRejectionData && (() => {
        const { rejected, withdrawn, resolved, rejection_rate, withdrawal_rate, non_offer_rate,
          stage_breakdown, portal_breakdown, monthly_trend } = rejection;
        return (
          <>
            <div className="border-t border-gray-200 dark:border-gray-700 pt-6">
              <h2 className="text-lg font-semibold text-gray-800 dark:text-gray-200 mb-4">Rejection Analysis</h2>
            </div>

            {/* Rejection KPI row */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              <KpiCard
                label="Rejection Rate"
                value={formatPercent(rejection_rate)}
                sub="of resolved applications"
                accent="text-red-600 dark:text-red-400"
              />
              <KpiCard
                label="Withdrawal Rate"
                value={formatPercent(withdrawal_rate)}
                sub="of all applications"
                accent="text-orange-500 dark:text-orange-400"
              />
              <KpiCard
                label="No-Offer Rate"
                value={formatPercent(non_offer_rate)}
                sub="rejected + withdrawn / resolved"
              />
              <KpiCard
                label="Resolved"
                value={resolved}
                sub={`${rejected} rejected · ${withdrawn} withdrawn`}
              />
            </div>

            {/* Stage breakdown + Monthly trend */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
              <div className={card}>
                <h2 className="text-base font-semibold text-gray-800 dark:text-gray-200 mb-4">
                  Where Did Applications Drop?
                </h2>
                <p className="text-xs text-gray-400 dark:text-gray-500 mb-3">
                  Funnel stage at which the application was rejected or withdrawn
                </p>
                <StageBars stage_breakdown={stage_breakdown} />
              </div>
              <div className={card}>
                <h2 className="text-base font-semibold text-gray-800 dark:text-gray-200 mb-4">
                  Monthly Trend
                </h2>
                <p className="text-xs text-gray-400 dark:text-gray-500 mb-3">
                  Rejections and withdrawals per month (last 6 months)
                </p>
                <MonthlyTrendBars monthly_trend={monthly_trend} />
              </div>
            </div>

            {/* Portal breakdown */}
            {portal_breakdown.length > 0 && (
              <div className={card}>
                <h2 className="text-base font-semibold text-gray-800 dark:text-gray-200 mb-4">
                  Rejection Rate by Portal
                </h2>
                <PortalTable portal_breakdown={portal_breakdown} />
              </div>
            )}
          </>
        );
      })()}
    </div>
  );
}
