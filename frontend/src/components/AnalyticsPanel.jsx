import { useState, useEffect } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import { api } from "../api/client";
import { STATUS_COLORS } from "../utils/constants";
import { formatPercent } from "../utils/formatters";

const SIGNAL_ICON = { green: "🟢", red: "🔴", neutral: "⚪" };

const FUNNEL_COLORS = [
  "#3b82f6", "#eab308", "#a855f7", "#7c3aed",
  "#f97316", "#22c55e", "#16a34a", "#ef4444", "#6b7280",
];

export default function AnalyticsPanel() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .getInsights()
      .then((d) => { setData(d); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <div className="text-sm text-gray-500 py-12 text-center">Loading insights…</div>;
  if (error) return <div className="text-sm text-red-600 py-8 text-center">Error: {error}</div>;
  if (!data || data.insufficient_data) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-12 text-center text-gray-500 text-sm">
        Add at least 10 applications to see insights.
      </div>
    );
  }

  const funnelData = data.funnel
    ? Object.entries(data.funnel).map(([status, count]) => ({ status, count }))
    : [];

  return (
    <div className="space-y-6">
      {funnelData.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="text-base font-semibold text-gray-800 mb-4">Application Funnel</h2>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={funnelData} margin={{ top: 4, right: 16, left: 0, bottom: 60 }}>
              <XAxis
                dataKey="status"
                tick={{ fontSize: 11 }}
                angle={-30}
                textAnchor="end"
                interval={0}
              />
              <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
              <Tooltip />
              <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                {funnelData.map((entry, i) => (
                  <Cell key={entry.status} fill={FUNNEL_COLORS[i % FUNNEL_COLORS.length]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {data.channels && data.channels.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="text-base font-semibold text-gray-800 mb-4">Channel Performance</h2>
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-100 text-left text-xs text-gray-500 uppercase tracking-wider">
                <th className="pb-2 pr-4">Source</th>
                <th className="pb-2 pr-4">Applications</th>
                <th className="pb-2 pr-4">Interview Rate</th>
                <th className="pb-2 pr-4">Offer Rate</th>
                <th className="pb-2">Signal</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {data.channels.map((ch) => (
                <tr key={ch.source}>
                  <td className="py-2 pr-4 font-medium text-gray-800">{ch.source}</td>
                  <td className="py-2 pr-4 text-gray-600">{ch.application_count}</td>
                  <td className="py-2 pr-4 text-gray-600">
                    {ch.interview_rate != null ? formatPercent(ch.interview_rate) : "—"}
                  </td>
                  <td className="py-2 pr-4 text-gray-600">
                    {ch.offer_rate != null ? formatPercent(ch.offer_rate) : "—"}
                  </td>
                  <td className="py-2">{SIGNAL_ICON[ch.signal] ?? "⚪"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {data.insights && data.insights.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="text-base font-semibold text-gray-800 mb-4">Insights</h2>
          <ul className="space-y-2">
            {data.insights.map((insight, i) => (
              <li key={i} className="flex items-start gap-2 text-sm">
                <span
                  className={`mt-0.5 px-2 py-0.5 rounded-full text-xs font-medium shrink-0 ${
                    insight.level === "positive"
                      ? "bg-green-100 text-green-800"
                      : insight.level === "negative"
                      ? "bg-red-100 text-red-800"
                      : "bg-gray-100 text-gray-600"
                  }`}
                >
                  {insight.level ?? "info"}
                </span>
                <span className="text-gray-700">{insight.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
