import { useState, useEffect, useCallback } from "react";
import { RefreshCw } from "lucide-react";
import { api } from "../api/client";
import { formatTimeDiff } from "../utils/formatters";

export default function PollerStatusBar() {
  const [pollerData, setPollerData] = useState(null);
  const [fetchError, setFetchError] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [triggerError, setTriggerError] = useState(false);

  const fetchStatus = useCallback(async () => {
    try {
      const data = await api.getPollerStatus();
      setPollerData(data);
      setFetchError(false);
    } catch {
      setFetchError(true);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 30_000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  const handleTrigger = async () => {
    if (triggering) return;
    setTriggering(true);
    setTriggerError(false);
    try {
      const result = await api.triggerPoll();
      if (result?.skipped) {
        setTriggerError("Poll already in progress — try again shortly");
        return;
      }
      // Poll status a few times until last_sync_at advances
      for (let i = 0; i < 6; i++) {
        await new Promise((r) => setTimeout(r, 3_000));
        await fetchStatus();
      }
    } catch (e) {
      setTriggerError(e.message || "Trigger failed");
    } finally {
      setTriggering(false);
    }
  };

  let dot = "bg-orange-400";
  let message = "Poller stopped — check logs";

  if (!fetchError && pollerData) {
    if (pollerData.status === "AUTH_ERROR") {
      dot = "bg-red-500";
      message = "Auth Error — run: python backend/setup_wizard.py reauth";
    } else if (pollerData.status === "RUNNING" || pollerData.last_sync_at) {
      dot = "bg-green-500";
      const lastSync = formatTimeDiff(pollerData.last_sync_at);
      message = triggering ? "Syncing…" : lastSync ? `Last synced: ${lastSync}` : "Syncing…";
    }
  }

  if (triggerError) {
    dot = "bg-red-500";
    message = triggerError;
  }

  return (
    <div className="bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 px-4 py-2 flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
      <span className={`inline-block w-2 h-2 rounded-full ${dot}`} />
      <span className="flex-1">{message}</span>
      <button
        onClick={handleTrigger}
        disabled={triggering}
        aria-label="Refresh Gmail now"
        title="Pull Gmail now"
        className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800 disabled:opacity-40 transition-colors"
      >
        <RefreshCw
          size={14}
          aria-hidden="true"
          className={triggering ? "animate-spin" : ""}
        />
      </button>
    </div>
  );
}
