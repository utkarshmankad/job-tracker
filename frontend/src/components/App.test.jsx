import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import App from "../App";
import { auth } from "../api/client";
import { ThemeProvider } from "../contexts/ThemeContext";
import { AuthProvider } from "../contexts/AuthContext";

vi.mock("../api/client", () => ({
  api: {
    getPollerStatus: vi.fn().mockResolvedValue({ status: "RUNNING", last_sync_at: null }),
    listApplications: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    listProspects: vi.fn().mockResolvedValue([]),
    getFlowData: vi.fn().mockResolvedValue({ insufficient_data: true }),
    getSearchPulse: vi.fn().mockResolvedValue({ recent: {}, matured_cohort: {}, activity: [] }),
    getApplicationTaxonomy: vi.fn().mockResolvedValue({ sources: [], methods: [] }),
    listDuplicateCandidates: vi.fn().mockResolvedValue([]),
    listMerges: vi.fn().mockResolvedValue({ items: [], total: 0, page: 1, page_size: 20 }),
    getConversionData: vi.fn().mockResolvedValue({ opportunities: {}, interviews: { outcomes: {} }, by_source: [], by_method: [] }),
    getInsights: vi.fn().mockResolvedValue({ channels: [], insights: [] }),
    getRejectionData: vi.fn().mockResolvedValue({ insufficient_data: true }),
    exportApplications: vi.fn(),
  },
  auth: {
    getSession: vi.fn(),
    getConfig: vi.fn(),
    loginWithGoogle: vi.fn(),
    loginLocal: vi.fn(),
    logout: vi.fn(),
  },
  setCsrfToken: vi.fn(),
  setUnauthorizedHandler: vi.fn(),
}));

// These tests cover the signed-in dashboard; sign-in itself is covered in Auth.test.jsx.
const renderApp = async () => {
  render(
    <ThemeProvider>
      <AuthProvider>
        <App />
      </AuthProvider>
    </ThemeProvider>
  );
  await screen.findByRole("button", { name: /sign out/i });
};

describe("App — icon button accessibility", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.getSession.mockResolvedValue({
      user: { email: "owner@example.com" },
      expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      csrf_token: "csrf-token",
    });
  });

  it("renders Applications tab with accessible name", async () => {
    await renderApp();
    expect(screen.getByRole("button", { name: /applications/i })).toBeInTheDocument();
  });

  it("renders Home tab with accessible name", async () => {
    await renderApp();
    expect(screen.getByRole("button", { name: /^home$/i })).toBeInTheDocument();
  });

  it("renders Opportunities tab with accessible name", async () => {
    await renderApp();
    expect(screen.getByRole("button", { name: /opportunities/i })).toBeInTheDocument();
  });

  it("renders Status tab with accessible name", async () => {
    await renderApp();
    expect(screen.getByRole("button", { name: /status/i })).toBeInTheDocument();
  });

  it("Export button is reachable by aria-label", async () => {
    await renderApp();
    expect(screen.getByRole("button", { name: /export/i })).toBeInTheDocument();
  });

  it("Add Application button is reachable", async () => {
    await renderApp();
    expect(screen.getByRole("button", { name: /add application/i })).toBeInTheDocument();
  });

  it("shows the automatic deployment status", async () => {
    await renderApp();
    expect(
      screen.getByText(/production updates automatically after changes pass ci on main/i)
    ).toBeInTheDocument();
  });

  it("dark mode toggle has an accessible label", async () => {
    await renderApp();
    expect(screen.getByRole("button", { name: /(dark|light) mode/i })).toBeInTheDocument();
  });

  it("shows the signed-in account and a sign-out button", async () => {
    await renderApp();
    expect(screen.getByText("owner@example.com")).toBeInTheDocument();
    expect(screen.getByText(/signed in as/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign out/i })).toBeInTheDocument();
  });

  it("clicking Add Application opens the form", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.click(screen.getByRole("button", { name: /add application/i }));
    expect(screen.getByRole("heading", { name: /add application/i })).toBeInTheDocument();
  });

  it("form close button is reachable by aria-label", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.click(screen.getByRole("button", { name: /add application/i }));
    expect(screen.getByRole("button", { name: /close/i })).toBeInTheDocument();
  });

  it("closing the form hides it", async () => {
    const user = userEvent.setup();
    await renderApp();
    await user.click(screen.getByRole("button", { name: /add application/i }));
    await user.click(screen.getByRole("button", { name: /close/i }));
    expect(screen.queryByRole("heading", { name: /add application/i })).not.toBeInTheDocument();
  });
});
