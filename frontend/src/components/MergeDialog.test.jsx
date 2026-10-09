import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi, beforeEach } from "vitest";
import MergeDialog from "./MergeDialog";
import MergeNotice from "./MergeNotice";
import DataQualityPanel from "./DataQualityPanel";
import { api, ApiError } from "../api/client";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal();
  return {
    ApiError: actual.ApiError,
    api: {
      previewMerge: vi.fn(),
      executeMerge: vi.fn(),
      undoMerge: vi.fn(),
      listMerges: vi.fn(),
      listDuplicateCandidates: vi.fn(),
      listApplications: vi.fn(),
      dismissDuplicate: vi.fn(),
    },
  };
});

const app = (id, extra = {}) => ({
  id,
  company: "Acme",
  role: "Engineer",
  source_portal: "LinkedIn",
  application_method: "Easy Apply",
  current_status: "Applied",
  applied_date: "2026-06-01",
  record_state: "active",
  external_job_id: null,
  evidence_count: 1,
  status_history_count: 1,
  event_count: 1,
  human_confirmed: false,
  filled_fields: 4,
  ...extra,
});

const field = (name, values, extra = {}) => ({
  name,
  values,
  proposed: Object.values(values)[0],
  proposed_from: Number(Object.keys(values)[0]),
  rule: "only_value",
  conflict: false,
  ...extra,
});

function makePreview(overrides = {}) {
  return {
    application_ids: [1, 2, 3],
    survivor_id: 1,
    default_survivor_id: 1,
    preview_token: "token-abc-123",
    safe: true,
    blocking: [],
    conflicts: ["role"],
    warnings: ["Role titles differ; the survivor keeps only the role you choose."],
    applications: [app(1), app(2, { role: "Senior Engineer" }), app(3)],
    fields: [
      field("company", { 1: "Acme", 2: "Acme", 3: "Acme" }),
      field(
        "role",
        { 1: "Engineer", 2: "Senior Engineer", 3: "Engineer" },
        { rule: "survivor_preferred", conflict: true },
      ),
      field("applied_date", { 1: "2026-06-01", 2: "2026-05-30", 3: null }, {
        proposed: "2026-05-30",
        proposed_from: 2,
        rule: "earliest",
      }),
    ],
    relink: { evidence: [11, 12], status_history: [21, 22], events: [31, 32], thread_links: [], prospects: [] },
    superseded: { status_history: [22], events: [32] },
    counts: { evidence: 3, status_history: 3, events: 3, status_history_superseded: 1, events_superseded: 1 },
    note: "Nothing is permanently deleted.",
    ...overrides,
  };
}

const operation = (extra = {}) => ({
  id: 7,
  status: "applied",
  survivor_application_id: 1,
  source_application_ids: [2, 3],
  applications: [
    { id: 1, company: "Acme", role: "Engineer", survivor: true },
    { id: 2, company: "Acme", role: "Senior Engineer", survivor: false },
    { id: 3, company: "Acme", role: "Engineer", survivor: false },
  ],
  counts: {},
  superseded: {},
  field_values: {},
  reason: null,
  initiated_by: "owner@example.com",
  created_at: "2026-06-02T10:00:00Z",
  undone_at: null,
  undone_by: null,
  undo_metadata: null,
  ...extra,
});

const IDS = [1, 2, 3];

async function openDialog(props = {}) {
  const onMerged = vi.fn();
  const onClose = vi.fn();
  render(<MergeDialog applicationIds={IDS} onClose={onClose} onMerged={onMerged} {...props} />);
  await screen.findByRole("group", { name: /record to keep/i });
  return { onMerged, onClose };
}

const confirmButton = () => screen.getByRole("button", { name: /confirm merge/i });

beforeEach(() => {
  vi.clearAllMocks();
  api.previewMerge.mockResolvedValue(makePreview());
});

describe("MergeDialog", () => {
  it("renders the comparison, counts, warnings and the no-deletion note", async () => {
    await openDialog();
    const dialog = screen.getByRole("dialog", { name: /merge 3 applications/i });
    expect(within(dialog).getByText(/nothing is permanently deleted/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/role titles differ/i)).toBeInTheDocument();
    expect(screen.getByTestId("merge-counts")).toHaveTextContent(
      "2 emails, 2 status changes and 2 milestones move to #1",
    );
    expect(screen.getByTestId("merge-counts")).toHaveTextContent("2 duplicate entries will be hidden");
    expect(within(dialog).getByText("Kept")).toBeInTheDocument();
    expect(within(dialog).getAllByText(/will be hidden \(merged\)/i)).toHaveLength(2);
    expect(api.previewMerge).toHaveBeenCalledWith(IDS, null);
  });

  it("keeps Confirm disabled until every conflict is chosen", async () => {
    const user = userEvent.setup();
    await openDialog();
    expect(confirmButton()).toBeDisabled();
    expect(screen.getByText(/choose a value for: role/i)).toBeInTheDocument();
    const roleGroup = screen.getByRole("group", { name: "Role" });
    await user.click(within(roleGroup).getByRole("radio", { name: /senior engineer/i }));
    expect(confirmButton()).toBeEnabled();
  });

  it("refetches the preview when a different survivor is chosen", async () => {
    const user = userEvent.setup();
    await openDialog();
    api.previewMerge.mockResolvedValueOnce(makePreview({ survivor_id: 2 }));
    await user.click(screen.getByRole("radio", { name: /keep #2/i }));
    await waitFor(() => expect(api.previewMerge).toHaveBeenLastCalledWith(IDS, 2));
    await waitFor(() => expect(screen.getByRole("radio", { name: /keep #2/i })).toBeChecked());
  });

  it("sends the explicit choices, token and an idempotency key", async () => {
    const user = userEvent.setup();
    api.executeMerge.mockResolvedValue(operation());
    const { onMerged } = await openDialog();
    await user.click(within(screen.getByRole("group", { name: "Role" })).getByRole("radio", { name: /senior engineer/i }));
    await user.click(confirmButton());
    await waitFor(() => expect(onMerged).toHaveBeenCalledWith(operation()));
    const body = api.executeMerge.mock.calls[0][0];
    expect(body).toMatchObject({
      application_ids: IDS,
      survivor_id: 1,
      field_choices: { role: 2 },
      preview_token: "token-abc-123",
    });
    expect(body.idempotency_key).toMatch(/^[A-Za-z0-9_-]{8,100}$/);
  });

  it("disables Confirm and lists the reasons when the merge is unsafe", async () => {
    api.previewMerge.mockResolvedValue(
      makePreview({ safe: false, conflicts: [], blocking: ["Application 2 is already merged into another record."] }),
    );
    await openDialog();
    expect(screen.getByText(/already merged into another record/i)).toBeInTheDocument();
    expect(confirmButton()).toBeDisabled();
  });

  it("marks the preview stale on 409 and offers a refresh", async () => {
    const user = userEvent.setup();
    api.executeMerge.mockRejectedValue(
      new ApiError(409, null, "These applications changed since the preview. Review the merge again."),
    );
    const { onMerged } = await openDialog();
    await user.click(within(screen.getByRole("group", { name: "Role" })).getByRole("radio", { name: /senior engineer/i }));
    await user.click(confirmButton());
    expect(await screen.findByText(/changed since the preview/i)).toBeInTheDocument();
    expect(confirmButton()).toBeDisabled();
    expect(onMerged).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: /refresh preview/i }));
    await waitFor(() => expect(api.previewMerge).toHaveBeenCalledTimes(2));
  });

  it("shows a load failure", async () => {
    api.previewMerge.mockRejectedValue(new ApiError(422, null, "Each application may appear only once"));
    render(<MergeDialog applicationIds={IDS} onClose={vi.fn()} onMerged={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/appear only once/i);
    expect(confirmButton()).toBeDisabled();
  });

  it("closes on Escape", async () => {
    const user = userEvent.setup();
    const { onClose } = await openDialog();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });
});

describe("MergeNotice", () => {
  it("undoes a merge", async () => {
    const user = userEvent.setup();
    const onUndone = vi.fn();
    api.undoMerge.mockResolvedValue(operation({ status: "undone" }));
    render(<MergeNotice operation={operation()} onUndone={onUndone} onDismiss={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent(/merged 3 applications into #1/i);
    await user.click(screen.getByRole("button", { name: /^undo$/i }));
    expect(await screen.findByText(/merge undone/i)).toBeInTheDocument();
    expect(api.undoMerge).toHaveBeenCalledWith(7);
    expect(onUndone).toHaveBeenCalled();
  });

  it("explains an unsafe undo instead of overwriting", async () => {
    const user = userEvent.setup();
    const payload = {
      detail: {
        message: "This merge can't be undone automatically without overwriting newer changes.",
        conflicts: ["Survivor field role changed after the merge."],
      },
    };
    api.undoMerge.mockRejectedValue(new ApiError(409, null, payload.detail, null, payload));
    render(<MergeNotice operation={operation()} onUndone={vi.fn()} onDismiss={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /^undo$/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/without overwriting newer changes/i);
    expect(alert).toHaveTextContent(/role changed after the merge/i);
  });
});

describe("DataQualityPanel merge review", () => {
  const pair = {
    primary: app(1),
    duplicate: app(2),
    score: 92,
    reasons: ["Same company and role"],
    pair_key: "1:2",
  };

  beforeEach(() => {
    api.listDuplicateCandidates.mockResolvedValue([pair]);
    api.listApplications.mockResolvedValue({ items: [] });
    api.listMerges.mockResolvedValue({ items: [operation()], total: 1, page: 1, page_size: 20 });
  });

  it("opens a merge review for a suggestion rather than merging directly", async () => {
    const user = userEvent.setup();
    api.previewMerge.mockResolvedValue(makePreview({ application_ids: [1, 2] }));
    render(<DataQualityPanel />);
    await user.click(await screen.findByRole("button", { name: /review merge/i }));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(api.previewMerge).toHaveBeenCalledWith([1, 2], null);
    expect(api.executeMerge).not.toHaveBeenCalled();
  });

  it("dismisses a suggestion", async () => {
    const user = userEvent.setup();
    api.dismissDuplicate.mockResolvedValue({ dismissed: true });
    render(<DataQualityPanel />);
    await user.click(await screen.findByRole("button", { name: /not a duplicate/i }));
    expect(api.dismissDuplicate).toHaveBeenCalledWith([1, 2]);
  });

  it("lists merge history and undoes from it", async () => {
    const user = userEvent.setup();
    api.undoMerge.mockResolvedValue(operation({ status: "undone" }));
    render(<DataQualityPanel />);
    const history = await screen.findByRole("region", { name: /merge history/i });
    expect(within(history).getByText(/merge #7/i)).toBeInTheDocument();
    await user.click(within(history).getByRole("button", { name: /undo merge #7/i }));
    expect(api.undoMerge).toHaveBeenCalledWith(7);
    await waitFor(() => expect(api.listMerges).toHaveBeenCalledTimes(2));
  });
});
