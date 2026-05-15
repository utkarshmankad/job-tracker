import { useState, useEffect } from "react";
import { api } from "../api/client";
import { formatTimeDiff } from "../utils/formatters";

export default function PollerStatusBar() {
  const [pollerData, setPollerData] = useState(null);
  const [fetchError, setFetchError] = useState(false);

  const fetchStatus = async () => {
    try {
      const data = await api.getPollerStatus();
      setPollerData(data);
      setFetchError(false);
    } catch {
      setFetchError(true);
    }
  };

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 30_000);
    return () => clearInterval(interval);
  }, []);

  let dot = "bg-orange-400";
  let message = "Poller stopped — check logs";

  if (!fetchError && pollerData) {
    if (pollerData.status === "AUTH_ERROR") {
      dot = "bg-red-500";
      message = "Auth Error — run: python backend/setup_wizard.py reauth";
    } else if (pollerData.status === "RUNNING" || pollerData.last_sync_at) {
      dot = "bg-green-500";
      const lastSync = formatTimeDiff(pollerData.last_sync_at);
      message = lastSync ? `Last synced: ${lastSync}` : "Syncing…";
    }
  }

  return (
    <div className="bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-700 px-4 py-2 flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
      <span className={`inline-block w-2 h-2 rounded-full ${dot}`} />
      <span>{message}</span>
    </div>
  );
}
