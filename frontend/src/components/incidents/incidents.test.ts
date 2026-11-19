import { describe, expect, it } from "vitest";
import { severityFromUrgency, transitionLabel } from "@/components/incidents/shared";
import { propType, toArguments } from "@/components/inventory/ResourceDiagnostics";
import type { ToolInfo } from "@/types/api";

describe("incident helpers", () => {
  it("labels status transitions", () => {
    expect(transitionLabel("investigating")).toBe("Start investigating");
    expect(transitionLabel("failed")).toBe("Mark failed");
    expect(transitionLabel("awaiting_approval")).toBeTruthy();
  });

  it("maps urgency to severity", () => {
    expect(severityFromUrgency("critical")).toBe("critical");
    expect(severityFromUrgency("low")).toBe("low");
    expect(severityFromUrgency(null)).toBe("medium");
    expect(severityFromUrgency("weird")).toBe("medium");
  });
});

describe("tool argument form", () => {
  const tool = {
    name: "linux_journal",
    input_schema: {
      properties: {
        resource_id: { type: "string" },
        unit: { anyOf: [{ type: "string" }, { type: "null" }], default: null, description: "Limit to one systemd unit" },
        priority: { type: "string", enum: ["err", "warning"], default: "warning" },
        lines: { type: "integer", default: 100 },
      },
    },
  } as unknown as ToolInfo;

  it("unwraps optional types", () => {
    expect(propType({ anyOf: [{ type: "integer" }, { type: "null" }], description: "x" })).toMatchObject({ type: "integer", description: "x" });
  });

  it("parses numbers and leaves out blanks and the resource id", () => {
    expect(toArguments(tool, { resource_id: "r1", unit: "  ", priority: "err", lines: "50" })).toEqual({ priority: "err", lines: 50 });
    expect(toArguments(tool, { unit: "nginx.service" })).toEqual({ unit: "nginx.service" });
  });
});
