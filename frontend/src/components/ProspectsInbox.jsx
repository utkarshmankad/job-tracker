import { useEffect, useState } from "react";
import { CalendarClock, Check, Mail, MessageSquare, X } from "lucide-react";
import { api } from "../api/client";

const CATEGORY = {
  meeting: { label: "Meeting", Icon: CalendarClock, color: "text-purple-700 bg-purple-50 dark:text-purple-300 dark:bg-purple-950/50" },
  profile_interest: { label: "Profile interest", Icon: Check, color: "text-emerald-700 bg-emerald-50 dark:text-emerald-300 dark:bg-emerald-950/50" },
  recruiter_outreach: { label: "Recruiter message", Icon: MessageSquare, color: "text-blue-700 bg-blue-50 dark:text-blue-300 dark:bg-blue-950/50" },
};

export default function ProspectsInbox() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let active = true;
    api.listProspects({ limit: 100 }).then(
      (data) => {
        if (active) setItems(data);
      },
      (e) => {
        if (active) setError(e.message);
      },
    ).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, []);

  const setStatus = async (id, status) => {
    try {
      const updated = await api.updateProspectStatus(id, status);
      setItems((current) => current.map((item) => item.id === id ? updated : item));
    } catch (e) {
      setError(e.message);
    }
  };

  if (loading) return <p className="text-sm text-gray-500">Loading opportunities…</p>;

  return (
    <section aria-labelledby="prospects-title" className="space-y-4">
      <div>
        <h2 id="prospects-title" className="text-xl font-semibold text-gray-900 dark:text-gray-100">Opportunities</h2>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Automatically detected recruiter messages, profile interest, and meeting requests from LinkedIn email.
        </p>
      </div>
      {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-300 p-10 text-center dark:border-gray-700">
          <Mail className="mx-auto mb-3 text-gray-400" aria-hidden="true" />
          <p className="font-medium text-gray-800 dark:text-gray-200">No important LinkedIn opportunities detected yet</p>
          <p className="mt-1 text-sm text-gray-500">New high-signal emails will appear here automatically after Gmail sync.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {items.map((item) => {
            const meta = CATEGORY[item.category] ?? CATEGORY.recruiter_outreach;
            const Icon = meta.Icon;
            return (
              <article key={item.id} className={`rounded-xl border bg-white p-4 shadow-sm dark:border-gray-800 dark:bg-gray-900 ${item.status === "Dismissed" ? "opacity-60" : ""}`}>
                <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${meta.color}`}>
                      <Icon size={14} aria-hidden="true" />{meta.label}
                    </span>
                    <h3 className="mt-2 font-semibold text-gray-900 dark:text-gray-100">{item.title}</h3>
                    <p className="mt-1 text-xs text-gray-500">{item.sender} · {new Date(item.received_at).toLocaleString()}</p>
                    {item.snippet && <p className="mt-2 line-clamp-2 text-sm text-gray-600 dark:text-gray-300">{item.snippet}</p>}
                    <p className="mt-2 text-xs text-gray-500">{item.classification_reason}</p>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {item.status === "New" && (
                      <button onClick={() => setStatus(item.id, "Reviewed")} className="min-h-11 rounded-lg border px-3 text-sm font-medium dark:border-gray-700">Mark reviewed</button>
                    )}
                    {item.status !== "Dismissed" && (
                      <button onClick={() => setStatus(item.id, "Dismissed")} className="min-h-11 rounded-lg border px-3 text-sm text-gray-600 dark:border-gray-700 dark:text-gray-300">
                        <X size={15} className="inline" aria-hidden="true" /> Dismiss
                      </button>
                    )}
                  </div>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
