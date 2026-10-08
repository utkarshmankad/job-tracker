// Production builds call the API same-origin: Vercel rewrites /api/* to the Fly backend,
// so the session cookie is first-party. Local dev talks to the backend directly.
const API_ORIGIN =
  import.meta.env.VITE_API_BASE ?? (import.meta.env.PROD ? "" : "http://jobtracker.localhost:8000");
const BASE = `${API_ORIGIN}/api/v1`;

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

// The CSRF token lives only in memory (never localStorage): the AuthProvider sets it from
// the /auth/session response and clears it on logout or expiry.
let csrfToken = null;
let unauthorizedHandler = null;

export function setCsrfToken(token) {
  csrfToken = token;
}

// Called with the ApiError whenever a protected request returns 401 (expired or revoked
// session) so the app can return to the login screen.
export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler;
}

export class ApiError extends Error {
  constructor(status, code, detail, body) {
    super(`${status} ${body}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function toApiError(res) {
  const body = await res.text();
  let code = null;
  let detail = null;
  try {
    const parsed = JSON.parse(body);
    code = parsed.code ?? null;
    detail = typeof parsed.detail === "string" ? parsed.detail : null;
  } catch {
    // non-JSON error body — keep the raw text in the message
  }
  return new ApiError(res.status, code, detail, body);
}

async function send(path, options = {}, { authRequest = false } = {}) {
  const method = (options.method ?? "GET").toUpperCase();
  const headers = { "Content-Type": "application/json", ...(options.headers ?? {}) };
  if (!SAFE_METHODS.has(method) && csrfToken) headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(`${BASE}${path}`, { ...options, headers, credentials: "include" });
  if (!res.ok) {
    const error = await toApiError(res);
    if (res.status === 401 && !authRequest && unauthorizedHandler) unauthorizedHandler(error);
    throw error;
  }
  return res;
}

async function request(path, options = {}, flags = {}) {
  const res = await send(path, options, flags);
  if (res.status === 204) return null;
  return res.json();
}

export const auth = {
  getConfig: () => request("/auth/config", {}, { authRequest: true }),
  getSession: () => request("/auth/session", {}, { authRequest: true }),
  loginWithGoogle: (credential) =>
    request(
      "/auth/google",
      { method: "POST", body: JSON.stringify({ credential }) },
      { authRequest: true },
    ),
  loginLocal: () => request("/auth/local", { method: "POST" }, { authRequest: true }),
  logout: () => request("/auth/logout", { method: "POST" }, { authRequest: true }),
};

export const api = {
  listApplications: (params = {}) =>
    request(`/applications?${new URLSearchParams(params)}`),
  getApplicationTaxonomy: () => request("/applications/meta/taxonomy"),
  listDuplicateCandidates: () => request("/applications/duplicates"),
  mergeDuplicateApplications: (primaryId, duplicateId) =>
    request("/applications/duplicates/merge", {
      method: "POST",
      body: JSON.stringify({ primary_id: primaryId, duplicate_id: duplicateId }),
    }),
  getApplication: (id, signal) => request(`/applications/${id}`, { signal }),
  createApplication: (body) =>
    request("/applications", { method: "POST", body: JSON.stringify(body) }),
  updateApplication: (id, body) =>
    request(`/applications/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteApplication: (id) =>
    request(`/applications/${id}`, { method: "DELETE" }),
  exportApplications: (format = "csv") => send(`/applications/export?format=${format}`),

  getInsights: () => request("/insights"),
  getFlowData: () => request("/insights/flow"),
  getSearchPulse: (windowDays = 28) => request(`/insights/pulse?window_days=${windowDays}`),
  getRejectionData: () => request("/insights/rejection"),

  getPollerStatus: () => request("/poller/status"),
  triggerPoll: () => request("/poller/trigger", { method: "POST" }),
  getSystemStatus: () => request("/status"),

  listProspects: (params = {}) =>
    request(`/prospects?${new URLSearchParams(params)}`),
  updateProspectStatus: (id, status, applicationId = null) =>
    request(`/prospects/${id}`, { method: "PATCH", body: JSON.stringify({ status, application_id: applicationId }) }),

  listApplicationEvents: (applicationId) => request(`/applications/${applicationId}/events`),
  createApplicationEvent: (applicationId, body) =>
    request(`/applications/${applicationId}/events`, { method: "POST", body: JSON.stringify(body) }),
  getConversionData: (months = 6) => request(`/insights/conversions?months=${months}`),

  bulkWithdraw: (companies) =>
    request("/applications/bulk-withdraw", { method: "POST", body: JSON.stringify({ companies }) }),

  bulkUpdateStatus: (applicationIds, status) =>
    request("/applications/bulk-status", {
      method: "POST",
      body: JSON.stringify({ application_ids: applicationIds, status }),
    }),

  bulkDeleteApplications: (applicationIds) =>
    request("/applications/bulk-delete", {
      method: "POST",
      body: JSON.stringify({ application_ids: applicationIds }),
    }),

  linkedinPreview: (text, mode) =>
    request("/applications/linkedin-import/preview", { method: "POST", body: JSON.stringify({ text, mode }) }),

  linkedinImportConfirmed: (mode, entries) =>
    request("/applications/linkedin-import/confirmed", { method: "POST", body: JSON.stringify({ mode, entries }) }),

  listSuppressRules: () => request("/suppress-rules"),
  createSuppressRule: (body) =>
    request("/suppress-rules", { method: "POST", body: JSON.stringify(body) }),
  deleteSuppressRule: (id) => request(`/suppress-rules/${id}`, { method: "DELETE" }),
};
