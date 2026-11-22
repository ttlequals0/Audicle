import { describe, expect, it } from "vitest";
import { statusTag } from "./format";

describe("statusTag", () => {
  it.each([
    ["done", "tag-done"],
    ["failed", "tag-failed"],
    ["cancelled", "tag-cancelled"],
    ["removed", "tag-cancelled"],
    ["processing", "tag-processing"],
    ["queued", "tag-queued"],
    ["not_queued", "tag-queued"],
  ])("maps %s to %s", (status, tag) => {
    expect(statusTag(status)).toBe(tag);
  });
});
