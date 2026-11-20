import { describe, expect, it } from "vitest";
import {
  formatBytes,
  indexMeta,
  isGenerated,
  parseTags,
  sourceLink,
  titleFromFilename,
} from "@/components/knowledge/knowledge";

describe("knowledge helpers", () => {
  it("parses tags like the API", () => {
    expect(parseTags("Disk, linux,,  Disk \nDB  Pool")).toEqual(["disk", "linux", "db pool"]);
    expect(parseTags("")).toEqual([]);
  });

  it("maps index status to tones", () => {
    expect(indexMeta("indexed").tone).toBe("success");
    expect(indexMeta("keyword_only").tone).toBe("warning");
    expect(indexMeta("keyword_only").hint).toContain("keyword search only");
    expect(indexMeta("failed").tone).toBe("danger");
    expect(indexMeta("weird").label).toBe("weird");
  });

  it("links generated documents to their origin", () => {
    expect(sourceLink({ source: "incident", source_id: "i1" })).toBe("/incidents/i1");
    expect(sourceLink({ source: "investigation", source_id: "v1" })).toBe("/investigations/v1");
    expect(sourceLink({ source: "upload", source_id: null })).toBeNull();
    expect(isGenerated({ source: "incident" })).toBe(true);
    expect(isGenerated({ source: "manual" })).toBe(false);
  });

  it("formats sizes and titles", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(titleFromFilename("db-pool_runbook.md")).toBe("db pool runbook");
  });
});
