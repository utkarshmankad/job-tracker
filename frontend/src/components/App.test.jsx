import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import App from "../App";
import { ThemeProvider } from "../contexts/ThemeContext";

vi.mock("../api/client", () => ({
  api: {
    getPollerStatus: vi.fn().mockResolvedValue({ status: "RUNNING", last_sync_at: null }),
    listApplications: vi.fn().mockResolvedValue([]),
    exportApplications: vi.fn(),
  },
}));

const renderApp = () =>
  render(
    <ThemeProvider>
      <App />
    </ThemeProvider>
  );

describe("App — icon button accessibility", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders Applications tab with accessible name", () => {
    renderApp();
    expect(screen.getByRole("button", { name: /applications/i })).toBeInTheDocument();
  });

  it("renders Analytics tab with accessible name", () => {
    renderApp();
    expect(screen.getByRole("button", { name: /analytics/i })).toBeInTheDocument();
  });

  it("renders Status tab with accessible name", () => {
    renderApp();
    expect(screen.getByRole("button", { name: /status/i })).toBeInTheDocument();
  });

  it("Export button is reachable by aria-label", () => {
    renderApp();
    expect(screen.getByRole("button", { name: /export/i })).toBeInTheDocument();
  });

  it("Add Application button is reachable", () => {
    renderApp();
    expect(screen.getByRole("button", { name: /add application/i })).toBeInTheDocument();
  });

  it("dark mode toggle has an accessible label", () => {
    renderApp();
    expect(screen.getByRole("button", { name: /(dark|light) mode/i })).toBeInTheDocument();
  });

  it("clicking Add Application opens the form", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("button", { name: /add application/i }));
    expect(screen.getByRole("heading", { name: /add application/i })).toBeInTheDocument();
  });

  it("form close button is reachable by aria-label", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("button", { name: /add application/i }));
    expect(screen.getByRole("button", { name: /close/i })).toBeInTheDocument();
  });

  it("closing the form hides it", async () => {
    const user = userEvent.setup();
    renderApp();
    await user.click(screen.getByRole("button", { name: /add application/i }));
    await user.click(screen.getByRole("button", { name: /close/i }));
    expect(screen.queryByRole("heading", { name: /add application/i })).not.toBeInTheDocument();
  });
});
