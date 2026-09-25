import { describe, expect, it } from "vitest";

import { formatInstant, formatValue, shortId, validity } from "./format";

describe("format", () => {
  it("renders values without losing type information", () => {
    expect(formatValue("ACTIVE")).toBe("ACTIVE");
    expect(formatValue(5000)).toBe("5000");
    expect(formatValue({ amount: 5000 })).toBe('{"amount":5000}');
    expect(formatValue(null)).toBe("—");
  });

  it("shows instants in UTC", () => {
    expect(formatInstant("2026-01-15T05:30:00-05:00")).toBe("2026-01-15 10:30:00 UTC");
    expect(formatInstant(null)).toBe("—");
  });

  it("shows open validity", () => {
    expect(validity("2026-01-15T09:00:00Z", null)).toBe("2026-01-15 09:00:00 UTC → open");
  });

  it("shortens ids", () => {
    expect(shortId("0b9f2c3e-0000-4000-8000-000000000001")).toBe("0b9f2c3e…");
  });
});
