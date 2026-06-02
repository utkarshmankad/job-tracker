import { useState, useEffect } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend, Cell,
} from "recharts";
import { api } from "../api/client";
import { formatPercent } from "../utils/formatters";

const card = "bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6";

function KpiCard({ label, value, sub, accent }) {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-5 flex flex-col gap-1">
      <span className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">{label}</span>
      <span className={`text-3xl font-bold ${accent ?? "text-gray-900 dark:text-gray-100"}`}>{value}</span>
      {sub && <span className="text-xs text-gray-400 dark:text-gray-500">{sub}</span>}
    </div>
  );
}

const STAGE_COLORS = {
  rejected: "#ef4444",
  withdrawn: "#f97316",
};

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
        <Bar dataKey="Rejected" fill={STAGE_COLORS.rejected} radius={[3, 3, 0, 0]} />
        <Bar dataKey="Withdrawn" fill={STAGE_COLORS.withdrawn} radius={[3, 3, 0, 0]} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function MonthlyTrendBars({ monthly_trend }) {
  return (
    <ResponsiveContainer width="100%" height={180}>
      <BarChart data={monthly_trend} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="label" tick={{ fontSize: 10 }} />
        <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
        <Tooltip />
        <Legend iconType="circle" iconSize={10} wrapperStyle={{ fontSize: 12 }} />
        <Bar dataKey="rejected" name="Rejected" fill={STAGE_COLORS.rejected} radius={[3, 3, 0, 0]} stackId="a" />
        <Bar dataKey="withdrawn" name="Withdrawn" fill={STAGE_COLORS.withdrawn} radius={[3, 3, 0, 0]} stackId="a" />
      </BarChart>
    </ResponsiveContainer>
  );
}

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

export default function RejectionPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.getRejectionData()
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="text-sm text-gray-500 dark:text-gray-400 py-12 text-center">Loading…</div>;
  if (error) return <div className="text-sm text-red-600 py-8 text-center">Error: {error}</div>;
  if (!data || data.insufficient_data) {
    return (
      <div className={`${card} py-12 text-center text-gray-500 dark:text-gray-400 text-sm`}>
        Not enough rejection data yet. At least 3 rejected or withdrawn applications are needed.
      </div>
    );
  }

  const { rejected, withdrawn, resolved, rejection_rate, withdrawal_rate, non_offer_rate,
    stage_breakdown, portal_breakdown, monthly_trend } = data;

  return (
    <div className="space-y-6">
      {/* KPI row */}
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
    </div>
  );
}
