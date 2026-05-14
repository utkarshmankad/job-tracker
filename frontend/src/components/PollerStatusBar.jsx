import { useState, useEffect } from "react";
import { api } from "../api/client";

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
      const lastSync = pollerData.last_sync_at
        ? Math.round((Date.now() - new Date(pollerData.last_sync_at).getTime()) / 60_000)
        : null;
      message = lastSync !== null ? `Last synced: ${lastSync} min ago` : "Syncing…";
    }
  }

  return (
    <div className="bg-white border-b border-gray-200 px-4 py-2 flex items-center gap-2 text-sm text-gray-700">
      <span className={`inline-block w-2 h-2 rounded-full ${dot}`} />
      <span>{message}</span>
    </div>
  );
}
