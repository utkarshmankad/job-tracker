import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, auth, setCsrfToken, setUnauthorizedHandler } from "./client";

function respond(status, body) {
  const text = body === undefined ? "" : JSON.stringify(body);
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => JSON.parse(text),
    text: async () => text,
  });
}

describe("api client", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(() => respond(200, {}));
    setCsrfToken(null);
    setUnauthorizedHandler(null);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("always sends credentials so the session cookie is included", async () => {
    await api.getPollerStatus();
    expect(fetch.mock.calls[0][1].credentials).toBe("include");
  });

  it("adds the CSRF token to state-changing requests only", async () => {
    setCsrfToken("csrf-1");
    await api.getPollerStatus();
    await api.triggerPoll();
    await api.deleteApplication(3);
    expect(fetch.mock.calls[0][1].headers["X-CSRF-Token"]).toBeUndefined();
    expect(fetch.mock.calls[1][1].headers["X-CSRF-Token"]).toBe("csrf-1");
    expect(fetch.mock.calls[2][1].headers["X-CSRF-Token"]).toBe("csrf-1");
  });

  it("raises ApiError with status and code", async () => {
    fetch.mockImplementationOnce(() => respond(403, { detail: "Nope", code: "csrf_failed" }));
    const error = await api.triggerPoll().catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(403);
    expect(error.code).toBe("csrf_failed");
    expect(error.detail).toBe("Nope");
  });

  it("notifies the unauthorized handler on 401 from protected endpoints", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    fetch.mockImplementationOnce(() => respond(401, { code: "session_expired" }));
    await expect(api.listApplications()).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler.mock.calls[0][0].code).toBe("session_expired");
  });

  it("does not treat auth endpoint 401s as an expired session", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    fetch.mockImplementationOnce(() => respond(401, { code: "not_authenticated" }));
    await expect(auth.getSession()).rejects.toMatchObject({ status: 401 });
    expect(handler).not.toHaveBeenCalled();
  });

  it("returns null for 204 responses", async () => {
    fetch.mockImplementationOnce(() => respond(204));
    await expect(auth.logout()).resolves.toBeNull();
  });

  it("export goes through the authenticated request path", async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    fetch.mockImplementationOnce(() => respond(401, { code: "not_authenticated" }));
    await expect(api.exportApplications("csv")).rejects.toMatchObject({ status: 401 });
    expect(fetch.mock.calls[0][1].credentials).toBe("include");
    expect(handler).toHaveBeenCalled();
  });

  it("tolerates non-JSON error bodies", async () => {
    fetch.mockImplementationOnce(() =>
      Promise.resolve({ ok: false, status: 502, text: async () => "Bad gateway" }),
    );
    const error = await api.getInsights().catch((e) => e);
    expect(error.status).toBe(502);
    expect(error.code).toBeNull();
    expect(error.message).toBe("502 Bad gateway");
  });
});
