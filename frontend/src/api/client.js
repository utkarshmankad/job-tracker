const BASE = "http://localhost:8000/api/v1";

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

export const api = {
  listApplications: (params = {}) =>
    request(`/applications?${new URLSearchParams(params)}`),
  getApplication: (id) => request(`/applications/${id}`),
  createApplication: (body) =>
    request("/applications", { method: "POST", body: JSON.stringify(body) }),
  updateApplication: (id, body) =>
    request(`/applications/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteApplication: (id) =>
    request(`/applications/${id}`, { method: "DELETE" }),
  exportApplications: (format = "csv") =>
    fetch(`${BASE}/applications/export?format=${format}`),

  getInsights: () => request("/insights"),

  getPollerStatus: () => request("/poller/status"),

  listSuppressRules: () => request("/suppress-rules"),
  createSuppressRule: (body) =>
    request("/suppress-rules", { method: "POST", body: JSON.stringify(body) }),
  deleteSuppressRule: (id) => request(`/suppress-rules/${id}`, { method: "DELETE" }),
};
