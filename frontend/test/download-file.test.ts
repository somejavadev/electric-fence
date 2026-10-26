import { afterEach, describe, expect, it, vi } from "vitest";
import { downloadFile } from "../src/export.ts";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("downloadFile", () => {
  it("clicks an a[download] on a Blob URL and revokes it", () => {
    const create = vi.fn(() => "blob:x");
    const revoke = vi.fn();
    vi.stubGlobal("URL", { createObjectURL: create, revokeObjectURL: revoke });
    const seen: string[] = [];
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        seen.push(this.download, this.getAttribute("href") ?? "");
      });
    downloadFile("f.csv", "text/csv", "body");
    expect(create).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    expect(seen).toEqual(["f.csv", "blob:x"]);
    expect(revoke).toHaveBeenCalledWith("blob:x");
    expect(document.querySelector("a[download]")).toBeNull();
  });
});
