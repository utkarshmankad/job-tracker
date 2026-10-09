// Unsafe-undo responses carry {detail: {message, conflicts}}; other failures a plain detail.
export function undoConflictMessages(err) {
  const detail = err?.payload?.detail;
  if (detail && typeof detail === "object") {
    return { message: detail.message, conflicts: detail.conflicts ?? [] };
  }
  return { message: err?.detail ?? "Undo failed. Nothing was changed.", conflicts: [] };
}
