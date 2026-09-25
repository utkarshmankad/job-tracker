import { useEffect, useState } from "react";
import { api } from "../api/client";
import { formatPercent } from "../utils/formatters";

const card = "rounded-xl border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-800";

function Metric({ label, value, detail }) {
  return <div className={card}><p className="text-xs font-medium uppercase tracking-wide text-gray-500">{label}</p><p className="mt-1 text-3xl font-bold text-gray-900 dark:text-gray-100">{value}</p><p className="mt-1 text-xs text-gray-400">{detail}</p></div>;
}

function Breakdown({ title, rows }) {
  return (
    <div className={`${card} overflow-x-auto`}>
      <h3 className="mb-3 font-semibold text-gray-900 dark:text-gray-100">{title}</h3>
      {rows.length === 0 ? <p className="text-sm text-gray-500">No attended interviews in this period.</p> : (
        <table className="w-full text-sm"><thead><tr className="border-b text-left text-xs uppercase text-gray-500"><th className="pb-2">Name</th><th>Interviewed</th><th>Offers</th><th>Rejected</th><th>Offer rate</th></tr></thead>
          <tbody>{rows.map((row) => <tr key={row.name} className="border-b border-gray-100 dark:border-gray-700"><td className="py-2 font-medium">{row.name}</td><td>{row.interviewed_applications}</td><td>{row.offers}</td><td>{row.rejections}</td><td>{formatPercent(row.offer_rate)}</td></tr>)}</tbody>
        </table>
      )}
    </div>
  );
}

export default function ConversionPanel() {
  const [months, setMonths] = useState(6);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    let active = true;
    api.getConversionData(months).then((result) => { if (active) { setData(result); setError(null); } }, (err) => { if (active) setError(err.message); });
    return () => { active = false; };
  }, [months]);
  if (error) return <div className="text-sm text-red-600">Conversion analytics failed: {error}</div>;
  if (!data) return <div className="text-sm text-gray-500">Loading conversion analytics…</div>;
  const opportunity = data.opportunities;
  const interview = data.interviews;
  return (
    <section className="space-y-4" aria-labelledby="conversion-title">
      <div className="flex flex-wrap items-end justify-between gap-3"><div><h2 id="conversion-title" className="text-lg font-semibold text-gray-900 dark:text-gray-100">Opportunity & Interview Conversion</h2><p className="text-sm text-gray-500">Counts distinct attended rounds separately from unique applications.</p></div>
        <div className="inline-flex rounded-lg border p-1 dark:border-gray-700">{[1, 3, 6, 12].map((value) => <button key={value} onClick={() => setMonths(value)} aria-pressed={months === value} className={`rounded-md px-3 py-1.5 text-sm ${months === value ? "bg-indigo-600 text-white" : "text-gray-500"}`}>{value}m</button>)}</div>
      </div>
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Metric label="Opportunities" value={opportunity.received} detail="recruiting prospects received" />
        <Metric label="Converted" value={opportunity.converted_to_application} detail={opportunity.application_conversion_rate == null ? "No opportunity cohort" : `${formatPercent(opportunity.application_conversion_rate)} to application`} />
        <Metric label="Interviews attended" value={interview.attended_events} detail={`${interview.interviewed_applications} unique applications`} />
        <Metric label="Interview → offer" value={interview.outcomes.offer} detail={interview.offer_rate == null ? "No attended interviews" : formatPercent(interview.offer_rate)} />
        <Metric label="Interview → rejection" value={interview.outcomes.rejected} detail={interview.rejection_rate == null ? "No attended interviews" : formatPercent(interview.rejection_rate)} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2"><Breakdown title="Interview outcomes by source" rows={data.by_source} /><Breakdown title="Interview outcomes by method" rows={data.by_method} /></div>
    </section>
  );
}
