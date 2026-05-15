import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { formatTimeDiff, formatDate, formatPercent, formatStatus } from "./formatters";

describe("formatTimeDiff", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-16T12:00:00.000Z"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns null for null input", () => {
    expect(formatTimeDiff(null)).toBeNull();
  });

  it("returns null for undefined input", () => {
    expect(formatTimeDiff(undefined)).toBeNull();
  });

  it("returns 'just now' for 0 seconds ago", () => {
    expect(formatTimeDiff("2026-05-16T12:00:00.000Z")).toBe("just now");
  });

  it("returns 'just now' for 30 seconds ago", () => {
    expect(formatTimeDiff("2026-05-16T11:59:30.000Z")).toBe("just now");
  });

  it("returns 'just now' for 59 seconds ago", () => {
    expect(formatTimeDiff("2026-05-16T11:59:01.000Z")).toBe("just now");
  });

  it("returns '1 min ago' for exactly 60 seconds ago", () => {
    expect(formatTimeDiff("2026-05-16T11:59:00.000Z")).toBe("1 min ago");
  });

  it("returns '5 min ago' for 5 minutes ago", () => {
    expect(formatTimeDiff("2026-05-16T11:55:00.000Z")).toBe("5 min ago");
  });

  it("returns '59 min ago' for 59 minutes ago", () => {
    expect(formatTimeDiff("2026-05-16T11:01:00.000Z")).toBe("59 min ago");
  });

  it("returns '1 hr ago' for exactly 1 hour ago", () => {
    expect(formatTimeDiff("2026-05-16T11:00:00.000Z")).toBe("1 hr ago");
  });

  it("returns '3 hr ago' for 3 hours ago", () => {
    expect(formatTimeDiff("2026-05-16T09:00:00.000Z")).toBe("3 hr ago");
  });

  it("returns '23 hr ago' for 23 hours ago", () => {
    expect(formatTimeDiff("2026-05-15T13:00:00.000Z")).toBe("23 hr ago");
  });

  it("returns '1 day ago' for exactly 1 day ago", () => {
    expect(formatTimeDiff("2026-05-15T12:00:00.000Z")).toBe("1 day ago");
  });

  it("returns '2 days ago' for 2 days ago", () => {
    expect(formatTimeDiff("2026-05-14T12:00:00.000Z")).toBe("2 days ago");
  });

  it("returns '7 days ago' for a week ago", () => {
    expect(formatTimeDiff("2026-05-09T12:00:00.000Z")).toBe("7 days ago");
  });

  it("does not show three-digit minutes", () => {
    // 2 hours ago — the original bug: would show '120 min ago'
    const result = formatTimeDiff("2026-05-16T10:00:00.000Z");
    expect(result).not.toMatch(/\d{3,}\s*min/);
    expect(result).toBe("2 hr ago");
  });
});

describe("formatDate", () => {
  it("returns '—' for null", () => {
    expect(formatDate(null)).toBe("—");
  });

  it("returns '—' for undefined", () => {
    expect(formatDate(undefined)).toBe("—");
  });

  it("formats a valid ISO date", () => {
    const result = formatDate("2026-05-16T00:00:00.000Z");
    expect(result).toMatch(/\d{2}\s\w{3}\s\d{4}/);
  });
});

describe("formatPercent", () => {
  it("formats 0 as 0%", () => {
    expect(formatPercent(0)).toBe("0%");
  });

  it("formats 1 as 100%", () => {
    expect(formatPercent(1)).toBe("100%");
  });

  it("formats 0.456 as 46%", () => {
    expect(formatPercent(0.456)).toBe("46%");
  });
});

describe("formatStatus", () => {
  it("returns the status string unchanged", () => {
    expect(formatStatus("APPLIED")).toBe("APPLIED");
  });

  it("returns '—' for falsy values", () => {
    expect(formatStatus(null)).toBe("—");
    expect(formatStatus("")).toBe("—");
    expect(formatStatus(undefined)).toBe("—");
  });
});
