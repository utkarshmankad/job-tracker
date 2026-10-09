import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import AnalyticsPanel from "./AnalyticsPanel";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: {
    getFlowData: vi.fn(),
    getInsights: vi.fn(),
    getRejectionData: vi.fn(),
    getSearchPulse: vi.fn(),
  },
}));

// Recharts needs a measurable container; jsdom has none.
vi.mock("recharts", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, ResponsiveContainer: ({ children }) => <div>{children}</div> };
});

const flow = {
  insufficient_data: false,
  kpis: { total: 208, active: 21, stale: 128, withdrawn: 29, interview_rate: 0.0577, offer_rate: 0.0096 },
  nodes: [
    { id: "Applied", count: 208 },
    { id: "Shortlisted", count: 16 },
    { id: "Interview", count: 12 },
    { id: "Offer / Joined", count: 2 },
  ],
  outcomes: [
    { name: "Active", value: 21, color: "#3b82f6" },
    { name: "Stale", value: 128, color: "#eab308" },
  ],
  weekly_activity: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  api.getFlowData.mockResolvedValue(flow);
  api.getInsights.mockResolvedValue({ channels: [], methods: [], insights: [] });
  api.getRejectionData.mockResolvedValue({ insufficient_data: true });
  api.getSearchPulse.mockResolvedValue(null);
});

describe("AnalyticsPanel terminology", () => {
  it("names the open pipeline distinctly from active (unmerged) records", async () => {
    render(<AnalyticsPanel />);
    expect(await screen.findByText("Open pipeline")).toBeInTheDocument();
    expect(screen.getByText("not closed, offered or stale")).toBeInTheDocument();
    expect(screen.getByText("merged duplicates excluded")).toBeInTheDocument();
    expect(screen.queryByText("in progress")).not.toBeInTheDocument();
    expect(screen.queryByText("Active", { exact: true })).not.toBeInTheDocument();
  });

  it("labels the first funnel stage as all applications and maps the outcome name", async () => {
    render(<AnalyticsPanel />);
    expect(await screen.findByText("Applied (all)")).toBeInTheDocument();
    expect(screen.getByText("Open pipeline (21)")).toBeInTheDocument();
    expect(screen.queryByText("Active (21)")).not.toBeInTheDocument();
  });
});
