import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ThemeProvider } from "../contexts/ThemeContext";
import { AuthProvider } from "../contexts/AuthContext";

// End-to-end through the real API client and AuthProvider: only `fetch` and Google
// Identity Services are stubbed.

const OWNER = "owner@example.com";

const DATA = {
  "/poller/status": { status: "RUNNING", last_sync_at: null },
  "/applications": { items: [], total: 0, page: 1, page_size: 50 },
  "/applications/meta/taxonomy": { sources: [], methods: [] },
  "/applications/duplicates": [],
  "/prospects": [],
  "/insights": { channels: [], insights: [] },
  "/insights/flow": { insufficient_data: true },
  "/insights/pulse": { recent: {}, matured_cohort: {}, activity: [] },
  "/insights/rejection": { insufficient_data: true },
  "/insights/conversions": { opportunities: {}, interviews: { outcomes: {} }, by_source: [], by_method: [] },
};

function response(status, body) {
  const text = body === undefined ? "" : JSON.stringify(body);
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => JSON.parse(text),
    text: async () => text,
  });
}

function session(expiresInMs = 60 * 60 * 1000) {
  return {
    user: { email: OWNER },
    expires_at: new Date(Date.now() + expiresInMs).toISOString(),
    csrf_token: "csrf-abc",
  };
}

let server;

function installServer(overrides = {}) {
  let nonceCounter = 0;
  server = {
    session: null,
    expired: false,
    config: () => ({
      mode: "google",
      configured: true,
      google_client_id: "client-id.apps.googleusercontent.com",
      nonce: `nonce-${++nonceCounter}`,
    }),
    google: () => {
      server.session = session();
      return response(200, server.session);
    },
    calls: [],
    ...overrides,
  };
  globalThis.fetch = vi.fn((url, options = {}) => {
    const path = new URL(url).pathname.replace(/^\/api\/v1/, "");
    const method = (options.method ?? "GET").toUpperCase();
    server.calls.push({ path, method, options });
    if (path === "/auth/session") {
      return server.session
        ? response(200, server.session)
        : response(401, { detail: "Authentication required.", code: "not_authenticated" });
    }
    if (path === "/auth/config") return response(200, server.config());
    if (path === "/auth/google") return server.google(JSON.parse(options.body));
    if (path === "/auth/local") {
      server.session = session();
      return response(200, server.session);
    }
    if (path === "/auth/logout") {
      server.session = null;
      return response(204);
    }
    if (!server.session) {
      return response(401, { detail: "Authentication required.", code: "not_authenticated" });
    }
    if (server.expired) {
      return response(401, { detail: "Your session has expired.", code: "session_expired" });
    }
    return response(200, DATA[path] ?? {});
  });
}

function installGoogle() {
  const google = { callback: null, initialize: vi.fn(), renderButton: vi.fn(), disableAutoSelect: vi.fn() };
  google.initialize.mockImplementation((options) => {
    google.callback = options.callback;
  });
  window.google = { accounts: { id: google } };
  return google;
}

const dataCalls = () => server.calls.filter((c) => !c.path.startsWith("/auth/"));

function renderApp() {
  return render(
    <ThemeProvider>
      <AuthProvider>
        <App />
      </AuthProvider>
    </ThemeProvider>
  );
}

describe("authentication", () => {
  let google;

  beforeEach(() => {
    installServer();
    google = installGoogle();
  });

  afterEach(() => {
    delete window.google;
    vi.restoreAllMocks();
  });

  it("shows the login screen and requests no tracker data before sign-in", async () => {
    renderApp();
    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
    await waitFor(() => expect(google.renderButton).toHaveBeenCalled());
    expect(dataCalls()).toEqual([]);
    expect(screen.queryByRole("button", { name: /add application/i })).not.toBeInTheDocument();
  });

  it("signs in with Google and loads protected data with the session cookie", async () => {
    renderApp();
    await waitFor(() => expect(google.initialize).toHaveBeenCalled());
    const options = google.initialize.mock.calls.at(-1)[0];
    expect(options.client_id).toBe("client-id.apps.googleusercontent.com");
    expect(options.nonce).toBe("nonce-1");

    await act(async () => {
      await google.callback({ credential: "google-id-token" });
    });

    expect(await screen.findByRole("button", { name: /sign out/i })).toBeInTheDocument();
    expect(screen.getByText(OWNER)).toBeInTheDocument();

    const login = server.calls.find((c) => c.path === "/auth/google");
    expect(JSON.parse(login.options.body)).toEqual({ credential: "google-id-token" });
    await waitFor(() => expect(dataCalls().length).toBeGreaterThan(0));
    for (const call of server.calls) expect(call.options.credentials).toBe("include");
  });

  it("rejects an unauthorized Google account and offers a fresh attempt", async () => {
    server.google = () =>
      response(403, {
        detail: "This Google account is not allowed to use Job Tracker.",
        code: "account_not_allowed",
      });
    renderApp();
    await waitFor(() => expect(google.initialize).toHaveBeenCalled());

    await act(async () => {
      await google.callback({ credential: "someone-elses-token" });
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(/isn't allowed/i);
    expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
    // A fresh single-use nonce is fetched for the next attempt.
    await waitFor(() =>
      expect(google.initialize.mock.calls.at(-1)[0].nonce).toBe("nonce-2"),
    );
    expect(dataCalls()).toEqual([]);
  });

  it("explains rate limiting on sign-in", async () => {
    server.google = () => response(429, { detail: "Too many requests.", code: "rate_limited" });
    renderApp();
    await waitFor(() => expect(google.initialize).toHaveBeenCalled());
    await act(async () => {
      await google.callback({ credential: "x" });
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(/too many sign-in attempts/i);
  });

  it("restores an existing session without showing the login screen", async () => {
    server.session = session();
    renderApp();
    expect(await screen.findByRole("button", { name: /sign out/i })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^sign in$/i })).not.toBeInTheDocument();
  });

  it("signs out, sends the CSRF token and returns to login", async () => {
    server.session = session();
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: /sign out/i }));

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/you have signed out/i);
    const logout = server.calls.find((c) => c.path === "/auth/logout");
    expect(logout.method).toBe("POST");
    expect(logout.options.headers["X-CSRF-Token"]).toBe("csrf-abc");
    expect(google.disableAutoSelect).toHaveBeenCalled();
  });

  it("returns to login when a protected request comes back 401", async () => {
    server.session = session();
    const user = userEvent.setup();
    renderApp();
    await screen.findByRole("button", { name: /sign out/i });

    server.expired = true;
    await user.click(screen.getByRole("button", { name: /^opportunities$/i }));

    expect(await screen.findByRole("heading", { name: /sign in/i })).toBeInTheDocument();
    expect(screen.getByText(/your session has expired/i)).toBeInTheDocument();
  });

  it("returns to login when the session lifetime ends while idle", async () => {
    server.session = session(150);
    renderApp();
    await screen.findByRole("button", { name: /sign out/i });
    expect(await screen.findByText(/your session has expired/i, {}, { timeout: 2000 })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /sign in/i })).toBeInTheDocument();
  });

  it("supports explicit local developer sign-in", async () => {
    server.config = () => ({ mode: "local", configured: true, google_client_id: null, nonce: null });
    const user = userEvent.setup();
    renderApp();
    await user.click(await screen.findByRole("button", { name: /continue as local developer/i }));
    expect(await screen.findByRole("button", { name: /sign out/i })).toBeInTheDocument();
    expect(server.calls.some((c) => c.path === "/auth/local" && c.method === "POST")).toBe(true);
  });

  it("says when sign-in is not configured on the server", async () => {
    server.config = () => ({ mode: "google", configured: false, google_client_id: null, nonce: null });
    renderApp();
    expect(await screen.findByText(/isn't configured on the server/i)).toBeInTheDocument();
    expect(google.initialize).not.toHaveBeenCalled();
  });

  it("reports an unreachable server instead of a blank screen", async () => {
    globalThis.fetch = vi.fn(() => Promise.reject(new TypeError("Failed to fetch")));
    renderApp();
    expect(await screen.findByText(/could not reach the server/i)).toBeInTheDocument();
  });

  it("keeps the theme toggle available on the login screen", async () => {
    const user = userEvent.setup();
    renderApp();
    const toggle = await screen.findByRole("button", { name: /(dark|light) mode/i });
    const before = document.documentElement.classList.contains("dark");
    await user.click(toggle);
    expect(document.documentElement.classList.contains("dark")).toBe(!before);
  });
});
