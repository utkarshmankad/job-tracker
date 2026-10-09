import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import SourcesPanel from "./SourcesPanel";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: {
    listCollectionSources: vi.fn(),
    listCollectionRuns: vi.fn(),
    getCollectionMetrics: vi.fn(),
    listCollectors: vi.fn(),
    listCollectionReview: vi.fn(),
    getCollectionSourceCatalog: vi.fn(),
    getCollectionRun: vi.fn(),
    createCollector: vi.fn(),
    rotateCollector: vi.fn(),
    revokeCollector: vi.fn(),
    acceptEvidence: vi.fn(),
    createApplicationFromEvidence: vi.fn(),
    dismissEvidence: vi.fn(),
    previewBrowserImport: vi.fn(),
    importFromBrowser: vi.fn(),
  },
}));

const catalog = [
  { key: "indeed", label: "Indeed", supported: true, live_verified: "2026-10-09", reason: null },
  { key: "linkedin", label: "LinkedIn", supported: false, live_verified: null,
    reason: "The new Job Tracker layout lacks safe stable row boundaries." },
  { key: "naukri", label: "Naukri", supported: false, live_verified: null,
    reason: "Unsupported until item identity, applied dates and complete inner-scroll collection can be established." },
  { key: "instahyre", label: "Instahyre", supported: false, live_verified: null,
    reason: "Instahyre has no application-history page." },
  { key: "careernet", label: "CareerNet", supported: false, live_verified: null,
    reason: "CareerNet's candidate history page has not been located." },
];

const collectorRow = (extra = {}) => ({
  id: 1, name: "My laptop", token_hint: "jtc_ab12…", scopes: ["indeed"], unsupported_scopes: [], state: "active",
  created_at: "2026-10-01T00:00:00Z", enrolled_at: "2026-10-01T00:00:00Z", last_used_at: "2026-10-09T08:00:00Z",
  rotated_at: null, revoked_at: null, ...extra,
});

const source = (id, key, status, extra = {}) => ({
  id,
  source_key: key,
  account_label: "default",
  collector_id: 1,
  last_attempt_at: "2026-10-09T08:00:00Z",
  last_success_at: status === "succeeded" ? "2026-10-09T08:00:00Z" : null,
  last_status: status,
  needs_attention: !["succeeded", null].includes(status),
  attention_reason: status,
  attention_message: status === "signed_out" ? "The browser is not signed in to this site." : "Check the site.",
  items: 3,
  ...extra,
});

const run = (id, status, extra = {}) => ({
  id,
  run_key: `run-${id}`,
  source_key: "linkedin",
  status,
  started_at: "2026-10-09T08:00:00Z",
  finished_at: "2026-10-09T08:01:00Z",
  collector_version: "0.1.0",
  adapter_version: "linkedin/0.1.0",
  items_seen: 3,
  observations_received: 3,
  created_count: 1,
  linked_count: 1,
  review_count: 1,
  unchanged_count: 0,
  error_count: 0,
  error_code: null,
  error_message: null,
  diagnostics: {},
  ...extra,
});

const reviewItem = {
  evidence: {
    id: 55,
    review_reason: "unverified_extraction_new_application",
    resolution: {
      candidates: [
        { application_id: 9, score: 70, signals: {}, application: { id: 9, company: "Northwind Robotics", role: "Engineering Manager" } },
      ],
    },
  },
  source_key: "naukri",
  company: "Northwind Robotics",
  role: "Engineering Manager",
  status: "applied",
  raw_status: "Application Sent",
  applied_on: "2026-09-12",
  observed_at: "2026-10-09T08:00:00Z",
  extraction: "unverified",
};

function mockData({ sources, runs, collectors, review } = {}) {
  api.listCollectionSources.mockResolvedValue(
    sources ?? [source(1, "linkedin", "succeeded"), source(2, "naukri", "signed_out"), source(3, "indeed", "challenged")],
  );
  api.listCollectionRuns.mockResolvedValue(runs ?? [run(7, "succeeded"), run(8, "failed", { error_message: "The page layout changed; this adapter needs maintenance." })]);
  api.getCollectionMetrics.mockResolvedValue({
    unique: { source_items: 4, observations: 5 },
    observations_by_decision: { created: 3, linked: 1, review: 1 },
    processed_across_runs: { items_processed: 9, created: 3, linked: 1, review: 1, unchanged: 4, errors: 0 },
    runs_by_status: {},
    items_by_decision: {},
    items_by_source: {},
  });
  api.listCollectors.mockResolvedValue(collectors ?? [collectorRow()]);
  api.listCollectionReview.mockResolvedValue(review ?? [reviewItem]);
  api.getCollectionSourceCatalog.mockResolvedValue(catalog);
}

async function renderPanel() {
  render(<SourcesPanel />);
  await screen.findByRole("heading", { name: "Configured sources" });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockData();
});

describe("SourcesPanel", () => {
  it("previews and imports browser-agent JSON through the protected workflow", async () => {
    const user = userEvent.setup();
    api.previewBrowserImport.mockResolvedValue({ source_key: "indeed", count: 1, observations: [{}] });
    api.importFromBrowser.mockResolvedValue({ counts: { unchanged: 1 }, results: [] });
    await renderPanel();
    const region = screen.getByRole("region", { name: "Browser Agent Import" });
    const input = within(region).getByLabelText("Application rows (JSON)");
    const rows = [{ source_item_id: "abc123", company: "Example Labs", role: "Engineer", job_url: "https://in.indeed.com/viewjob?jk=abc123" }];
    fireEvent.change(input, { target: { value: JSON.stringify(rows) } });
    await user.click(within(region).getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(api.previewBrowserImport).toHaveBeenCalledWith({ source_key: "indeed", items: rows }));
    expect(within(region).getByText("Validated 1 row(s). Ready to import.")).toBeInTheDocument();
    await user.click(within(region).getByRole("button", { name: "Import 1" }));
    await waitFor(() => expect(api.importFromBrowser).toHaveBeenCalledWith({ source_key: "indeed", items: rows }));
    expect(within(region).getByText(/Import complete: unchanged 1/)).toBeInTheDocument();
  });

  it("does not enable browser import until server preview succeeds", async () => {
    const user = userEvent.setup();
    await renderPanel();
    const region = screen.getByRole("region", { name: "Browser Agent Import" });
    expect(within(region).getByRole("button", { name: /^Import/ })).toBeDisabled();
    const input = within(region).getByLabelText("Application rows (JSON)");
    await user.clear(input);
    await user.type(input, "not-json");
    await user.click(within(region).getByRole("button", { name: "Preview" }));
    expect(within(region).getByRole("alert")).toHaveTextContent("not valid");
    expect(api.previewBrowserImport).not.toHaveBeenCalled();
  });

  it("labels every state with words, not colour alone, and flags attention", async () => {
    await renderPanel();
    const sources = screen.getByRole("region", { name: "Configured sources" });
    expect(within(sources).getByText("Collected")).toBeInTheDocument();
    expect(within(sources).getByText("Signed out")).toBeInTheDocument();
    expect(within(sources).getByText("Challenged")).toBeInTheDocument();
    const alert = screen.getAllByRole("alert")[0];
    expect(alert).toHaveTextContent("Needs your attention");
    expect(alert).toHaveTextContent("Naukri (default): The browser is not signed in to this site.");
  });

  it("separates unique stored records from totals added up across runs", async () => {
    await renderPanel();
    const totals = screen.getByRole("region", { name: "Totals" });
    const value = (group, label) => within(group).getByText(label).closest("div").querySelector("dd").textContent;
    const unique = totals.querySelector('dl[aria-labelledby="totals-unique"]');
    const runs = totals.querySelector('dl[aria-labelledby="totals-processed"]');
    expect(value(unique, "Unique source items")).toBe("4");
    expect(value(unique, "Unique observations")).toBe("5");
    expect(value(unique, "Imported (new)")).toBe("3");
    expect(value(unique, "Linked")).toBe("1");
    expect(value(unique, "Needs review")).toBe("1");
    expect(value(runs, "Items processed across runs")).toBe("9");
    expect(value(runs, "Already known")).toBe("4");
    expect(value(runs, "Failed items")).toBe("0");
    // A run-level sum is never labelled as a unique count.
    expect(within(totals).queryByText("Observed")).not.toBeInTheDocument();
    expect(within(unique).queryByText("Items processed across runs")).not.toBeInTheDocument();
  });

  it("inspects a run in place", async () => {
    const user = userEvent.setup();
    api.getCollectionRun.mockResolvedValue({
      run: run(7, "succeeded"),
      observations: [
        { id: 1, item_id: 1, observed_at: "2026-10-09T08:00:00Z", extraction: "unverified", decision: "review",
          decision_reason: "unverified_extraction_new_application", confidence: null, evidence_id: 5,
          company: "Contoso Analytics", role: "Staff Engineer", status: "applied", application_id: null },
      ],
    });
    await renderPanel();
    const runs = screen.getByRole("region", { name: "Recent runs" });
    expect(within(runs).getByText(/layout changed/)).toBeInTheDocument();
    const inspect = within(runs).getAllByRole("button", { name: "Inspect" })[0];
    expect(inspect).toHaveAttribute("aria-expanded", "false");
    await user.click(inspect);
    expect(await screen.findByText("Contoso Analytics")).toBeInTheDocument();
    expect(screen.getByText(/Needs review/, { selector: "span" })).toBeInTheDocument();
    expect(within(runs).getByRole("button", { name: "Hide" })).toHaveAttribute("aria-expanded", "true");
  });

  it("creates a collector and shows the one-time setup command, never a credential", async () => {
    const user = userEvent.setup();
    api.createCollector.mockResolvedValue({
      collector: { id: 2, name: "Desk", token_hint: "jtc_cd34…", scopes: ["indeed"], state: "pending_enrollment" },
      setup_code: "one-time-code-abcdefghijklmnop",
      expires_at: "2026-10-09T08:10:00Z",
      command: "python scripts/collect.py enroll --api-url https://tracker.example.test --code one-time-code-abcdefghijklmnop",
    });
    await renderPanel();
    await user.click(screen.getByRole("button", { name: "Set up a collector" }));
    const form = screen.getByRole("form", { name: "Set up a collector" });
    await user.clear(within(form).getByLabelText("Collector name"));
    await user.type(within(form).getByLabelText("Collector name"), "Desk");
    await user.click(within(form).getByRole("button", { name: "Create setup command" }));
    expect(api.createCollector).toHaveBeenCalledWith("Desk", ["indeed"]);
    const panel = await screen.findByRole("status");
    expect(panel).toHaveTextContent("scripts/collect.py enroll");
    expect(panel).toHaveTextContent("never shown here");
    expect(document.body.textContent).not.toMatch(/jtc_[0-9a-f]{16}\./);
  });

  it("refuses a collector without sources", async () => {
    const user = userEvent.setup();
    await renderPanel();
    await user.click(screen.getByRole("button", { name: "Set up a collector" }));
    const form = screen.getByRole("form", { name: "Set up a collector" });
    await user.click(within(form).getByRole("checkbox", { name: /^Indeed/ }));
    await user.click(within(form).getByRole("button", { name: "Create setup command" }));
    expect(within(form).getByRole("alert")).toHaveTextContent("Choose at least one source.");
    expect(api.createCollector).not.toHaveBeenCalled();
  });

  it("defaults a new collector to Indeed only and disables unsupported sources with a reason", async () => {
    const user = userEvent.setup();
    await renderPanel();
    await user.click(screen.getByRole("button", { name: "Set up a collector" }));
    const form = screen.getByRole("form", { name: "Set up a collector" });
    const indeed = within(form).getByRole("checkbox", { name: /^Indeed/ });
    expect(indeed).toBeChecked();
    expect(indeed).toBeEnabled();
    expect(within(form).getByText("Live verified")).toBeInTheDocument();
    for (const entry of catalog.filter((c) => !c.supported)) {
      const box = within(form).getByRole("checkbox", { name: new RegExp(`^${entry.label}`) });
      expect(box).toBeDisabled();
      expect(box).not.toBeChecked();
      expect(box).toHaveAccessibleDescription(entry.reason);
    }
    expect(within(form).getAllByText("Unsupported")).toHaveLength(4);
    // A disabled source cannot be added, even by clicking it.
    await user.click(within(form).getByRole("checkbox", { name: /^LinkedIn/ }));
    await user.click(within(form).getByRole("button", { name: "Create setup command" }));
    expect(api.createCollector).toHaveBeenCalledWith("My laptop", ["indeed"]);
  });

  it("keeps showing a collector with a now-unsupported scope, without offering rotation", async () => {
    mockData({
      collectors: [collectorRow({ scopes: ["indeed", "linkedin"], unsupported_scopes: ["linkedin"] })],
    });
    await renderPanel();
    const collectors = screen.getByRole("region", { name: "Collectors" });
    expect(within(collectors).getByText(/sources: Indeed, LinkedIn \(unsupported\)/)).toBeInTheDocument();
    expect(within(collectors).getByText(/no longer supported/)).toBeInTheDocument();
    expect(within(collectors).queryByRole("button", { name: /Rotate/ })).not.toBeInTheDocument();
    expect(within(collectors).getByRole("button", { name: "Revoke" })).toBeInTheDocument();
  });

  it("offers rotation for a collector with only supported sources", async () => {
    await renderPanel();
    const collectors = screen.getByRole("region", { name: "Collectors" });
    expect(within(collectors).getByText(/sources: Indeed ·/)).toBeInTheDocument();
    expect(within(collectors).getByRole("button", { name: /Rotate/ })).toBeInTheDocument();
  });

  it("shows the browser workflow without bearer-credential controls", async () => {
    mockData({
      collectors: [collectorRow({
        name: "ChatGPT browser workflow",
        token_hint: "signed-in browser",
        scopes: ["linkedin", "indeed"],
        unsupported_scopes: ["linkedin"],
        state: "browser_workflow",
      })],
    });
    await renderPanel();
    const collectors = screen.getByRole("region", { name: "Collectors" });
    expect(within(collectors).getByText(/Uses signed-in browser/)).toBeInTheDocument();
    expect(within(collectors).queryByRole("button", { name: /Rotate/ })).not.toBeInTheDocument();
    expect(within(collectors).queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
  });

  it("asks before rotating or revoking", async () => {
    const user = userEvent.setup();
    api.revokeCollector.mockResolvedValue({});
    await renderPanel();
    const collectors = screen.getByRole("region", { name: "Collectors" });
    await user.click(within(collectors).getByRole("button", { name: "Revoke" }));
    expect(api.revokeCollector).not.toHaveBeenCalled();
    expect(within(collectors).getByText(/Revoke permanently/)).toBeInTheDocument();
    await user.click(within(collectors).getByRole("button", { name: "Yes, revoke" }));
    await waitFor(() => expect(api.revokeCollector).toHaveBeenCalledWith(1));
  });

  it("decides review items with the existing evidence actions", async () => {
    const user = userEvent.setup();
    api.acceptEvidence.mockResolvedValue({});
    api.createApplicationFromEvidence.mockResolvedValue({});
    api.dismissEvidence.mockResolvedValue({});
    await renderPanel();
    const review = screen.getByRole("region", { name: /Needs review/ });
    expect(review).toHaveTextContent("Naukri · site status Application Sent");
    await user.click(within(review).getByRole("button", { name: /Same as #9/ }));
    await waitFor(() => expect(api.acceptEvidence).toHaveBeenCalledWith(55, 9));
    await user.click(within(review).getByRole("button", { name: "Create application" }));
    await waitFor(() => expect(api.createApplicationFromEvidence).toHaveBeenCalledWith(55));
    await user.click(within(review).getByRole("button", { name: "Not an application" }));
    await waitFor(() => expect(api.dismissEvidence).toHaveBeenCalledWith(55));
  });

  it("has no control that acts on a job site", async () => {
    await renderPanel();
    const labels = screen.getAllByRole("button").map((b) => b.textContent.toLowerCase());
    for (const forbidden of ["apply now", "easy apply", "withdraw", "send message", "accept interview", "submit application"]) {
      expect(labels.some((l) => l.includes(forbidden))).toBe(false);
    }
  });

  it("explains empty and failed loads", async () => {
    mockData({ sources: [], runs: [], collectors: [], review: [] });
    await renderPanel();
    expect(screen.getByText("No collection has run yet. Set up a collector below.")).toBeInTheDocument();
    expect(screen.getByText("Nothing waiting.")).toBeInTheDocument();
  });

  it("reports a load failure", async () => {
    api.listCollectionSources.mockRejectedValue(new Error("down"));
    render(<SourcesPanel />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Collection status could not be loaded.");
  });
});
