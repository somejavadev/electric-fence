import { afterEach, describe, expect, it } from "vitest";
import { EfAudit, valueLines } from "../src/views/ef-audit.ts";
import { EfChangeRow } from "../src/components/ef-change-row.ts";
import type { ChangeItem } from "../src/types.ts";
import { change, fakeApi } from "./helpers.ts";

async function mountAudit(
  details: Record<string, unknown>,
  over: Partial<ChangeItem> = {},
) {
  const api = fakeApi({
    changes: { items: [change({ details, ...over })], next_before_id: null },
    users: { items: [] },
  });
  const el = document.createElement("ef-audit") as EfAudit;
  (el as unknown as { hass: unknown }).hass = api.hass;
  document.body.append(el);
  for (let i = 0; i < 20; i += 1) {
    await Promise.resolve();
  }
  await el.updateComplete;
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
  }
  await el.updateComplete;
  return el;
}

const squash = (t: string | null | undefined) => t?.replace(/\s+/g, " ").trim();

afterEach(() => {
  document.body.replaceChildren();
});

describe("valueLines", () => {
  it("shows a plain value", () => {
    expect(
      valueLines({
        changed: ["time_zone"],
        values: { time_zone: { before: "Home", after: "Cabin" } },
      }),
    ).toEqual([
      { name: "Time zone", before: "Home", after: "Cabin", hidden: false },
    ]);
  });

  it("marks a hidden value", () => {
    expect(
      valueLines({ changed: ["token"], values: { token: { hidden: true } } }),
    ).toEqual([{ name: "Token", hidden: true }]);
  });

  it("shows an object compactly", () => {
    const [line] = valueLines({
      changed: ["unit_system"],
      values: {
        unit_system: {
          before: { name: "metric", length: "km" },
          after: { length: "mi", mass: "lb" },
        },
      },
    });
    expect(line?.before).toBe("metric");
    expect(line?.after).toBe("length: mi, mass: lb");
  });

  it("shows only the nested fields that changed when the name is the same", () => {
    const backup = {
      name: "MyName",
      agent_ids: ["hassio.local", "cloud"],
      include_database: true,
      include_folders: null,
      password: { hidden: true },
    };
    const [off] = valueLines({
      changed: ["create_backup"],
      values: {
        create_backup: {
          before: backup,
          after: { ...backup, include_database: false },
        },
      },
    });
    expect(off).toEqual({
      name: "Create backup",
      before: "include_database: true",
      after: "include_database: false",
      hidden: false,
    });
    const [agents] = valueLines({
      changed: ["create_backup"],
      values: {
        create_backup: {
          before: backup,
          after: { ...backup, agent_ids: [], include_database: false },
        },
      },
    });
    expect(agents?.before).toBe(
      "agent_ids: hassio.local, cloud, include_database: true",
    );
    expect(agents?.after).toBe("agent_ids: —, include_database: false");
  });

  it("shows a nested field added or removed", () => {
    const [line] = valueLines({
      changed: ["create_backup"],
      values: {
        create_backup: {
          before: { name: "N", schedule: "daily" },
          after: { name: "N" },
        },
      },
    });
    expect(line?.before).toBe("schedule: daily");
    expect(line?.after).toBe("schedule: —");
  });

  it("uses the name when it is the only field or it changed", () => {
    const [only] = valueLines({
      changed: ["area"],
      values: { area: { before: { name: "A" }, after: { name: "B" } } },
    });
    expect(only?.before).toBe("A");
    expect(only?.after).toBe("B");
    const [renamed] = valueLines({
      changed: ["create_backup"],
      values: {
        create_backup: {
          before: { name: "Old", include_database: true },
          after: { name: "New", include_database: false },
        },
      },
    });
    expect(renamed?.before).toBe("Old");
    expect(renamed?.after).toBe("New");
  });

  it("compares nested objects by content, not identity", () => {
    const [line] = valueLines({
      changed: ["create_backup"],
      values: {
        create_backup: {
          before: { name: "N", retention: { copies: 3, days: null }, x: 1 },
          after: { name: "N", retention: { days: null, copies: 3 }, x: 2 },
        },
      },
    });
    expect(line?.before).toBe("x: 1");
    expect(line?.after).toBe("x: 2");
  });

  it("shows a nested hidden field as changed (hidden)", () => {
    const [line] = valueLines({
      changed: ["headers"],
      values: {
        headers: {
          before: { Accept: "a" },
          after: { Accept: "b", Authorization: { hidden: true } },
        },
      },
    });
    expect(line?.before).toBe("Accept: a, Authorization: —");
    expect(line?.after).toBe("Accept: b, Authorization: changed (hidden)");
  });

  it("shows lists as comma lists", () => {
    const [line] = valueLines({
      changed: ["allowed"],
      values: { allowed: { before: ["a", "b"], after: [] } },
    });
    expect(line?.before).toBe("a, b");
    expect(line?.after).toBe("—");
  });

  it("shows a missing before as a dash", () => {
    const [line] = valueLines({
      changed: ["x"],
      values: { x: { after: "on" } },
    });
    expect(line).toEqual({
      name: "X",
      before: "—",
      after: "on",
      hidden: false,
    });
  });

  it("keeps a changed name that has no values entry", () => {
    expect(
      valueLines({
        changed: ["a", "b"],
        values: { a: { before: 1, after: 2 } },
      }),
    ).toEqual([
      { name: "A", before: "1", after: "2", hidden: false },
      { name: "B", hidden: false },
    ]);
  });

  it("labels the home name", () => {
    expect(valueLines({ changed: ["location_name"] })[0]?.name).toBe(
      "Home name",
    );
  });

  it("names options and humanises", () => {
    expect(valueLines({ changed: ["options.scan_interval"] })[0]?.name).toBe(
      "Option: Scan interval",
    );
  });

  it("returns nothing for legacy details", () => {
    expect(valueLines({ keys: ["latitude"] })).toEqual([]);
    expect(valueLines({})).toEqual([]);
  });
});

describe("overview change lines", () => {
  it("shows at most three lines, then and N more", async () => {
    const el = document.createElement("ef-change-row") as EfChangeRow;
    el.hass = {
      locale: { language: "en" },
      config: { time_zone: "UTC" },
    } as never;
    el.details = true;
    el.item = change({
      kind: "http",
      details: {
        changed: ["a", "b", "c", "d", "e"],
        values: { a: { before: 1, after: 2 } },
      },
    });
    document.body.append(el);
    await el.updateComplete;
    const lines = [...(el.shadowRoot?.querySelectorAll(".line") ?? [])].map(
      (l) => squash(l.textContent),
    );
    expect(lines).toEqual(["A: 1 → 2", "B", "C", "and 2 more"]);
  });

  it("caps a changed-only row truthfully", async () => {
    const el = document.createElement("ef-change-row") as EfChangeRow;
    el.hass = {
      locale: { language: "en" },
      config: { time_zone: "UTC" },
    } as never;
    el.details = true;
    el.item = change({
      kind: "http",
      details: { changed: ["a", "b", "c", "d", "e"] },
    });
    document.body.append(el);
    await el.updateComplete;
    const lines = [...(el.shadowRoot?.querySelectorAll(".line") ?? [])].map(
      (l) => squash(l.textContent),
    );
    expect(lines).toEqual(["Changed: A, B, C", "and 2 more"]);
  });

  it("shows no empty Changed line for empty values", async () => {
    const el = document.createElement("ef-change-row") as EfChangeRow;
    el.hass = {
      locale: { language: "en" },
      config: { time_zone: "UTC" },
    } as never;
    el.details = true;
    el.item = change({ kind: "http", details: { values: {} } });
    document.body.append(el);
    await el.updateComplete;
    expect(el.shadowRoot?.textContent).not.toContain("Changed:");
  });

  it("is off by default", async () => {
    const el = document.createElement("ef-change-row") as EfChangeRow;
    el.item = change({ details: { changed: ["a"] } });
    el.hass = {
      locale: { language: "en" },
      config: { time_zone: "UTC" },
    } as never;
    document.body.append(el);
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector(".details")).toBeNull();
  });
});

describe("audit rows with values", () => {
  it("renders lines under the sentence", async () => {
    const el = await mountAudit({
      changed: ["location_name", "token"],
      values: {
        location_name: { before: "Home", after: "Cabin" },
        token: { hidden: true },
      },
    });
    const lines = [
      ...(el.shadowRoot?.querySelectorAll(".details .line") ?? []),
    ].map((l) => squash(l.textContent));
    expect(lines).toEqual([
      "Home name: Home → Cabin",
      "Token: changed (hidden)",
    ]);
  });

  it("renders a value hidden as a whole as changed (hidden)", async () => {
    const el = await mountAudit({
      changed: ["create_backup"],
      values: { create_backup: { hidden: true } },
    });
    const lines = [
      ...(el.shadowRoot?.querySelectorAll(".details .line") ?? []),
    ].map((l) => squash(l.textContent));
    expect(lines).toEqual(["Create backup: changed (hidden)"]);
  });

  it("truncates a long value and keeps the full one in the title", async () => {
    const long = "x".repeat(200);
    const el = await mountAudit({
      changed: ["a"],
      values: { a: { before: "1", after: long } },
    });
    const bdis = [
      ...(el.shadowRoot?.querySelectorAll(".details .line bdi") ?? []),
    ];
    const value = bdis.find((b) => b.getAttribute("title") === long);
    expect(value?.textContent.length).toBe(65);
  });

  it("renders changed-only rows as a Changed line", async () => {
    const el = await mountAudit(
      { changed: ["location_name"] },
      { kind: "http" },
    );
    expect(squash(el.shadowRoot?.querySelector(".details")?.textContent)).toBe(
      "Changed: Home name",
    );
  });

  it("shows option names and data keys on entries", async () => {
    const el = await mountAudit({
      domain: "tuya",
      changed: ["options.scan_interval"],
      data_keys_changed: ["token"],
    });
    const lines = [
      ...(el.shadowRoot?.querySelectorAll(".details .line") ?? []),
    ].map((l) => squash(l.textContent));
    expect(lines).toEqual([
      "Changed: Option: Scan interval",
      "Data keys: token",
    ]);
  });
});
