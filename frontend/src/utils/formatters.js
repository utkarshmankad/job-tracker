export const formatDate = (iso) =>
  iso
    ? new Date(iso).toLocaleDateString("en-IN", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      })
    : "—";

export const formatPercent = (ratio) => `${(ratio * 100).toFixed(0)}%`;

export const formatStatus = (s) => s || "—";
