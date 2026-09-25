import { useEffect, useState } from "react";
import { CalendarPlus } from "lucide-react";
import { api } from "../api/client";
import { formatDate } from "../utils/formatters";

const EVENT_TYPES = ["Interview Scheduled", "Interview Attended", "Interview Rescheduled", "Interview Cancelled"];
const ROUNDS = ["Recruiter Screen", "Hiring Manager", "Technical / System Design", "Leadership / Behavioural", "Executive / Final", "Other"];
const inputClass = "rounded-md border border-gray-300 bg-white px-2 py-1.5 text-sm dark:border-gray-600 dark:bg-gray-800";

export default function ApplicationEvents({ applicationId }) {
  const [events, setEvents] = useState([]);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState(null);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({ event_type: "Interview Attended", interview_round: "Recruiter Screen", occurred_at: new Date().toISOString().slice(0, 16), notes: "" });

  const load = () => api.listApplicationEvents(applicationId).then(setEvents).catch((err) => setError(err.message));
  useEffect(() => {
    let active = true;
    api.listApplicationEvents(applicationId).then((items) => { if (active) setEvents(items); }, (err) => { if (active) setError(err.message); });
    return () => { active = false; };
  }, [applicationId]);

  const submit = async (event) => {
    event.preventDefault();
    setSaving(true);
    try {
      await api.createApplicationEvent(applicationId, { ...form, occurred_at: new Date(form.occurred_at).toISOString(), notes: form.notes || null });
      setShowForm(false);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="mb-6">
      <div className="mb-3 flex items-center justify-between"><h3 className="text-sm font-semibold text-gray-700 dark:text-gray-300">Activity & interview rounds</h3><button onClick={() => setShowForm((value) => !value)} className="inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-xs dark:border-gray-700"><CalendarPlus size={13} /> Add event</button></div>
      {error && <p className="mb-2 text-xs text-red-600">{error}</p>}
      {showForm && <form onSubmit={submit} className="mb-4 grid gap-2 rounded-lg bg-gray-50 p-3 dark:bg-gray-800 sm:grid-cols-2">
        <select className={inputClass} value={form.event_type} onChange={(e) => setForm((value) => ({ ...value, event_type: e.target.value }))}>{EVENT_TYPES.map((type) => <option key={type}>{type}</option>)}</select>
        <select className={inputClass} value={form.interview_round} onChange={(e) => setForm((value) => ({ ...value, interview_round: e.target.value }))}>{ROUNDS.map((round) => <option key={round}>{round}</option>)}</select>
        <input className={inputClass} type="datetime-local" value={form.occurred_at} onChange={(e) => setForm((value) => ({ ...value, occurred_at: e.target.value }))} required />
        <input className={inputClass} placeholder="Optional note" value={form.notes} onChange={(e) => setForm((value) => ({ ...value, notes: e.target.value }))} />
        <button disabled={saving} className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50 sm:col-span-2">{saving ? "Saving…" : "Save event"}</button>
      </form>}
      {events.length === 0 ? <p className="text-sm text-gray-400">No activity events yet.</p> : <ol className="space-y-2">{events.map((item) => <li key={item.id} className="flex flex-wrap justify-between gap-2 rounded-lg bg-gray-50 px-3 py-2 text-sm dark:bg-gray-800"><span><strong>{item.event_type}</strong>{item.interview_round ? ` · ${item.interview_round}` : ""}<span className="ml-2 text-xs text-gray-400">via {item.source}</span></span><span className="text-xs text-gray-500">{formatDate(item.occurred_at)}</span>{item.notes && <p className="w-full text-xs text-gray-500">{item.notes}</p>}</li>)}</ol>}
    </section>
  );
}
