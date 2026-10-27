import { describe, expect, it } from "vitest";
import {
  formatDate,
  formatTime,
  rangeBounds,
  reasonLabel,
  truncate,
} from "../src/format.ts";
import type { HomeAssistant } from "../src/types.ts";

const fakeHass = (time_zone: string): HomeAssistant =>
  ({
    locale: { language: "en" },
    config: { time_zone },
    language: "en",
  }) as unknown as HomeAssistant;

describe("truncate", () => {
  it("cuts to the limit plus an ellipsis and keeps the full text", () => {
    const text = "x".repeat(100);
    const { short, full } = truncate(text, 64);
    expect(short).toHaveLength(65);
    expect(short.endsWith("…")).toBe(true);
    expect(full).toBe(text);
  });
  it("leaves short text alone", () => {
    expect(truncate("abc", 64)).toEqual({ short: "abc", full: "abc" });
  });
  it("never splits a surrogate pair", () => {
    const { short } = truncate("😀".repeat(70), 64);
    expect([...short]).toHaveLength(65);
  });
});

describe("formatTime", () => {
  const iso = "2026-10-24T23:30:00+00:00";
  it("respects the time zone", () => {
    expect(formatTime(iso, fakeHass("UTC"))).toContain("Oct 24, 2026");
    expect(formatTime(iso, fakeHass("Asia/Tokyo"))).toContain("Oct 25, 2026");
    expect(formatTime(iso, fakeHass("America/Los_Angeles"))).toContain(
      "4:30:00",
    );
  });
  it("returns invalid input unchanged", () => {
    expect(formatTime("nope", fakeHass("UTC"))).toBe("nope");
  });
});

describe("reasonLabel", () => {
  it("maps known reasons and passes unknown ones through", () => {
    expect(reasonLabel("wrong_2fa_code")).toBe("Wrong two-factor code");
    expect(reasonLabel("blocked_local_only")).toBe("Blocked: local only");
    expect(reasonLabel("something_new")).toBe("something_new");
  });
});

describe("formatDate", () => {
  it("shows the calendar date only, with no time zone shift", () => {
    for (const zone of ["UTC", "Pacific/Honolulu", "Pacific/Kiritimati"]) {
      expect(formatDate("2026-10-01", fakeHass(zone))).toBe("Oct 1, 2026");
    }
  });
  it("returns other input unchanged", () => {
    expect(formatDate("2026-10-01T00:00:00Z", fakeHass("UTC"))).toBe(
      "2026-10-01T00:00:00Z",
    );
    expect(formatDate("2026-13-45", fakeHass("UTC"))).toBe("2026-13-45");
  });
});

describe("rangeBounds", () => {
  it("swaps a custom range whose start is after its end", () => {
    expect(rangeBounds("custom", "2026-10-03", "2026-10-01")).toEqual(
      rangeBounds("custom", "2026-10-01", "2026-10-03"),
    );
  });
  it("keeps a one-sided custom range as it is", () => {
    const only = rangeBounds("custom", "2026-10-03", "");
    expect(only.since).toBe(new Date(2026, 9, 3).toISOString());
    expect(only.until).toBeUndefined();
  });
});
